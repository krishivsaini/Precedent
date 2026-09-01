"""The thin, non-graph resolution path (Ring 1.3).

One retrieval, one model call, one parse. No tools, no investigation loop, no verify/revise
cycle — those are Ring 2. This exists to answer one question and nothing else:

> Does injecting retrieved precedents beat asking the same model the same question with no
> precedents at all?

That is the kill criterion. If grounding does not beat zero-shot here, the premise the whole
project rests on is false, and the honest response is to say so in `docs/ARCHITECTURE.md`
and fall back to plain adjudication — not to add a graph on top and hope it rescues the
number. Keeping this path deliberately thin is what makes the comparison mean something: the
only difference between the arms is which precedents go into the prompt.

Every exit is a `ResolutionOutcome` with a reason code. There is no path out of here that
raises, because a batch that dies half-way through loses the results already computed.
"""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from precedent.adapters.llm.base import LLMClient, LLMResponse, LLMUnavailable
from precedent.adapters.retrieval.base import RetrievedPrecedent
from precedent.domain.case import ReconciliationCase
from precedent.domain.confidence import DEFAULT_AUTO_RESOLVE_THRESHOLD
from precedent.domain.precedent import ESCALATION_REASON_CODES
from precedent.domain.reasons import ReasonCode

PROMPT_PATH = Path(__file__).resolve().parents[3] / "prompts" / "resolve" / "v1.md"

_SYSTEM_MARKER = "\n## System\n"
_USER_MARKER = "\n## User\n"
_FENCED_JSON = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


class ProposedResolution(BaseModel):
    """What the model is asked to return. Anything else is a parse failure."""

    model_config = ConfigDict(extra="forbid")

    reason_code: ReasonCode
    confidence: Annotated[float, Field(ge=0.0, le=1.0)]
    rationale: Annotated[str, Field(min_length=1)]
    cited_precedent_ids: list[str] = Field(default_factory=list)

    @field_validator("reason_code")
    @classmethod
    def _must_not_self_escalate(cls, value: ReasonCode) -> ReasonCode:
        # Escalation is a decision this code makes about the model's answer, never an
        # answer the model gets to give. Letting it self-escalate would let a model opt out
        # of being scored, and the escalation rate would stop measuring anything.
        if value in ESCALATION_REASON_CODES:
            raise ValueError(
                f"{value.value!r} is not a resolution. Return the reason code of what is "
                "actually true, or the lowest honest confidence."
            )
        return value


@dataclass(frozen=True)
class ResolutionOutcome:
    """The result of one attempt, resolved or escalated. Never an exception."""

    case_id: str
    reason_code: ReasonCode
    confidence: float
    rationale: str
    cited_precedent_ids: list[str] = field(default_factory=list)
    retrieved_precedent_ids: list[str] = field(default_factory=list)
    escalated: bool = False
    model: str | None = None
    latency_ms: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    thinking_tokens: int | None = None
    cached: bool = False
    """Replayed from the eval cache rather than fetched. Excluded from latency stats."""

    attempts: int = 1
    """Requests it took, including retries. >1 means the provider was throttling — surfaced
    so a run degraded by rate limits is visible in the result rather than silently wrong."""

    @property
    def total_tokens(self) -> int | None:
        if self.prompt_tokens is None or self.completion_tokens is None:
            return None
        return self.prompt_tokens + self.completion_tokens + (self.thinking_tokens or 0)

    @property
    def hallucinated_citations(self) -> list[str]:
        """Cited ids that were never retrieved — the model inventing its own grounding.

        Measured rather than merely prevented, because the rate at which it happens is a
        property of the prompt worth reporting (spec §6, precedent precision).
        """
        retrieved = set(self.retrieved_precedent_ids)
        return [pid for pid in self.cited_precedent_ids if pid not in retrieved]


def load_prompt() -> tuple[str, str]:
    """Split `prompts/resolve/v1.md` into its system and user halves.

    The prompt lives in a versioned file rather than a string literal so that `v2.md` can be
    measured against it — spec §4's "prompt engineering with a number attached".
    """
    text = PROMPT_PATH.read_text(encoding="utf-8")
    if _SYSTEM_MARKER not in text or _USER_MARKER not in text:
        raise ValueError(f"{PROMPT_PATH} must contain '## System' and '## User' sections")
    system_part = text.split(_SYSTEM_MARKER, 1)[1]
    system, user = system_part.split(_USER_MARKER, 1)
    return system.strip(), user.strip()


def render_precedents(hits: list[RetrievedPrecedent]) -> str:
    """Retrieved precedents as prompt context.

    The zero-shot arm gets the empty-corpus sentence, not an absent section, so the two arms
    differ in the *content* of the precedent block rather than in the shape of the prompt.
    A structural difference would confound the comparison with a prompt-format change.
    """
    if not hits:
        return "(No precedents are available. The corpus is empty.)"
    blocks = []
    for index, hit in enumerate(hits, start=1):
        record = hit.record
        entities = ", ".join(record.entities) if record.entities else "none recorded"
        blocks.append(
            f"[{index}] id: {record.precedent_id}\n"
            f"    situation: {record.situation}\n"
            f"    resolution: {record.resolution}\n"
            f"    reason_code: {record.reason_code}\n"
            f"    amount_signature: {record.amount_signature}\n"
            f"    entities: {entities}\n"
            f"    confidence_when_deposited: {record.confidence_at_deposit:.2f}"
        )
    return "\n\n".join(blocks)


def extract_json(text: str) -> str:
    """Pull the JSON object out of a model response.

    Models wrap JSON in prose or fences even when told not to. Recovering from that is not
    leniency about correctness — the object still has to validate — it just avoids scoring a
    well-formed answer as a parse failure because of packaging.
    """
    fenced = _FENCED_JSON.search(text)
    candidate = fenced.group(1) if fenced else text
    start, end = candidate.find("{"), candidate.rfind("}")
    if start == -1 or end == -1 or end < start:
        return candidate.strip()
    return candidate[start : end + 1]


def _escalate(
    case_id: str,
    reason_code: ReasonCode,
    rationale: str,
    retrieved_ids: list[str],
    response: LLMResponse | None = None,
    confidence: float = 0.0,
) -> ResolutionOutcome:
    return ResolutionOutcome(
        case_id=case_id,
        reason_code=reason_code,
        confidence=confidence,
        rationale=rationale,
        retrieved_precedent_ids=retrieved_ids,
        escalated=True,
        model=response.model if response else None,
        latency_ms=response.latency_ms if response else None,
        prompt_tokens=response.prompt_tokens if response else None,
        completion_tokens=response.completion_tokens if response else None,
        thinking_tokens=response.thinking_tokens if response else None,
        attempts=response.attempts if response else 1,
        cached=response.cached if response else False,
    )


def resolve_case(
    case: ReconciliationCase,
    llm: LLMClient,
    precedents: list[RetrievedPrecedent] | None = None,
    confidence_threshold: float = DEFAULT_AUTO_RESOLVE_THRESHOLD,
) -> ResolutionOutcome:
    """Attempt one case. Returns an outcome; never raises.

    The four escalation paths — model unavailable, unparseable output, schema violation, low
    confidence — are each reachable and each tested directly (spec §7, Ring 2.4's discipline
    applied early). A fallback nobody has exercised is a fallback that does not work.
    """
    hits = precedents or []
    retrieved_ids = [hit.record.precedent_id for hit in hits]
    system, user_template = load_prompt()
    user = user_template.replace("{case_summary}", case.summarize()).replace(
        "{precedents}", render_precedents(hits)
    )

    try:
        response = llm.complete(system, user)
    except LLMUnavailable as error:
        return _escalate(
            case.case_id, ReasonCode.ESCALATED_MODEL_UNAVAILABLE, str(error), retrieved_ids
        )

    try:
        payload = json.loads(extract_json(response.text))
    except (json.JSONDecodeError, ValueError) as error:
        return _escalate(
            case.case_id,
            ReasonCode.ESCALATED_PARSE_FAILURE,
            f"Model output was not JSON: {error}",
            retrieved_ids,
            response,
        )

    try:
        proposal = ProposedResolution.model_validate(payload)
    except ValidationError as error:
        return _escalate(
            case.case_id,
            ReasonCode.ESCALATED_PARSE_FAILURE,
            f"Model output did not match the resolution schema: {error}",
            retrieved_ids,
            response,
        )

    if proposal.confidence < confidence_threshold:
        return _escalate(
            case.case_id,
            ReasonCode.ESCALATED_LOW_CONFIDENCE,
            proposal.rationale,
            retrieved_ids,
            response,
            confidence=proposal.confidence,
        )

    return ResolutionOutcome(
        case_id=case.case_id,
        reason_code=proposal.reason_code,
        confidence=proposal.confidence,
        rationale=proposal.rationale,
        cited_precedent_ids=proposal.cited_precedent_ids,
        retrieved_precedent_ids=retrieved_ids,
        escalated=False,
        model=response.model,
        latency_ms=response.latency_ms,
        prompt_tokens=response.prompt_tokens,
        completion_tokens=response.completion_tokens,
        thinking_tokens=response.thinking_tokens,
        attempts=response.attempts,
        cached=response.cached,
    )
