from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from watcher.notifier import Dispatcher
from watcher.prefilter import compile_markers
from watcher.store import Store

FIXTURES = Path(__file__).parent / "fixtures"


def load_post(name: str) -> str:
    return (FIXTURES / "posts" / f"{name}.txt").read_text(encoding="utf-8")


def result(**overrides) -> dict:
    base = {
        "is_job_posting": True,
        "title": "Senior Backend Engineer",
        "company": "Acme",
        "source_language": "en",
        "summary_en": "Builds APIs.",
        "stack": ["Node.js", "PostgreSQL"],
        "seniority": "senior",
        "employment_type": "full-time",
        "salary": "$4000",
        "location_rules": "worldwide",
        "eligible_from_ethiopia": "yes",
        "fit_score": 85,
        "fit_reasons": ["NestJS match"],
        "gaps": [],
        "how_to_apply": "@acme_hr",
        "apply_urgency": "normal",
    }
    base.update(overrides)
    return base


class FakeScorer:
    """Returns canned results keyed by a phrase in the post, and records calls."""

    def __init__(self, by_phrase: dict[str, dict]):
        self.by_phrase = by_phrase
        self.calls: list[str] = []

    async def score(self, text: str, channel: str) -> dict:
        self.calls.append(text)
        for phrase, res in self.by_phrase.items():
            if phrase in text:
                return res
        return result(is_job_posting=False, fit_score=0)


class FakeNotifier:
    def __init__(self):
        self.sent: list[tuple[str, bool]] = []

    async def send(self, text: str, silent: bool = False) -> None:
        self.sent.append((text, silent))


class Clock:
    def __init__(self, dt: datetime):
        self.dt = dt

    def __call__(self) -> datetime:
        return self.dt


@pytest.fixture
def store():
    return Store(":memory:")


@pytest.fixture
def notifier():
    return FakeNotifier()


@pytest.fixture
def clock():
    return Clock(datetime(2026, 11, 16, 12, 0))


@pytest.fixture
def dispatcher(notifier, store, clock):
    return Dispatcher(notifier, store, quiet_hours=None, now=clock)


@pytest.fixture
def markers():
    return compile_markers()
