"""Small loopback-only client for Ollama's local HTTP API."""

from __future__ import annotations

import json
from collections.abc import Callable
from threading import Event
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

OLLAMA_BASE_URL = "http://127.0.0.1:11434"


def _urlopen(request: Request, timeout: int):
    return build_opener(ProxyHandler({})).open(request, timeout=timeout)


class OllamaError(RuntimeError):
    """An unavailable Ollama service, model, or invalid local API response."""


class OllamaClient:
    """Use Ollama locally without allowing a configurable remote endpoint."""

    def list_models(self) -> list[dict[str, Any]]:
        payload = self._request_json("/api/tags")
        models = payload.get("models")
        if not isinstance(models, list):
            raise OllamaError("Ollama returned an invalid model list.")
        return [
            model
            for model in models
            if isinstance(model, dict)
            and isinstance(model.get("name"), str)
            and isinstance(model.get("size"), int)
        ]

    def stream_chat(
        self,
        model: str,
        messages: list[dict[str, str]],
        cancel_event: Event,
        on_delta: Callable[[str], None],
    ) -> str:
        body = json.dumps(
            {"model": model, "messages": messages, "stream": True},
            ensure_ascii=False,
        ).encode("utf-8")
        request = Request(
            f"{OLLAMA_BASE_URL}/api/chat",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        response_text: list[str] = []
        try:
            with _urlopen(request, timeout=180) as response:
                for line in response:
                    if cancel_event.is_set():
                        break
                    if not line.strip():
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise OllamaError("Ollama returned an invalid chat stream.") from exc
                    if not isinstance(event, dict):
                        raise OllamaError("Ollama returned an invalid chat stream.")
                    if error := event.get("error"):
                        raise OllamaError(str(error))
                    message = event.get("message", {})
                    delta = message.get("content") if isinstance(message, dict) else None
                    if isinstance(delta, str) and delta:
                        response_text.append(delta)
                        on_delta(delta)
                    if event.get("done") is True:
                        break
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise OllamaError(f"Could not reach Ollama at 127.0.0.1:11434: {reason}") from exc
        return "".join(response_text)

    @staticmethod
    def _request_json(path: str) -> dict[str, Any]:
        request = Request(f"{OLLAMA_BASE_URL}{path}", headers={"Accept": "application/json"})
        try:
            with _urlopen(request, timeout=4) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise OllamaError(
                f"Could not reach Ollama at 127.0.0.1:11434: {reason}"
            ) from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OllamaError("Ollama returned invalid JSON.") from exc
        if not isinstance(payload, dict):
            raise OllamaError("Ollama returned an invalid response.")
        return payload
