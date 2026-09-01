"""Synthetic payment generation, calibrated off the 21 real payments in
`real_payments.json` — fee and tax rates are measured from real data, not guessed
(spec: bank lines and ledger are "synthetic, generated from real payment data"; the same
discipline applies to any synthetic payment volume needed to reach eval-dataset scale).
"""

import random
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Sequence

from evals.dataset.ids import random_id as _random_id
from precedent.adapters.storage.records import PaymentRecord
from precedent.domain.money import apply_rate_paise

# Fixed so the dataset is reproducible regardless of when the generator actually runs —
# never datetime.now(). Chosen to sit just after the real payment collection window.
REFERENCE_DATE = date(2026, 9, 1)
CAPTURED_AT_SPREAD_DAYS = 75

AMOUNT_BUCKETS_PAISE = [
    (9_900, 49_900),      # ₹99 - ₹499
    (49_900, 199_900),    # ₹499 - ₹1,999
    (199_900, 499_900),   # ₹1,999 - ₹4,999
]


def calibrate_fee_model(real_payments: Sequence[PaymentRecord]) -> tuple[Decimal, Decimal]:
    """(mean fee-rate, mean tax-on-fee-rate) measured from real captured payments.

    Returned as `Decimal`, not `float`: these rates are fed straight back into paise
    amounts, so letting them be floats would reintroduce exactly the NFR-1 leak that
    `money.apply_rate_paise` refuses.
    """
    fee_rates = [
        Decimal(p.fee_paise) / Decimal(p.amount_paise)
        for p in real_payments
        if p.amount_paise > 0
    ]
    tax_rates = [
        Decimal(p.tax_paise) / Decimal(p.fee_paise) for p in real_payments if p.fee_paise > 0
    ]
    if not fee_rates:
        raise ValueError("real_payments must contain at least one payment with amount_paise > 0")
    mean_fee_rate = sum(fee_rates) / Decimal(len(fee_rates))
    mean_tax_rate = sum(tax_rates) / Decimal(len(tax_rates)) if tax_rates else Decimal(0)
    return mean_fee_rate, mean_tax_rate


def random_amount_paise(rng: random.Random) -> int:
    lo, hi = rng.choice(AMOUNT_BUCKETS_PAISE)
    rupees = rng.randint(lo // 100, hi // 100)
    return rupees * 100


def random_captured_at(rng: random.Random) -> str:
    offset_days = rng.randint(0, CAPTURED_AT_SPREAD_DAYS)
    offset_seconds = rng.randint(0, 24 * 3600 - 1)
    day = REFERENCE_DATE - timedelta(days=offset_days)
    moment = datetime.combine(day, time()) + timedelta(seconds=offset_seconds)
    return moment.isoformat() + "+00:00"


def generate_synthetic_payment(
    rng: random.Random,
    order_id: str,
    amount_paise: int,
    fee_rate: Decimal,
    tax_rate: Decimal,
    captured_at: str | None = None,
) -> PaymentRecord:
    fee_paise = apply_rate_paise(amount_paise, fee_rate)
    tax_paise = apply_rate_paise(fee_paise, tax_rate)
    return PaymentRecord(
        payment_id=_random_id(rng, "pay"),
        order_id=order_id,
        amount_paise=amount_paise,
        fee_paise=fee_paise,
        tax_paise=tax_paise,
        captured_at=captured_at or random_captured_at(rng),
        status="captured",
        source="synthetic",
    )
