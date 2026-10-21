"""Exception hierarchy for OpenJarvis.

Anything raised deliberately by OpenJarvis inherits from :class:`JarvisError`, so the
CLI can print a clean one-line message instead of a traceback. Unexpected exceptions
still bubble up with a full traceback, which is what you want while debugging.
"""

from __future__ import annotations


class JarvisError(Exception):
    """Base class for all expected OpenJarvis failures."""

    hint: str | None = None

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if hint is not None:
            self.hint = hint


class ConfigError(JarvisError):
    """The configuration file or environment is missing something required."""


class ProviderError(JarvisError):
    """An LLM / STT / TTS provider could not be reached or returned garbage."""


class RateLimitProviderError(ProviderError):
    """A provider rate limit outlasted the retry budget for this request."""


class ToolError(JarvisError):
    """A tool failed in a way the agent should hear about and can recover from."""


class SafetyError(JarvisError):
    """A tool call was blocked by the safety policy and must not be retried as-is."""


class SpeechError(JarvisError):
    """Audio capture, wake-word detection, or synthesis is unavailable."""


class PlatformUnsupported(JarvisError):
    """The requested capability has no adapter on this operating system."""
