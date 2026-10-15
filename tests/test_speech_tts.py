from __future__ import annotations

import logging
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from openjarvis.errors import SpeechError
from openjarvis.speech import tts


class WritingCommunicate:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    async def save(self, path: str) -> None:
        Path(path).write_bytes(b"partial audio")
        if self.fail:
            raise RuntimeError(
                "HTTP 403 wss://speech.example/?TrustedClientToken=connection-secret"
            )


class FakeSpeaker:
    def __init__(self, name: str, error: Exception | None = None) -> None:
        self.name = name
        self.error = error
        self.spoken: list[str] = []

    def speak(self, text: str, *, block: bool = True) -> None:
        if self.error is not None:
            raise self.error
        self.spoken.append(text)

    def stop(self) -> None:
        pass


def make_edge_speaker(communicate: WritingCommunicate, platform) -> tts.EdgeSpeaker:
    speaker = tts.EdgeSpeaker.__new__(tts.EdgeSpeaker)
    speaker._edge = SimpleNamespace(
        Communicate=lambda *args, **kwargs: communicate,
    )
    speaker.voice = "en-GB-SoniaNeural"
    speaker.rate = "+0%"
    speaker.platform = platform
    return speaker


def test_edge_synthesis_failure_cleans_partial_audio_and_falls_back(
    tmp_path, monkeypatch, caplog
) -> None:
    monkeypatch.setattr(tts.tempfile, "gettempdir", lambda: str(tmp_path))
    path = tmp_path / f"openjarvis_tts_{os.getpid()}.mp3"
    primary = make_edge_speaker(WritingCommunicate(fail=True), platform=None)
    fallback = FakeSpeaker("system")
    speaker = tts.FallbackSpeaker(primary, fallback)

    with caplog.at_level(logging.WARNING, logger="openjarvis.speech.tts"):
        speaker.speak("Hello Sienna.")
        speaker.speak("How can I help?")

    assert not path.exists()
    assert fallback.spoken == ["Hello Sienna.", "How can I help?"]
    assert speaker.name == "system"
    assert "speech.example" not in caplog.text
    assert "TrustedClientToken" not in caplog.text
    assert "connection-secret" not in caplog.text


def test_edge_playback_failure_is_wrapped_and_audio_is_removed(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(tts.tempfile, "gettempdir", lambda: str(tmp_path))
    path = tmp_path / f"openjarvis_tts_{os.getpid()}.mp3"

    class BrokenPlatform:
        def play_audio_file(self, path: Path, *, block: bool) -> None:
            raise OSError("MCI playback failed")

    speaker = make_edge_speaker(WritingCommunicate(), BrokenPlatform())

    with pytest.raises(SpeechError, match="playback failed"):
        speaker.speak("Hello Sienna.")

    assert not path.exists()


def test_fallback_does_not_retry_failed_primary_and_does_not_log_raw_error(caplog) -> None:
    primary = FakeSpeaker(
        "edge",
        SpeechError("HTTP 403 from wss://speech.example/?token=secret"),
    )
    fallback = FakeSpeaker("system")
    speaker = tts.FallbackSpeaker(primary, fallback)

    with caplog.at_level(logging.WARNING, logger="openjarvis.speech.tts"):
        speaker.speak("First.")
        speaker.speak("Second.")

    assert fallback.spoken == ["First.", "Second."]
    assert "speech.example" not in caplog.text
    assert "secret" not in caplog.text
