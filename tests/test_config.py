import pytest

from watcher.config import ConfigError, Settings, resolve_provider

BASE = {
    "TELEGRAM_API_ID": "1",
    "TELEGRAM_API_HASH": "h",
    "TELEGRAM_SESSION_STRING": "s",
    "BOT_TOKEN": "t",
    "NOTIFY_CHAT_ID": "2",
    "CANDIDATE_LOCATION": "Kenya",
}
KEYS = ("TZ_NAME", "LLM_PROVIDER", "LLM_MODEL", "GEMINI_API_KEY", "ANTHROPIC_API_KEY", "CLAUDE_MODEL", "GOOGLE_API_KEY")


@pytest.fixture
def env(monkeypatch):
    for k in KEYS:
        monkeypatch.delenv(k, raising=False)
    for k, v in BASE.items():
        monkeypatch.setenv(k, v)
    return monkeypatch


def test_gemini_key_alone_picks_gemini(env):
    env.setenv("GEMINI_API_KEY", "g")
    s = Settings.from_env()
    assert (s.llm_provider, s.llm_model) == ("gemini", "gemini-3.5-flash-lite")


def test_anthropic_key_alone_picks_anthropic(env):
    env.setenv("ANTHROPIC_API_KEY", "a")
    s = Settings.from_env()
    assert (s.llm_provider, s.llm_model) == ("anthropic", "claude-haiku-5-5")


def test_explicit_provider_needs_its_key(env):
    env.setenv("LLM_PROVIDER", "anthropic")
    env.setenv("GEMINI_API_KEY", "g")
    with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY"):
        Settings.from_env()


def test_model_override_and_legacy_claude_model(env):
    env.setenv("GEMINI_API_KEY", "g")
    env.setenv("CLAUDE_MODEL", "claude-haiku-5-5")  # left over in old .env files; ignored for Gemini
    assert Settings.from_env().llm_model == "gemini-3.5-flash-lite"
    env.setenv("LLM_MODEL", "gemini-3.6-flash")
    assert Settings.from_env().llm_model == "gemini-3.6-flash"


def test_no_key_and_bad_provider(env):
    with pytest.raises(ConfigError, match="GEMINI_API_KEY or ANTHROPIC_API_KEY"):
        resolve_provider()
    env.setenv("LLM_PROVIDER", "openai")
    with pytest.raises(ConfigError, match="LLM_PROVIDER"):
        resolve_provider()


def test_bad_preferences_yaml_is_a_config_error(tmp_path):
    from watcher.config import load_profile

    (tmp_path / "resume.md").write_text("resume")
    (tmp_path / "preferences.yaml").write_text("target_roles:\n  - AI engineer: building\n    RAG\n")
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_profile(tmp_path)


def test_candidate_location_is_required(env):
    env.setenv("GEMINI_API_KEY", "g")
    assert Settings.from_env().candidate_location == "Kenya"
    env.delenv("CANDIDATE_LOCATION")
    with pytest.raises(ConfigError, match="CANDIDATE_LOCATION"):
        Settings.from_env()


def test_timezone_defaults_to_utc(env):
    env.setenv("GEMINI_API_KEY", "g")
    assert Settings.from_env().timezone == "UTC"
