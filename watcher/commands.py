"""Bot commands: /summary, /top, /status, /scan, /help.

Long-polls getUpdates and answers only the owner's chat (NOTIFY_CHAT_ID); messages from
anyone else are dropped unread. The user account is still never used to send anything.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter

import httpx

from .notifier import BotNotifier, esc, format_summary, format_top

log = logging.getLogger(__name__)

COMMANDS = [
    ("summary", "Summary of the last 24h, or /summary 48"),
    ("top", "Best job posts of the last 24h, or /top 10"),
    ("status", "Is the watcher running, and what is it watching"),
    ("scan", "Re-read the last 24h of every channel, or /scan 6"),
    ("help", "List the commands"),
]
MAX_HOURS = 168
MAX_SCAN_HOURS = 72


def parse_int(args: list[str], default: int, low: int, high: int) -> int:
    try:
        value = int(args[0]) if args else default
    except ValueError:
        value = default
    return max(low, min(high, value))


def human_age(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds // 60} min"
    if seconds < 172800:
        return f"{seconds // 3600} h"
    return f"{seconds // 86400} days"


class Commands:
    """What each command does. Kept apart from the polling so it can be tested directly."""

    def __init__(self, settings, store, listener, started_at: float, clock=time.time):
        self.settings = settings
        self.store = store
        self.listener = listener
        self.started_at = started_at
        self.clock = clock
        self._scan_lock = asyncio.Lock()

    def summary_text(self, hours: int, title: str = "Summary") -> str:
        since = self.clock() - hours * 3600
        return format_summary(
            hours,
            self.store.outcome_counts(since),
            self.store.results_since(since),
            self.settings.fit_threshold,
            self.settings.near_miss_min,
            title=title,
        )

    async def summary(self, args: list[str]) -> str:
        return self.summary_text(parse_int(args, 24, 1, MAX_HOURS))

    async def top(self, args: list[str]) -> str:
        n = parse_int(args, 5, 1, 20)
        return format_top(self.store.results_since(self.clock() - 24 * 3600), 24, n)

    async def status(self, args: list[str]) -> str:
        now = self.clock()
        last = self.store.last_outcome_at()
        today = self.store.outcome_counts(now - 24 * 3600)
        s = self.settings
        lines = [
            "🟢 <b>Watcher running</b>" if self.listener.is_connected() else "🔴 <b>Telegram disconnected</b>",
            f"Up {human_age(now - self.started_at)} · watching {len(self.listener.channels)} chats in "
            f"<i>{esc(s.folder_name)}</i>",
            f"Last post read {human_age(now - last)} ago" if last else "No posts read yet",
            f"Posts read in 24h: {sum(today.values())}",
            f"Model: {esc(s.llm_provider)} / {esc(s.llm_model)} · alert at {s.fit_threshold}+",
            f"Daily summary: {s.digest_at.strftime('%H:%M') if s.digest_at else 'off'} · quiet hours: "
            + (f"{s.quiet_hours[0]:%H:%M}–{s.quiet_hours[1]:%H:%M}" if s.quiet_hours else "off"),
        ]
        if s.dry_run:
            lines.append("⚠️ DRY_RUN is on: alerts go to the logs only")
        return "\n".join(lines)

    async def scan(self, args: list[str], reply) -> str | None:
        hours = parse_int(args, 24, 1, MAX_SCAN_HOURS)
        if self._scan_lock.locked():
            return "A scan is already running."
        async with self._scan_lock:
            await reply(
                f"🔎 Reading the last {hours}h of {len(self.listener.channels)} chats. Posts already "
                "seen are skipped; alerts arrive as usual. I'll report when done."
            )
            started = self.clock()
            counts: Counter = await self.listener.scan_recent(hours)
            took = human_age(self.clock() - started)
        new = sum(n for k, n in counts.items() if k not in ("seen", "duplicate") and not k.startswith("skipped:"))
        head = (
            f"✅ Scan done in {took}: read {sum(counts.values())} posts, {new} new scored, "
            f"{counts.get('seen', 0)} already seen, {counts.get('alerted', 0) + counts.get('queued', 0)} alerts."
        )
        return head + "\n\n" + self.summary_text(hours, title="Scan results")

    def help(self) -> str:
        return "\n".join(["<b>Commands</b>"] + [f"/{name} — {esc(text)}" for name, text in COMMANDS])


class CommandBot:
    def __init__(self, http: httpx.AsyncClient, bot_token: str, owner_chat_id: int, notifier: BotNotifier,
                 commands: Commands, store):
        self.http = http
        self.owner_chat_id = owner_chat_id
        self.notifier = notifier
        self.commands = commands
        self.store = store
        self._base = f"https://api.telegram.org/bot{bot_token}"
        self._tasks: set[asyncio.Task] = set()

    async def register(self) -> None:
        """Show the commands in Telegram's / menu."""
        try:
            await self.http.post(
                f"{self._base}/setMyCommands",
                json={"commands": [{"command": c, "description": d} for c, d in COMMANDS]},
            )
        except httpx.HTTPError as exc:
            log.warning("could not register bot commands (%s)", type(exc).__name__)

    async def dispatch(self, text: str) -> None:
        parts = text.strip().split()
        name = parts[0][1:].split("@")[0].lower()
        args = parts[1:]
        if name == "summary":
            reply = await self.commands.summary(args)
        elif name == "top":
            reply = await self.commands.top(args)
        elif name == "status":
            reply = await self.commands.status(args)
        elif name == "scan":
            reply = await self.commands.scan(args, self.notifier.send)
        elif name in ("help", "start"):
            reply = self.commands.help()
        else:
            reply = f"Unknown command /{esc(name)}.\n\n" + self.commands.help()
        if reply:
            await self.notifier.send(reply)

    async def handle_update(self, update: dict) -> None:
        message = update.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        text = message.get("text") or ""
        if chat_id != self.owner_chat_id:
            if chat_id is not None:
                log.info("ignored a message to the bot from chat %s (not the owner)", chat_id)
            return
        if not text.startswith("/"):
            await self.notifier.send(self.commands.help())
            return
        try:
            await self.dispatch(text)
        except Exception:
            log.exception("command failed: %s", text.split()[0])
            await self.notifier.send(f"⚠️ {esc(text.split()[0])} failed; see the watcher logs.")

    async def run(self) -> None:
        await self.register()
        offset = int(self.store.get_meta("bot_update_offset") or 0)
        delay = 5
        while True:
            try:
                response = await self.http.post(
                    f"{self._base}/getUpdates",
                    json={"offset": offset, "timeout": 50, "allowed_updates": ["message"]},
                    timeout=70,
                )
                body = response.json()
                if not body.get("ok"):
                    # 409: another process polls this token (or a webhook is set).
                    log.warning("getUpdates refused: %s %s", response.status_code, body.get("description"))
                    await asyncio.sleep(60)
                    continue
                delay = 5
                for update in body.get("result", []):
                    offset = update["update_id"] + 1
                    self.store.set_meta("bot_update_offset", str(offset))
                    # A /scan can take minutes; don't hold up other commands while it runs.
                    task = asyncio.create_task(self.handle_update(update))
                    self._tasks.add(task)
                    task.add_done_callback(self._tasks.discard)
            except (httpx.HTTPError, ValueError) as exc:
                log.warning("getUpdates failed (%s), retrying in %ss", type(exc).__name__, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 300)
