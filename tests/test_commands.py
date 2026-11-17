from collections import Counter
from datetime import time as dtime
from types import SimpleNamespace

from watcher.commands import CommandBot, Commands, parse_int

from .conftest import FakeNotifier, result

OWNER = 424242424


def settings(**kw):
    base = dict(
        fit_threshold=70, near_miss_min=50, folder_name="remote-jobs", llm_provider="gemini",
        llm_model="gemini-3.5-flash-lite", digest_at=dtime(20, 0), quiet_hours=None, dry_run=False,
    )
    base.update(kw)
    return SimpleNamespace(**base)


class FakeListener:
    def __init__(self):
        self.channels = {1: None, 2: None}
        self.scans = []

    def is_connected(self):
        return True

    async def scan_recent(self, hours):
        self.scans.append(hours)
        return Counter({"alerted": 1, "below_threshold": 3, "seen": 5, "skipped:ad": 2})


def make(store, **kw):
    listener = FakeListener()
    commands = Commands(settings(**kw), store, listener, started_at=0, clock=lambda: 7200)
    notifier = FakeNotifier()
    bot = CommandBot(http=None, bot_token="t", owner_chat_id=OWNER, notifier=notifier, commands=commands, store=store)
    return bot, notifier, listener


def update(text, chat_id=OWNER):
    return {"update_id": 1, "message": {"chat": {"id": chat_id}, "text": text}}


def test_parse_int_clamps():
    assert parse_int([], 24, 1, 72) == 24
    assert parse_int(["500"], 24, 1, 72) == 72
    assert parse_int(["abc"], 24, 1, 72) == 24


async def test_strangers_are_ignored(store):
    bot, notifier, _ = make(store)
    await bot.handle_update(update("/summary", chat_id=999))
    assert notifier.sent == []


async def test_summary_and_top(store):
    store.record_result(-1, 1, "https://t.me/a/1", "A", result(title="Backend", fit_score=88))
    store.record_outcome("below_threshold")
    bot, notifier, _ = make(store)
    await bot.handle_update(update("/summary 48"))
    await bot.handle_update(update("/top@example_job_watcher_bot"))
    summary, top = notifier.sent[0][0], notifier.sent[1][0]
    assert "last 48h" in summary and "Read 1 posts" in summary
    assert "Top 1" in top and "Backend" in top


async def test_status(store):
    bot, notifier, _ = make(store, dry_run=True)
    await bot.handle_update(update("/status"))
    text = notifier.sent[0][0]
    assert "Watcher running" in text and "watching 2 chats" in text and "20:00" in text
    assert "DRY_RUN is on" in text


async def test_scan_reports_counts(store):
    bot, notifier, listener = make(store)
    await bot.handle_update(update("/scan 6"))
    assert listener.scans == [6]
    first, done = notifier.sent[0][0], notifier.sent[1][0]
    assert "Reading the last 6h of 2 chats" in first
    assert "read 11 posts, 4 new scored, 5 already seen, 1 alerts" in done


async def test_unknown_command_and_plain_text_get_help(store):
    bot, notifier, _ = make(store)
    await bot.handle_update(update("/nope"))
    await bot.handle_update(update("hello"))
    assert "Unknown command /nope" in notifier.sent[0][0]
    assert "/summary" in notifier.sent[1][0]
