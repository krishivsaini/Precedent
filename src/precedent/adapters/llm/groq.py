"""Groq — configuration over `openai_compatible.OpenAICompatibleClient`.

Added because Gemini's free tier caps `gemini-3.5-flash` at 20 requests per day and the Ring
1.3 ablation needs 186 (see FAILURES.md).

**Its own limits, measured rather than assumed** (from the response headers): 1000 requests
per day, but **8000 tokens per minute** — the binding constraint at ~2500 tokens per grounded
prompt, hence the throttle. There is also a per-day *token* budget that the headers do not
expose, and which 186 calls exhaust; that is what `evals/cache.py` exists to survive.

**The trade, stated plainly.** These are open-weight models. On a task turning on multi-step
arithmetic they are not equivalent to a frontier model, and a weaker model can fail the kill
criterion for reasons unrelated to whether retrieval works. So the model goes in every result
file, and cross-provider comparisons are invalid — the arms within one run share a model,
which is what makes *that* comparison sound.
"""

import time

from precedent.adapters.llm.openai_compatible import OpenAICompatibleClient
from precedent.adapters.llm.throttle import TokenRateThrottle
from precedent.config import groq_api_key

BASE_URL = "https://api.groq.com/openai/v1"

#: The largest reasoning-capable chat model this key can reach, and these cases turn on
#: multi-step arithmetic, so capacity matters. `--model` overrides it.
DEFAULT_MODEL = "openai/gpt-oss-120b"

#: Read from `x-ratelimit-limit-tokens` on a live response, not guessed.
DEFAULT_TOKENS_PER_MINUTE = 8000


class GroqClient(OpenAICompatibleClient):
    def __init__(
        self,
        api_key: str | None = None,
        model: str = DEFAULT_MODEL,
        timeout: float = 120.0,
        tokens_per_minute: int = DEFAULT_TOKENS_PER_MINUTE,
        sleep=time.sleep,
        throttle: TokenRateThrottle | None = None,
        **kwargs,
    ):
        super().__init__(
            base_url=BASE_URL,
            api_key=api_key if api_key is not None else groq_api_key(),
            model=model,
            provider="Groq",
            timeout=timeout,
            tokens_per_minute=tokens_per_minute,
            sleep=sleep,
            throttle=throttle,
            **kwargs,
        )
