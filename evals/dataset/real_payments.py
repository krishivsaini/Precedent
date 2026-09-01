"""Loads the committed real-payment fixture (spec §2.3: Payments are real Razorpay
test-mode API calls). 21 payments collected via actual test-mode checkouts on
2026-08-31/09-01 — see `real_payments.json`. Committed so the eval dataset is
reproducible from a clone with no live Razorpay access required (NFR-3).
"""

import json
from pathlib import Path

from precedent.adapters.storage.records import PaymentRecord

_FIXTURE_PATH = Path(__file__).parent / "real_payments.json"


def load_real_payments() -> list[PaymentRecord]:
    with open(_FIXTURE_PATH) as f:
        rows = json.load(f)
    return [PaymentRecord(**row) for row in rows]
