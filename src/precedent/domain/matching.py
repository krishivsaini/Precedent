"""Deterministic three-way reconciliation matching (Ring 0 baseline).

Pure, zero I/O: callers supply already-fetched `Payment` / `BankLine` / `LedgerEntry`
value objects, this module never touches storage or the network. `match_batch` is the
Ring 0 baseline itself — everything it cannot confidently resolve becomes an
`UnmatchedException` bound for the investigation agent in later rings.

`is_netted_group_match` is a pure checker over a *given* candidate group of payments; it
does not discover which payments belong together. Automatic settlement grouping needs
real settlement/grouping data and belongs to a usecase in a later ring, not this module.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Sequence

from precedent.domain.money import ROUNDING_TOLERANCE_PAISE, within_tolerance
from precedent.domain.reasons import ReasonCode

# Typical settlement lag is T+2; a match requiring more than that but within T+5 is
# still valid, but is worth distinguishing (DATE_WINDOW_TIMING) from a same-window
# EXACT_MATCH for telemetry on how often settlement is running late.
NARROW_WINDOW_DAYS = 2
WIDE_WINDOW_DAYS = 5


@dataclass(frozen=True)
class Payment:
    payment_id: str
    order_id: str
    amount_paise: int
    captured_at: datetime
    fee_paise: int = 0
    tax_paise: int = 0


@dataclass(frozen=True)
class BankLine:
    line_id: str
    value_date: date
    amount_paise: int
    direction: str  # "credit" | "debit"
    narration: str = ""


@dataclass(frozen=True)
class LedgerEntry:
    entry_id: str
    order_id: str
    expected_amount_paise: int
    invoice_no: str = ""
    customer_name: str = ""


@dataclass(frozen=True)
class Match:
    reason_code: ReasonCode
    payment_ids: tuple[str, ...]
    bank_line_id: str
    ledger_entry_id: str
    matched_amount_paise: int


@dataclass(frozen=True)
class UnmatchedException:
    reason_code: ReasonCode
    payment_ids: tuple[str, ...] = ()
    bank_line_ids: tuple[str, ...] = ()
    ledger_entry_ids: tuple[str, ...] = ()
    detail: str = ""


@dataclass(frozen=True)
class MatchBatchResult:
    matches: list[Match] = field(default_factory=list)
    exceptions: list[UnmatchedException] = field(default_factory=list)


def _net_amount_paise(payment: Payment) -> int:
    """What actually hits the bank for one payment: gross, minus fee, minus tax."""
    return payment.amount_paise - payment.fee_paise - payment.tax_paise


def is_exact_match(payment: Payment, bank_line: BankLine, ledger_entry: LedgerEntry) -> bool:
    """The bank credit equals the payment net of fee/tax, and the ledger's expected
    amount equals the payment's gross value — the bank never sees the gross figure, and
    the ledger never sees the fee deduction, so these are two different comparisons, not
    one three-way equality."""
    return (
        bank_line.direction == "credit"
        and ledger_entry.order_id == payment.order_id
        and bank_line.amount_paise == _net_amount_paise(payment)
        and ledger_entry.expected_amount_paise == payment.amount_paise
    )


def is_tolerance_match(
    payment: Payment,
    bank_line: BankLine,
    ledger_entry: LedgerEntry,
    tolerance_paise: int = ROUNDING_TOLERANCE_PAISE,
) -> bool:
    """Same comparison as `is_exact_match`, but within a rounding-delta tolerance rather
    than exactly (spec §5)."""
    return (
        bank_line.direction == "credit"
        and ledger_entry.order_id == payment.order_id
        and within_tolerance(bank_line.amount_paise, _net_amount_paise(payment), tolerance_paise)
        and within_tolerance(ledger_entry.expected_amount_paise, payment.amount_paise, tolerance_paise)
    )


def is_within_date_window(payment: Payment, bank_line: BankLine, window_days: int) -> bool:
    """Whether the bank credit landed within `window_days` of the payment capture date."""
    delta_days = abs((bank_line.value_date - payment.captured_at.date()).days)
    return delta_days <= window_days


def net_settlement_amount_paise(payments: Sequence[Payment]) -> int:
    """Sum of payment amounts net of fees and tax — what should actually hit the bank."""
    return sum(_net_amount_paise(p) for p in payments)


def is_netted_group_match(
    payments: Sequence[Payment],
    bank_line: BankLine,
    tolerance_paise: int = ROUNDING_TOLERANCE_PAISE,
) -> bool:
    """Whether `payments`, netted of fees/tax, account for a single settlement credit."""
    return bank_line.direction == "credit" and within_tolerance(
        net_settlement_amount_paise(payments), bank_line.amount_paise, tolerance_paise
    )


def _find_single_payment_match(
    payment: Payment, ledger_entry: LedgerEntry, candidate_lines: Sequence[BankLine]
) -> tuple[BankLine, ReasonCode] | None:
    """Try, in priority order, to pair one payment with one of `candidate_lines`."""
    for line in candidate_lines:
        if is_exact_match(payment, line, ledger_entry) and is_within_date_window(
            payment, line, NARROW_WINDOW_DAYS
        ):
            return line, ReasonCode.EXACT_MATCH
    for line in candidate_lines:
        if is_exact_match(payment, line, ledger_entry) and is_within_date_window(
            payment, line, WIDE_WINDOW_DAYS
        ):
            return line, ReasonCode.DATE_WINDOW_TIMING
    for line in candidate_lines:
        if is_tolerance_match(payment, line, ledger_entry) and is_within_date_window(
            payment, line, NARROW_WINDOW_DAYS
        ):
            return line, ReasonCode.TOLERANCE_ROUNDING
    return None


def match_batch(
    payments: Sequence[Payment],
    bank_lines: Sequence[BankLine],
    ledger_entries: Sequence[LedgerEntry],
) -> MatchBatchResult:
    """The Ring 0 deterministic matcher: clears the confident majority, escalates the rest.

    One payment, one bank line, and one ledger entry are each consumed by at most one
    match. When more than one payment lands on the same order (a duplicate customer
    payment), only the earliest is eligible to match — the rest are rejected outright
    (FR-2.5) rather than being matched against a second, nonexistent counterpart.

    Everything left over becomes an `UnmatchedException`, not a silent gap (FR-2.6): an
    orphaned payment, a ledger entry with no payment behind it (e.g. a direct NEFT
    transfer that bypassed the PSP entirely), and a credit line nobody claimed are all
    escalated the same way.
    """
    matches: list[Match] = []
    exceptions: list[UnmatchedException] = []

    payments_by_order: dict[str, list[Payment]] = defaultdict(list)
    for payment in payments:
        payments_by_order[payment.order_id].append(payment)

    credit_lines = [line for line in bank_lines if line.direction == "credit"]
    consumed_bank_line_ids: set[str] = set()
    unresolved_payments: dict[str, Payment] = {p.payment_id: p for p in payments}

    for ledger_entry in ledger_entries:
        candidates = payments_by_order.get(ledger_entry.order_id, [])
        if not candidates:
            # No payment ever came in through the PSP for this order — e.g. a direct
            # NEFT transfer bypassing Razorpay entirely (spec §5). FR-2.6: this must be
            # escalated, not silently skipped just because there's no payment to anchor on.
            exceptions.append(
                UnmatchedException(
                    reason_code=ReasonCode.UNMATCHABLE_NO_COUNTERPART,
                    ledger_entry_ids=(ledger_entry.entry_id,),
                    detail=f"no payment found for order {ledger_entry.order_id}",
                )
            )
            continue

        ordered_candidates = sorted(candidates, key=lambda p: p.captured_at)
        primary, duplicates = ordered_candidates[0], ordered_candidates[1:]

        available_lines = [
            line for line in credit_lines if line.line_id not in consumed_bank_line_ids
        ]
        found = _find_single_payment_match(primary, ledger_entry, available_lines)
        if found is not None:
            line, reason_code = found
            matches.append(
                Match(
                    reason_code=reason_code,
                    payment_ids=(primary.payment_id,),
                    bank_line_id=line.line_id,
                    ledger_entry_id=ledger_entry.entry_id,
                    matched_amount_paise=line.amount_paise,
                )
            )
            consumed_bank_line_ids.add(line.line_id)
            unresolved_payments.pop(primary.payment_id, None)

        for duplicate in duplicates:
            exceptions.append(
                UnmatchedException(
                    reason_code=ReasonCode.DUPLICATE_PAYMENT_REJECTED,
                    payment_ids=(duplicate.payment_id,),
                    ledger_entry_ids=(ledger_entry.entry_id,),
                    detail=(
                        f"order {ledger_entry.order_id} already claimed by "
                        f"payment {primary.payment_id}"
                    ),
                )
            )
            unresolved_payments.pop(duplicate.payment_id, None)

    for payment_id in unresolved_payments:
        exceptions.append(
            UnmatchedException(
                reason_code=ReasonCode.UNMATCHABLE_NO_COUNTERPART,
                payment_ids=(payment_id,),
            )
        )

    # A credit nobody claimed is just as much an unresolved case as an orphaned payment
    # or ledger entry (FR-2.6) — e.g. the bank-line side of a garbled-narration NEFT.
    for line in credit_lines:
        if line.line_id not in consumed_bank_line_ids:
            exceptions.append(
                UnmatchedException(
                    reason_code=ReasonCode.UNMATCHABLE_NO_COUNTERPART,
                    bank_line_ids=(line.line_id,),
                    detail="credit line has no claiming payment/ledger entry",
                )
            )

    return MatchBatchResult(matches=matches, exceptions=exceptions)
