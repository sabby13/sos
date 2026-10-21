"""Provider request retries, exercised with mocked HTTP responses."""

from __future__ import annotations

from types import SimpleNamespace

import openai
import pytest

from openjarvis.errors import ProviderError
from openjarvis.llm.openai_compatible import OpenAICompatibleProvider


def build_provider(
    *,
    max_retries: int = 3,
    max_wait: float = 8.0,
    fallback_model: str | None = None,
    audio_fallback_model: str | None = None,
):
    return OpenAICompatibleProvider(
        name="groq",
        api_key="test-key",
        model="test-model",
        max_retries=max_retries,
        rate_limit_max_wait_s=max_wait,
        fallback_model=fallback_model,
        audio_fallback_model=audio_fallback_model,
    )


def chat_response(content: str) -> SimpleNamespace:
    message = SimpleNamespace(
        content=content,
        reasoning=None,
        reasoning_content=None,
        tool_calls=[],
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="stop")],
        usage=None,
    )


class FakeCompletions:
    """Stand-in for ``client.chat.completions`` that records the requested model."""

    def __init__(self, outcomes) -> None:
        self.outcomes = outcomes
        self.models: list[str] = []

    def create(self, **payload):
        self.models.append(payload["model"])
        result = next(self.outcomes)
        if isinstance(result, Exception):
            raise result
        return result


class FakeTranscriptions:
    """Stand-in for ``client.audio.transcriptions`` that records the requested model."""

    def __init__(self, outcomes) -> None:
        self.outcomes = outcomes
        self.models: list[str] = []

    def create(self, **kwargs):
        self.models.append(kwargs["model"])
        result = next(self.outcomes)
        if isinstance(result, Exception):
            raise result
        return result


def test_completion_separates_provider_reasoning_from_assistant_content(monkeypatch):
    provider = build_provider()
    message = SimpleNamespace(
        content="The task is complete.",
        reasoning="Private analysis and self-correction.",
        reasoning_content=None,
        tool_calls=[],
    )
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="stop")],
        usage=None,
    )
    monkeypatch.setattr(provider, "_request", lambda _call: response)

    completion = provider.complete([], tools=[])

    assert completion.message.content == "The task is complete."
    assert completion.reasoning == "Private analysis and self-correction."
    assert completion.message.to_wire()["content"] == "The task is complete."


def rate_limit_error(headers: dict[str, str] | None = None) -> openai.RateLimitError:
    response = SimpleNamespace(
        status_code=429,
        headers={key.lower(): value for key, value in (headers or {}).items()},
        request=SimpleNamespace(),
    )
    return openai.RateLimitError(
        "rate limited",
        response=response,
        body={"error": {"message": "rate limited"}},
    )


def test_429_respects_retry_after_and_recovers(monkeypatch):
    provider = build_provider()
    sleeps: list[float] = []
    monkeypatch.setattr("openjarvis.llm.openai_compatible.time.sleep", sleeps.append)
    responses = iter([rate_limit_error({"Retry-After": "2.5"}), "ok"])
    calls = 0

    def call():
        nonlocal calls
        calls += 1
        result = next(responses)
        if isinstance(result, Exception):
            raise result
        return result

    assert provider._request(call) == "ok"
    assert calls == 2
    assert sleeps == [2.5]


def test_groq_reset_headers_are_respected(monkeypatch):
    provider = build_provider()
    sleeps: list[float] = []
    monkeypatch.setattr("openjarvis.llm.openai_compatible.time.sleep", sleeps.append)
    responses = iter(
        [
            rate_limit_error(
                {
                    "retry-after": "2",
                    "x-ratelimit-reset-tokens": "3s",
                    "x-ratelimit-reset-requests": "5s",
                }
            ),
            "ok",
        ]
    )

    def call():
        result = next(responses)
        if isinstance(result, Exception):
            raise result
        return result

    assert provider._request(call) == "ok"
    assert sleeps == [5.0]


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Retry-After": "not-a-duration", "x-ratelimit-reset-tokens": "unknown"},
    ],
)
def test_429_without_usable_timing_uses_jittered_backoff(monkeypatch, headers):
    provider = build_provider()
    sleeps: list[float] = []
    monkeypatch.setattr("openjarvis.llm.openai_compatible.time.sleep", sleeps.append)
    monkeypatch.setattr("openjarvis.llm.openai_compatible.random.uniform", lambda _a, _b: 0.75)
    responses = iter([rate_limit_error(headers), "ok"])

    def call():
        result = next(responses)
        if isinstance(result, Exception):
            raise result
        return result

    assert provider._request(call) == "ok"
    assert sleeps == [0.75]


def test_repeated_429_uses_exponential_backoff_with_jitter(monkeypatch):
    provider = build_provider()
    sleeps: list[float] = []
    monkeypatch.setattr("openjarvis.llm.openai_compatible.time.sleep", sleeps.append)
    monkeypatch.setattr(
        "openjarvis.llm.openai_compatible.random.uniform",
        lambda low, high: high * 0.75,
    )
    responses = iter([rate_limit_error(), rate_limit_error(), "ok"])

    def call():
        result = next(responses)
        if isinstance(result, Exception):
            raise result
        return result

    assert provider._request(call) == "ok"
    assert sleeps == [0.75, 1.5]


def test_rate_limit_wait_over_budget_fails_without_sleeping(monkeypatch):
    provider = build_provider(max_wait=2.0)
    sleeps: list[float] = []
    error = rate_limit_error({"Retry-After": "5"})
    monkeypatch.setattr("openjarvis.llm.openai_compatible.time.sleep", sleeps.append)

    def call():
        raise error

    with pytest.raises(ProviderError, match=r"exceeding the configured 2\.0s total retry budget"):
        provider._request(call)

    assert sleeps == []


def test_cumulative_rate_limit_wait_cannot_exceed_budget(monkeypatch):
    provider = build_provider(max_wait=5.0)
    sleeps: list[float] = []
    monkeypatch.setattr("openjarvis.llm.openai_compatible.time.sleep", sleeps.append)
    responses = iter(
        [
            rate_limit_error({"Retry-After": "3"}),
            rate_limit_error({"Retry-After": "3"}),
        ]
    )

    def call():
        raise next(responses)

    with pytest.raises(ProviderError, match=r"3\.0s already waited"):
        provider._request(call)

    assert sleeps == [3.0]


def test_invalid_retry_after_fails_clearly_when_retry_budget_is_zero(monkeypatch):
    provider = build_provider(max_wait=0)
    error = rate_limit_error({"Retry-After": "tomorrow"})
    monkeypatch.setattr(
        "openjarvis.llm.openai_compatible.random.uniform", lambda _a, _b: 0.75
    )

    def call():
        raise error

    with pytest.raises(ProviderError, match="No further retry was attempted"):
        provider._request(call)


def test_rate_limit_over_budget_continues_on_fallback_model(monkeypatch):
    provider = build_provider(max_wait=2.0, fallback_model="fallback-model")
    sleeps: list[float] = []
    monkeypatch.setattr("openjarvis.llm.openai_compatible.time.sleep", sleeps.append)
    models_used: list[str] = []
    responses = iter([rate_limit_error({"Retry-After": "50"}), "ok"])

    def call_for_model(model_name: str):
        def call():
            models_used.append(model_name)
            result = next(responses)
            if isinstance(result, Exception):
                raise result
            return result

        return call

    assert (
        provider._request_with_model_fallback(
            "test-model", call_for_model, fallback_model="fallback-model"
        )
        == "ok"
    )
    assert models_used == ["test-model", "fallback-model"]
    assert sleeps == []


def test_fallback_model_also_rate_limited_reports_the_primary_limit(monkeypatch):
    provider = build_provider(max_wait=2.0, fallback_model="fallback-model")
    sleeps: list[float] = []
    monkeypatch.setattr("openjarvis.llm.openai_compatible.time.sleep", sleeps.append)
    models_used: list[str] = []
    responses = iter(
        [rate_limit_error({"Retry-After": "50"}), rate_limit_error({"Retry-After": "50"})]
    )

    def call_for_model(model_name: str):
        def call():
            models_used.append(model_name)
            raise next(responses)

        return call

    with pytest.raises(ProviderError, match=r"exceeding the configured 2\.0s total retry budget"):
        provider._request_with_model_fallback(
            "test-model", call_for_model, fallback_model="fallback-model"
        )

    assert models_used == ["test-model", "fallback-model"]
    assert sleeps == []


def test_unavailable_fallback_model_reports_the_primary_rate_limit(monkeypatch):
    provider = build_provider(max_wait=2.0, fallback_model="fallback-model")
    models_used: list[str] = []

    def call_for_model(model_name: str):
        def call():
            models_used.append(model_name)
            if model_name == "test-model":
                raise rate_limit_error({"Retry-After": "50"})
            raise openai.NotFoundError(
                "model not found",
                response=SimpleNamespace(status_code=404, headers={}, request=SimpleNamespace()),
                body={"error": {"message": "model not found"}},
            )

        return call

    with pytest.raises(ProviderError, match=r"exceeding the configured 2\.0s total retry budget"):
        provider._request_with_model_fallback(
            "test-model", call_for_model, fallback_model="fallback-model"
        )

    assert models_used == ["test-model", "fallback-model"]


def test_complete_runs_on_the_fallback_when_the_primary_is_capped():
    provider = build_provider(max_wait=2.0, fallback_model="fallback-model")
    completions = FakeCompletions(
        iter([rate_limit_error({"Retry-After": "50"}), chat_response("Recovered")])
    )
    provider._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

    completion = provider.complete([], tools=[])

    assert completions.models == ["test-model", "fallback-model"]
    assert completion.message.content == "Recovered"


def test_complete_without_fallback_keeps_the_budget_error():
    provider = build_provider(max_wait=2.0)
    completions = FakeCompletions(iter([rate_limit_error({"Retry-After": "50"})]))
    provider._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

    with pytest.raises(ProviderError, match="No further retry was attempted"):
        provider.complete([], tools=[])

    assert completions.models == ["test-model"]


def test_transcribe_falls_back_to_the_audio_bucket():
    provider = build_provider(audio_fallback_model="whisper-large-v3")
    transcriptions = FakeTranscriptions(
        iter([rate_limit_error({"Retry-After": "50"}), "transcribed text"])
    )
    provider._client = SimpleNamespace(audio=SimpleNamespace(transcriptions=transcriptions))

    assert provider.transcribe(b"audio", model="whisper-large-v3-turbo") == "transcribed text"
    assert transcriptions.models == ["whisper-large-v3-turbo", "whisper-large-v3"]
