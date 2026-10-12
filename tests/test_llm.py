"""Provider request retries, exercised with mocked HTTP responses."""

from __future__ import annotations

from types import SimpleNamespace

import openai
import pytest

from openjarvis.errors import ProviderError
from openjarvis.llm.openai_compatible import OpenAICompatibleProvider


def build_provider(*, max_retries: int = 3, max_wait: float = 8.0):
    return OpenAICompatibleProvider(
        name="groq",
        api_key="test-key",
        model="test-model",
        max_retries=max_retries,
        rate_limit_max_wait_s=max_wait,
    )


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

    with pytest.raises(ProviderError, match="exceeding the configured 2.0s total retry budget"):
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

    with pytest.raises(ProviderError, match="3.0s already waited"):
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
