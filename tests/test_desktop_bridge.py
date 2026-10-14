"""Tests for the line-oriented desktop IPC bridge."""

from __future__ import annotations

import io
import json
from threading import Event

from openjarvis.desktop_bridge import DesktopBridge
from openjarvis.speech.audio import record_utterance


def test_bridge_reports_safe_settings_and_accepts_shutdown() -> None:
    output = io.StringIO()
    bridge = DesktopBridge(output=output)
    bridge.run(
        io.StringIO(
            '{"id":"settings","type":"get-settings"}\n'
            '{"type":"shutdown"}\n'
        )
    )

    events = [json.loads(line) for line in output.getvalue().splitlines()]
    assert events[0] == {"type": "ready", "backend": "openjarvis"}
    settings = next(event["settings"] for event in events if event["type"] == "settings")
    assert settings["provider"] in {"groq", "openai"}
    assert isinstance(settings["apiKeyConfigured"], bool)
    assert "apiKey" not in settings


def test_bridge_configures_supported_tts_without_starting_audio() -> None:
    output = io.StringIO()
    bridge = DesktopBridge(output=output)
    bridge.run(
        io.StringIO(
            '{"id":"configure","type":"configure","settings":{"ttsEngine":"system"}}\n'
            '{"type":"shutdown"}\n'
        )
    )

    events = [json.loads(line) for line in output.getvalue().splitlines()]
    settings = next(event["settings"] for event in events if event["type"] == "settings")
    assert settings["ttsEngine"] == "system"


def test_bridge_configures_sonia_edge_voice() -> None:
    output = io.StringIO()
    bridge = DesktopBridge(output=output)
    bridge.run(
        io.StringIO(
            '{"id":"configure","type":"configure","settings":'
            '{"ttsEngine":"edge","ttsVoice":"en-GB-SoniaNeural"}}\n'
            '{"type":"shutdown"}\n'
        )
    )

    events = [json.loads(line) for line in output.getvalue().splitlines()]
    settings = next(event["settings"] for event in events if event["type"] == "settings")
    assert settings["ttsEngine"] == "edge"
    assert settings["ttsVoice"] == "en-GB-SoniaNeural"


def test_bridge_rejects_malformed_text_request_without_calling_provider() -> None:
    output = io.StringIO()
    bridge = DesktopBridge(output=output)
    bridge.run(
        io.StringIO(
            '{"id":"empty","type":"ask","text":"  "}\n'
            '{"type":"shutdown"}\n'
        )
    )

    events = [json.loads(line) for line in output.getvalue().splitlines()]
    assert any(event.get("requestId") == "empty" and event["type"] == "error" for event in events)


def test_bridge_cancels_only_the_matching_active_request() -> None:
    output = io.StringIO()
    bridge = DesktopBridge(output=output)
    bridge._active_request_id = "active"

    bridge._cancel_turn("stale")
    assert not bridge._turn_cancel.is_set()

    bridge._cancel_turn("active")
    assert bridge._turn_cancel.is_set()
    events = [json.loads(line) for line in output.getvalue().splitlines()]
    assert [event["type"] for event in events] == ["cancel-error", "state"]
    assert events[-1]["state"] == "cancelling"


def test_audio_capture_returns_promptly_when_cancelled() -> None:
    class UnreadMicrophone:
        sample_rate = 16_000
        block_size = 1_024

        def preroll(self) -> bytes:
            return b""

        def read(self, timeout: float = 1.0) -> bytes | None:
            raise AssertionError("cancelled capture must not wait for microphone audio")

    cancellation = Event()
    cancellation.set()
    assert record_utterance(UnreadMicrophone(), cancel_event=cancellation) is None
