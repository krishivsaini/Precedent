"""The vendor-agnostic LLM boundary.

Everything above this line — use cases, the Ring 2 graph, the evals — depends on this
interface and never on a vendor SDK. That is not architectural decoration: spec §7 requires
every LLM call site to have an explicit fallback to escalation, and a fallback can only be
written once there is a single, named way for a model call to fail.

Hence `LLMUnavailable`. Every adapter converts its vendor's timeouts, rate limits, transport
errors, and refusals into it, so callers have exactly one exception to catch and exactly one
reason code to emit (`ESCALATED_MODEL_UNAVAILABLE`). An adapter that lets a vendor exception
escape is a bug, because the caller's fallback will not fire.
"""

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


class LLMUnavailable(RuntimeError):
    """The model could not be reached, or refused to answer.

    Deliberately does not distinguish "down" from "rate limited" from "timed out": at the
    call site every one of them means the same thing — this exception cannot be resolved by
    the model right now, so escalate it to a human.
    """


@dataclass(frozen=True)
class LLMResponse:
    text: str
    model: str
    latency_ms: int
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    thinking_tokens: int | None = None
    """Tokens spent reasoning before answering, where the model reports them separately.

    Tracked as its own field because it is billed and is not in `completion_tokens`. On a
    reasoning model it routinely dominates: a two-token prompt in testing produced nine
    visible tokens and two hundred and nine thinking tokens. Spec §6 reports tokens per
    exception as a cost-discipline metric, and a figure excluding these would understate
    the real cost by more than an order of magnitude.
    """

    attempts: int = 1
    """How many requests it took, including retries. >1 means the provider was throttling."""

    cached: bool = False
    """True when replayed from `evals/cache.py` rather than fetched. Latency statistics
    exclude these: a cache hit's microseconds measure the disk, not the model."""

    def total_tokens(self) -> int | None:
        if self.prompt_tokens is None or self.completion_tokens is None:
            return None
        return self.prompt_tokens + self.completion_tokens + (self.thinking_tokens or 0)


@runtime_checkable
class LLMClient(Protocol):
    model: str

    def complete(self, system: str, user: str, *, temperature: float = 0.0) -> LLMResponse:
        """One turn, no history. Raises `LLMUnavailable` and nothing else.

        Temperature defaults to 0: an eval whose numbers move between runs for reasons
        unrelated to the change under test cannot support a claim of improvement.
        """
        ...
