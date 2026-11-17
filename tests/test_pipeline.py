"""The six sample posts through the whole pipeline, with the Claude call faked.

What the real model returns for these posts is covered by test_live_scorer.py."""

from watcher.pipeline import Pipeline, Post, post_link

from .conftest import FakeScorer, load_post, result

CANNED = {
    "Lumera Health": result(title="Senior Backend Engineer", source_language="uk", fit_score=88),
    "DataPort": result(title="Python Developer", source_language="ru", fit_score=76, apply_urgency="high"),
    "Orbital Labs": result(title="Full-Stack Engineer", source_language="en", fit_score=82),
    "Nordwave": result(title="Senior Node.js Developer", eligible_from_ethiopia="no", fit_score=30),
}


def make(store, dispatcher, markers, scorer):
    return Pipeline(store, scorer, dispatcher, markers, fit_threshold=70, min_post_chars=200)


def post(name, chat_id=-1001111111111, msg_id=1, username="ua_jobs"):
    return Post(chat_id=chat_id, msg_id=msg_id, text=load_post(name), channel_title="UA Jobs", username=username)


async def test_ukrainian_fit_alerts(store, dispatcher, notifier, markers):
    scorer = FakeScorer(CANNED)
    outcome = await make(store, dispatcher, markers, scorer).handle(post("ua_backend"))
    assert outcome == "alerted"
    text, _ = notifier.sent[0]
    assert "88/100" in text and "https://t.me/ua_jobs/1" in text


async def test_russian_fit_alerts(store, dispatcher, notifier, markers):
    outcome = await make(store, dispatcher, markers, FakeScorer(CANNED)).handle(post("ru_python"))
    assert outcome == "alerted"
    assert notifier.sent[0][0].startswith("🔥")  # high urgency


async def test_english_fit_alerts(store, dispatcher, notifier, markers):
    outcome = await make(store, dispatcher, markers, FakeScorer(CANNED)).handle(post("en_fullstack"))
    assert outcome == "alerted"


async def test_ad_never_reaches_the_api(store, dispatcher, notifier, markers):
    scorer = FakeScorer(CANNED)
    outcome = await make(store, dispatcher, markers, scorer).handle(post("ad"))
    assert outcome == "skipped:ad"
    assert scorer.calls == [] and notifier.sent == []
    assert store.outcome_counts(0) == {"skipped:ad": 1}  # skips still count in summaries


async def test_eu_only_role_is_not_sent(store, dispatcher, notifier, markers):
    outcome = await make(store, dispatcher, markers, FakeScorer(CANNED)).handle(post("eu_only"))
    assert outcome == "ineligible"
    assert notifier.sent == []


async def test_cross_post_scored_once(store, dispatcher, notifier, markers):
    scorer = FakeScorer(CANNED)
    pipeline = make(store, dispatcher, markers, scorer)
    first = await pipeline.handle(post("ua_backend", chat_id=-1001111111111, msg_id=10))
    second = await pipeline.handle(
        post("ua_backend_crosspost", chat_id=-1002222222222, msg_id=77, username=None)
    )
    assert (first, second) == ("alerted", "duplicate")
    assert len(scorer.calls) == 1 and len(notifier.sent) == 1


async def test_same_message_twice_is_seen(store, dispatcher, notifier, markers):
    pipeline = make(store, dispatcher, markers, FakeScorer(CANNED))
    await pipeline.handle(post("en_fullstack"))
    assert await pipeline.handle(post("en_fullstack")) == "seen"
    assert len(notifier.sent) == 1


async def test_below_threshold_is_kept_for_digest(store, dispatcher, notifier, markers):
    scorer = FakeScorer({"Orbital Labs": result(fit_score=60, eligible_from_ethiopia="unclear")})
    outcome = await make(store, dispatcher, markers, scorer).handle(post("en_fullstack"))
    assert outcome == "below_threshold"
    assert notifier.sent == []
    assert [r["fit_score"] for r in store.results_since(0)] == [60]
    assert store.outcome_counts(0) == {"below_threshold": 1}


async def test_scoring_error_is_recorded_not_raised(store, dispatcher, notifier, markers):
    from watcher.scorer import ScoreError

    class Broken:
        async def score(self, text, channel):
            raise ScoreError("Claude API 529: overloaded")

    outcome = await make(store, dispatcher, markers, Broken()).handle(post("en_fullstack"))
    assert outcome == "error"
    assert store.results_since(0)[0]["status"] == "error"


def test_links():
    assert post_link(Post(-1001234567890, 5, "x", "c", "jobs")) == "https://t.me/jobs/5"
    assert post_link(Post(-1001234567890, 5, "x", "c", None)) == "https://t.me/c/1234567890/5"
    assert post_link(Post(-4242, 5, "x", "c", None)) == ""
