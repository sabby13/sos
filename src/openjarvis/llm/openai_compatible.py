"""One client for both Groq and OpenAI.

Groq exposes an OpenAI-compatible endpoint, so a single implementation parameterised by
``base_url`` + ``api_key`` + ``model`` serves both. That covers chat with tool calling,
speech-to-text, and text-to-speech.
"""

from __future__ import annotations

import io
import time
from typing import Any

import openai

from ..config import DEFAULT_LLM_MODELS, Settings
from ..console import get_logger
from ..errors import ProviderError
from .base import Completion, Message, ToolCall

log = get_logger("llm")

# Transient failures worth retrying with backoff.
_RETRYABLE = (
    openai.APIConnectionError,
    openai.APITimeoutError,
    openai.RateLimitError,
    openai.InternalServerError,
)


class OpenAICompatibleProvider:
    """Chat + audio against any OpenAI-wire-format endpoint."""

    def __init__(
        self,
        *,
        name: str,
        api_key: str,
        model: str,
        base_url: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 1024,
        timeout_s: float = 60.0,
        max_retries: int = 3,
    ) -> None:
        self.name = name
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.max_retries = max_retries
        # We do our own retry loop so we can log between attempts.
        self._client = openai.OpenAI(
            api_key=api_key, base_url=base_url, timeout=timeout_s, max_retries=0
        )

    # ------------------------------------------------------------------ chat

    def complete(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
    ) -> Completion:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [m.to_wire() for m in messages],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        response = self._request(lambda: self._client.chat.completions.create(**payload))

        if not response.choices:
            raise ProviderError(f"{self.name} returned no choices.")
        choice = response.choices[0]
        raw = choice.message

        tool_calls: list[ToolCall] = []
        for tc in raw.tool_calls or []:
            # Some servers emit non-function tool types; we only support functions.
            if getattr(tc, "type", "function") != "function":
                log.warning("ignoring unsupported tool call type %r", tc.type)
                continue
            tool_calls.append(
                ToolCall(id=tc.id, name=tc.function.name, arguments=tc.function.arguments or "{}")
            )

        usage = getattr(response, "usage", None)
        return Completion(
            message=Message.assistant(raw.content, tool_calls),
            finish_reason=choice.finish_reason,
            prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
        )

    # ----------------------------------------------------------------- audio

    def transcribe(
        self,
        audio: bytes,
        *,
        filename: str = "speech.wav",
        model: str | None = None,
        language: str | None = None,
        prompt: str | None = None,
    ) -> str:
        buffer = io.BytesIO(audio)
        buffer.name = filename  # the SDK infers the MIME type from this
        kwargs: dict[str, Any] = {
            "model": model or "whisper-1",
            "file": buffer,
            "response_format": "text",
        }
        if language:
            kwargs["language"] = language
        if prompt:
            # Biases the decoder toward domain words - names of apps, folders, etc.
            kwargs["prompt"] = prompt

        result = self._request(lambda: self._client.audio.transcriptions.create(**kwargs))
        # response_format="text" yields a bare string; other formats yield an object.
        text = result if isinstance(result, str) else getattr(result, "text", "")
        return text.strip()

    def synthesize(
        self,
        text: str,
        *,
        model: str,
        voice: str,
        response_format: str = "mp3",
    ) -> bytes:
        response = self._request(
            lambda: self._client.audio.speech.create(
                model=model, voice=voice, input=text, response_format=response_format
            )
        )
        return response.read()

    # -------------------------------------------------------------- internal

    def _request(self, call):  # type: ignore[no-untyped-def]
        """Run ``call`` with exponential backoff, translating SDK errors."""
        delay = 1.0
        last_exc: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                return call()
            except _RETRYABLE as exc:
                last_exc = exc
                if attempt == self.max_retries:
                    break
                log.warning(
                    "%s request failed (%s), retrying in %.1fs [%d/%d]",
                    self.name, type(exc).__name__, delay, attempt, self.max_retries,
                )
                time.sleep(delay)
                delay *= 2
            except openai.AuthenticationError as exc:
                raise ProviderError(
                    f"{self.name} rejected the API key.",
                    hint=f"Check your API key for {self.name} in .env",
                ) from exc
            except openai.NotFoundError as exc:
                raise ProviderError(
                    f"{self.name} does not have a model named {self.model!r}.",
                    hint="Run `jarvis models` to see what this provider currently offers.",
                ) from exc
            except openai.BadRequestError as exc:
                # Groq validates tool calls server-side and 400s when the model emits a
                # malformed one (llama models do this intermittently, writing the call as
                # plain text). It is stochastic, so a retry usually succeeds.
                if _is_tool_use_failure(exc) and attempt < self.max_retries:
                    last_exc = exc
                    log.warning(
                        "%s produced a malformed tool call, retrying [%d/%d]",
                        self.name, attempt, self.max_retries,
                    )
                    time.sleep(delay)
                    delay *= 2
                    continue
                if _is_tool_use_failure(exc):
                    raise ProviderError(
                        f"{self.name}/{self.model} keeps emitting malformed tool calls.",
                        hint=(
                            "This model is weak at tool use. Try --model openai/gpt-oss-120b "
                            "on Groq, or --provider openai."
                        ),
                    ) from exc
                raise ProviderError(f"{self.name} rejected the request: {exc}") from exc
        raise ProviderError(
            f"{self.name} is unreachable after {self.max_retries} attempts: {last_exc}",
            hint="Check your network connection, or try the other provider with --provider.",
        ) from last_exc

    def list_models(self) -> list[str]:
        try:
            return sorted(m.id for m in self._client.models.list().data)
        except openai.OpenAIError as exc:
            raise ProviderError(f"Could not list {self.name} models: {exc}") from exc


def _is_tool_use_failure(exc: openai.BadRequestError) -> bool:
    """Detect Groq's ``tool_use_failed`` rejection, whichever shape it arrives in."""
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and error.get("code") == "tool_use_failed":
            return True
    return "tool_use_failed" in str(exc)


def build_provider(settings: Settings, *, provider: str | None = None) -> OpenAICompatibleProvider:
    """Construct the chat provider described by ``settings``."""
    name = provider or settings.llm.provider
    # A configured model name belongs to the configured provider; if the caller asks for a
    # different one, fall back to that provider's default rather than sending a bad id.
    if name == settings.llm.provider:
        model = settings.llm.resolved_model()
    else:
        model = DEFAULT_LLM_MODELS[name]
    return OpenAICompatibleProvider(
        name=name,
        api_key=settings.api_key_for(name),
        base_url=settings.base_url_for(name),
        model=model,
        temperature=settings.llm.temperature,
        max_tokens=settings.llm.max_tokens,
        timeout_s=settings.llm.timeout_s,
    )


def build_audio_provider(settings: Settings, provider: str) -> OpenAICompatibleProvider:
    """A client aimed at a provider's audio endpoints (model is passed per call)."""
    return OpenAICompatibleProvider(
        name=provider,
        api_key=settings.api_key_for(provider),
        base_url=settings.base_url_for(provider),
        model="",
        timeout_s=settings.llm.timeout_s,
    )
