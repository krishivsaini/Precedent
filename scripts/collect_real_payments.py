"""Local, throwaway server for collecting a batch of real Razorpay test-mode payments.

Serves a page that creates an order, lets you pay it (UPI success@razorpay, instant, no
OTP), records the captured payment straight into the DB, then loads the next order
automatically. Not part of the application — a one-time data-collection tool for
anchoring the Ring 0.4/0.5 synthetic generators against real payment shape (amount,
fee %, timing).

    uv run python scripts/collect_real_payments.py [--target 20] [--db precedent.db]

Then open http://127.0.0.1:8787/ and click through `--target` payments.
"""

import argparse
import random
import uuid
from datetime import datetime, timezone

import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from precedent.adapters.razorpay.client import RazorpayClient
from precedent.adapters.storage.db import connect, init_db, transaction
from precedent.adapters.storage.records import PaymentRecord
from precedent.adapters.storage.repositories import PaymentsRepository
from precedent.config import razorpay_config

# Plausible e-commerce order amounts, in paise: mostly two- and three-figure rupee
# amounts with a long tail up to a few thousand rupees.
AMOUNT_BUCKETS_PAISE = [
    (9_900, 49_900),      # ₹99 - ₹499
    (49_900, 199_900),    # ₹499 - ₹1,999
    (199_900, 499_900),   # ₹1,999 - ₹4,999
]


def random_amount_paise() -> int:
    lo, hi = random.choice(AMOUNT_BUCKETS_PAISE)
    rupees = random.randint(lo // 100, hi // 100)
    return rupees * 100  # whole-rupee amounts, like real order values usually are


class RecordPaymentRequest(BaseModel):
    payment_id: str


def build_app(target: int, db_path: str) -> FastAPI:
    app = FastAPI()
    config = razorpay_config()
    client = RazorpayClient(config)

    conn = connect(db_path)
    init_db(conn)
    payments_repo = PaymentsRepository(conn)

    def collected_count() -> int:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM payments WHERE source = 'razorpay'"
        ).fetchone()
        return row["n"]

    @app.get("/", response_class=HTMLResponse)
    def index():
        return _PAGE_HTML.replace("__KEY_ID__", config.key_id).replace("__TARGET__", str(target))

    @app.post("/create_order")
    def create_order():
        amount = random_amount_paise()
        receipt = f"collect_{uuid.uuid4().hex[:12]}"
        order = client.create_order(amount_paise=amount, receipt=receipt)
        return {"order_id": order["id"], "amount": amount, "collected": collected_count()}

    @app.post("/record_payment")
    def record_payment(body: RecordPaymentRequest):
        payment = client.fetch_payment(body.payment_id)
        if payment["status"] != "captured":
            return {"ok": False, "reason": f"status={payment['status']!r}", "collected": collected_count()}

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
        if payments_repo.get(record.payment_id) is None:
            with transaction(conn):
                payments_repo.insert(record)
        return {"ok": True, "collected": collected_count()}

    return app


_PAGE_HTML = """
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Collecting real test-mode payments</title>
<style>
  body { font-family: -apple-system, sans-serif; max-width: 560px; margin: 60px auto; padding: 0 20px; }
  button { font-size: 16px; padding: 12px 24px; cursor: pointer; }
  #status { color: #555; margin-top: 16px; }
  #count { font-size: 28px; font-weight: bold; }
</style>
</head>
<body>
<h2>Collecting real Razorpay test-mode payments</h2>
<p><span id="count">0</span> / __TARGET__ collected</p>
<button id="pay" disabled>Loading order...</button>
<p id="status"></p>

<script src="https://checkout.razorpay.com/v1/checkout.js"></script>
<script>
let currentOrder = null;

async function loadNextOrder() {
  document.getElementById("pay").disabled = true;
  document.getElementById("pay").textContent = "Loading order...";
  const res = await fetch("/create_order", { method: "POST" });
  const data = await res.json();
  currentOrder = data;
  document.getElementById("count").textContent = data.collected;
  document.getElementById("pay").textContent = "Pay ₹" + (data.amount / 100).toFixed(2);
  document.getElementById("pay").disabled = false;
}

async function recordAndAdvance(paymentId) {
  document.getElementById("status").textContent = "Recording " + paymentId + "...";
  const res = await fetch("/record_payment", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ payment_id: paymentId })
  });
  const data = await res.json();
  document.getElementById("count").textContent = data.collected;
  document.getElementById("status").textContent = data.ok
    ? "Recorded. Loading next order..."
    : "Not captured (" + data.reason + "), skipping.";
  loadNextOrder();
}

document.getElementById("pay").onclick = function () {
  var rzp = new Razorpay({
    key: "__KEY_ID__",
    order_id: currentOrder.order_id,
    name: "Precedent",
    description: "Real payment collection batch",
    handler: function (response) {
      recordAndAdvance(response.razorpay_payment_id);
    },
    theme: { color: "#3399cc" }
  });
  rzp.open();
};

loadNextOrder();
</script>
</body>
</html>
"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", type=int, default=20)
    parser.add_argument("--db", default="precedent.db")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()

    app = build_app(target=args.target, db_path=args.db)
    print(f"Open http://127.0.0.1:{args.port}/ and click through {args.target} payments.")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
