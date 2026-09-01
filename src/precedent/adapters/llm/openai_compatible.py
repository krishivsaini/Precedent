"""One client for every provider that speaks the OpenAI chat-completions shape.

Groq and NVIDIA NIM both do, and so do most hosted-inference services. What varies between
them is a base URL, a default model, a rate limit, and the wording a provider uses to say
"you are out of quota for today" — none of which is logic. What must *not* vary is the
handling around those calls: the single `LLMUnavailable` failure mode, the split between
transient and permanent errors, the immediate stop on a daily quota, and the client-side
token pacing. Every one of those was learned from a run that produced a wrong number rather
than an error (see FAILURES.md), and each is a place where a copy-pasted third adapter would
quietly drift out of step with the other two.

So providers here are *configuration*, not subclasses with their own request handling.
"""

import random
import time

import httpx

from precedent.adapters.llm.base import LLMResponse, LLMUnavailable
from precedent.adapters.llm.throttle import TokenRateThrottle, estimate_tokens

#: Worth trying again: throttling and transport-level faults.
RETRYABLE_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504})

DEFAULT_MAX_ATTEMPTS = 6
BASE_BACKOFF_SECONDS = 2.0
MAX_BACKOFF_SECONDS = 60.0

#: Substrings that mean a *per-day* allowance is spent rather than a per-minute one. Both
#: arrive as HTTP 429 carrying a retry hint of about a minute, but that hint only applies to
#: the per-minute case; backing off against a daily cap waits for something that will not
#: happen for hours.
DAILY_QUOTA_MARKERS = (
    "per day", "requests per day", "tokens per day", "RPD", "TPD",
    "daily quota", "daily limit", "out of credits", "insufficient credits",
)


class DailyQuotaExhausted(LLMUnavailable):
    """The provider's per-day allowance is spent.

    A subclass of `LLMUnavailable`, so every call site still escalates correctly without
    knowing this type exists — but a batch runner that *does* know can stop immediately
    instead of grinding through the rest of the batch to produce a file full of escalations.
    """


class OpenAICompatibleClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        provider: str = "provider",
        timeout: float = 120.0,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        tokens_per_minute: int | None = None,
        sleep=time.sleep,
        throttle: TokenRateThrottle | None = None,
        extra_body: dict | None = None,
    ):
        self.model = model
        self.provider = provider
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._api_key = api_key
        self._timeout = timeout
        self._max_attempts = max_attempts
        self._sleep = sleep
        self._extra_body = extra_body or {}
        if throttle is not None:
            self._throttle = throttle
        elif tokens_per_minute:
            self._throttle = TokenRateThrottle(tokens_per_minute, sleep=sleep)
        else:
            self._throttle = None

    def _backoff_seconds(self, attempt: int, response: httpx.Response | None) -> float:
        if response is not None:
            retry_after = response.headers.get("retry-after")
            if retry_after:
                try:
                    return min(float(retry_after), MAX_BACKOFF_SECONDS)
                except ValueError:
                    pass
        window = min(BASE_BACKOFF_SECONDS * (2**attempt), MAX_BACKOFF_SECONDS)
        # Jitter, because the ablation runs several workers: without it they retry in
        # lockstep and re-trigger the same limit together.
        return window / 2 + random.random() * (window / 2)

    def complete(self, system: str, user: str, *, temperature: float = 0.0) -> LLMResponse:
        if not self._api_key:
            # Unavailability rather than a config error: a batch must escalate and keep the
            # results it already has rather than dying part-way through.
            raise LLMUnavailable(f"No API key configured for {self.provider}")

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "response_format": {"type": "json_object"},
            **self._extra_body,
        }

        estimated = estimate_tokens(system, user)
        if self._throttle:
            self._throttle.acquire(estimated)

        started = time.monotonic()
        body, attempts = self._post_with_retries(payload)
        latency_ms = int((time.monotonic() - started) * 1000)

        usage = body.get("usage") or {}
        if self._throttle:
            self._throttle.reconcile(estimated, usage.get("total_tokens") or estimated)

        try:
            message = body["choices"][0]["message"]
            text = message.get("content")
        except (KeyError, IndexError, TypeError) as error:
            raise LLMUnavailable(
                f"{self.provider} returned no usable choice: {body}"
            ) from error
        if not text:
            raise LLMUnavailable(f"{self.provider} returned an empty message")

        return LLMResponse(
            text=text,
            model=self.model,
            latency_ms=latency_ms,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            # Where a provider reports reasoning tokens separately it is here; most fold
            # them into completion_tokens, so this stays None rather than being guessed.
            thinking_tokens=(usage.get("completion_tokens_details") or {}).get(
                "reasoning_tokens"
            ),
            attempts=attempts,
        )

    def _post_with_retries(self, payload: dict) -> tuple[dict, int]:
        last_error = "no attempt was made"
        for attempt in range(self._max_attempts):
            response = None
            try:
                response = httpx.post(
                    self._url,
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Accept": "application/json",
                    },
                    json=payload,
                    timeout=self._timeout,
                )
                if response.status_code in (401, 403):
                    raise LLMUnavailable(
                        f"{self.provider} rejected the API key "
                        f"(HTTP {response.status_code}): {response.text[:200]}"
                    )
                if response.status_code == 429 and any(
                    marker in response.text for marker in DAILY_QUOTA_MARKERS
                ):
                    raise DailyQuotaExhausted(
                        f"{self.provider} daily quota is exhausted for {self.model!r}. "
                        f"Retrying will not help until it resets. "
                        f"Provider said: {response.text[:400]}"
                    )
                if response.status_code in RETRYABLE_STATUS:
                    last_error = f"HTTP {response.status_code}: {response.text[:200]}"
                elif response.is_error:
                    # Permanent: an unknown model, a malformed request. Retrying spends
                    # quota to receive the same refusal.
                    raise LLMUnavailable(
                        f"{self.provider} returned HTTP {response.status_code} "
                        f"(not retryable): {response.text[:300]}"
                    )
                else:
                    return response.json(), attempt + 1
            except httpx.HTTPError as error:
                last_error = f"{type(error).__name__}: {error}"
            except ValueError as error:
                raise LLMUnavailable(
                    f"{self.provider} returned a non-JSON body: {error}"
                ) from error

            if attempt < self._max_attempts - 1:
                self._sleep(self._backoff_seconds(attempt, response))

        raise LLMUnavailable(
            f"{self.provider} unavailable after {self._max_attempts} attempts; "
            f"last error: {last_error}"
        )
