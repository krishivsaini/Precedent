"""Bank-line and ledger-entry generators (Ring 0.4, spec §9).

Pure with respect to randomness: every function takes a `random.Random` instance the
caller controls, never touches the module-level `random` — that is what makes the
downstream exception dataset generator (Ring 0.5) reproducible from a fixed seed
(NFR-3). IDs are derived from the same `rng` rather than `uuid4()`, so a fixed seed
reproduces identical IDs across runs, not just identical amounts and dates.

These generators don't care whether a `PaymentRecord` is real (`source='razorpay'`) or
synthetic — they just net out its fee/tax and place it in time. What's real vs.
synthetic about a payment is decided by whoever constructs the `PaymentRecord`, not here.
"""

import random
from datetime import datetime, timedelta
from typing import Sequence

from evals.dataset.ids import random_id as _random_id
from precedent.adapters.storage.records import BankLineRecord, LedgerEntryRecord, PaymentRecord
from precedent.domain.matching import net_settlement_amount_paise

FAKE_CUSTOMERS = [
    "Acme Retail Pvt Ltd", "Bluepeak Traders", "Coral Textiles", "Dhanashree Foods",
    "Everest Electronics", "Falcon Logistics", "Greenfield Agro", "Horizon Apparel",
    "Indus Home Goods", "Jaya Enterprises", "Kalyan Hardware", "Lotus Stationery",
    "Meridian Furnishings", "Nirvana Wellness", "Orbit Auto Parts", "Prakash Books",
    "Quartz Digital", "Ravi Sports Gear", "Sunrise Bakery", "Trident Tools",
]

DEFAULT_LAG_DAYS = (1, 3)


def generate_bank_line_for_payment(
    payment: PaymentRecord,
    rng: random.Random,
    lag_days: tuple[int, int] = DEFAULT_LAG_DAYS,
) -> BankLineRecord:
    """One bank credit line for a single payment, net of its fee and tax.

    `lag_days` is the (min, max) settlement lag in days after `captured_at`, inclusive —
    callers construct scenarios by choosing a lag range: within `matching.NARROW_WINDOW_DAYS`
    for a clean exact match, beyond it (but within `WIDE_WINDOW_DAYS`) for a date-window case.
    """
    net_amount_paise = payment.amount_paise - payment.fee_paise - payment.tax_paise
    lag = rng.randint(*lag_days)
    captured_date = datetime.fromisoformat(payment.captured_at).date()
    value_date = (captured_date + timedelta(days=lag)).isoformat()
    return BankLineRecord(
        line_id=_random_id(rng, "line"),
        value_date=value_date,
        amount_paise=net_amount_paise,
        direction="credit",
        narration=f"NEFT-CR {payment.order_id}",
        source="synthetic",
    )


def generate_netted_bank_line(
    payments: Sequence[PaymentRecord],
    rng: random.Random,
    lag_days: tuple[int, int] = DEFAULT_LAG_DAYS,
) -> BankLineRecord:
    """One bank credit line netting several payments into a single settlement batch."""
    if not payments:
        raise ValueError("payments must be non-empty")
    net_amount_paise = net_settlement_amount_paise(payments)
    latest_captured_date = max(
        datetime.fromisoformat(p.captured_at).date() for p in payments
    )
    lag = rng.randint(*lag_days)
    value_date = (latest_captured_date + timedelta(days=lag)).isoformat()
    return BankLineRecord(
        line_id=_random_id(rng, "line"),
        value_date=value_date,
        amount_paise=net_amount_paise,
        direction="credit",
        narration=f"NEFT-CR SETTLEMENT batch/{len(payments)}",
        source="synthetic",
    )


def generate_ledger_entry_for_payment(
    payment: PaymentRecord,
    rng: random.Random,
    expected_amount_paise: int | None = None,
) -> LedgerEntryRecord:
    """A ledger entry for `payment`'s order.

    `expected_amount_paise` defaults to the payment's own gross amount (the clean-match
    case); callers pass a different value to construct a deliberate mismatch (TDS
    short-payment, rounding delta, split payment, ...).
    """
    return LedgerEntryRecord(
        entry_id=_random_id(rng, "led"),
        order_id=payment.order_id,
        expected_amount_paise=(
            expected_amount_paise if expected_amount_paise is not None else payment.amount_paise
        ),
        invoice_no=f"INV-{rng.randint(1000, 9999)}",
        customer_name=rng.choice(FAKE_CUSTOMERS),
        terms="net_15",
    )
