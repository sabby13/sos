"""Offline tests for the loopback-only Ollama adapter."""

from __future__ import annotations

import io
import json
from threading import Event

from openjarvis.llm import ollama


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def test_lists_local_models_from_loopback(monkeypatch) -> None:
    calls = []
    payload = {"models": [{"name": "qwen2.5:7b", "size": 4_700_000_000}]}

    def fake_urlopen(request, timeout):
        calls.append((request.full_url, timeout))
        return FakeResponse(json.dumps(payload).encode())

    monkeypatch.setattr(ollama, "_urlopen", fake_urlopen)

    assert ollama.OllamaClient().list_models() == payload["models"]
    assert calls == [("http://127.0.0.1:11434/api/tags", 4)]


def test_streams_local_chat_deltas(monkeypatch) -> None:
    chunks = [
        {"message": {"content": "Hello"}, "done": False},
        {"message": {"content": " there."}, "done": True},
    ]
    data = b"".join(json.dumps(chunk).encode() + b"\n" for chunk in chunks)
    calls = []

    def fake_urlopen(request, timeout):
        calls.append((request.full_url, timeout, json.loads(request.data)))
        return FakeResponse(data)

    monkeypatch.setattr(ollama, "_urlopen", fake_urlopen)
    deltas = []
    response = ollama.OllamaClient().stream_chat(
        "qwen2.5:7b",
        [{"role": "user", "content": "Hi"}],
        Event(),
        deltas.append,
    )

    assert response == "Hello there."
    assert deltas == ["Hello", " there."]
    assert calls[0][0] == "http://127.0.0.1:11434/api/chat"
    assert calls[0][2]["stream"] is True
    assert calls[0][2]["model"] == "qwen2.5:7b"


def test_stops_consuming_stream_when_cancelled(monkeypatch) -> None:
    data = b'{"message":{"content":"first"},"done":false}\n'
    data += b'{"message":{"content":"second"},"done":true}\n'
    monkeypatch.setattr(ollama, "_urlopen", lambda *_args, **_kwargs: FakeResponse(data))
    cancellation = Event()
    deltas = []

    def cancel_after_first(delta):
        deltas.append(delta)
        cancellation.set()

    response = ollama.OllamaClient().stream_chat(
        "qwen2.5:7b",
        [{"role": "user", "content": "Hi"}],
        cancellation,
        cancel_after_first,
    )

    assert response == "first"
    assert deltas == ["first"]
