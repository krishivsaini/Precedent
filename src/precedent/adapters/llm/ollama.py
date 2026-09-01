"""Ollama — the local fallback.

Exists so the eval can be re-run without a vendor key or a network, which matters for a
result set that is meant to be reproducible from a clone. Results from this adapter are not
comparable with Gemini's; every committed result file records which model produced it, and
mixing the two inside one comparison would be the sort of error the eval discipline exists
to prevent.
"""

import time

import httpx

from precedent.adapters.llm.base import LLMResponse, LLMUnavailable

DEFAULT_MODEL = "llama3.1:8b"
DEFAULT_HOST = "http://localhost:11434"
DEFAULT_TIMEOUT_SECONDS = 180.0


class OllamaClient:
    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        host: str = DEFAULT_HOST,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ):
        self.model = model
        self._host = host.rstrip("/")
        self._timeout = timeout

    def complete(self, system: str, user: str, *, temperature: float = 0.0) -> LLMResponse:
        started = time.monotonic()
        try:
            response = httpx.post(
                f"{self._host}/api/chat",
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "stream": False,
                    "format": "json",
                    "options": {"temperature": temperature},
                },
                timeout=self._timeout,
            )
            response.raise_for_status()
            body = response.json()
        except httpx.HTTPStatusError as error:
            raise LLMUnavailable(
                f"Ollama returned HTTP {error.response.status_code}"
            ) from error
        except httpx.HTTPError as error:
            raise LLMUnavailable(f"Ollama request failed: {error}") from error
        except ValueError as error:
            raise LLMUnavailable(f"Ollama returned a non-JSON body: {error}") from error

        text = (body.get("message") or {}).get("content")
        if not text:
            raise LLMUnavailable(f"Ollama returned an empty message: {body}")

        return LLMResponse(
            text=text,
            model=self.model,
            latency_ms=int((time.monotonic() - started) * 1000),
            prompt_tokens=body.get("prompt_eval_count"),
            completion_tokens=body.get("eval_count"),
        )
