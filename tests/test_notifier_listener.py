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
    store.record_result(-1, 2, "https://t.me/a/2", "A", result(title="Near", fit_score=60, eligible="unclear"))
    store.record_result(-1, 3, "https://t.me/a/3", "A",
                        result(title="EU only", fit_score=30, eligible="no", location_rules="Poland residents"))
    store.record_result(-1, 4, "https://t.me/a/4", "A", result(title="Weak", fit_score=40))
    store.record_result(-1, 6, "https://t.me/a/6", "A", result(is_job_posting=False, fit_score=0, summary_en="A course ad"))
    store.record_error(-1, 5, "https://t.me/a/5", "A", "Gemini API 429: quota")
    store.record_outcome("skipped:ad", -1, 7, "https://t.me/a/7", "A")
    store.record_outcome("skipped:too_short", -1, 8, "https://t.me/a/8", "A")
    store.record_outcome("skipped:too_short")  # recorded before links were stored
    store.record_outcome("duplicate", -2, 9, "https://t.me/b/9", "B")
    store.record_outcome("seen", -1, 1, "https://t.me/a/1", "A")  # a re-read, not listed again


def _summary(store):
    return format_summary(24, store.outcomes_since(0), store.results_since(0), 70, 50)


def test_summary_lists_every_post_with_a_link(store):
    _seed(store)
    text = _summary(store)
    assert "Read 10 · scored 5 · job posts 4 · alerts 1 · near misses 1 · not open to you 1" in text
    assert "skipped 3 · duplicates 1 · errors 1" in text
    for n in (1, 2, 3, 4, 5, 6, 7, 8):
        assert f'href="https://t.me/a/{n}"' in text, n
    assert 'href="https://t.me/b/9"' in text
    assert text.count('href="https://t.me/a/1"') == 1  # the "seen" re-read is not listed twice
    for heading in ("Alerts", "Near misses", "Low fit", "Not open to your location", "Not job posts",
                    "Could not score", "Skipped, ads", "Skipped, too short", "Duplicates"):
        assert heading in text, heading
    assert "Poland residents" in text and "A course ad" in text
    assert "1 more from before links were recorded" in text


def test_summary_sections_are_in_order(store):
    _seed(store)
    text = _summary(store)
    order = ["Alerts", "Near misses", "Low fit", "Not open to your location", "Not job posts", "Could not score", "Skipped", "Duplicates"]
    positions = [text.index(h) for h in order]
    assert positions == sorted(positions)


def test_empty_summary(store):
    assert "Nothing read in this window yet." in _summary(store)


def test_long_summary_splits_under_the_limit(store):
    for i in range(300):
        store.record_result(-1, 100 + i, f"https://t.me/a/{100 + i}", "Some long channel name",
                            result(title=f"Role number {i} with a long title", fit_score=30))
    parts = split_message(_summary(store))
    from watcher.notifier import visible_len

    assert len(parts) > 1 and all(visible_len(p) <= 4000 for p in parts)
    assert sum(p.count("href=") for p in parts) == 300  # no line lost or cut


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


def test_hidden_links_and_buttons_are_added_to_the_text():
    from telethon.tl.types import (
        InlineButtonTypeCallback, InlineButtonTypeUrl, KeyboardInlineButton, KeyboardInlineButtonRow,
        MessageEntityBold, MessageEntityTextUrl, ReplyInlineMarkup,
    )

    from watcher.listener import text_with_links

    entities = [(MessageEntityTextUrl(0, 5, "https://jobs.example/apply"), "Apply"),
                (MessageEntityBold(6, 3), "now"),
                (MessageEntityTextUrl(10, 4, "https://t.me/visible"), "here")]
    markup = ReplyInlineMarkup(rows=[KeyboardInlineButtonRow(buttons=[
        KeyboardInlineButton(text="Apply on site", type=InlineButtonTypeUrl(url="https://jobs.example/form")),
        KeyboardInlineButton(text="Like", type=InlineButtonTypeCallback(data=b"x")),
    ])])
    message = SimpleNamespace(
        message="Apply now here https://t.me/visible",
        get_entities_text=lambda: entities,
        reply_markup=markup,
    )
    text = text_with_links(message)
    assert "- Apply: https://jobs.example/apply" in text
    assert "- Apply on site: https://jobs.example/form" in text
    assert text.count("https://t.me/visible") == 1  # already in the text, not repeated
    assert "Like" not in text


def test_old_layer_url_buttons_still_work():
    from watcher.listener import extract_links

    button = SimpleNamespace(text="Apply", url="https://old.example")
    message = SimpleNamespace(message="x", get_entities_text=lambda: [],
                              reply_markup=SimpleNamespace(rows=[SimpleNamespace(buttons=[button])]))
    assert extract_links(message) == [("Apply", "https://old.example")]


def test_post_without_links_is_unchanged():
    from watcher.listener import text_with_links

    message = SimpleNamespace(message="plain", get_entities_text=lambda: [], reply_markup=None)
    assert text_with_links(message) == "plain"
