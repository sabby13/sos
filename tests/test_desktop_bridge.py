"""Tests for the line-oriented desktop IPC bridge."""

from __future__ import annotations

import io
import json

from openjarvis.desktop_bridge import DesktopBridge


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
