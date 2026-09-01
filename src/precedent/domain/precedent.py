"""The `Precedent` — the core artifact of the whole system (spec §4).

A precedent is one unit of transferable knowledge: a *generalised* description of a
situation, paired with what was done about it and why. The corpus of these is what makes
autonomous resolution rate climb over time, so the quality bar on a single precedent is
the quality bar on the system.

Two representations exist deliberately:

* `Precedent` (here) — a validated Pydantic model. It is what an LLM's deposit output is
  parsed into, and every constraint below exists because an unconstrained model will
  happily produce a precedent that is syntactically fine and permanently useless.
* `PrecedentRecord` (`adapters.storage.records`) — a flat dataclass mirroring the SQL row,
  carrying the storage-only fields (id, timestamps, corpus version, retrieval counters).

`to_record` / `from_record` are the only sanctioned crossing between them.
"""

import re
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

from precedent.adapters.storage.records import PrecedentRecord
from precedent.domain.reasons import ReasonCode

#: Reason codes that record the agent giving up rather than knowing something. A precedent
#: built on one of these teaches the corpus to escalate, which is the corpus-poisoning
#: failure mode described in docs/ARCHITECTURE.md.
ESCALATION_REASON_CODES = frozenset(
    {
        ReasonCode.ESCALATED_LOW_CONFIDENCE,
        ReasonCode.ESCALATED_VERIFY_FAILED,
        ReasonCode.ESCALATED_PARSE_FAILURE,
        ReasonCode.ESCALATED_MODEL_UNAVAILABLE,
    }
)

#: Razorpay-shaped identifiers. Their presence in `situation` means the precedent has been
#: written about one specific past case and cannot match a future one.
_CONCRETE_ID = re.compile(r"\b(pay|order|rfnd|inv)_[A-Za-z0-9]{8,}\b")

#: `amount_signature` is a grouping key ("short_by_2pct_tds"), not a second prose field.
_AMOUNT_SIGNATURE = re.compile(r"^[a-z0-9]+(_[a-z0-9]+)*$")

#: Long enough that the situation carries retrievable content, short enough that it stays a
#: description rather than a transcript.
MIN_SITUATION_CHARS = 40
MAX_SITUATION_CHARS = 600


class Precedent(BaseModel):
    """One deposited unit of knowledge. Spec §4, with provenance made explicit."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    situation: Annotated[
        str, Field(min_length=MIN_SITUATION_CHARS, max_length=MAX_SITUATION_CHARS)
    ]
    """The retrieval target: what kind of case this is, phrased so a *future* case can match it."""

    resolution: Annotated[str, Field(min_length=1)]
    """What was done and why. Free to be specific — it is read, not retrieved on."""

    reason_code: ReasonCode
    entities: list[str] = Field(default_factory=list)
    amount_signature: str = ""
    confidence_at_deposit: Annotated[float, Field(ge=0.0, le=1.0)]

    derived_from: str | None = None
    """The `resolution_id` this was deposited from — full provenance.

    `None` only for the hand-written seed corpus (`corpus_version = 0`), which by definition
    predates any resolution. Spec §4 types this as a plain `str`; it is widened here for
    exactly that case, and the storage column is nullable for the same reason.
    """

    @field_validator("situation")
    @classmethod
    def _situation_must_generalise(cls, value: str) -> str:
        value = value.strip()
        found = _CONCRETE_ID.search(value)
        if found:
            raise ValueError(
                f"situation contains the concrete identifier {found.group(0)!r}; a precedent "
                "pinned to one past case can never retrieve against a future one. Describe "
                "the shape of the case, not the record."
            )
        return value

    @field_validator("amount_signature")
    @classmethod
    def _amount_signature_is_a_key(cls, value: str) -> str:
        value = value.strip()
        if value and not _AMOUNT_SIGNATURE.match(value):
            raise ValueError(
                f"amount_signature must be a lower snake_case key such as 'short_by_2pct_tds', "
                f"got {value!r}"
            )
        return value

    @field_validator("reason_code")
    @classmethod
    def _reason_code_must_be_knowledge(cls, value: ReasonCode) -> ReasonCode:
        if value in ESCALATION_REASON_CODES:
            raise ValueError(
                f"{value.value!r} is an escalation code — it records that the agent gave up, "
                "which is not knowledge. Deposit the reason code of what was actually true."
            )
        return value

    @field_validator("entities")
    @classmethod
    def _clean_entities(cls, value: list[str]) -> list[str]:
        return [item.strip() for item in value if item.strip()]

    def retrieval_text(self) -> str:
        """The text a retriever indexes.

        Deliberately excludes `resolution`: retrieval matches a new situation against past
        situations. Indexing the answer text would let a precedent surface on words from its
        own conclusion, which inflates apparent retrieval quality without improving it.
        """
        parts = [self.situation, self.amount_signature, *self.entities]
        return " ".join(part for part in parts if part)

    def to_record(
        self,
        precedent_id: str,
        deposited_at: str,
        corpus_version: int,
        embedding: bytes | None = None,
    ) -> PrecedentRecord:
        return PrecedentRecord(
            precedent_id=precedent_id,
            situation=self.situation,
            resolution=self.resolution,
            reason_code=self.reason_code.value,
            entities=list(self.entities),
            amount_signature=self.amount_signature,
            confidence_at_deposit=self.confidence_at_deposit,
            deposited_at=deposited_at,
            corpus_version=corpus_version,
            embedding=embedding,
            derived_from_resolution=self.derived_from,
        )

    @classmethod
    def from_record(cls, record: PrecedentRecord) -> "Precedent":
        return cls(
            situation=record.situation,
            resolution=record.resolution,
            reason_code=ReasonCode(record.reason_code),
            entities=list(record.entities),
            amount_signature=record.amount_signature,
            confidence_at_deposit=record.confidence_at_deposit,
            derived_from=record.derived_from_resolution,
        )
