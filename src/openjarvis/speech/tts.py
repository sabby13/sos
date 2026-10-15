"""Text to speech.

Four engines behind one ``speak(text)`` call:

    edge   - Microsoft Edge's neural voices. Free, no API key, genuinely good. Needs a
             network round trip, so it is the default but not the fallback.
    system - pyttsx3 over SAPI5 / NSSpeech / espeak. Robotic, but works offline and
             always answers, which is why it is the fallback.
    openai - gpt-4o-mini-tts, if you are already paying for OpenAI.
    groq   - playai-tts.

Speaking blocks by default. That is deliberate: if the reply is still playing when the
microphone reopens, Jarvis transcribes itself and answers its own question.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import tempfile
from pathlib import Path
from typing import Protocol

from ..config import DEFAULT_TTS_VOICES, Settings
from ..console import get_logger
from ..diagnostics import sanitize_diagnostic
from ..errors import SpeechError
from ..platforms import PlatformAdapter, get_adapter

log = get_logger("speech.tts")


class Speaker(Protocol):
    name: str

    def speak(self, text: str, *, block: bool = True) -> None: ...

    def stop(self) -> None: ...


class NullSpeaker:
    """Prints instead of speaking. Used by text mode and ``--no-speech``."""

    name = "none"

    def speak(self, text: str, *, block: bool = True) -> None:
        return None

    def stop(self) -> None:
        return None


class EdgeSpeaker:
    """Microsoft Edge neural voices via the edge-tts package."""

    name = "edge"

    def __init__(self, voice: str, rate_pct: int = 0, platform: PlatformAdapter | None = None):
        try:
            import edge_tts
        except ImportError as exc:
            raise SpeechError(
                f"edge-tts is not installed: {exc}",
                hint='Install the voice extras: pip install -e ".[voice]"',
            ) from exc
        self._edge = edge_tts
        self.voice = voice or DEFAULT_TTS_VOICES["edge"]
        # edge-tts wants a signed percentage string.
        self.rate = f"{rate_pct:+d}%"
        self.platform = platform or get_adapter()

    def speak(self, text: str, *, block: bool = True) -> None:
        if not text.strip():
            return
        path = Path(tempfile.gettempdir()) / f"openjarvis_tts_{os.getpid()}.mp3"
        try:
            asyncio.run(self._synthesize(text, path))
        except Exception as exc:
            path.unlink(missing_ok=True)
            raise SpeechError(f"edge-tts failed: {exc}") from exc

        try:
            self.platform.play_audio_file(path, block=block)
        except Exception as exc:
            raise SpeechError(f"edge-tts playback failed: {exc}") from exc
        finally:
            if block:
                path.unlink(missing_ok=True)

    async def _synthesize(self, text: str, path: Path) -> None:
        communicate = self._edge.Communicate(text, self.voice, rate=self.rate)
        await communicate.save(str(path))

    def stop(self) -> None:
        self.platform.stop_audio()


class SystemSpeaker:
    """Offline synthesis through pyttsx3."""

    name = "system"

    def __init__(self, voice: str = "", rate_wpm: int = 190) -> None:
        try:
            import pyttsx3
        except ImportError as exc:
            raise SpeechError(
                f"pyttsx3 is not installed: {exc}",
                hint='Install the voice extras: pip install -e ".[voice]"',
            ) from exc
        self._pyttsx3 = pyttsx3
        self._voice = voice
        self._rate = rate_wpm
        try:
            self._engine = self._make_engine()
        except Exception as exc:
            raise SpeechError(f"system TTS is unavailable ({type(exc).__name__})") from exc

    def _make_engine(self):  # type: ignore[no-untyped-def]
        engine = self._pyttsx3.init()
        engine.setProperty("rate", self._rate)
        if self._voice:
            for candidate in engine.getProperty("voices"):
                if self._voice.lower() in (candidate.name or "").lower():
                    engine.setProperty("voice", candidate.id)
                    break
        return engine

    def speak(self, text: str, *, block: bool = True) -> None:
        if not text.strip():
            return
        try:
            self._engine.say(text)
            self._engine.runAndWait()
        except RuntimeError:
            # pyttsx3's run loop can get wedged if a previous call was interrupted.
            log.debug("pyttsx3 run loop wedged, reinitialising")
            self._engine = self._make_engine()
            self._engine.say(text)
            self._engine.runAndWait()

    def stop(self) -> None:
        with contextlib.suppress(Exception):
            self._engine.stop()


class ApiSpeaker:
    """OpenAI- or Groq-hosted TTS."""

    def __init__(self, settings: Settings, provider: str) -> None:
        from ..llm.openai_compatible import build_audio_provider

        self.name = provider
        self.settings = settings
        self._client = build_audio_provider(settings, provider)
        self.model = settings.speech.resolved_tts_model()
        self.voice = settings.speech.resolved_tts_voice(provider)
        self.platform = get_adapter()

    def speak(self, text: str, *, block: bool = True) -> None:
        if not text.strip():
            return
        try:
            audio = self._client.synthesize(
                text, model=self.model, voice=self.voice, response_format="mp3"
            )
        except Exception as exc:
            raise SpeechError(f"{self.name} TTS failed: {exc}") from exc

        path = Path(tempfile.gettempdir()) / f"openjarvis_tts_{os.getpid()}.mp3"
        path.write_bytes(audio)
        try:
            self.platform.play_audio_file(path, block=block)
        finally:
            if block:
                path.unlink(missing_ok=True)

    def stop(self) -> None:
        self.platform.stop_audio()


class FallbackSpeaker:
    """Wraps a primary engine and swaps to a backup after it fails.

    Voice mode dies badly if the network drops mid-session, so the first failure is
    logged, spoken through the backup, and remembered - we do not retry the dead engine
    on every subsequent sentence.
    """

    def __init__(self, primary: Speaker, backup: Speaker) -> None:
        self._primary = primary
        self._backup = backup
        self._failed = False
        self._fallback_notice_pending = False

    @property
    def name(self) -> str:
        return self._backup.name if self._failed else self._primary.name

    def speak(self, text: str, *, block: bool = True) -> None:
        if not self._failed:
            try:
                self._primary.speak(text, block=block)
                return
            except SpeechError as exc:
                log.warning(
                    "%s TTS failed, switching to %s: %s",
                    self._primary.name,
                    self._backup.name,
                    sanitize_diagnostic(str(exc)),
                )
                self._failed = True
                self._fallback_notice_pending = True
        self._backup.speak(text, block=block)

    def consume_fallback_notice(self) -> bool:
        pending = self._fallback_notice_pending
        self._fallback_notice_pending = False
        return pending

    def stop(self) -> None:
        self._primary.stop()
        self._backup.stop()


def build_speaker(settings: Settings) -> Speaker:
    """Construct the configured engine, wrapped in its fallback where one is useful."""
    engine = settings.speech.tts_engine
    if engine == "none":
        return NullSpeaker()

    primary = _make(engine, settings)
    backup_name = settings.speech.tts_fallback
    if backup_name in (engine, "none"):
        return primary
    try:
        return FallbackSpeaker(primary, _make(backup_name, settings))
    except SpeechError as exc:
        log.debug("no TTS fallback available: %s", exc)
        return primary


def _make(engine: str, settings: Settings) -> Speaker:
    speech = settings.speech
    if engine == "edge":
        return EdgeSpeaker(speech.resolved_tts_voice("edge"), speech.tts_rate_pct)
    if engine == "system":
        return SystemSpeaker(speech.resolved_tts_voice("system"), speech.tts_rate_wpm)
    if engine in ("openai", "groq"):
        return ApiSpeaker(settings, engine)
    return NullSpeaker()


async def list_edge_voices(prefix: str = "en-") -> list[tuple[str, str]]:
    """(short name, gender) for the Edge voices matching ``prefix``."""
    import edge_tts

    voices = await edge_tts.list_voices()
    return sorted(
        (v["ShortName"], v.get("Gender", ""))
        for v in voices
        if v["ShortName"].startswith(prefix)
    )
