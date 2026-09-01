"""A scripted `LLMClient` — deterministic model responses, no network.

Lives in `src/` rather than `tests/` deliberately. Spec §7 requires every LLM call site to
have a fallback to escalation, and requires those fallback paths to be **tested directly**
rather than trusted to fire during a demo. Testing them needs a client that can be made to
return malformed JSON, or an empty string, or to raise `LLMUnavailable`, on command. That
makes it part of the system's testing surface, not a fixture belonging to one test module —
Ring 2's graph tests need the same thing.

It is never wired into a real run: the eval records the model behind every result, and this
client's model name says plainly what it is.
"""

from collections.abc import Callable, Iterable

from precedent.adapters.llm.base import LLMResponse, LLMUnavailable


class ScriptedLLM:
    """Returns queued responses in order. A queued `Exception` is raised instead."""

    def __init__(
        self,
        responses: Iterable[str | Exception] | None = None,
        model: str = "scripted-test-double",
        on_call: Callable[[str, str], None] | None = None,
    ):
        self.model = model
        self._queue = list(responses or [])
        self._on_call = on_call
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, user: str, *, temperature: float = 0.0) -> LLMResponse:
        self.calls.append((system, user))
        if self._on_call:
            self._on_call(system, user)
        if not self._queue:
            raise LLMUnavailable("ScriptedLLM ran out of queued responses")
        nxt = self._queue.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return LLMResponse(text=nxt, model=self.model, latency_ms=0,
                           prompt_tokens=0, completion_tokens=0)

    @property
    def last_user_prompt(self) -> str:
        return self.calls[-1][1]
