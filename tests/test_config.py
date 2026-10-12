"""Configuration layering, plus the small pure helpers in the speech pipeline."""

from __future__ import annotations

import pytest

from openjarvis.agent.prompts import SYSTEM_PROMPT, TRANSCRIPTION_HINT
from openjarvis.config import DEFAULT_LLM_MODELS, DEFAULT_TTS_VOICES, Settings
from openjarvis.errors import ConfigError
from openjarvis.speech.stt import clean_transcript, strip_wake_word


def write_config(tmp_path, body: str):
    path = tmp_path / "config.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def test_defaults_are_usable_with_no_config_file(tmp_path):
    cfg = Settings.load(tmp_path / "does-not-exist.yaml")
    assert cfg.llm.provider == "groq"
    assert cfg.llm.resolved_model() == DEFAULT_LLM_MODELS["groq"]
    assert cfg.speech.tts_engine == "edge"
    assert cfg.speech.resolved_tts_voice() == "en-GB-SoniaNeural"
    assert cfg.speech.wake_word == "hey_jarvis"


def test_assistant_identity_and_transcription_vocabulary():
    assert "You are Sienna" in SYSTEM_PROMPT
    assert "OpenJarvis" not in SYSTEM_PROMPT
    assert "HelloSienna" in TRANSCRIPTION_HINT
    assert "Sienna" in TRANSCRIPTION_HINT


def test_existing_wake_word_loads_from_yaml(tmp_path):
    path = write_config(tmp_path, "speech:\n  wake_word: hey_jarvis\n")
    assert Settings.load(path).speech.wake_word == "hey_jarvis"


def test_yaml_overrides_defaults(tmp_path):
    path = write_config(tmp_path, "llm:\n  provider: openai\n  temperature: 0.9\n")
    cfg = Settings.load(path)
    assert cfg.llm.provider == "openai"
    assert cfg.llm.resolved_model() == DEFAULT_LLM_MODELS["openai"]
    assert cfg.llm.temperature == 0.9


def test_environment_overrides_yaml(tmp_path, monkeypatch):
    path = write_config(tmp_path, "llm:\n  provider: openai\n")
    monkeypatch.setenv("OPENJARVIS_LLM_PROVIDER", "groq")
    assert Settings.load(path).llm.provider == "groq"


def test_cli_flags_override_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENJARVIS_LLM_PROVIDER", "groq")
    cfg = Settings.load(tmp_path / "none.yaml").override(provider="openai")
    assert cfg.llm.provider == "openai"


def test_none_overrides_are_ignored(tmp_path):
    cfg = Settings.load(tmp_path / "none.yaml")
    before = cfg.llm.provider
    cfg.override(provider=None, model=None)
    assert cfg.llm.provider == before


def test_broken_yaml_gives_a_clear_error(tmp_path):
    path = write_config(tmp_path, "llm:\n  provider: [unclosed\n")
    with pytest.raises(ConfigError, match="not valid YAML"):
        Settings.load(path)


def test_bad_environment_value_gives_a_clear_error(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENJARVIS_MAX_STEPS", "not-a-number")
    with pytest.raises(ConfigError, match="not a valid int"):
        Settings.load(tmp_path / "none.yaml")


def test_rate_limit_retry_wait_is_configurable(tmp_path, monkeypatch):
    path = write_config(tmp_path, "llm:\n  rate_limit_max_wait_s: 5\n")
    assert Settings.load(path).llm.rate_limit_max_wait_s == 5

    monkeypatch.setenv("OPENJARVIS_RATE_LIMIT_MAX_WAIT_S", "3")
    assert Settings.load(path).llm.rate_limit_max_wait_s == 3


def test_rate_limit_retry_wait_rejects_out_of_range_values(tmp_path, monkeypatch):
    path = write_config(tmp_path, "llm:\n  rate_limit_max_wait_s: 31\n")
    with pytest.raises(ValueError):
        Settings.load(path)

    monkeypatch.setenv("OPENJARVIS_RATE_LIMIT_MAX_WAIT_S", "31")
    with pytest.raises(ConfigError, match="between 0 and 30 seconds"):
        Settings.load(tmp_path / "none.yaml")


def test_missing_api_key_names_the_variable(tmp_path, monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    cfg = Settings.load(tmp_path / "none.yaml")
    with pytest.raises(ConfigError, match="GROQ_API_KEY"):
        cfg.api_key_for("groq")


def test_saved_yaml_never_contains_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_supersecret")
    rendered = Settings.load(tmp_path / "none.yaml").to_yaml()
    assert "gsk_supersecret" not in rendered
    assert "api_key" not in rendered


def test_config_round_trips_through_yaml(tmp_path):
    original = Settings()
    original.llm.provider = "openai"
    original.speech.wake_word = "alexa"
    original.safety.allowed_roots = ["~/Documents"]
    path = original.save(tmp_path / "config.yaml")

    reloaded = Settings.load(path)
    assert reloaded.llm.provider == "openai"
    assert reloaded.speech.wake_word == "alexa"
    assert reloaded.safety.allowed_roots == ["~/Documents"]


def test_voice_defaults_are_per_engine():
    """An edge voice name passed to pyttsx3 would just error, so fallback re-resolves."""
    cfg = Settings()
    cfg.speech.tts_engine = "edge"
    cfg.speech.tts_voice = "en-GB-RyanNeural"
    assert cfg.speech.resolved_tts_voice() == "en-GB-RyanNeural"
    assert cfg.speech.resolved_tts_voice("system") == DEFAULT_TTS_VOICES["system"]
    assert cfg.speech.resolved_tts_voice("openai") == "alloy"


def test_tts_voice_can_be_configured_in_yaml_and_environment(tmp_path, monkeypatch):
    path = write_config(tmp_path, "speech:\n  tts_voice: en-GB-RyanNeural\n")
    assert Settings.load(path).speech.resolved_tts_voice() == "en-GB-RyanNeural"

    monkeypatch.setenv("OPENJARVIS_TTS_VOICE", "en-GB-SoniaNeural")
    assert Settings.load(path).speech.resolved_tts_voice() == "en-GB-SoniaNeural"


def test_edge_voice_and_natural_rate_defaults():
    cfg = Settings()
    assert cfg.speech.resolved_tts_voice("edge") == "en-GB-SoniaNeural"
    assert cfg.speech.tts_rate_pct == 0
    assert cfg.speech.tts_fallback == "system"


# ------------------------------------------------------------- transcript hygiene


@pytest.mark.parametrize(
    "raw", ["", "   ", "you", "Thank you.", "Thanks for watching!", "[BLANK_AUDIO]", "..."]
)
def test_whisper_silence_hallucinations_are_discarded(raw):
    """Whisper invents stock phrases from silence; acting on them would be chaos."""
    assert clean_transcript(raw) == ""


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  open   chrome  ", "open chrome"),
        ("[NOISE] find my latest pdf", "find my latest pdf"),
    ],
)
def test_real_speech_survives_cleaning(raw, expected):
    assert clean_transcript(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["Hey Jarvis, open Chrome", "hey jarvis open Chrome", "Jarvis, open Chrome", "open Chrome"],
)
def test_the_wake_word_is_stripped_from_the_command(raw):
    assert strip_wake_word(raw, "hey_jarvis") == "open Chrome"


def test_stripping_never_returns_an_empty_command():
    """"Hey Jarvis" alone should stay intact rather than becoming an empty request."""
    assert strip_wake_word("Hey Jarvis", "hey_jarvis") == "Hey Jarvis"
