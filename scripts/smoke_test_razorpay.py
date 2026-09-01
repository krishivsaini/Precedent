"""Manual smoke test against the real Razorpay TEST-MODE API.

Not part of the pytest suite on purpose — it makes a real network call and needs live
credentials. Run once you've filled in `.env` from `.env.example`:

    uv run python scripts/smoke_test_razorpay.py
"""

import sys
import uuid

from precedent.adapters.razorpay.client import RazorpayClient
from precedent.config import ConfigError, razorpay_config


def main() -> int:
    try:
        config = razorpay_config()
    except ConfigError as exc:
        print(f"Config error: {exc}")
        return 1

    client = RazorpayClient(config)
    receipt = f"smoke_test_{uuid.uuid4().hex[:12]}"

    try:
        order = client.create_order(amount_paise=100, receipt=receipt)
    except Exception as exc:
        print(f"Order creation failed: {exc}")
        return 1

    print("Order created successfully against Razorpay test mode:")
    print(f"  id:      {order['id']}")
    print(f"  amount:  {order['amount']} paise")
    print(f"  status:  {order['status']}")
    print(f"  receipt: {order['receipt']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
