"""NVIDIA NIM (build.nvidia.com) — configuration over `OpenAICompatibleClient`.

The third provider tried for the Ring 1.3 ablation, and the reason the request handling was
extracted into a shared client first: Gemini's free tier capped at 20 requests/day, and
Groq's per-day *token* budget ran out partway through a 186-call run. NIM's free tier is
credit-based rather than request-capped, which suits a batch of this size better.

No default token-per-minute throttle is set, because NIM does not publish a per-minute token
limit the way Groq does in its headers. The retry path still handles throttling if it
appears, and `--tokens-per-minute` can impose a ceiling if one turns out to be needed —
guessing a limit and pacing against it would only slow the run down for no reason.

Same caveat as every other provider here: the model goes in the result file, and comparisons
across providers are invalid. Only the three arms *within* one run share a model, and that is
what makes that comparison sound.
"""

import time

from precedent.adapters.llm.openai_compatible import OpenAICompatibleClient
from precedent.adapters.llm.throttle import TokenRateThrottle
from precedent.config import nvidia_api_key

BASE_URL = "https://integrate.api.nvidia.com/v1"

#: Chosen by probing, not from the catalogue. NIM lists 82 models to this key, but most of
#: the large ones do not actually serve: `meta/llama-3.3-70b-instruct` returns 410 Gone,
#: `llama-3.1-nemotron-ultra-253b` and `qwen3-235b` return 404, and the deepseek endpoints
#: time out. This one answers in about two seconds and honours the JSON response format.
#: `nvidia/nemotron-3-super-120b-a12b` also works, at roughly 2.5x the latency.
#:
#: `list_models()` reports what the key can reach, but reaching is not serving — probe with
#: a real request before trusting a default. The Gemini path burned a run discovering that
#: `gemini-2.5-flash` was still listed while returning 404 to new users.
DEFAULT_MODEL = "openai/gpt-oss-120b"


class NvidiaClient(OpenAICompatibleClient):
    def __init__(
        self,
        api_key: str | None = None,
        model: str = DEFAULT_MODEL,
        timeout: float = 180.0,
        tokens_per_minute: int | None = None,
        sleep=time.sleep,
        throttle: TokenRateThrottle | None = None,
        **kwargs,
    ):
        super().__init__(
            base_url=BASE_URL,
            api_key=api_key if api_key is not None else nvidia_api_key(),
            model=model,
            provider="NVIDIA NIM",
            timeout=timeout,
            tokens_per_minute=tokens_per_minute,
            sleep=sleep,
            throttle=throttle,
            **kwargs,
        )


def list_models(api_key: str | None = None) -> list[str]:
    """Model ids this key can actually reach.

    Worth calling before a run rather than trusting a default: the Gemini path wasted a run
    on `gemini-2.5-flash`, which the catalogue still listed but which returns 404 for new
    users, and Groq turned out not to serve `llama-3.3-70b` at all.
    """
    import httpx

    response = httpx.get(
        f"{BASE_URL}/models",
        headers={"Authorization": f"Bearer {api_key or nvidia_api_key()}"},
        timeout=30,
    )
    response.raise_for_status()
    return sorted(entry["id"] for entry in response.json().get("data", []))
