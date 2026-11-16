from watcher.prefilter import compile_markers, reject_reason
from watcher.store import DUPLICATE_WINDOW_SECONDS, Store, normalize

from .conftest import load_post


def test_prefilter(markers):
    assert reject_reason("", 200, markers) == "no_text"
    assert reject_reason("Python dev wanted, DM @x", 200, markers) == "too_short"
    assert reject_reason(load_post("ad"), 200, markers) == "ad"
    assert reject_reason(load_post("ua_backend"), 200, markers) is None
    # A real marketing vacancy mentioning "реклама" is not an ad.
    vacancy = "Шукаємо спеціаліста з таргетованої реклами. " * 10
    assert reject_reason(vacancy, 200, markers) is None


def test_custom_markers():
    markers = compile_markers(["Спонсор поста"])
    assert reject_reason("спонсор поста: " + "x" * 300, 200, markers) == "ad"


def test_normalize_drops_links_tags_and_footer_noise():
    assert normalize("Hi @hr #job https://t.me/x 👉 Apply!") == "hi apply"


def test_claim_and_window():
    now = [1_000_000.0]
    store = Store(":memory:", clock=lambda: now[0])
    text = load_post("en_fullstack")
    assert store.claim(-100, 1, text) == "new"
    assert store.claim(-100, 1, text) == "seen"
    assert store.claim(-200, 9, text) == "duplicate"
    now[0] += DUPLICATE_WINDOW_SECONDS + 1  # the same vacancy reposted weeks later counts again
    assert store.claim(-300, 3, text) == "new"
    assert store.last_msg_id(-100) == 1 and store.last_msg_id(-999) is None


def test_near_duplicate_with_different_footer(store):
    assert store.claim(-1, 1, load_post("ua_backend")) == "new"
    assert store.claim(-2, 2, load_post("ua_backend_crosspost")) == "duplicate"


def test_similar_but_different_vacancies_are_not_duplicates(store):
    # Same template, different company, role, stack and terms.
    assert store.claim(-1, 1, load_post("ua_backend")) == "new"
    assert store.claim(-1, 2, load_post("eu_only")) == "new"
    assert store.claim(-1, 3, load_post("ru_python")) == "new"
    assert store.claim(-1, 4, load_post("en_fullstack")) == "new"


def test_outbox_and_meta(store):
    store.enqueue("a")
    store.enqueue("b")
    rows = store.pending_outbox()
    assert [r["body"] for r in rows] == ["a", "b"]
    store.mark_sent(rows[0]["id"])
    assert [r["body"] for r in store.pending_outbox()] == ["b"]
    store.set_meta("k", "v")
    assert store.get_meta("k") == "v"
