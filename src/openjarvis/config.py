"""Layered configuration for OpenJarvis.

Precedence, lowest to highest:

    1. the defaults baked into these models
    2. ``~/.openjarvis/config.yaml`` (or ``--config <path>``)
    3. environment variables, including ``.env`` files
    4. CLI flags (applied by :mod:`openjarvis.cli` via :meth:`Settings.override`)

Secrets only ever come from the environment - we never read or write API keys in the
YAML file, so a user can safely commit or paste their config.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field

from .errors import ConfigError

APP_DIR = Path(os.environ.get("OPENJARVIS_HOME") or Path.home() / ".openjarvis")
CONFIG_PATH = APP_DIR / "config.yaml"

Provider = Literal["groq", "openai"]
SafetyMode = Literal["tiered", "strict", "auto"]
# "edge" is Microsoft Edge's neural read-aloud service: free, no key, very natural.
# "system" is pyttsx3 (SAPI5 / NSSpeech / espeak) - robotic, but fully offline.
TTSEngine = Literal["edge", "system", "openai", "groq", "none"]

# Provider defaults. Overridable per-user; these are just what you get out of the box.
# Tool-calling reliability matters far more here than raw reasoning: a model that emits a
# malformed function call is useless to an agent no matter how clever it is. gpt-oss-120b
# is markedly steadier at this on Groq than llama-3.3-70b, which regularly emits its tool
# calls as plain text and gets rejected.
DEFAULT_LLM_MODELS: dict[str, str] = {
    "groq": "openai/gpt-oss-120b",
    "openai": "gpt-4o-mini",
}
# Rate-limit fallback: Groq serves free-tier limits per model, so when the workhorse is
# capped past the retry window, one bounded attempt on a different model keeps the
# request moving instead of leaving the user in dead air. llama-3.1-8b-instant sits in
# its own bucket and handles tool calls. OpenAI's org-level envelopes make a silent
# model switch less predictable, so it disables the fallback by default.
DEFAULT_FALLBACK_MODELS: dict[str, str | None] = {
    "groq": "llama-3.1-8b-instant",
    "openai": None,
}
# Groq's whisper-large-v3-turbo and whisper-large-v3 are separately-bucketed models.
AUDIO_FALLBACK_MODELS: dict[str, str | None] = {
    "groq": "whisper-large-v3",
    "openai": None,
}
DEFAULT_STT_MODELS: dict[str, str] = {
    "groq": "whisper-large-v3-turbo",
    "openai": "whisper-1",
}
DEFAULT_TTS_MODELS: dict[str, str] = {
    "groq": "playai-tts",
    "openai": "gpt-4o-mini-tts",
}
# Voice identifiers are engine-specific, so each engine gets its own default.
DEFAULT_TTS_VOICES: dict[str, str] = {
    "edge": "en-GB-SoniaNeural",
    "system": "",  # empty = whatever the OS has configured
    "openai": "alloy",
    "groq": "Fritz-PlayAI",
    "none": "",
}
BASE_URLS: dict[str, str | None] = {
    "groq": "https://api.groq.com/openai/v1",
    "openai": None,  # the SDK default
}
API_KEY_ENV: dict[str, str] = {
    "groq": "GROQ_API_KEY",
    "openai": "OPENAI_API_KEY",
}


class LLMConfig(BaseModel):
    provider: Provider = "groq"
    model: str | None = None
    temperature: float = 0.2
    max_tokens: int = 1024
    rate_limit_max_wait_s: float = Field(default=8.0, ge=0, le=30)
    # Hard ceiling on tool-call rounds per request, so a confused model cannot loop forever.
    max_steps: int = 12
    # Conversation turns retained between requests (the system prompt is always kept).
    history_turns: int = 20
    timeout_s: float = 60.0
    # Model switch when the primary model's rate limit outlasts rate_limit_max_wait_s.
    # None = provider default (see DEFAULT_FALLBACK_MODELS); "" disables the fallback.
    fallback_model: str | None = None

    def resolved_model(self) -> str:
        return self.model or DEFAULT_LLM_MODELS[self.provider]

    def resolved_fallback_model(self) -> str | None:
        if self.fallback_model is not None:
            return self.fallback_model or None
        return DEFAULT_FALLBACK_MODELS[self.provider]


class SpeechConfig(BaseModel):
    """Voice pipeline: wake word -> record until silence -> transcribe -> speak reply."""

    wake_word: str = "hey_jarvis"
    wake_threshold: float = 0.5
    # Seconds after a trigger during which we skip wake-word detection (avoids double fires).
    wake_cooldown_s: float = 2.0

    stt_provider: Provider = "groq"
    stt_model: str | None = None
    language: str | None = None  # ISO-639-1, e.g. "en". None = auto-detect.

    tts_engine: TTSEngine = "edge"
    tts_model: str | None = None
    tts_voice: str | None = None  # None = the engine's default (see DEFAULT_TTS_VOICES)
    # Speaking rate. edge-tts takes a percentage delta ("+10%"); pyttsx3 takes words/min.
    tts_rate_pct: int = 0
    tts_rate_wpm: int = 190
    # Fall back to the offline engine if the network-backed one fails mid-session.
    tts_fallback: TTSEngine = "system"

    input_device: int | str | None = None
    sample_rate: int = 16000
    # End-of-utterance detection.
    silence_ms: int = 900
    min_utterance_ms: int = 400
    max_utterance_s: float = 20.0
    # Multiplier over the measured noise floor that counts as speech.
    vad_sensitivity: float = 2.5
    # Keep listening for follow-ups for this long after a reply, no wake word needed.
    followup_window_s: float = 8.0

    def resolved_stt_model(self) -> str:
        return self.stt_model or DEFAULT_STT_MODELS[self.stt_provider]

    def resolved_tts_model(self) -> str:
        if self.tts_engine in ("edge", "system", "none"):
            return ""
        return self.tts_model or DEFAULT_TTS_MODELS[self.tts_engine]

    def resolved_tts_voice(self, engine: str | None = None) -> str:
        engine = engine or self.tts_engine
        # An explicit voice only applies to the engine it was chosen for; falling back to
        # pyttsx3 with an edge voice name would just error.
        if self.tts_voice and engine == self.tts_engine:
            return self.tts_voice
        return DEFAULT_TTS_VOICES.get(engine, "")


class SafetyConfig(BaseModel):
    """See :mod:`openjarvis.safety.policy` for how these are enforced."""

    mode: SafetyMode = "tiered"
    # Tool names that never prompt, whatever their risk level.
    auto_approve: list[str] = Field(default_factory=list)
    # Tool names that always prompt, even if they are read-only.
    always_confirm: list[str] = Field(default_factory=list)
    # If non-empty, file tools may only touch paths under these roots.
    allowed_roots: list[str] = Field(default_factory=list)
    # Paths the agent may never write to, on top of the built-in OS protections.
    denied_paths: list[str] = Field(default_factory=list)
    protect_system_paths: bool = True
    # Deletes go to the recycle bin / trash when available instead of unlinking.
    prefer_recycle_bin: bool = True


class ToolsConfig(BaseModel):
    # Friendly name -> executable, URI, or path. Merged over the built-in alias table.
    app_aliases: dict[str, str] = Field(default_factory=dict)
    # Extra roots for file search, on top of the user's home directory.
    search_roots: list[str] = Field(default_factory=list)
    search_max_results: int = 50
    search_timeout_s: float = 12.0
    # Directory names never descended into during search.
    search_exclude_dirs: list[str] = Field(
        default_factory=lambda: [
            "node_modules", ".git", "__pycache__", ".venv", "venv", "AppData",
            "$RECYCLE.BIN", "System Volume Information", ".cache", "site-packages",
        ]
    )
    search_engine: str = "https://www.google.com/search?q={query}"
    browser: str | None = None  # None = OS default


class Settings(BaseModel):
    llm: LLMConfig = Field(default_factory=LLMConfig)
    speech: SpeechConfig = Field(default_factory=SpeechConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    log_level: str = "INFO"
    # Where transcripts and the undo journal live.
    data_dir: Path = Field(default_factory=lambda: APP_DIR)

    # ---------------------------------------------------------------- loading

    @classmethod
    def load(cls, config_path: Path | None = None) -> Settings:
        """Build settings from YAML + environment."""
        _load_dotenv_files()
        path = config_path or CONFIG_PATH
        raw: dict[str, Any] = {}
        if path.exists():
            try:
                raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            except yaml.YAMLError as exc:
                raise ConfigError(
                    f"{path} is not valid YAML: {exc}",
                    hint="Fix the syntax, or delete the file to fall back to defaults.",
                ) from exc
            if not isinstance(raw, dict):
                raise ConfigError(f"{path} must contain a YAML mapping at the top level.")

        settings = cls.model_validate(raw)
        settings._apply_env()
        return settings

    def _apply_env(self) -> None:
        """Environment variables win over the YAML file."""
        env_map: list[tuple[str, Any, str, type]] = [
            ("OPENJARVIS_LLM_PROVIDER", self.llm, "provider", str),
            ("OPENJARVIS_LLM_MODEL", self.llm, "model", str),
            ("OPENJARVIS_LLM_TEMPERATURE", self.llm, "temperature", float),
            ("OPENJARVIS_RATE_LIMIT_MAX_WAIT_S", self.llm, "rate_limit_max_wait_s", float),
            ("OPENJARVIS_FALLBACK_MODEL", self.llm, "fallback_model", str),
            ("OPENJARVIS_MAX_STEPS", self.llm, "max_steps", int),
            ("OPENJARVIS_STT_PROVIDER", self.speech, "stt_provider", str),
            ("OPENJARVIS_STT_MODEL", self.speech, "stt_model", str),
            ("OPENJARVIS_TTS_PROVIDER", self.speech, "tts_engine", str),
            ("OPENJARVIS_TTS_VOICE", self.speech, "tts_voice", str),
            ("OPENJARVIS_WAKE_WORD", self.speech, "wake_word", str),
            ("OPENJARVIS_SAFETY_MODE", self.safety, "mode", str),
            ("OPENJARVIS_LOG_LEVEL", self, "log_level", str),
        ]
        for env_key, target, field, caster in env_map:
            value = os.environ.get(env_key)
            if value is None or value == "":
                continue
            try:
                parsed = caster(value)
                if field == "rate_limit_max_wait_s" and not 0 <= parsed <= 30:
                    raise ConfigError(
                        f"{env_key}={value!r} must be between 0 and 30 seconds."
                    )
                setattr(target, field, parsed)
            except (TypeError, ValueError) as exc:
                raise ConfigError(f"{env_key}={value!r} is not a valid {caster.__name__}.") from exc

    def override(self, **kwargs: Any) -> Settings:
        """Apply CLI flags. ``None`` values are ignored so flags stay optional."""
        dotted = {
            "provider": ("llm", "provider"),
            "model": ("llm", "model"),
            "temperature": ("llm", "temperature"),
            "max_steps": ("llm", "max_steps"),
            "stt_provider": ("speech", "stt_provider"),
            "tts_engine": ("speech", "tts_engine"),
            "wake_word": ("speech", "wake_word"),
            "input_device": ("speech", "input_device"),
            "safety_mode": ("safety", "mode"),
            "log_level": (None, "log_level"),
        }
        for key, value in kwargs.items():
            if value is None:
                continue
            if key not in dotted:
                raise ConfigError(f"Unknown override {key!r}.")
            section, field = dotted[key]
            target = self if section is None else getattr(self, section)
            setattr(target, field, value)
        return self

    # ------------------------------------------------------------ credentials

    def api_key_for(self, provider: str) -> str:
        env_key = API_KEY_ENV.get(provider)
        if env_key is None:
            raise ConfigError(
                f"Unknown provider {provider!r}. Expected one of {list(API_KEY_ENV)}."
            )
        key = os.environ.get(env_key, "").strip()
        if not key:
            raise ConfigError(
                f"{env_key} is not set, but the {provider} provider is selected.",
                hint=(
                    f"Add {env_key}=... to your .env file (see .env.example), "
                    f"or switch providers with --provider."
                ),
            )
        return key

    def base_url_for(self, provider: str) -> str | None:
        return BASE_URLS.get(provider)

    def available_providers(self) -> list[str]:
        """Providers that currently have a usable API key."""
        return [p for p, env_key in API_KEY_ENV.items() if os.environ.get(env_key, "").strip()]

    # ----------------------------------------------------------------- saving

    def to_yaml(self) -> str:
        data = self.model_dump(mode="json", exclude={"data_dir"})
        return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)

    def save(self, path: Path | None = None) -> Path:
        target = path or CONFIG_PATH
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.to_yaml(), encoding="utf-8")
        return target


def _load_dotenv_files() -> None:
    """Load ./.env then ~/.openjarvis/.env. Existing env vars always win."""
    for candidate in (Path.cwd() / ".env", APP_DIR / ".env"):
        if candidate.exists():
            load_dotenv(candidate, override=False)
