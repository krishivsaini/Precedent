"""Thin wrapper over the official `razorpay` Python SDK.

Only order creation and payment fetch — both stable, unambiguous REST endpoints
(`POST /v1/orders`, `GET /v1/payments/{id}`) that the SDK targets correctly. Refund
*creation* is deliberately not here: it belongs to Ring 5 remediation
(`usecases/remediate.py`), and its exact idempotency-header wiring through the SDK needs
verifying against a live account before being implemented, not guessed at now.
"""

import razorpay

from precedent.config import RazorpayConfig


class RazorpayClient:
    def __init__(self, config: RazorpayConfig):
        self._client = razorpay.Client(auth=(config.key_id, config.key_secret))

    def create_order(self, amount_paise: int, receipt: str, notes: dict | None = None) -> dict:
        """`receipt` should be unique per logical order — self-enforced idempotency
        (spec §4: Orders/Payments have no native idempotency header) relies on the caller
        supplying the same receipt on a retried request and checking the `idempotency`
        table before calling this, not on anything this method does itself."""
        if amount_paise <= 0:
            raise ValueError("amount_paise must be positive")
        return self._client.order.create(
            {
                "amount": amount_paise,
                "currency": "INR",
                "receipt": receipt,
                "notes": notes or {},
            }
        )

    def fetch_payment(self, payment_id: str) -> dict:
        return self._client.payment.fetch(payment_id)
