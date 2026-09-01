"""Gemini via the REST API.

Called over plain HTTP rather than through the vendor SDK. The surface used here is one
endpoint and one JSON shape, and a direct call keeps the failure modes visible — an SDK's
own retry and exception hierarchy would sit between the model and `LLMUnavailable`, which is
the one thing this adapter exists to guarantee.
"""

import random
import time

import httpx

from precedent.adapters.llm.base import LLMResponse, LLMUnavailable
from precedent.adapters.llm.openai_compatible import (
    BASE_BACKOFF_SECONDS,
    DAILY_QUOTA_MARKERS,
    MAX_BACKOFF_SECONDS,
    RETRYABLE_STATUS,
    DailyQuotaExhausted,
)
from precedent.config import gemini_api_key

#: Gemini's own spelling of a per-day quota, on top of the shared markers. Both the
#: per-minute and per-day caps arrive as 429 carrying a `retryDelay` of about a minute, but
#: that hint only applies to the per-minute one — backing off against a daily cap waits for
#: something that will not happen until the quota resets. Free-tier `gemini-3.5-flash` is
#: 20 requests per day, against an ablation that needs 186. See FAILURES.md.
_DAILY_QUOTA_MARKERS = DAILY_QUOTA_MARKERS + ("PerDay", "RequestsPerDay")

DEFAULT_MAX_ATTEMPTS = 6

#: Every committed result file records the model that produced it, because numbers from
#: different models are not comparable. Changing this default invalidates comparison with
#: results already on disk — re-run both arms rather than mixing them.
DEFAULT_MODEL = "gemini-3.5-flash"
API_ROOT = "https://generativelanguage.googleapis.com/v1beta/models"
DEFAULT_TIMEOUT_SECONDS = 60.0


def _is_daily_quota_exhausted(response: httpx.Response) -> bool:
    body = response.text
    return any(marker in body for marker in _DAILY_QUOTA_MARKERS)


class GeminiClient:
    def __init__(
        self,
        api_key: str | None = None,
        model: str = DEFAULT_MODEL,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        sleep=time.sleep,
    ):
        self.model = model
        self._api_key = api_key or gemini_api_key()
        self._timeout = timeout
        self._max_attempts = max_attempts
        # Injected so the retry tests assert on the backoff schedule without waiting it out.
        self._sleep = sleep

    def _backoff_seconds(self, attempt: int, response: httpx.Response | None) -> float:
        """Exponential backoff with jitter, deferring to `Retry-After` when the server
        sends one. Jitter matters here because the ablation fires several requests
        concurrently: without it they retry in lockstep and re-trigger the same limit."""
        if response is not None:
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                try:
                    return min(float(retry_after), MAX_BACKOFF_SECONDS)
                except ValueError:
                    pass
        window = min(BASE_BACKOFF_SECONDS * (2**attempt), MAX_BACKOFF_SECONDS)
        return window / 2 + random.random() * (window / 2)

    def complete(self, system: str, user: str, *, temperature: float = 0.0) -> LLMResponse:
        if not self._api_key:
            # Raised as unavailability rather than a config error on purpose: a missing key
            # at run time must escalate the exception like any other outage, not crash a
            # batch part-way through and lose the results already computed.
            raise LLMUnavailable("GEMINI_API_KEY is not set")

        payload = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {
                "temperature": temperature,
                "responseMimeType": "application/json",
            },
        }
        started = time.monotonic()
        body, attempts = self._post_with_retries(payload)
        latency_ms = int((time.monotonic() - started) * 1000)

        try:
            parts = body["candidates"][0]["content"]["parts"]
            text = "".join(part.get("text", "") for part in parts)
        except (KeyError, IndexError, TypeError) as error:
            # A blocked or empty candidate lands here. It is an outage from the caller's
            # point of view: no answer came back, so the case escalates.
            raise LLMUnavailable(f"Gemini returned no usable candidate: {body}") from error

        usage = body.get("usageMetadata") or {}
        return LLMResponse(
            attempts=attempts,
            thinking_tokens=usage.get("thoughtsTokenCount"),
            text=text,
            model=self.model,
            latency_ms=latency_ms,
            prompt_tokens=usage.get("promptTokenCount"),
            completion_tokens=usage.get("candidatesTokenCount"),
        )

    def _post_with_retries(self, payload: dict) -> tuple[dict, int]:
        """POST until it succeeds, exhausts attempts, or hits a non-retryable error.

        Returns the decoded body and the number of attempts it took. Every exit that is not
        a success is an `LLMUnavailable`, so the caller still has exactly one exception to
        catch and one reason code to emit.
        """
        last_error = "no attempt was made"
        for attempt in range(self._max_attempts):
            response = None
            try:
                response = httpx.post(
                    f"{API_ROOT}/{self.model}:generateContent",
                    headers={"x-goog-api-key": self._api_key},
                    json=payload,
                    timeout=self._timeout,
                )
                if response.status_code == 429 and _is_daily_quota_exhausted(response):
                    raise DailyQuotaExhausted(
                        "Gemini daily free-tier quota is exhausted for "
                        f"{self.model!r}. Retrying will not help until the quota resets. "
                        f"Provider said: {response.text[:400]}"
                    )
                if response.status_code in RETRYABLE_STATUS:
                    last_error = f"HTTP {response.status_code}"
                elif response.is_error:
                    # Permanent: a bad key, an unknown model, a malformed request. Retrying
                    # spends quota to receive the same refusal.
                    raise LLMUnavailable(
                        f"Gemini returned HTTP {response.status_code} "
                        f"(not retryable): {response.text[:300]}"
                    )
                else:
                    return response.json(), attempt + 1
            except httpx.HTTPError as error:
                # Transport-level: timeouts, connection resets, DNS. Transient by nature.
                last_error = f"{type(error).__name__}: {error}"
            except ValueError as error:
                raise LLMUnavailable(f"Gemini returned a non-JSON body: {error}") from error

            if attempt < self._max_attempts - 1:
                self._sleep(self._backoff_seconds(attempt, response))

        raise LLMUnavailable(
            f"Gemini unavailable after {self._max_attempts} attempts; last error: {last_error}"
        )
