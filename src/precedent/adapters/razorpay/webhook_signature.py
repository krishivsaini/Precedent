"""Razorpay webhook signature verification.

Per Razorpay's docs: HMAC-SHA256, keyed by the webhook secret, over the raw (unparsed)
request body — never the parsed/re-serialized JSON, which can byte-for-byte differ from
what was signed. Comparison is constant-time to avoid a timing side-channel on the check
itself.
"""

import hashlib
import hmac


def compute_signature(raw_body: bytes, webhook_secret: str) -> str:
    return hmac.new(webhook_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()


def verify_webhook_signature(raw_body: bytes, signature: str, webhook_secret: str) -> bool:
    expected = compute_signature(raw_body, webhook_secret)
    return hmac.compare_digest(expected, signature)
