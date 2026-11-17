from datetime import datetime, time
from types import SimpleNamespace

import pytest
from telethon.tl.types import DialogFilter, DialogFilterDefault, TextWithEntities

from watcher.listener import Channel, FolderNotFound, Listener, find_folder
from watcher.notifier import Dispatcher, format_alert, format_summary, format_top, in_window, split_message

from .conftest import result


def test_alert_escapes_html():
    text = format_alert(result(title="<b>Dev</b> & co", how_to_apply="mail a@b.c"), "https://t.me/x/1", "Jobs <1>")
    assert "&lt;b&gt;Dev&lt;/b&gt; &amp; co" in text
    assert 'href="https://t.me/x/1"' in text and "Jobs &lt;1&gt;" in text


def test_split_message_respects_limit():
    parts = split_message("\n".join(["line " * 50] * 100), limit=1000)
    assert all(len(p) <= 1000 for p in parts) and len(parts) > 1


def test_quiet_window():
    night = (time(23, 0), time(7, 0))
    assert in_window(time(23, 30), night) and in_window(time(3, 0), night)
    assert not in_window(time(7, 0), night) and not in_window(time(12, 0), night)
    assert in_window(time(13, 0), (time(12, 0), time(14, 0)))
    assert not in_window(time(13, 0), None)


async def test_quiet_hours_hold_then_flush(store, notifier, clock):
    clock.dt = datetime(2026, 11, 16, 2, 0)
    dispatcher = Dispatcher(notifier, store, (time(23, 0), time(7, 0)), clock)
    assert await dispatcher.deliver("alert 1") is False
    assert await dispatcher.flush_outbox() == 0 and notifier.sent == []
    clock.dt = datetime(2026, 11, 16, 7, 5)
    assert await dispatcher.flush_outbox() == 1
    assert [t for t, _ in notifier.sent][1] == "alert 1"
    assert store.pending_outbox() == []


def _seed(store):
    store.record_result(-1, 1, "https://t.me/a/1", "A", result(title="Backend", fit_score=88))
    store.mark_alerted(-1, 1)
    store.record_result(-1, 2, "https://t.me/a/2", "A", result(title="Near", fit_score=60, eligible_from_ethiopia="unclear"))
    store.record_result(-1, 3, "https://t.me/a/3", "A", result(title="EU only", fit_score=30, eligible_from_ethiopia="no"))
    store.record_result(-1, 4, "https://t.me/a/4", "A", result(title="Weak", fit_score=40))
    store.record_error(-1, 5, "https://t.me/a/5", "A", "Gemini API 429: quota")
    for outcome in ["alerted", "below_threshold", "ineligible", "below_threshold", "error", "skipped:ad", "skipped:too_short", "duplicate"]:
        store.record_outcome(outcome)


def test_summary_counts_and_sections(store):
    _seed(store)
    text = format_summary(24, store.outcome_counts(0), store.results_since(0), 70, 50)
    assert "Read 8 posts · skipped 2" in text and "duplicates 1" in text
    assert "job posts 4 · alerts 1 · near misses 1 · closed to Ethiopia 1 · errors 1" in text
    assert "🔥 <b>Alerts</b>" in text and "Backend" in text
    assert "🟡 <b>Near misses</b> (50–69)" in text and "location unclear" in text
    assert "Could not score" in text and "429" in text
    assert "EU only" not in text and "Weak" not in text


def test_summary_with_nothing_good_shows_best_of_the_rest(store):
    store.record_result(-1, 4, "https://t.me/a/4", "A", result(title="Weak", fit_score=40))
    text = format_summary(24, {"below_threshold": 1}, store.results_since(0), 70, 50)
    assert "Nothing scored 50+" in text and "Weak" in text


def test_top_skips_ineligible(store):
    _seed(store)
    text = format_top(store.results_since(0), 24, 5)
    assert text.index("Backend") < text.index("Near") < text.index("Weak")
    assert "EU only" not in text


def _folder(title, ids):
    from telethon.tl.types import InputPeerChannel

    return DialogFilter(
        id=2,
        title=title,
        pinned_peers=[],
        include_peers=[InputPeerChannel(i, 0) for i in ids],
        exclude_peers=[],
    )


def test_find_folder_handles_both_title_shapes():
    rich = _folder(TextWithEntities(text="Remote-Jobs", entities=[]), [1])
    assert find_folder([DialogFilterDefault(), rich], "remote-jobs") is rich
    plain = _folder("remote-jobs", [1])  # older layers used plain strings
    assert find_folder([plain], "REMOTE-JOBS") is plain
    with pytest.raises(FolderNotFound, match="Remote-Jobs"):
        find_folder([rich], "jobs")


def test_to_post_skips_media_without_caption():
    listener = Listener.__new__(Listener)
    listener.channels = {-1001: Channel(title="Jobs", username="jobs")}
    album_item = SimpleNamespace(id=5, message="", grouped_id=123)
    assert listener.to_post(album_item, -1001) is None
    captioned = SimpleNamespace(id=6, message="Vacancy text", grouped_id=123)
    post = listener.to_post(captioned, -1001)
    assert post.username == "jobs" and post.channel_title == "Jobs"
