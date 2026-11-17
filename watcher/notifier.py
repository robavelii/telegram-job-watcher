"""Sends messages through the Bot API, and formats alerts and digests.

The user account never sends anything; every outgoing message goes through the bot.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
from collections.abc import Callable
from datetime import datetime, time

import httpx

log = logging.getLogger(__name__)



def esc(value: object) -> str:
    return html.escape(str(value or ""), quote=False)


def attr(value: str) -> str:
    return html.escape(value, quote=True)


_TAG = re.compile(r"<[^>]+>")


def visible_len(text: str) -> int:
    """Length Telegram counts against its limit: the text after HTML entities are parsed."""
    return len(html.unescape(_TAG.sub("", text)))


def split_message(text: str, limit: int = 4000) -> list[str]:
    """Split on line breaks (HTML tags here never span lines) so each part stays under
    Telegram's 4096-character limit, measured on visible text."""
    if visible_len(text) <= limit:
        return [text]
    parts: list[str] = []
    current, size = [], 0
    for line in text.split("\n"):
        n = visible_len(line) + 1
        if n > limit:  # one giant line: hard cut, tags be damned
            if current:
                parts.append("\n".join(current))
                current, size = [], 0
            plain = html.unescape(_TAG.sub("", line))
            parts += [esc(plain[i : i + limit]) for i in range(0, len(plain), limit)]
            continue
        if size + n > limit and current:
            parts.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += n
    if current:
        parts.append("\n".join(current))
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


def _source(link: str, channel: str, msg_id=None) -> str:
    label = esc(channel or "post") + (f" #{msg_id}" if msg_id else "")
    return f'<a href="{attr(link)}">{label}</a>' if link else label


def _data(row) -> dict:
    try:
        return json.loads(row["result_json"]) if row["result_json"] else {}
    except (TypeError, ValueError):
        return {}


def _job_line(row) -> str:
    data = _data(row)
    label = f"<b>{row['fit_score']}</b> · {esc(data.get('title') or 'Untitled')}"
    if data.get("company"):
        label += f" · {esc(data['company'])}"
    if row["eligible"] == "unclear":
        label += " · location unclear"
    return f"• {label} — {_source(row['link'], row['channel'])}"


def _closed_line(row) -> str:
    data = _data(row)
    where = (data.get("location_rules") or "").strip()
    line = f"• {esc(data.get('title') or 'Untitled')} — {_source(row['link'], row['channel'])}"
    return line + (f" · <i>{esc(where[:70])}</i>" if where else "")


def _not_job_line(row) -> str:
    gist = (_data(row).get("summary_en") or "").strip()
    return f"• {_source(row['link'], row['channel'])}" + (f": {esc(gist[:90])}" if gist else "")


def _link_list(rows: list, per_line: int = 8) -> list[str]:
    """Many links, a few per line, so the list stays short."""
    sources = [_source(r["link"], r["channel"], r["msg_id"]) for r in rows]
    return ["• " + " · ".join(sources[i : i + per_line]) for i in range(0, len(sources), per_line)]


SKIP_LABELS = {
    "skipped:too_short": "too short",
    "skipped:ad": "ads",
    "skipped:no_text": "no text",
}


def format_summary(
    hours: int,
    outcomes: list,
    results: list,
    threshold: int,
    near_min: int,
    title: str = "Summary",
) -> str:
    """Every post read in the window, grouped, each linked to the original message.

    outcomes: rows from Store.outcomes_since (every post the pipeline looked at);
    results: rows from Store.results_since (posts that reached the model)."""
    scored = [r for r in results if r["status"] == "scored"]
    errors = [r for r in results if r["status"] == "error"]
    jobs = [r for r in scored if r["is_job"]]
    not_jobs = [r for r in scored if not r["is_job"]]
    closed = [r for r in jobs if r["eligible"] == "no"]
    open_jobs = [r for r in jobs if r["eligible"] != "no"]
    alerts = [r for r in open_jobs if r["fit_score"] >= threshold]
    near = [r for r in open_jobs if near_min <= r["fit_score"] < threshold]
    below = [r for r in open_jobs if r["fit_score"] < near_min]

    skipped = [o for o in outcomes if o["outcome"].startswith("skipped:")]
    duplicates = [o for o in outcomes if o["outcome"] == "duplicate"]
    read = len(results) + len(skipped) + len(duplicates)

    lines = [
        f"📊 <b>{esc(title)}</b>, last {hours}h — every post read, each linked",
        f"Read {read} · scored {len(scored)} · job posts {len(jobs)} · alerts {len(alerts)} · "
        f"near misses {len(near)} · closed to Ethiopia {len(closed)} · skipped {len(skipped)} · "
        f"duplicates {len(duplicates)} · errors {len(errors)}",
    ]

    def section(heading: str, rows: list, render) -> None:
        if rows:
            lines.extend(["", f"{heading} ({len(rows)})"] + [render(r) for r in rows])

    section(f"🔥 <b>Alerts</b>, {threshold}+", alerts, _job_line)
    section(f"🟡 <b>Near misses</b>, {near_min}–{threshold - 1}", near, _job_line)
    section(f"📉 <b>Low fit</b>, under {near_min}", below, _job_line)
    section("🚫 <b>Closed to Ethiopia</b>", closed, _closed_line)
    section("📰 <b>Not job posts</b>", not_jobs, _not_job_line)
    section("⚠️ <b>Could not score</b>", errors,
            lambda r: f"• {_source(r['link'], r['channel'])}: {esc((r['error'] or '')[:90])}")

    by_reason: dict[str, list] = {}
    for o in skipped:
        by_reason.setdefault(o["outcome"], []).append(o)
    for reason, rows in by_reason.items():
        linked = [r for r in rows if r["link"]]
        label = SKIP_LABELS.get(reason, reason.removeprefix("skipped:"))
        lines += ["", f"⏭ <b>Skipped, {esc(label)}</b> ({len(rows)})"] + _link_list(linked)
        if len(linked) < len(rows):
            lines.append(f"• {len(rows) - len(linked)} more from before links were recorded")
    if duplicates:
        linked = [r for r in duplicates if r["link"]]
        lines += ["", f"🔁 <b>Duplicates of posts above</b> ({len(duplicates)})"] + _link_list(linked)
    if read == 0:
        lines += ["", "Nothing read in this window yet."]
    return "\n".join(lines)


def format_top(rows: list, hours: int, n: int) -> str:
    jobs = [r for r in rows if r["status"] == "scored" and r["is_job"] and r["eligible"] != "no"][:n]
    if not jobs:
        return f"No job posts open to Ethiopia in the last {hours}h yet."
    return "\n".join([f"🏆 <b>Top {len(jobs)}</b>, last {hours}h"] + [_job_line(r) for r in jobs])
