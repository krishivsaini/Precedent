"""The shared OpenAI-compatible client. No test here touches the network.

Groq and NVIDIA NIM are both configuration over this class, so this is where their common
behaviour is pinned: transient failures retry, permanent ones fail fast, and a spent daily
quota raises immediately rather than sleeping against a limit that will not lift. Each
provider module has its own small test for *its configuration*; the request handling is
tested once, here, so a third provider cannot quietly drift out of step with the other two.
"""

import httpx
import pytest

from precedent.adapters.llm.base import LLMUnavailable
from precedent.adapters.llm.openai_compatible import (
    DEFAULT_MAX_ATTEMPTS,
    DailyQuotaExhausted,
    OpenAICompatibleClient,
)


def ok_body(text='{"ok": true}'):
    return {
        "choices": [{"message": {"content": text}}],
        "usage": {"prompt_tokens": 2083, "completion_tokens": 240},
    }


def client_over(responses, **kwargs):
    queue = list(responses)
    slept: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        status, payload = queue.pop(0)
        if isinstance(payload, dict):
            return httpx.Response(status, json=payload)
        return httpx.Response(status, text=payload)

    transport = httpx.MockTransport(handler)
    client = OpenAICompatibleClient(
        base_url="https://example.test/v1", api_key="test-key",
        model="test-model", provider="TestProvider",
        sleep=slept.append, **kwargs,
    )
    httpx.post = lambda url, **kw: httpx.Client(transport=transport).post(
        url, **{k: v for k, v in kw.items() if k != "timeout"}
    )
    client.slept = slept
    return client


@pytest.fixture(autouse=True)
def restore_httpx():
    original = httpx.post
    yield
    httpx.post = original


class TestKeyHandling:
    def test_a_missing_key_escalates_rather_than_crashing(self):
        # Unavailability, not a config error: a batch must escalate and keep the results it
        # already has rather than dying part-way through.
        client = OpenAICompatibleClient(
            base_url="https://example.test/v1", api_key="", model="m", provider="TestProvider"
        )
        with pytest.raises(LLMUnavailable, match="No API key configured"):
            client.complete("system", "user")

    def test_a_rejected_key_is_not_retried(self):
        client = client_over([(401, "invalid key"), (200, ok_body())])
        with pytest.raises(LLMUnavailable, match="rejected the API key"):
            client.complete("s", "u")
        assert client.slept == []


class TestSuccess:
    def test_returns_the_message_content_and_usage(self):
        response = client_over([(200, ok_body())]).complete("s", "u")
        assert response.text == '{"ok": true}'
        assert response.prompt_tokens == 2083
        assert response.completion_tokens == 240
        assert response.attempts == 1

    def test_requests_a_json_object_response(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            import json as _json

            captured.update(_json.loads(request.content))
            return httpx.Response(200, json=ok_body())

        transport = httpx.MockTransport(handler)
        httpx.post = lambda url, **kw: httpx.Client(transport=transport).post(
            url, **{k: v for k, v in kw.items() if k != "timeout"}
        )
        OpenAICompatibleClient(base_url="https://example.test/v1", api_key="k", model="test-model", provider="TestProvider").complete("s", "u")
        assert captured["response_format"] == {"type": "json_object"}
        assert captured["temperature"] == 0.0


class TestFailureHandling:
    def test_recovers_from_a_transient_429(self):
        client = client_over([(429, "rate limit, try again"), (200, ok_body())])
        assert client.complete("s", "u").attempts == 2

    def test_a_daily_quota_message_fails_immediately_without_backing_off(self):
        # Backing off against a per-day cap waits for something that will not happen for
        # hours. Gemini's free tier taught this the expensive way — see FAILURES.md.
        client = client_over([(429, "Rate limit reached: 1000 requests per day")])
        with pytest.raises(DailyQuotaExhausted, match="daily quota"):
            client.complete("s", "u")
        assert client.slept == []

    def test_does_not_retry_a_permanent_error(self):
        client = client_over([(404, "unknown model"), (200, ok_body())])
        with pytest.raises(LLMUnavailable, match="not retryable"):
            client.complete("s", "u")
        assert client.slept == []

    def test_gives_up_after_the_attempt_cap(self):
        client = client_over([(503, "overloaded")] * DEFAULT_MAX_ATTEMPTS)
        with pytest.raises(LLMUnavailable, match=f"after {DEFAULT_MAX_ATTEMPTS} attempts"):
            client.complete("s", "u")

    def test_an_empty_message_escalates(self):
        client = client_over([(200, {"choices": [{"message": {"content": ""}}]})])
        with pytest.raises(LLMUnavailable, match="empty message"):
            client.complete("s", "u")

    def test_a_missing_choice_escalates(self):
        client = client_over([(200, {"choices": []})])
        with pytest.raises(LLMUnavailable, match="no usable choice"):
            client.complete("s", "u")
