"""Speech to text.

Both providers take the same call: a WAV file in, a string out. Groq's
``whisper-large-v3-turbo`` is the default because it typically returns in well under a
second, and latency is what makes a voice assistant feel alive or dead.
"""

from __future__ import annotations

import re
import time

from ..agent.prompts import TRANSCRIPTION_HINT
from ..config import Settings
from ..console import get_logger
from ..errors import ProviderError
from ..llm.openai_compatible import build_audio_provider

log = get_logger("speech.stt")

# Whisper's habit on silence or noise: it emits a stock phrase rather than nothing.
_HALLUCINATIONS = {
    "you", "thank you", "thanks for watching", "thank you for watching",
    "please subscribe", "bye", ".", "..", "...", "[blank_audio]", "[silence]",
    "subtitles by the amara.org community", "amara.org",
}


class Transcriber:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.provider_name = settings.speech.stt_provider
        self.model = settings.speech.resolved_stt_model()
        self._client = build_audio_provider(settings, self.provider_name)

    def transcribe(self, wav_bytes: bytes) -> str:
        """Return the recognised text, or an empty string for silence/noise."""
        started = time.monotonic()
        try:
            raw = self._client.transcribe(
                wav_bytes,
                filename="utterance.wav",
                model=self.model,
                language=self.settings.speech.language,
                prompt=TRANSCRIPTION_HINT,
            )
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(f"Transcription failed: {exc}") from exc

        text = clean_transcript(raw)
        log.debug("transcribed %.2fs -> %r", time.monotonic() - started, text)
        return text


def clean_transcript(raw: str) -> str:
    """Strip Whisper's artefacts and reject its known silence hallucinations."""
    text = (raw or "").strip()
    # Whisper sometimes emits bracketed non-speech annotations.
    text = re.sub(r"\[(?:BLANK_AUDIO|SILENCE|MUSIC|NOISE|INAUDIBLE)\]", "", text, flags=re.I)
    text = re.sub(r"\s+", " ", text).strip()

    if not text:
        return ""
    if text.lower().strip(" .!?") in _HALLUCINATIONS:
        log.debug("discarding likely hallucination: %r", text)
        return ""
    # A single character is never a real command.
    if len(text.strip(" .!?,")) < 2:
        return ""
    return text


def strip_wake_word(text: str, wake_word: str) -> str:
    """Remove a leading "hey jarvis" that got captured along with the command."""
    words = wake_word.replace("_", " ").strip()
    if not words:
        return text
    pattern = rf"^\s*(?:hey\s+)?{re.escape(words.removeprefix('hey ').strip())}\s*[,.!]*\s*"
    return re.sub(pattern, "", text, flags=re.I).strip() or text
