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


# ---------------------------------------------------------------------------------------
# Ring 2.5 — counterparty knowledge. Not derivable from the case.
#
# Every class above this line can be worked out from the evidence: test the shortfall
# against the statutory rates, sum the netted payments, count the payments on an order. The
# Ring 2 measurements showed the consequence — with tools, the investigation *derives* what
# a precedent would have *told* it, so the corpus's measured contribution fell to one case.
#
# These two cannot be derived at all. The shortfall is a rate no statute produces, or an
# amount withheld from a transaction that is not in front of the agent. The evidence is
# genuinely insufficient, and the only thing that resolves it is having seen that
# counterparty before.
#
# The load-bearing property is **repeat counterparties**: each of these customers appears
# several times with the same convention. The first occurrence cannot be resolved by anyone
# and must escalate to a human; the resolution deposits a precedent naming that customer;
# later occurrences are then resolvable by retrieving it. That is the learning curve the
# spec asks for, and it is not measurable on a dataset where every case is self-contained.

#: Rates no statute produces. Deliberately not 2%, 5% or 10% — a round statutory rate would
#: let the agent guess correctly without ever having seen the customer, which is exactly the
#: derivability that makes the rest of the dataset unable to test the thesis.
NEGOTIATED_RATES = (
    Decimal("0.0325"), Decimal("0.0175"), Decimal("0.0435"), Decimal("0.0265"),
    Decimal("0.0385"), Decimal("0.0215"), Decimal("0.0475"), Decimal("0.0295"),
    Decimal("0.0155"), Decimal("0.0415"),
)

#: Customers with a standing negotiated rebate. Kept small and repeated so a precedent
#: deposited on one occurrence is worth retrieving on the next.
REBATE_CUSTOMERS = (
    "Kavery Textiles", "Sundaram Auto Parts", "Vindhya Chemicals", "Konark Logistics",
    "Palghat Ceramics", "Aravalli Cement", "Cauvery Granites", "Marwar Packaging",
    "Zuari Agrochem", "Hampi Ironworks",
)

#: Customers who hold advances against future invoices.
ADVANCE_CUSTOMERS = (
    "Deccan Foods", "Nilgiri Exports", "Chenab Steel", "Beas Papers",
    "Coromandel Spices", "Satpura Timber", "Gomti Dairy", "Rann Salt",
)


def build_negotiated_rebate(
    rng: random.Random, scenario_id: str, order_id: str, invoice_amount_paise: int,
    customer_index: int, fee_rate: Decimal, tax_rate: Decimal,
) -> Scenario:
    """A customer deducts a rebate negotiated with them, at a rate no statute produces.

    Indistinguishable from a withholding case except that the rate is wrong for every
    statutory band — so an agent reasoning from the evidence alone can see *that* something
    was deducted but has no way to establish *what*, or whether it was authorised. The
    correct resolution depends entirely on knowing this customer's terms.
    """
    customer = REBATE_CUSTOMERS[customer_index % len(REBATE_CUSTOMERS)]
    rate = NEGOTIATED_RATES[customer_index % len(NEGOTIATED_RATES)]
    occurrence = customer_index // len(REBATE_CUSTOMERS) + 1
    net_amount_paise = net_of_tds_paise(invoice_amount_paise, rate)
    payment = generate_synthetic_payment(rng, order_id, net_amount_paise, fee_rate, tax_rate)
    bank_line = generate_bank_line_for_payment(payment, rng, lag_days=(1, 2))
    ledger_entry = LedgerEntryRecord(
        entry_id=random_id(rng, "led"), order_id=order_id,
        expected_amount_paise=invoice_amount_paise,
        invoice_no=f"INV-{rng.randint(1000, 9999)}", customer_name=customer, terms="net_30",
    )
    return Scenario(
        scenario_id=scenario_id, kind="negotiated_rebate", is_exception=True,
        payments=[payment], bank_lines=[bank_line], ledger_entries=[ledger_entry],
        expected_reason_code=ReasonCode.NEGOTIATED_REBATE.value,
        counterparty=customer, occurrence_index=occurrence,
        notes=(
            f"{customer} deducted their negotiated {rate:.2%} rebate. Not a statutory rate; "
            f"resolvable only from prior knowledge of this customer's terms."
        ),
    )


#: Rates a proportional-deduction hypothesis would test. An advance must not resemble one.
_ROUND_RATES = (
    Decimal("0.01"), Decimal("0.02"), Decimal("0.05"),
    Decimal("0.075"), Decimal("0.10"), Decimal("0.20"),
)
_RATE_TOLERANCE = Decimal("0.004")


def _advance_clear_of_round_rates(advance_paise: int, invoice_paise: int) -> int:
    """Nudge an advance until it is not mistakable for a percentage deduction.

    Steps in whole rupees so the result still looks like an advance rather than a computed
    figure, and walks upward deterministically so the dataset stays reproducible.
    """
    for step in range(0, 40):
        candidate = advance_paise + step * 500
        if candidate >= invoice_paise:
            break
        rate = Decimal(candidate) / Decimal(invoice_paise)
        if all(abs(rate - r) > _RATE_TOLERANCE for r in _ROUND_RATES):
            return candidate
    return advance_paise


def build_advance_adjusted(
    rng: random.Random, scenario_id: str, order_id: str, invoice_amount_paise: int,
    customer_index: int, fee_rate: Decimal, tax_rate: Decimal,
) -> Scenario:
    """The customer nets an advance they already paid against this invoice.

    The advance belongs to an earlier transaction that is not among this case's records, so
    the shortfall is an arbitrary absolute amount with no proportional structure and nothing
    in the case to explain it. It looks exactly like a netted refund, and the only thing
    that distinguishes them is knowing this customer carries an advance.
    """
    customer = ADVANCE_CUSTOMERS[customer_index % len(ADVANCE_CUSTOMERS)]
    occurrence = customer_index // len(ADVANCE_CUSTOMERS) + 1
    # A round rupee figure, as an advance would be — and deliberately *not* a proportion of
    # the invoice, so no rate hypothesis can reproduce it.
    #
    # The customer's standing advance is fixed but the invoice is drawn at random, so the
    # ratio between them is not: four cases in an earlier generation landed within a
    # rounding tolerance of 7.5% or 10%, which would have made them solvable by testing
    # rates and quietly defeated the entire point of the class. Caught by the invariant
    # test, not by inspection. The advance is now nudged in whole-rupee steps until it is
    # clear of every round rate — deterministic, and it keeps the round-sum character.
    advance_paise = (
        25_000, 50_000, 75_000, 40_000, 60_000, 35_000, 90_000, 55_000
    )[customer_index % 8]
    advance_paise = _advance_clear_of_round_rates(advance_paise, invoice_amount_paise)
    paid_paise = invoice_amount_paise - advance_paise
    payment = generate_synthetic_payment(rng, order_id, paid_paise, fee_rate, tax_rate)
    bank_line = generate_bank_line_for_payment(payment, rng, lag_days=(1, 2))
    ledger_entry = LedgerEntryRecord(
        entry_id=random_id(rng, "led"), order_id=order_id,
        expected_amount_paise=invoice_amount_paise,
        invoice_no=f"INV-{rng.randint(1000, 9999)}", customer_name=customer, terms="net_30",
    )
    return Scenario(
        scenario_id=scenario_id, kind="advance_adjusted", is_exception=True,
        payments=[payment], bank_lines=[bank_line], ledger_entries=[ledger_entry],
        expected_reason_code=ReasonCode.ADVANCE_ADJUSTED.value,
        counterparty=customer, occurrence_index=occurrence,
        notes=(
            f"{customer} netted a {advance_paise} paise advance paid earlier against this "
            f"invoice. The advance is not among this case's records."
        ),
    )
