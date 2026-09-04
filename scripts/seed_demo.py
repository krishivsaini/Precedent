"""Populate a database with real dataset cases so the approval screen has something to show.

Uses the committed eval scenarios and the real seed corpus rather than invented rows, so what
the screen displays is the same data every number in `evals/results/` was computed from.

    uv run python scripts/seed_demo.py
    uv run uvicorn precedent.api.main:app --reload
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals.dataset.loader import load_dataset  # noqa: E402
from precedent.adapters.retrieval.bm25 import BM25Retriever  # noqa: E402
from precedent.adapters.storage.db import connect, init_db
from precedent.adapters.storage.records import ExceptionRecord, ResolutionRecord
from precedent.adapters.storage.repositories import (
    BankLinesRepository,
    ExceptionsRepository,
    LedgerEntriesRepository,
    PaymentsRepository,
    PrecedentsRepository,
    ResolutionsRepository,
)
from precedent.corpus.seed import seed_precedent_records
from precedent.domain.case import ReconciliationCase

from precedent.domain.case import format_paise  # noqa: E402

DB = "precedent.db"


def _rationale(case, scenario) -> str:
    """Agent-style prose for the demo, in rupees rather than the dataset's raw paise."""
    settles, landed = case.net_settlement_paise(), case.credited_paise()
    expected = case.expected_paise()
    gross = sum(p.amount_paise for p in case.payments)
    parts = [
        f"The payments settle to {format_paise(settles)} once each one's own processor fee "
        f"and tax are deducted, against {format_paise(landed)} that reached the bank."
    ]
    if expected and expected != gross:
        parts.append(
            f"The ledger expects {format_paise(expected)} gross while the customer paid "
            f"{format_paise(gross)}, leaving {format_paise(expected - gross)} held back."
        )
    # The dataset's own notes are deliberately not appended: they are written for the
    # generator and carry raw paise figures, which must never reach a screen about money.
    return " ".join(parts)
HOW_MANY = 12


def main() -> int:
    Path(DB).unlink(missing_ok=True)
    conn = connect(DB)
    init_db(conn)

    precedents = PrecedentsRepository(conn)
    corpus = seed_precedent_records()
    for record in corpus:
        precedents.insert(record)
    retriever = BM25Retriever(corpus)

    payments = PaymentsRepository(conn)
    lines = BankLinesRepository(conn)
    ledger = LedgerEntriesRepository(conn)
    exceptions = ExceptionsRepository(conn)
    resolutions = ResolutionsRepository(conn)

    # A spread of classes, so the screen shows the range of states a reviewer actually meets
    # rather than twelve of the easiest kind.
    pool = [s for s in load_dataset() if s.is_exception and s.pool_or_test == "pool"]
    seen: set[str] = set()
    chosen = []
    for scenario in pool:
        if scenario.kind not in seen:
            seen.add(scenario.kind)
            chosen.append(scenario)
    chosen += [s for s in pool if s not in chosen][: HOW_MANY - len(chosen)]

    for index, scenario in enumerate(chosen[:HOW_MANY], start=1):
        refs = []
        for payment in scenario.payments:
            payments.insert(payment)
            refs.append(payment.payment_id)
        for line in scenario.bank_lines:
            lines.insert(line)
            refs.append(line.line_id)
        for entry in scenario.ledger_entries:
            ledger.insert(entry)
            refs.append(entry.entry_id)

        case = ReconciliationCase(scenario.scenario_id, scenario.payments,
                                  scenario.bank_lines, scenario.ledger_entries)
        hits = retriever.retrieve(case.retrieval_query(), 3)

        exceptions.insert(ExceptionRecord(
            exception_id=f"exc_{index:04d}", batch_id="batch_demo", kind=scenario.kind,
            member_refs=refs, detected_at=f"2026-09-{index:02d}T09:00:00+00:00",
            status="open", correlation_id=f"corr_{index:04d}",
        ))
        # Confidence varies across the demo set so the low-confidence and verify-failed
        # states are both reachable — they are normal outcomes, not edge cases.
        confidence = (0.94, 0.91, 0.78, 0.88)[index % 4]
        resolutions.insert(ResolutionRecord(
            resolution_id=f"res_{index:04d}", exception_id=f"exc_{index:04d}",
            proposed_by="agent", confidence=confidence,
            # The dataset's notes carry raw paise; a money screen must never show them.
            # The real rationale comes from the agent and reads as prose.
            rationale=_rationale(case, scenario),
            cited_precedents=[h.record.precedent_id for h in hits],
            verified=index % 5 != 0,
        ))

    conn.commit()
    counts = {
        t: conn.execute(f"SELECT COUNT(*) n FROM {t}").fetchone()["n"]
        for t in ("exceptions", "resolutions", "precedents", "payments")
    }
    conn.close()
    print(f"seeded {DB}: " + ", ".join(f"{v} {k}" for k, v in counts.items()))
    print("run: uv run uvicorn precedent.api.main:app --reload")
    return 0


if __name__ == "__main__":
    sys.exit(main())
