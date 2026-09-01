"""A content-addressed cache of model responses.

Two problems this solves, one practical and one methodological.

**Practical:** the Ring 1.3 ablation is 186 calls, and Groq's free tier exhausts its daily
token budget partway through. Without a cache, a run that dies at case 150 throws away 150
calls' worth of quota and has to start over tomorrow — so the eval is never runnable at all.
With one, tomorrow's run replays what is already known and spends quota only on what is left.

**Methodological, and the more important of the two:** spec §6 requires re-running before any
claim of improvement. An eval that costs a full day's quota per run makes that discipline
impossible to follow, and a discipline nobody can follow is not a discipline. Caching makes
re-running nearly free, which is what lets the rule be obeyed rather than admired.

**Why this does not launder a stale result.** The key is a hash of everything that determines
the answer: model, temperature, and the exact system and user prompts. Change the prompt, the
retrieved precedents, the corpus, or the model, and the key changes — so a cache hit means
*this identical question was already asked of this identical model*, and replaying the answer
is not an approximation of re-running it, it is the same thing. Nothing about the scoring is
cached; every metric is recomputed from the responses on every run.

The cache directory is gitignored. Committed results must be reproducible from a clone by
running the eval, not by shipping its answers.
"""

import hashlib
import json
import threading
from pathlib import Path

from precedent.adapters.llm.base import LLMClient, LLMResponse

CACHE_DIR = Path(__file__).parent / ".cache"


def cache_key(
    model: str, system: str, user: str, temperature: float, provider: str = ""
) -> str:
    """Everything that determines the response, and nothing that does not.

    `provider` is part of the key because the same model id is served by more than one
    host — `openai/gpt-oss-120b` is available on both Groq and NVIDIA NIM — and two hosts
    serving one set of weights may quantise or configure them differently. Keying on the
    model alone would replay one provider's answers for the other, which is precisely the
    cross-provider comparison every result file in this project declares invalid.
    """
    digest = hashlib.blake2b(digest_size=16)
    for part in (provider, model, str(temperature), system, user):
        digest.update(part.encode("utf-8"))
        digest.update(b"\x00")  # domain separator, so concatenations cannot collide
    return digest.hexdigest()


class CachingLLM:
    """Wraps any `LLMClient`, replaying identical requests from disk.

    Implements `LLMClient` itself, so it is a drop-in at every call site and the code under
    test cannot tell the difference — which is the point: the cache must not change what is
    measured, only what it costs.
    """

    def __init__(self, inner: LLMClient, cache_dir: Path = CACHE_DIR, enabled: bool = True):
        self._inner = inner
        self._dir = cache_dir
        self._enabled = enabled
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0
        if enabled:
            self._dir.mkdir(parents=True, exist_ok=True)

    @property
    def model(self) -> str:
        return self._inner.model

    @property
    def _provider(self) -> str:
        return getattr(self._inner, "provider", "")

    def _path(self, key: str) -> Path:
        return self._dir / f"{key}.json"

    def complete(self, system: str, user: str, *, temperature: float = 0.0) -> LLMResponse:
        if not self._enabled:
            return self._inner.complete(system, user, temperature=temperature)

        key = cache_key(self.model, system, user, temperature, self._provider)
        path = self._path(key)
        if path.exists():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                with self._lock:
                    self.hits += 1
                # `cached` marks a replayed response so latency statistics can exclude it —
                # a cache hit's microseconds are not a measurement of the model.
                return LLMResponse(**payload["response"], cached=True)
            except (json.JSONDecodeError, KeyError, TypeError):
                # A truncated or stale-schema entry is a nuisance, not an error: drop it and
                # ask the model. Never let a corrupt cache take down a run.
                path.unlink(missing_ok=True)

        response = self._inner.complete(system, user, temperature=temperature)
        with self._lock:
            self.misses += 1
        record = {
            "response": {
                "text": response.text,
                "model": response.model,
                "latency_ms": response.latency_ms,
                "prompt_tokens": response.prompt_tokens,
                "completion_tokens": response.completion_tokens,
                "thinking_tokens": response.thinking_tokens,
                "attempts": response.attempts,
            }
        }
        # Written via a temporary file and renamed, so a run killed mid-write leaves either
        # the old entry or the new one, never a half-written file for the next run to trip on.
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(record), encoding="utf-8")
        temp.replace(path)
        return response

    def stats(self) -> dict:
        return {"hits": self.hits, "misses": self.misses}
