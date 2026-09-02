"""The six investigation tools bound to the `investigate` node (spec §7).

**No business logic lives here.** Every tool is a thin wrapper over the storage repositories
and the domain layer built in Ring 0. Netted-group reasoning calls
`matching.is_netted_group_match`; expected amounts go through `domain.money`. A tool that
reimplemented either would give the agent a second, subtly different arithmetic to the one
the deterministic matcher and the verifier use — and the eval would then be measuring which
of two implementations the model happened to invoke.

Tools read from a **per-case SQLite database**, built by loading exactly that case's records.
Two reasons, and the second matters more:

1. It exercises the real storage layer rather than a test seam, so the graph runs the same
   path in an eval as it would in production.
2. It enforces per-scenario isolation (FR-9.10) structurally. A tool physically cannot see
   another scenario's payments, so a model cannot accidentally "solve" a case by matching
   against a record that belongs to a different one. Sharing one database across scenarios
   would make that possible and the resulting accuracy meaningless.

Every tool returns a JSON-serialisable dict — never a record object — because results are fed
back to the model as text and stored in the trace.
"""

import sqlite3
from typing import Any, Callable

from precedent.adapters.storage.db import connect, init_db
from precedent.adapters.storage.repositories import (
    BankLinesRepository,
    LedgerEntriesRepository,
    PaymentsRepository,
)
from precedent.domain import matching
from precedent.domain.case import ReconciliationCase, format_paise


class CaseWorkspace:
    """A per-case database plus the tools bound to it.

    Owns a connection; call `close()` (or use it as a context manager) when the case is done.
    """

    def __init__(self, case: ReconciliationCase):
        self.case = case
        self._conn: sqlite3.Connection = connect(":memory:")
        init_db(self._conn)
        payments = PaymentsRepository(self._conn)
        bank_lines = BankLinesRepository(self._conn)
        ledger = LedgerEntriesRepository(self._conn)
        for payment in case.payments:
            payments.insert(payment)
        for line in case.bank_lines:
            bank_lines.insert(line)
        for entry in case.ledger_entries:
            ledger.insert(entry)
        self._conn.commit()
        self._payments = payments
        self._bank_lines = bank_lines
        self._ledger = ledger

    def __enter__(self) -> "CaseWorkspace":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self._conn.close()

    # ---- the six tools -------------------------------------------------------------

    def fetch_payment(self, payment_id: str | None = None, order_id: str | None = None) -> dict:
        """Payments for this case, by id or by order reference."""
        if payment_id:
            record = self._payments.get(payment_id)
            found = [record] if record else []
        elif order_id:
            found = self._payments.list_by_order(order_id)
        else:
            found = list(self.case.payments)
        return {
            "count": len(found),
            "payments": [
                {
                    "payment_id": p.payment_id,
                    "order_id": p.order_id,
                    "gross_paise": p.amount_paise,
                    "fee_paise": p.fee_paise,
                    "tax_on_fee_paise": p.tax_paise,
                    "settles_to_paise": p.amount_paise - p.fee_paise - p.tax_paise,
                    "captured_at": p.captured_at,
                    "status": p.status,
                }
                for p in found
            ],
        }

    def fetch_ledger_entry(self, order_id: str | None = None) -> dict:
        """Open ledger entries, by order reference or all of them for this case."""
        if order_id:
            entry = self._ledger.get_by_order(order_id)
            found = [entry] if entry else []
        else:
            found = list(self.case.ledger_entries)
        return {
            "count": len(found),
            "ledger_entries": [
                {
                    "entry_id": e.entry_id,
                    "order_id": e.order_id,
                    "invoice_no": e.invoice_no,
                    "customer_name": e.customer_name,
                    "expects_paise": e.expected_amount_paise,
                    "terms": e.terms,
                }
                for e in found
            ],
        }

    def fetch_bank_lines(self, direction: str | None = None) -> dict:
        """Bank statement lines for this case, optionally filtered to credits or debits."""
        found = [
            line for line in self._bank_lines.list_all()
            if direction is None or line.direction == direction
        ]
        return {
            "count": len(found),
            "bank_lines": [
                {
                    "line_id": b.line_id,
                    "direction": b.direction,
                    "amount_paise": b.amount_paise,
                    "value_date": b.value_date,
                    "narration": b.narration,
                }
                for b in found
            ],
        }

    def fetch_refunds(self, order_id: str | None = None) -> dict:
        """Refund evidence for this case.

        **This returns nothing on the current dataset, and that is the correct answer.** A
        refund netted into a same-day settlement leaves no processor refund record at all —
        it shows up only as a credit smaller than the payments settle to. That absence is
        exactly what makes the `refund_netted` class hard, so the tool reports the absence
        plainly instead of implying the data is missing.

        Debit lines are returned when present, since a refund raised after the settlement
        window does appear as its own debit.
        """
        debits = [
            line for line in self._bank_lines.list_all()
            if line.direction == "debit"
            and (order_id is None or order_id in line.narration)
        ]
        settled = self.case.net_settlement_paise()
        credited = self.case.credited_paise()
        unexplained = settled - credited
        return {
            "processor_refund_records": [],
            "note": (
                "No processor refund record exists for this case. A refund netted into a "
                "same-day settlement leaves no such record — it is visible only as a credit "
                "smaller than the payments settle to."
            ),
            "debit_lines": [
                {
                    "line_id": b.line_id,
                    "amount_paise": b.amount_paise,
                    "value_date": b.value_date,
                    "narration": b.narration,
                }
                for b in debits
            ],
            "unexplained_shortfall_paise": unexplained if unexplained > 0 else 0,
            "unexplained_shortfall": (
                format_paise(unexplained) if unexplained > 0 else None
            ),
        }

    def compute_expected_amount(
        self,
        payment_ids: list[str] | None = None,
        deduction_rate: str | None = None,
    ) -> dict:
        """Arithmetic, computed by the domain layer rather than by the model.

        Given payments, returns what they settle to net of each one's own fee and tax — the
        per-payment netting that a batch total must match. `deduction_rate` optionally
        reconstructs a gross invoice from a net receipt (the withholding case).

        Exists so the model can *check* arithmetic instead of performing it. The verifier
        recomputes independently regardless; this is to stop a wrong intermediate figure
        propagating into a proposal in the first place.
        """
        from decimal import Decimal, InvalidOperation

        from precedent.domain.money import gross_before_tds_paise

        selected = (
            [p for p in self.case.payments if p.payment_id in set(payment_ids)]
            if payment_ids
            else list(self.case.payments)
        )
        settles_to = sum(p.amount_paise - p.fee_paise - p.tax_paise for p in selected)
        result: dict[str, Any] = {
            "payments_considered": [p.payment_id for p in selected],
            "gross_paise": sum(p.amount_paise for p in selected),
            "total_fee_and_tax_paise": sum(p.fee_paise + p.tax_paise for p in selected),
            "settles_to_paise": settles_to,
            "settles_to": format_paise(settles_to),
            "ledger_expects_paise": self.case.expected_paise(),
            "bank_credited_paise": self.case.credited_paise(),
        }
        if deduction_rate:
            try:
                rate = Decimal(str(deduction_rate))
                gross = sum(
                    gross_before_tds_paise(p.amount_paise, rate) for p in selected
                )
                result["reconstructed_gross_at_rate_paise"] = gross
                result["reconstructed_gross_at_rate"] = format_paise(gross)
                result["matches_ledger_expectation"] = (
                    abs(gross - self.case.expected_paise()) <= 100
                )
            except (InvalidOperation, ValueError, ArithmeticError) as error:
                result["deduction_rate_error"] = str(error)
        return result

    def search_prior_resolutions(self, question: str = "") -> dict:  # noqa: ARG002
        """Group-matching evidence, delegated to the deterministic matcher.

        Answers the question the netted-settlement class turns on — does some subset of
        these payments account for the credit exactly? — by calling
        `matching.is_netted_group_match` rather than reimplementing the grouping. That
        delegation is the point: the agent, the deterministic matcher and the verifier must
        all be using one definition of what "nets" means.
        """
        credits = [b for b in self.case.bank_lines if b.direction == "credit"]
        findings = []
        for credit in credits:
            matched = matching.is_netted_group_match(list(self.case.payments), credit)
            findings.append(
                {
                    "line_id": credit.line_id,
                    "amount_paise": credit.amount_paise,
                    "all_payments_net_to_this_credit": bool(matched),
                }
            )
        return {
            "credits_examined": len(credits),
            "findings": findings,
            "note": (
                "Computed by the deterministic matcher, not inferred. A true result means "
                "the payments' individually-netted amounts sum exactly to that credit."
            ),
        }

    def registry(self) -> dict[str, Callable[..., dict]]:
        """Tool name to callable. The single source of what `investigate` may call, so the
        prompt, the dispatcher and the tests cannot disagree about the tool list."""
        return {
            "fetch_payment": self.fetch_payment,
            "fetch_ledger_entry": self.fetch_ledger_entry,
            "fetch_bank_lines": self.fetch_bank_lines,
            "fetch_refunds": self.fetch_refunds,
            "compute_expected_amount": self.compute_expected_amount,
            "search_prior_resolutions": self.search_prior_resolutions,
        }


TOOL_NAMES = (
    "fetch_payment",
    "fetch_ledger_entry",
    "fetch_bank_lines",
    "fetch_refunds",
    "compute_expected_amount",
    "search_prior_resolutions",
)
