"""Fetch a real Razorpay test-mode payment by ID and store it in the local DB.

    uv run python scripts/ingest_test_payment.py <payment_id> [--db precedent.db]

Manual, one-off tool for seeding real payment data during Ring 0 — this is not the
`usecases/ingest.py` webhook-driven ingestion path the spec describes; that comes later.
"""

import argparse
import sys
from datetime import datetime, timezone

from precedent.adapters.razorpay.client import RazorpayClient
from precedent.adapters.storage.db import connect, init_db, transaction
from precedent.adapters.storage.records import PaymentRecord
from precedent.adapters.storage.repositories import PaymentsRepository
from precedent.config import razorpay_config


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("payment_id")
    parser.add_argument("--db", default="precedent.db")
    args = parser.parse_args()

    client = RazorpayClient(razorpay_config())
    payment = client.fetch_payment(args.payment_id)

    print("Fetched payment:")
    for key in ("id", "order_id", "amount", "currency", "status", "method", "fee", "tax", "created_at"):
        print(f"  {key}: {payment.get(key)!r}")

    if payment["status"] != "captured":
        print(f"\nPayment {args.payment_id} is not captured (status={payment['status']!r}); not ingesting.")
        return 1

    record = PaymentRecord(
        payment_id=payment["id"],
        order_id=payment["order_id"],
        amount_paise=payment["amount"],
        fee_paise=payment.get("fee") or 0,
        tax_paise=payment.get("tax") or 0,
        captured_at=datetime.fromtimestamp(payment["created_at"], tz=timezone.utc).isoformat(),
        status=payment["status"],
        source="razorpay",
    )

    conn = connect(args.db)
    init_db(conn)
    with transaction(conn):
        PaymentsRepository(conn).insert(record)
    conn.close()

    print(f"\nIngested into {args.db}: {record.payment_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
