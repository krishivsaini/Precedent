"""One builder per exception class in spec §5. Each returns a `Scenario` — the raw
payments/bank_lines/ledger_entries plus a gold label. Two classes (`clean_match`,
`rounding_delta`) are deliberately `is_exception=False`: the Ring 0.1 deterministic
matcher already resolves both on its own (exact match and tolerance-band match
respectively) — see `generate.py`'s module docstring for why this departs from the
spec's literal "~130 exceptions" arithmetic.

Every builder takes an explicit `rng` and, where relevant, an explicit `payment` (to
let the caller anchor a scenario on a *real* payment instead of a synthetic one) —
nothing in here reaches for global randomness or wall-clock time.
"""

import random
from datetime import date, datetime, timedelta
from decimal import Decimal

from evals.dataset.generators import (
    FAKE_CUSTOMERS,
    generate_bank_line_for_payment,
    generate_ledger_entry_for_payment,
    generate_netted_bank_line,
)
from evals.dataset.ids import random_id
from evals.dataset.scenario import Scenario
from evals.dataset.synthetic_payments import generate_synthetic_payment
from precedent.adapters.storage.records import BankLineRecord, LedgerEntryRecord, PaymentRecord
from precedent.domain.money import net_of_tds_paise
from precedent.domain.reasons import ReasonCode


def build_clean_match(rng: random.Random, scenario_id: str, payment: PaymentRecord) -> Scenario:
    bank_line = generate_bank_line_for_payment(payment, rng, lag_days=(1, 2))
    ledger_entry = generate_ledger_entry_for_payment(payment, rng)
    return Scenario(
        scenario_id=scenario_id, kind="clean_match", is_exception=False,
        payments=[payment], bank_lines=[bank_line], ledger_entries=[ledger_entry],
        expected_reason_code=ReasonCode.EXACT_MATCH.value,
        notes="Amount, order, and settlement date all agree exactly.",
        uses_real_payment=(payment.source == "razorpay"),
    )


def build_rounding_delta(rng: random.Random, scenario_id: str, payment: PaymentRecord) -> Scenario:
    """Ledger's expected amount is off from the payment by a few paise — within the
    ±₹1 tolerance band, still auto-resolved by the deterministic matcher's tolerance tier."""
    delta_paise = rng.choice([-97, -55, -23, 1, 40, 88, 100])
    bank_line = generate_bank_line_for_payment(payment, rng, lag_days=(1, 2))
    expected = payment.amount_paise + delta_paise
    ledger_entry = generate_ledger_entry_for_payment(payment, rng, expected_amount_paise=expected)
    return Scenario(
        scenario_id=scenario_id, kind="rounding_delta", is_exception=False,
        payments=[payment], bank_lines=[bank_line], ledger_entries=[ledger_entry],
        expected_reason_code=ReasonCode.TOLERANCE_ROUNDING.value,
        notes=f"Ledger expected amount differs from payment by {delta_paise} paise.",
        uses_real_payment=(payment.source == "razorpay"),
    )


def build_netted_settlement(
    rng: random.Random, scenario_id: str, payments: list[PaymentRecord]
) -> Scenario:
    """Several payments (distinct orders) net into a single settlement credit."""
    if len(payments) < 2:
        raise ValueError("netted_settlement needs at least 2 payments")
    bank_line = generate_netted_bank_line(payments, rng, lag_days=(1, 2))
    ledger_entries = [generate_ledger_entry_for_payment(p, rng) for p in payments]
    return Scenario(
        scenario_id=scenario_id, kind="netted_settlement", is_exception=True,
        payments=payments, bank_lines=[bank_line], ledger_entries=ledger_entries,
        expected_reason_code=ReasonCode.NETTED_SETTLEMENT.value,
        notes=f"{len(payments)} payments netted (minus fees/tax) into one settlement credit.",
        uses_real_payment=any(p.source == "razorpay" for p in payments),
    )


def build_direct_neft_bypass(
    rng: random.Random, scenario_id: str, order_id: str, amount_paise: int, customer_name: str,
    value_date: date,
) -> Scenario:
    """A real bank transfer that bypassed the PSP entirely — no payment record exists."""
    ledger_entry = LedgerEntryRecord(
        entry_id=random_id(rng, "led"), order_id=order_id, expected_amount_paise=amount_paise,
        invoice_no=f"INV-{rng.randint(1000, 9999)}", customer_name=customer_name, terms="net_15",
    )
    garbled_narration = f"NEFT/{customer_name[:6].upper().replace(' ', '')}{rng.randint(100, 999)}/XFER"
    bank_line = BankLineRecord(
        line_id=random_id(rng, "line"), value_date=value_date.isoformat(),
        amount_paise=amount_paise, direction="credit", narration=garbled_narration,
        source="synthetic",
    )
    return Scenario(
        scenario_id=scenario_id, kind="direct_neft_bypass", is_exception=True,
        payments=[], bank_lines=[bank_line], ledger_entries=[ledger_entry],
        expected_reason_code=ReasonCode.DIRECT_NEFT_BYPASS.value,
        notes="Direct NEFT credit, garbled narration, no PSP payment record behind it.",
    )


def build_tds_short_payment(
    rng: random.Random, scenario_id: str, order_id: str, invoice_amount_paise: int,
    tds_rate: Decimal, fee_rate: Decimal, tax_rate: Decimal,
    payment: PaymentRecord | None = None,
) -> Scenario:
    """Customer deducted TDS before paying — the payment (and its bank credit) fall
    short of the invoice by exactly `tds_rate`."""
    if payment is None:
        net_amount_paise = net_of_tds_paise(invoice_amount_paise, tds_rate)
        payment = generate_synthetic_payment(rng, order_id, net_amount_paise, fee_rate, tax_rate)
    else:
        order_id = payment.order_id
    bank_line = generate_bank_line_for_payment(payment, rng, lag_days=(1, 2))
    ledger_entry = generate_ledger_entry_for_payment(
        payment, rng, expected_amount_paise=invoice_amount_paise
    )
    return Scenario(
        scenario_id=scenario_id, kind="tds_short_payment", is_exception=True,
        payments=[payment], bank_lines=[bank_line], ledger_entries=[ledger_entry],
        expected_reason_code=ReasonCode.TDS_SHORT_PAYMENT.value,
        notes=f"{tds_rate:.0%} TDS deducted before payment; short by that amount vs. invoice.",
        uses_real_payment=(payment.source == "razorpay"),
    )


def build_split_payment(
    rng: random.Random, scenario_id: str, order_id: str, share_a_paise: int, share_b_paise: int,
    fee_rate: Decimal, tax_rate: Decimal,
) -> Scenario:
    """One payment/credit covers two separate invoices."""
    total_paise = share_a_paise + share_b_paise
    payment = generate_synthetic_payment(rng, order_id, total_paise, fee_rate, tax_rate)
    bank_line = generate_bank_line_for_payment(payment, rng, lag_days=(1, 2))
    customer_name = rng.choice(FAKE_CUSTOMERS)
    ledger_a = LedgerEntryRecord(
        entry_id=random_id(rng, "led"), order_id=order_id, expected_amount_paise=share_a_paise,
        invoice_no=f"INV-{rng.randint(1000, 9999)}", customer_name=customer_name, terms="net_15",
    )
    ledger_b = LedgerEntryRecord(
        entry_id=random_id(rng, "led"), order_id=order_id, expected_amount_paise=share_b_paise,
        invoice_no=f"INV-{rng.randint(1000, 9999)}", customer_name=customer_name, terms="net_15",
    )
    return Scenario(
        scenario_id=scenario_id, kind="split_payment", is_exception=True,
        payments=[payment], bank_lines=[bank_line], ledger_entries=[ledger_a, ledger_b],
        expected_reason_code=ReasonCode.SPLIT_PAYMENT.value,
        notes="One payment/credit split across two invoices for the same order.",
    )


def build_refund_netted(
    rng: random.Random, scenario_id: str, order_id: str, gross_amount_paise: int,
    refund_amount_paise: int, fee_rate: Decimal, tax_rate: Decimal,
    payment: PaymentRecord | None = None,
) -> Scenario:
    """A same-day refund reduces the settlement credit below what the ledger expects."""
    if payment is None:
        payment = generate_synthetic_payment(rng, order_id, gross_amount_paise, fee_rate, tax_rate)
    else:
        order_id = payment.order_id
    if refund_amount_paise >= payment.amount_paise - payment.fee_paise - payment.tax_paise:
        raise ValueError("refund_amount_paise must be less than the net settlement amount")
    net_of_refund_paise = (
        payment.amount_paise - payment.fee_paise - payment.tax_paise - refund_amount_paise
    )
    captured_date = datetime.fromisoformat(payment.captured_at).date()
    bank_line = BankLineRecord(
        line_id=random_id(rng, "line"), value_date=(captured_date + timedelta(days=2)).isoformat(),
        amount_paise=net_of_refund_paise, direction="credit",
        narration=f"NEFT-CR {order_id} (net of same-day refund)", source="synthetic",
    )
    ledger_entry = generate_ledger_entry_for_payment(payment, rng)  # unaware of the refund
    return Scenario(
        scenario_id=scenario_id, kind="refund_netted", is_exception=True,
        payments=[payment], bank_lines=[bank_line], ledger_entries=[ledger_entry],
        expected_reason_code=ReasonCode.REFUND_NETTED.value,
        notes=f"Same-day refund of {refund_amount_paise} paise netted into the settlement credit.",
        uses_real_payment=(payment.source == "razorpay"),
    )


def build_duplicate_payment(
    rng: random.Random, scenario_id: str, order_id: str, amount_paise: int,
    fee_rate: Decimal, tax_rate: Decimal, payment: PaymentRecord | None = None,
) -> Scenario:
    """Customer pays the same order twice; only the earlier payment may match."""
    if payment is None:
        payment_a = generate_synthetic_payment(rng, order_id, amount_paise, fee_rate, tax_rate)
    else:
        payment_a = payment
        order_id = payment.order_id
    captured_a = datetime.fromisoformat(payment_a.captured_at)
    payment_b = generate_synthetic_payment(
        rng, order_id, amount_paise, fee_rate, tax_rate,
        captured_at=(captured_a + timedelta(minutes=7)).isoformat(),
    )
    bank_line = generate_bank_line_for_payment(payment_a, rng, lag_days=(1, 2))
    ledger_entry = generate_ledger_entry_for_payment(payment_a, rng)
    return Scenario(
        scenario_id=scenario_id, kind="duplicate_payment", is_exception=True,
        payments=[payment_a, payment_b], bank_lines=[bank_line], ledger_entries=[ledger_entry],
        expected_reason_code=ReasonCode.DUPLICATE_PAYMENT_REJECTED.value,
        notes="Two payments landed on the same order; only the earlier one may match.",
        uses_real_payment=(payment_a.source == "razorpay"),
    )


def build_unmatchable(
    rng: random.Random, scenario_id: str, order_id: str, amount_paise: int,
    fee_rate: Decimal, tax_rate: Decimal, payment: PaymentRecord | None = None,
) -> Scenario:
    """A payment with genuinely no counterpart — no ledger entry, no bank credit."""
    if payment is None:
        payment = generate_synthetic_payment(rng, order_id, amount_paise, fee_rate, tax_rate)
    return Scenario(
        scenario_id=scenario_id, kind="unmatchable", is_exception=True,
        payments=[payment], bank_lines=[], ledger_entries=[],
        expected_reason_code=ReasonCode.UNMATCHABLE_NO_COUNTERPART.value,
        notes="No ledger entry or bank line exists for this payment — no valid counterpart.",
        uses_real_payment=(payment.source == "razorpay"),
    )
