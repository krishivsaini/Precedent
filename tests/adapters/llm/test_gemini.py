"""Gemini adapter. No test here touches the network.

The retry behaviour is the load-bearing part: a smoke run of the Ring 1.3 ablation reported
a kill-criterion FAIL that was entirely an artifact of free-tier throttling — the zero-shot
arm exhausted the per-minute quota and the two precedent-carrying arms escalated 100% of
their cases on HTTP 429. A transient 429 must not be allowed to masquerade as a finding.
"""

import httpx
import pytest

from precedent.adapters.llm.base import LLMUnavailable
from precedent.adapters.llm.gemini import DEFAULT_MAX_ATTEMPTS, GeminiClient


def ok_body(text='{"ok": true}', thoughts=209):
    return {
        "candidates": [{"content": {"parts": [{"text": text}]}}],
        "usageMetadata": {
            "promptTokenCount": 16,
            "candidatesTokenCount": 9,
            "thoughtsTokenCount": thoughts,
        },
    }


def client_over(responses, **kwargs):
    """A client whose transport replays `responses` (status, json|text) in order."""
    queue = list(responses)
    slept: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        status, payload = queue.pop(0)
        if isinstance(payload, dict):
            return httpx.Response(status, json=payload)
        return httpx.Response(status, text=payload)

    transport = httpx.MockTransport(handler)
    client = GeminiClient(api_key="test-key", sleep=slept.append, **kwargs)
    original = httpx.post

    def patched_post(url, **request_kwargs):
        request_kwargs.pop("timeout", None)
        with httpx.Client(transport=transport) as http:
            return http.post(url, **request_kwargs)

    httpx.post = patched_post
    client._restore = lambda: setattr(httpx, "post", original)
    client.slept = slept
    return client


@pytest.fixture
def restore_httpx():
    original = httpx.post
    yield
    httpx.post = original


class TestKeyHandling:
    def test_a_missing_key_escalates_rather_than_crashing(self, monkeypatch):
        # Raised as unavailability on purpose: a batch must escalate its remaining cases and
        # keep the results already computed, not die part-way through and lose them.
        monkeypatch.setattr("precedent.adapters.llm.gemini.gemini_api_key", lambda: "")
        with pytest.raises(LLMUnavailable, match="GEMINI_API_KEY"):
            GeminiClient(api_key="").complete("system", "user")

    def test_reads_the_key_through_config_so_dotenv_is_honoured(self, monkeypatch):
        # The adapter must not reach into os.environ itself: only precedent.config calls
        # load_dotenv(), so a direct read works under pytest and finds nothing from a CLI.
        monkeypatch.setattr("precedent.adapters.llm.gemini.gemini_api_key", lambda: "from-dotenv")
        assert GeminiClient()._api_key == "from-dotenv"

    def test_an_explicit_key_wins_over_the_environment(self, monkeypatch):
        monkeypatch.setattr("precedent.adapters.llm.gemini.gemini_api_key", lambda: "from-dotenv")
        assert GeminiClient(api_key="explicit")._api_key == "explicit"


class TestSuccess:
    def test_returns_the_text_and_usage(self, restore_httpx):
        client = client_over([(200, ok_body())])
        response = client.complete("s", "u")
        assert response.text == '{"ok": true}'
        assert response.attempts == 1
        assert response.prompt_tokens == 16

    def test_counts_thinking_tokens_separately_and_in_the_total(self, restore_httpx):
        # Excluding them understates the spec §6 cost metric by more than an order of
        # magnitude on a reasoning model: 9 visible tokens against 209 thinking tokens.
        response = client_over([(200, ok_body(thoughts=209))]).complete("s", "u")
        assert response.thinking_tokens == 209
        assert response.completion_tokens == 9
        assert response.total_tokens() == 16 + 9 + 209


class TestRetries:
    def test_recovers_from_a_transient_429(self, restore_httpx):
        client = client_over([(429, "quota"), (429, "quota"), (200, ok_body())])
        response = client.complete("s", "u")
        assert response.text == '{"ok": true}'
        assert response.attempts == 3

    def test_backs_off_between_attempts(self, restore_httpx):
        client = client_over([(429, "q"), (429, "q"), (200, ok_body())])
        client.complete("s", "u")
        assert len(client.slept) == 2
        assert client.slept[1] > client.slept[0]  # exponential, not flat

    def test_honours_a_retry_after_header(self, restore_httpx):
        def handler(request):
            return httpx.Response(429, text="q", headers={"Retry-After": "7"})

        client = GeminiClient(api_key="k", max_attempts=2, sleep=lambda s: slept.append(s))
        slept = []
        transport = httpx.MockTransport(handler)
        httpx.post = lambda url, **kw: httpx.Client(transport=transport).post(
            url, **{k: v for k, v in kw.items() if k != "timeout"}
        )
        with pytest.raises(LLMUnavailable):
            client.complete("s", "u")
        assert slept == [7.0]

    def test_retries_a_server_error(self, restore_httpx):
        response = client_over([(503, "unavailable"), (200, ok_body())]).complete("s", "u")
        assert response.attempts == 2

    def test_gives_up_after_the_attempt_cap_and_escalates(self, restore_httpx):
        client = client_over([(429, "q")] * DEFAULT_MAX_ATTEMPTS)
        with pytest.raises(LLMUnavailable, match=f"after {DEFAULT_MAX_ATTEMPTS} attempts"):
            client.complete("s", "u")

    def test_does_not_retry_a_permanent_error(self, restore_httpx):
        # A bad key or an unknown model returns the same refusal every time; retrying
        # spends quota to learn nothing.
        client = client_over([(400, "API key not valid"), (200, ok_body())])
        with pytest.raises(LLMUnavailable, match="not retryable"):
            client.complete("s", "u")
        assert client.slept == []

    def test_a_blocked_or_empty_candidate_escalates(self, restore_httpx):
        client = client_over([(200, {"candidates": []})])
        with pytest.raises(LLMUnavailable, match="no usable candidate"):
            client.complete("s", "u")
