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
    assert events[0] == {
        "type": "ready",
        "category": "status",
        "backend": "openjarvis",
    }
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
            '{"ttsEngine":"edge","ttsVoice":"en-GB-SoniaNeural","ttsFallback":"none"}}\n'
            '{"type":"shutdown"}\n'
        )
    )

    events = [json.loads(line) for line in output.getvalue().splitlines()]
    settings = next(event["settings"] for event in events if event["type"] == "settings")
    assert settings["ttsEngine"] == "edge"
    assert settings["ttsVoice"] == "en-GB-SoniaNeural"
    assert bridge.settings.speech.tts_fallback == "none"


def test_bridge_classifies_conversation_and_backend_events() -> None:
    output = io.StringIO()
    bridge = DesktopBridge(output=output)
    for event_type in ("transcript", "reply", "state", "tool-result", "error"):
        bridge.emit(event_type)

    events = [json.loads(line) for line in output.getvalue().splitlines()]
    assert [event["category"] for event in events] == [
        "user_message",
        "assistant_message",
        "status",
        "tool_result",
        "error",
    ]


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


def test_voice_speech_failure_does_not_fail_completed_request(caplog) -> None:
    class SuccessfulAgent:
        calls = 0

        def ask(self, text: str, *, cancel_event: Event) -> str:
            self.calls += 1
            assert text == "open calculator"
            return "Calculator opened."

    class BrokenSpeaker:
        def speak(self, text: str, *, block: bool) -> None:
            raise RuntimeError("HTTP 403 from https://provider.example/?token=secret")

    output = io.StringIO()
    bridge = DesktopBridge(output=output)
    agent = SuccessfulAgent()
    bridge.settings.speech.tts_engine = "edge"
    bridge._capture_voice = lambda: "open calculator"
    bridge._get_agent = lambda: agent
    bridge._get_speaker = lambda: BrokenSpeaker()
    bridge._turn_lock.acquire()
    bridge._active_request_id = "voice-1"

    bridge._handle_turn("voice", "voice-1", "")

    events = [json.loads(line) for line in output.getvalue().splitlines()]
    speech_error = next(event for event in events if event["type"] == "speech-error")
    reply = next(event for event in events if event["type"] == "reply")
    final_state = [event for event in events if event["type"] == "state"][-1]
    assert agent.calls == 1
    assert reply == {
        "type": "reply",
        "category": "assistant_message",
        "requestId": "voice-1",
        "text": "Calculator opened.",
    }
    assert speech_error == {
        "type": "speech-error",
        "category": "error",
        "requestId": "voice-1",
        "message": "Voice playback unavailable.",
    }
    assert final_state["state"] == "idle"
    assert "provider.example" not in caplog.text
    assert "secret" not in caplog.text


def test_successful_voice_turn_keeps_message_categories() -> None:
    class SuccessfulAgent:
        def ask(self, text: str, *, cancel_event: Event) -> str:
            assert text == "what time is it"
            return "It is noon."

    class SuccessfulSpeaker:
        def __init__(self) -> None:
            self.spoken: list[str] = []

        def speak(self, text: str, *, block: bool) -> None:
            self.spoken.append(text)

    output = io.StringIO()
    bridge = DesktopBridge(output=output)
    speaker = SuccessfulSpeaker()
    bridge.settings.speech.tts_engine = "edge"
    bridge._capture_voice = lambda: "what time is it"
    bridge._get_agent = lambda: SuccessfulAgent()
    bridge._get_speaker = lambda: speaker
    bridge._turn_lock.acquire()
    bridge._active_request_id = "voice-success"

    bridge._handle_turn("voice", "voice-success", "")

    events = [json.loads(line) for line in output.getvalue().splitlines()]
    transcript = next(event for event in events if event["type"] == "transcript")
    reply = next(event for event in events if event["type"] == "reply")
    assert transcript["category"] == "user_message"
    assert reply["category"] == "assistant_message"
    assert speaker.spoken == ["It is noon."]
    assert not any(event["type"] == "speech-error" for event in events)


def test_bridge_validates_local_chat_context() -> None:
    assert DesktopBridge._valid_ollama_chat(
        {
            "model": "qwen2.5:7b",
            "messages": [{"role": "user", "content": "Hello"}],
        }
    )
    assert not DesktopBridge._valid_ollama_chat(
        {
            "model": "qwen2.5:7b",
            "messages": [{"role": "tool", "content": "run this command"}],
        }
    )
    assert not DesktopBridge._valid_ollama_chat(
        {
            "model": "qwen2.5:7b",
            "messages": [{"role": [], "content": "malformed role"}],
        }
    )


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
