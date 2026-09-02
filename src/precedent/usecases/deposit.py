"""Authoring a precedent from a confirmed or corrected resolution (spec §7).

This is the step the whole project rests on. Everything before it produces answers; this is
what turns an answer into knowledge the system can use again. Ring 2.5 demonstrated the
effect with a precedent written **by hand** — this module has to produce one at least as
useful from a model, and there is no reason to assume it will.

Three rules, each of which the spec states and each of which is enforced here rather than
trusted:

* **Deposits fire only on `confirmed` or `corrected`.** Never on `rejected`, never on an
  unreviewed resolution. A corpus that deposits its own unreviewed output is a corpus that
  amplifies its own errors, which is the failure mode named in `docs/ARCHITECTURE.md`.
* **Corrections deposit too, and at the corrected answer.** Spec §7 is explicit that a
  corrected resolution is the *higher*-value precedent, because it encodes a case the system
  got wrong. Depositing the agent's original would teach it the mistake.
* **The write is one transaction** across the human action, the audit row and the precedent
  (FR-7.5). `db.transaction()` exists for this. A precedent that survives while the audit row
  recording *why* it exists does not is a precedent with no provenance.

A failed deposit is never fatal to the resolution it came from: the human's decision stands
whether or not a precedent could be authored from it.
"""

import json
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from precedent.adapters.llm.base import LLMClient, LLMUnavailable
from precedent.adapters.storage.records import AuditLogRecord
from precedent.adapters.storage.repositories import (
    AuditLogRepository,
    PrecedentsRepository,
    ResolutionsRepository,
)
from precedent.domain.case import ReconciliationCase
from precedent.domain.precedent import Precedent
from precedent.domain.reasons import ReasonCode
from precedent.usecases.resolve import extract_json

PROMPT_DIR = Path(__file__).resolve().parents[3] / "prompts" / "deposit"

#: Which deposit prompt authors precedents by default. Versioned because spec §4 asks the
#: eval to measure whether one version's precedents *retrieve better* than another's —
#: "prompt engineering with a number attached". `evals/deposit_eval.py` is that measurement.
DEFAULT_PROMPT_VERSION = "v3"
#: v3 over v2 over v1, measured in `evals/results/deposit-prompt-*.json`. It dominates on the
#: near-deterministic criteria — 9/9 authored against v1's 7/9, no rupee amounts against v1's
#: 4/9, 100% deposit retrieval against v1's 78% — and leads on resolution (78% / 63% / 56%).
#: The resolution lead is *not* statistically established: 10W-4L over v1 at p=0.18 on 27
#: sightings. Adopted on the criteria that are established, not on the headline.

#: The two human actions that deposit. `rejected` deliberately absent.
DEPOSITING_ACTIONS = frozenset({"confirmed", "corrected"})


class DepositRefused(RuntimeError):
    """The deposit was not attempted, because it must not be.

    Distinct from a failure to author one: refusing to deposit a rejected resolution is
    correct behaviour, and conflating it with "the model was unavailable" would hide a
    policy violation inside a retryable-looking error.
    """


@dataclass(frozen=True)
class DepositOutcome:
    precedent_id: str | None
    precedent: Precedent | None
    corpus_version: int | None
    deposited: bool
    reason: str


def load_deposit_prompt(version: str = DEFAULT_PROMPT_VERSION) -> tuple[str, str]:
    path = PROMPT_DIR / f"{version}.md"
    text = path.read_text(encoding="utf-8")
    if "\n## System\n" not in text or "\n## User\n" not in text:
        raise ValueError(f"{path} must contain '## System' and '## User' sections")
    system_part = text.split("\n## System\n", 1)[1]
    system, user = system_part.split("\n## User\n", 1)
    return system.strip(), user.strip()


def author_precedent(
    llm: LLMClient,
    case: ReconciliationCase,
    reason_code: ReasonCode,
    resolution_narrative: str,
    human_action: str,
    correction_note: str = "",
    prompt_version: str = DEFAULT_PROMPT_VERSION,
) -> Precedent:
    """Ask the model to write one precedent. Raises on anything unusable.

    The reason code is **taken from the confirmed resolution, not from the model**. The human
    has already decided what this case was; letting the deposit re-decide it would allow a
    precedent to disagree with the resolution it claims to derive from.
    """
    system, user_template = load_deposit_prompt(prompt_version)
    user = (
        user_template.replace("{case_summary}", case.summarize())
        .replace("{resolution_narrative}", resolution_narrative)
        .replace("{reason_code}", reason_code.value)
        .replace("{human_action}", human_action)
        .replace("{correction_note}", correction_note or "(none)")
    )
    response = llm.complete(system, user)
    payload = json.loads(extract_json(response.text))
    # Overridden rather than validated-against: see the docstring.
    payload["reason_code"] = reason_code.value
    payload.pop("derived_from", None)
    return Precedent(**payload)


def deposit_precedent(
    conn,
    llm: LLMClient,
    case: ReconciliationCase,
    resolution_id: str,
    reason_code: ReasonCode,
    resolution_narrative: str,
    human_action: str,
    correction_note: str = "",
    now: str = "",
    correlation_id: str = "",
) -> DepositOutcome:
    """Author and store one precedent, inside the caller's transaction.

    Does not commit — the caller owns the boundary (NFR-8), because the human action, the
    audit row and the precedent must land together or not at all.
    """
    if human_action not in DEPOSITING_ACTIONS:
        raise DepositRefused(
            f"human_action {human_action!r} does not deposit. Only "
            f"{sorted(DEPOSITING_ACTIONS)} do — a corpus that deposits unreviewed or "
            "rejected output amplifies its own errors."
        )

    precedents = PrecedentsRepository(conn)
    audit = AuditLogRepository(conn)

    try:
        precedent = author_precedent(
            llm, case, reason_code, resolution_narrative, human_action, correction_note
        )
    except (LLMUnavailable, json.JSONDecodeError, ValueError, ValidationError) as error:
        # Recorded, not raised: the human's decision stands whether or not a precedent could
        # be authored from it, and losing the resolution because the deposit failed would be
        # a far worse outcome than losing the precedent.
        audit.append(
            AuditLogRecord(
                correlation_id=correlation_id or resolution_id,
                stage="deposited",
                actor="system",
                created_at=now,
                reason=f"deposit failed: {type(error).__name__}: {error}",
                model=getattr(llm, "model", None),
            )
        )
        return DepositOutcome(None, None, None, False, f"could not author: {error}")

    corpus_version = next_corpus_version(conn)
    precedent_id = f"prec_{corpus_version:04d}"
    record = precedent.to_record(
        precedent_id=precedent_id, deposited_at=now, corpus_version=corpus_version
    )
    record = type(record)(**{**record.__dict__, "derived_from_resolution": resolution_id})
    precedents.insert(record)

    audit.append(
        AuditLogRecord(
            correlation_id=correlation_id or resolution_id,
            stage="deposited",
            actor="system",
            created_at=now,
            model=getattr(llm, "model", None),
            reason=(
                f"deposited {precedent_id} at corpus_version {corpus_version} "
                f"from {human_action} resolution {resolution_id}"
            ),
        )
    )
    return DepositOutcome(precedent_id, precedent, corpus_version, True, "deposited")


def next_corpus_version(conn) -> int:
    """One past the highest deposited version.

    Seeds sit at version 0, so the first deposit is version 1 and the count of deposits is
    the version number — which is what makes `list_as_of_corpus_version(n)` mean "the corpus
    after n deposits" and the replay snapshots exact rather than approximate.
    """
    row = conn.execute("SELECT MAX(corpus_version) FROM precedents").fetchone()
    highest = row[0] if row and row[0] is not None else 0
    return highest + 1


def record_and_deposit(
    conn,
    llm: LLMClient,
    case: ReconciliationCase,
    resolution_id: str,
    reason_code: ReasonCode,
    resolution_narrative: str,
    human_action: str,
    corrected_payload: dict | None = None,
    correction_note: str = "",
    now: str = "",
) -> DepositOutcome:
    """The whole gate-to-corpus step: record the human's action, then deposit.

    One transaction, owned here, spanning both (FR-7.5). A precedent whose resolution was
    never marked reviewed, or a review with no precedent behind it, are both states the
    system should not be able to reach.
    """
    from precedent.adapters.storage.db import transaction

    with transaction(conn):
        ResolutionsRepository(conn).record_human_action(
            resolution_id=resolution_id,
            human_action=human_action,
            corrected_payload=corrected_payload,
            resolved_at=now,
        )
        if human_action not in DEPOSITING_ACTIONS:
            return DepositOutcome(None, None, None, False, f"{human_action} does not deposit")
        return deposit_precedent(
            conn, llm, case, resolution_id, reason_code, resolution_narrative,
            human_action, correction_note, now,
        )
