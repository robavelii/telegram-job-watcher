"""Settings from the environment, plus the resume and preferences files."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import time
from pathlib import Path

import yaml

PROVIDERS = {
    # provider: (API key variable, default model)
    "gemini": ("GEMINI_API_KEY", "gemini-3.5-flash-lite"),
    "anthropic": ("ANTHROPIC_API_KEY", "claude-haiku-5-5"),
}


class ConfigError(RuntimeError):
    pass


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"{name} is not set")
    return value


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _required_int(name: str) -> int:
    raw = _required(name)
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def parse_hhmm(raw: str) -> time:
    try:
        hours, minutes = raw.strip().split(":")
        return time(int(hours), int(minutes))
    except ValueError as exc:
        raise ConfigError(f"expected HH:MM, got {raw!r}") from exc


def parse_window(raw: str) -> tuple[time, time] | None:
    """'23:00-07:00' -> (23:00, 07:00). Empty means no quiet hours."""
    raw = raw.strip()
    if not raw:
        return None
    try:
        start, end = raw.split("-")
    except ValueError as exc:
        raise ConfigError(f"QUIET_HOURS must look like 23:00-07:00, got {raw!r}") from exc
    return parse_hhmm(start), parse_hhmm(end)


def resolve_provider() -> str:
    """LLM_PROVIDER if set; otherwise whichever provider has a key, Gemini first."""
    raw = os.environ.get("LLM_PROVIDER", "").strip().lower()
    if raw:
        if raw not in PROVIDERS:
            raise ConfigError(f"LLM_PROVIDER must be one of {sorted(PROVIDERS)}, got {raw!r}")
        return raw
    for provider, (key_var, _) in PROVIDERS.items():
        if os.environ.get(key_var, "").strip():
            return provider
    raise ConfigError("no model API key: set GEMINI_API_KEY or ANTHROPIC_API_KEY (and optionally LLM_PROVIDER)")


@dataclass(frozen=True)
class Settings:
    api_id: int
    api_hash: str
    session_string: str
    folder_name: str
    bot_token: str
    notify_chat_id: int
    llm_provider: str
    llm_model: str
    llm_effort: str
    fit_threshold: int
    near_miss_min: int
    min_post_chars: int
    backfill_per_channel: int
    folder_refresh_seconds: int
    timezone: str
    quiet_hours: tuple[time, time] | None
    digest_at: time | None
    data_dir: Path
    config_dir: Path
    dry_run: bool

    @classmethod
    def from_env(cls) -> "Settings":
        provider = resolve_provider()
        key_var, default_model = PROVIDERS[provider]
        # The SDK reads the key itself; check it here so a missing key fails at startup.
        _required(key_var)
        legacy_model = os.environ.get("CLAUDE_MODEL", "").strip() if provider == "anthropic" else ""
        digest_raw = os.environ.get("DAILY_DIGEST_AT", "").strip()
        return cls(
            api_id=_required_int("TELEGRAM_API_ID"),
            api_hash=_required("TELEGRAM_API_HASH"),
            session_string=_required("TELEGRAM_SESSION_STRING"),
            folder_name=os.environ.get("TELEGRAM_FOLDER_NAME", "remote-jobs").strip() or "remote-jobs",
            bot_token=_required("BOT_TOKEN"),
            notify_chat_id=_required_int("NOTIFY_CHAT_ID"),
            llm_provider=provider,
            llm_model=os.environ.get("LLM_MODEL", "").strip() or legacy_model or default_model,
            llm_effort=os.environ.get("CLAUDE_EFFORT", "").strip() or "medium",
            fit_threshold=_int("FIT_THRESHOLD", 70),
            near_miss_min=_int("NEAR_MISS_MIN", 50),
            min_post_chars=_int("MIN_POST_CHARS", 200),
            backfill_per_channel=_int("BACKFILL_PER_CHANNEL", 0),
            folder_refresh_seconds=_int("FOLDER_REFRESH_SECONDS", 900),
            timezone=os.environ.get("TZ_NAME", "").strip() or "Africa/Addis_Ababa",
            quiet_hours=parse_window(os.environ.get("QUIET_HOURS", "")),
            digest_at=parse_hhmm(digest_raw) if digest_raw else None,
            data_dir=Path(os.environ.get("DATA_DIR", "/data")),
            config_dir=Path(os.environ.get("CONFIG_DIR", "/app/config")),
            dry_run=os.environ.get("DRY_RUN", "0").strip().lower() in {"1", "true", "yes"},
        )


@dataclass(frozen=True)
class Profile:
    resume: str
    preferences_text: str
    ad_markers: list[str]


def load_profile(config_dir: Path) -> Profile:
    resume_path = config_dir / "resume.md"
    prefs_path = config_dir / "preferences.yaml"
    for path in (resume_path, prefs_path):
        if not path.is_file():
            raise ConfigError(f"{path} is missing (copy the .example file next to it and fill it in)")
    resume = resume_path.read_text(encoding="utf-8").strip()
    prefs_text = prefs_path.read_text(encoding="utf-8").strip()
    prefs = yaml.safe_load(prefs_text) or {}
    if not isinstance(prefs, dict):
        raise ConfigError(f"{prefs_path} must be a YAML mapping")
    markers = [str(m) for m in prefs.get("ad_markers", []) or []]
    return Profile(resume=resume, preferences_text=prefs_text, ad_markers=markers)
