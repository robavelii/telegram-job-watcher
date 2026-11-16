"""Sends messages through the Bot API, and formats alerts and digests.

The user account never sends anything; every outgoing message goes through the bot.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
from collections.abc import Callable
from datetime import datetime, time

import httpx

log = logging.getLogger(__name__)

TELEGRAM_LIMIT = 4096


def esc(value: object) -> str:
    return html.escape(str(value or ""), quote=False)


def attr(value: str) -> str:
    return html.escape(value, quote=True)


def split_message(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Split on line breaks so HTML tags (always within one line here) stay balanced."""
    if len(text) <= limit:
        return [text]
    parts, current = [], ""
    for line in text.split("\n"):
        while len(line) > limit:  # a single giant line: hard cut, tags be damned
            if current:
                parts.append(current)
                current = ""
            parts.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            parts.append(current)
            current = line
        else:
            current = candidate
    if current:
        parts.append(current)
    return parts


class BotNotifier:
    def __init__(self, http: httpx.AsyncClient, bot_token: str, chat_id: int, dry_run: bool = False):
        self.http = http
        self.chat_id = chat_id
        self.dry_run = dry_run
        self._url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

    async def send(self, text: str, silent: bool = False) -> None:
        for part in split_message(text):
            await self._send_one(part, silent)

    async def _send_one(self, text: str, silent: bool) -> None:
        if self.dry_run:
            log.info("DRY_RUN, would send:\n%s", text)
            return
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
            "disable_notification": silent,
        }
        for attempt in range(5):
            try:
                response = await self.http.post(self._url, json=payload)
            except httpx.HTTPError as exc:
                # Never log the request URL: it contains the bot token.
                log.warning("Bot API unreachable (%s), attempt %d", type(exc).__name__, attempt + 1)
                await asyncio.sleep(2**attempt)
                continue
            if response.status_code == 200:
                return
            body = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
            if response.status_code == 429:
                wait = int(body.get("parameters", {}).get("retry_after", 5))
                log.warning("Bot API rate limit, waiting %ss", wait)
                await asyncio.sleep(wait)
                continue
            if response.status_code >= 500:
                await asyncio.sleep(2**attempt)
                continue
            # 400/401/403: wrong token, bad chat id, or the bot was never started. Retrying won't help.
            raise RuntimeError(f"Bot API {response.status_code}: {body.get('description', response.text[:200])}")
        raise RuntimeError("Bot API: gave up after 5 attempts")


def in_window(now: time, window: tuple[time, time] | None) -> bool:
    if window is None:
        return False
    start, end = window
    if start <= end:
        return start <= now < end
    return now >= start or now < end  # crosses midnight


class Dispatcher:
    """Sends now, or holds messages in the outbox during quiet hours."""

    def __init__(self, notifier: BotNotifier, store, quiet_hours, now: Callable[[], datetime]):
        self.notifier = notifier
        self.store = store
        self.quiet_hours = quiet_hours
        self.now = now

    def quiet(self) -> bool:
        return in_window(self.now().time(), self.quiet_hours)

    async def deliver(self, text: str) -> bool:
        """Returns True if sent now, False if queued."""
        if self.quiet():
            self.store.enqueue(text)
            return False
        await self.notifier.send(text)
        return True

    async def flush_outbox(self) -> int:
        if self.quiet():
            return 0
        pending = self.store.pending_outbox()
        if not pending:
            return 0
        await self.notifier.send(f"🌅 <b>{len(pending)} held during quiet hours</b>", silent=True)
        for row in pending:
            await self.notifier.send(row["body"], silent=True)
            self.store.mark_sent(row["id"])
        return len(pending)


# --- formatting ----------------------------------------------------------------

def format_alert(result: dict, link: str, channel: str) -> str:
    urgent = result.get("apply_urgency") == "high"
    eligible = result.get("eligible_from_ethiopia", "unclear")
    head = f"{'🔥' if urgent else '✅'} <b>{result['fit_score']}/100</b> · {esc(result.get('title') or 'Untitled role')}"
    lines = [head]
    if result.get("company"):
        lines.append(f"🏢 {esc(result['company'])}")
    facts = [str(result[k]) for k in ("seniority", "employment_type", "salary") if result.get(k)]
    if facts:
        lines.append("💼 " + esc(" · ".join(facts)))
    if result.get("stack"):
        lines.append("🧰 " + esc(", ".join(result["stack"][:12])))
    location = result.get("location_rules") or "not stated"
    lines.append(f"🌍 Ethiopia: <b>{esc(eligible)}</b> ({esc(location)})")
    if result.get("summary_en"):
        lines += ["", esc(result["summary_en"])]
    if result.get("fit_reasons"):
        lines += ["", "<b>Why it fits</b>"] + [f"• {esc(r)}" for r in result["fit_reasons"][:3]]
    if result.get("gaps"):
        lines += ["", "<b>Gaps</b>"] + [f"• {esc(g)}" for g in result["gaps"][:4]]
    if result.get("how_to_apply"):
        lines += ["", f"<b>Apply:</b> {esc(result['how_to_apply'])}"]
    source = f'<a href="{attr(link)}">{esc(channel)}</a>' if link else esc(channel)
    lines += ["", f"📎 {source}"]
    return "\n".join(lines)


def format_digest(rows: list, threshold: int) -> str:
    lines = [f"📋 <b>Daily digest</b>: {len(rows)} near misses and errors (below {threshold})", ""]
    for row in rows:
        link = row["link"]
        if row["status"] == "error":
            label = f"⚠️ could not score: {esc(row['error'])}"
        else:
            data = json.loads(row["result_json"])
            label = f"<b>{row['fit_score']}</b> · {esc(data.get('title') or 'Untitled')}"
            if data.get("company"):
                label += f" · {esc(data['company'])}"
            if row["eligible"] == "unclear":
                label += " · location unclear"
        lines.append(f'• {label} — <a href="{attr(link)}">{esc(row["channel"])}</a>' if link else f"• {label}")
    return "\n".join(lines)
