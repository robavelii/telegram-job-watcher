"""Cheap checks that run before any API call."""

from __future__ import annotations

import re

# Markers of paid posts in RU/UA channels. Bare "реклама" is left out on purpose:
# it shows up in real marketing vacancies. Extend with `ad_markers` in preferences.yaml.
DEFAULT_AD_MARKERS = [
    r"\berid\b",  # Russian mandatory ad label
    r"#реклама",
    r"на правах рекламы",
    r"на правах реклами",
    r"партнерский материал",
    r"партнерський матеріал",
    r"#промо",
    r"#promo\b",
    r"#ad\b",
    r"промокод",
    r"розыгрыш",
    r"розіграш",
    r"giveaway",
    r"казино",
    r"casino",
    r"букмекер",
]


def compile_markers(extra: list[str] | None = None) -> list[re.Pattern[str]]:
    patterns = DEFAULT_AD_MARKERS + [re.escape(m.lower()) for m in (extra or []) if m.strip()]
    return [re.compile(p, re.IGNORECASE) for p in patterns]


def reject_reason(text: str, min_chars: int, markers: list[re.Pattern[str]]) -> str | None:
    """None if the post should go to the scorer, otherwise a short reason."""
    stripped = text.strip()
    if not stripped:
        return "no_text"
    if len(stripped) < min_chars:
        return "too_short"
    for marker in markers:
        if marker.search(stripped):
            return "ad"
    return None
