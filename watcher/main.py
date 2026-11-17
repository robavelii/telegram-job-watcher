"""Entry point: python -m watcher.main"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from .backends import make_backend

from .config import ConfigError, Settings, load_profile
from .listener import FolderNotFound, Listener
from .notifier import BotNotifier, Dispatcher, format_digest
from .pipeline import Pipeline
from .prefilter import compile_markers
from .scorer import Scorer
from .store import Store

log = logging.getLogger("watcher")

HEARTBEAT_FILE = "heartbeat"


def configure_logging() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    # httpx logs every request URL at INFO, and Bot API URLs contain the bot token.
    for noisy in ("httpx", "httpcore", "telethon", "google_genai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


class Scheduler:
    """Once a minute: heartbeat for the healthcheck, flush the quiet-hours outbox,
    and send the daily digest when it is due."""

    def __init__(self, settings: Settings, store: Store, dispatcher: Dispatcher, listener: Listener, now):
        self.settings = settings
        self.store = store
        self.dispatcher = dispatcher
        self.listener = listener
        self.now = now

    def heartbeat(self) -> None:
        if self.listener.is_connected():
            path = self.settings.data_dir / HEARTBEAT_FILE
            path.write_text(str(int(time.time())))

    async def maybe_digest(self) -> None:
        if self.settings.digest_at is None:
            return
        now = self.now()
        today = now.date().isoformat()
        if now.time() < self.settings.digest_at or self.store.get_meta("last_digest") == today:
            return
        rows = self.store.pending_digest(self.settings.near_miss_min, self.settings.fit_threshold)
        if rows:
            await self.dispatcher.deliver(format_digest(rows, self.settings.fit_threshold))
            self.store.mark_digested(rows)
        self.store.set_meta("last_digest", today)

    async def run(self) -> None:
        while True:
            try:
                self.heartbeat()
                await self.dispatcher.flush_outbox()
                await self.maybe_digest()
            except Exception:
                log.exception("scheduler tick failed")
            await asyncio.sleep(60)


async def run_listener(listener: Listener, notifier: BotNotifier) -> None:
    delay = 5
    while True:
        started = time.monotonic()
        try:
            await listener.run()
            log.warning("disconnected from Telegram; reconnecting")
        except (PermissionError, FolderNotFound) as exc:
            # Retrying cannot fix a revoked session or a missing folder. Say so, then exit.
            log.error("%s", exc)
            try:
                await notifier.send(f"⛔ job watcher stopped: {exc}")
            except Exception:
                log.exception("could not send the stop notice")
            raise SystemExit(2) from exc
        except (ConnectionError, OSError, asyncio.TimeoutError) as exc:
            log.warning("connection problem: %s", exc)
        except Exception:
            log.exception("listener crashed")
        if time.monotonic() - started > 300:
            delay = 5  # it ran for a while, so start the backoff over
        log.info("restarting the listener in %ss", delay)
        await asyncio.sleep(delay)
        delay = min(delay * 2, 600)


async def amain() -> None:
    configure_logging()
    try:
        settings = Settings.from_env()
        profile = load_profile(settings.config_dir)
    except ConfigError as exc:
        log.error("config: %s", exc)
        raise SystemExit(2) from exc

    tz = ZoneInfo(settings.timezone)
    now = lambda: datetime.now(tz)  # noqa: E731
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    store = Store(settings.data_dir / "watcher.db")

    backend = make_backend(settings.llm_provider, settings.llm_model, settings.llm_effort)
    scorer = Scorer(backend, resume=profile.resume, preferences=profile.preferences_text)

    async with httpx.AsyncClient(timeout=30) as http:
        notifier = BotNotifier(http, settings.bot_token, settings.notify_chat_id, dry_run=settings.dry_run)
        dispatcher = Dispatcher(notifier, store, settings.quiet_hours, now)
        pipeline = Pipeline(
            store,
            scorer,
            dispatcher,
            compile_markers(profile.ad_markers),
            fit_threshold=settings.fit_threshold,
            min_post_chars=settings.min_post_chars,
        )
        try:
            listener = Listener(settings, pipeline, store)
        except ValueError as exc:
            log.error("config: TELEGRAM_SESSION_STRING is not a valid session string (%s)", exc)
            raise SystemExit(2) from exc
        scheduler = Scheduler(settings, store, dispatcher, listener, now)

        log.info(
            "starting: folder=%r provider=%s model=%s threshold=%d quiet=%s digest=%s dry_run=%s",
            settings.folder_name,
            settings.llm_provider,
            settings.llm_model,
            settings.fit_threshold,
            settings.quiet_hours,
            settings.digest_at,
            settings.dry_run,
        )
        await asyncio.gather(run_listener(listener, notifier), scheduler.run())


def main() -> None:
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
