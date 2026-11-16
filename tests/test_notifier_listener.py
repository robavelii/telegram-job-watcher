from datetime import datetime, time
from types import SimpleNamespace

import pytest
from telethon.tl.types import DialogFilter, DialogFilterDefault, TextWithEntities

from watcher.listener import Channel, FolderNotFound, Listener, find_folder
from watcher.notifier import Dispatcher, format_alert, format_digest, in_window, split_message

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


def test_digest_lists_near_misses_and_errors(store):
    store.record_result(-1, 1, "https://t.me/a/1", "A", result(fit_score=60, eligible_from_ethiopia="unclear"))
    store.record_error(-1, 2, "https://t.me/a/2", "A", "Claude API 529")
    store.record_result(-1, 3, "https://t.me/a/3", "A", result(fit_score=40))
    rows = store.pending_digest(50, 70)
    text = format_digest(rows, 70)
    assert "<b>60</b>" in text and "location unclear" in text and "could not score" in text
    assert "/3" not in text


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
