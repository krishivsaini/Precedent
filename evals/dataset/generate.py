"""Ring 0.5: generates the 240-record exception dataset (spec §5) deterministically from
`SEED`, and writes `evals/gold.jsonl` plus `evals/dataset/{payments,bank_lines,ledger_entries}.json`.

    uv run python -m evals.dataset.generate

Two deliberate departures from the spec's literal numbers, both disclosed here and in
`docs/ARCHITECTURE.md`:

1. **`rounding_delta` (4%, 10 records) is *not* counted as an exception.** The spec's own
   arithmetic ("240 records yielding ~130 exceptions") sums all 8 non-"clean match" rows,
   which would include it. But the Ring 0.1 deterministic matcher's tolerance-band tier
   already resolves a ±₹1 rounding delta on its own — escalating it to the agent anyway
   would be worse engineering just to hit a round number. `clean_match` + `rounding_delta`
   = 118 records resolved by rules alone; the remaining 122 are exceptions.
2. **The corpus pool is ~62, not spec's "~70"**, as a direct consequence of (1): with 122
   total exceptions and the test set fixed at exactly 60 (spec's own number, not
   adjusted), the pool is whatever's left — 62.

Real vs. synthetic payments: of the 21 real Razorpay test-mode payments collected into
`real_payments.json`, all 21 are used — spread across `clean_match`, `tds_short_payment`,
`rounding_delta`, and `refund_netted` — with the remaining volume needed for each class,
and every other class, built from synthetic payments calibrated (fee rate, tax rate)
against the real ones. See `docs/PRECEDENT_SPEC.md` §2.3 for the honesty requirement this
follows.

**Scoring note for the eval runner (0.6):** run `match_batch` once per scenario, not once
over the whole pooled dataset. `match_batch` searches all *available* credit lines
globally when resolving a match, not scoped to one scenario — pooling 240 scenarios'
bank lines into a single call risks a rare but real cross-scenario collision (one
scenario's line coincidentally satisfying another's payment). Per-scenario isolation
removes that risk by construction and makes each scenario's outcome independently
reproducible.
"""

import json
from collections import defaultdict
from dataclasses import asdict, replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from random import Random

from evals.dataset import builders
from evals.dataset.generators import FAKE_CUSTOMERS
from evals.dataset.real_payments import load_real_payments
from evals.dataset.scenario import Scenario
from evals.dataset.synthetic_payments import (
    REFERENCE_DATE,
    calibrate_fee_model,
    generate_synthetic_payment,
    random_amount_paise,
)
from precedent.domain.money import gross_before_tds_paise

SEED = 20260901
TEST_SET_SIZE = 60  # spec §5: fixed, held-out, never deposited

CLASS_COUNTS = {
    "clean_match": 72,
    "netted_settlement": 36,
    "direct_neft_bypass": 24,
    "tds_short_payment": 19,
    "split_payment": 14,
    "refund_netted": 12,
    "rounding_delta": 10,
    "duplicate_payment": 7,
    "unmatchable": 10,
    # Ring 2.5 — counterparty knowledge, not derivable from the case. Sized so every
    # customer recurs: 4 rebate customers x 4 occurrences, 3 advance customers x 4. The
    # recurrence is the point — a precedent deposited on one occurrence has to be worth
    # retrieving on the next, or there is no learning curve to measure.
    "negotiated_rebate": 20,
    "advance_adjusted": 16,
}
assert sum(CLASS_COUNTS.values()) == 240

REAL_PAYMENT_ALLOCATION = {
    "clean_match": 10,
    "tds_short_payment": 4,
    "rounding_delta": 4,
    "refund_netted": 3,
}
assert sum(REAL_PAYMENT_ALLOCATION.values()) == 21

_OUTPUT_DIR = Path(__file__).parent
_GOLD_PATH = _OUTPUT_DIR.parent / "gold.jsonl"


def _order_id_allocator():
    counter = 0
    while True:
        counter += 1
        yield f"order_synth_{counter:04d}"


def _real_flags(class_name: str, count: int) -> list[bool]:
    n_real = min(REAL_PAYMENT_ALLOCATION.get(class_name, 0), count)
    return [True] * n_real + [False] * (count - n_real)


def _assign_pool_or_test(scenarios: list[Scenario], rng: Random) -> list[Scenario]:
    """Stratified split, with the counterparty classes handled first and by hand.

    For every other class a proportional random split is fine: the cases are independent, so
    which ones land in the test set does not change what the test set can measure.

    The counterparty classes are not independent — they come in groups that share a customer,
    and the split has to preserve the structure the learning curve is read from:

    * **Each customer's first sighting goes to the pool.** It is the case nobody can resolve,
      which a human resolves, and whose resolution is deposited. Put it in the held-out set
      and there is nothing to deposit *from*.
    * **Every customer keeps at least one sighting in the test set.** Otherwise that
      customer's deposited precedent can never be shown to help, and the deposit is
      unmeasurable. A purely proportional split left two of nine customers with no test
      sighting at all — invisible, because the class-level counts looked correct.

    The remaining test-set places are then filled proportionally from the other classes.
    """
    exceptions = [s for s in scenarios if s.is_exception]
    test_ids: set[str] = set()

    # --- counterparty classes, grouped by customer ---
    by_customer: dict[str, list[Scenario]] = defaultdict(list)
    for s in exceptions:
        if s.counterparty:
            by_customer[s.counterparty].append(s)

    counterparty_ids = set()
    for customer, sightings in sorted(by_customer.items()):
        ordered = sorted(sightings, key=lambda s: (s.occurrence_index or 0, s.scenario_id))
        counterparty_ids.update(s.scenario_id for s in ordered)
        later = ordered[1:]  # ordered[0] — the first sighting — always stays in the pool
        if not later:
            continue
        # Half the later sightings, at least one, so every customer is represented on both
        # sides of the split.
        take = max(1, len(later) // 2)
        shuffled = later[:]
        rng.shuffle(shuffled)
        test_ids.update(s.scenario_id for s in shuffled[:take])

    # --- everything else, proportionally, filling the remaining places ---
    remaining_slots = TEST_SET_SIZE - len(test_ids)
    others = [s for s in exceptions if s.scenario_id not in counterparty_ids]
    by_kind: dict[str, list[Scenario]] = defaultdict(list)
    for s in others:
        by_kind[s.kind].append(s)

    total = len(others)
    raw = {kind: len(items) * remaining_slots / total for kind, items in by_kind.items()}
    floor_alloc = {kind: int(v) for kind, v in raw.items()}
    remainder = remaining_slots - sum(floor_alloc.values())
    by_fraction = sorted(raw.items(), key=lambda kv: kv[1] - int(kv[1]), reverse=True)
    for kind, _ in by_fraction[:remainder]:
        floor_alloc[kind] += 1

    for kind, items in by_kind.items():
        shuffled = items[:]
        rng.shuffle(shuffled)
        test_ids.update(s.scenario_id for s in shuffled[: floor_alloc[kind]])

    return [
        s if not s.is_exception else replace(s, pool_or_test="test" if s.scenario_id in test_ids else "pool")
        for s in scenarios
    ]


def generate_dataset() -> list[Scenario]:
    rng = Random(SEED)
    all_real_payments = load_real_payments()
    fee_rate, tax_rate = calibrate_fee_model(all_real_payments)
    real_payments = iter(all_real_payments)
    order_ids = _order_id_allocator()

    scenario_counter = 0

    def next_scenario_id() -> str:
        nonlocal scenario_counter
        scenario_counter += 1
        return f"rec_{scenario_counter:04d}"

    scenarios: list[Scenario] = []

    for use_real in _real_flags("clean_match", CLASS_COUNTS["clean_match"]):
        payment = next(real_payments) if use_real else generate_synthetic_payment(
            rng, next(order_ids), random_amount_paise(rng), fee_rate, tax_rate
        )
        scenarios.append(builders.build_clean_match(rng, next_scenario_id(), payment))

    for use_real in _real_flags("rounding_delta", CLASS_COUNTS["rounding_delta"]):
        payment = next(real_payments) if use_real else generate_synthetic_payment(
            rng, next(order_ids), random_amount_paise(rng), fee_rate, tax_rate
        )
        scenarios.append(builders.build_rounding_delta(rng, next_scenario_id(), payment))

    for _ in range(CLASS_COUNTS["netted_settlement"]):
        group_size = rng.choice([2, 2, 3])
        payments = [
            generate_synthetic_payment(rng, next(order_ids), random_amount_paise(rng), fee_rate, tax_rate)
            for _ in range(group_size)
        ]
        scenarios.append(builders.build_netted_settlement(rng, next_scenario_id(), payments))

    for _ in range(CLASS_COUNTS["direct_neft_bypass"]):
        order_id = next(order_ids)
        amount = random_amount_paise(rng)
        customer_name = rng.choice(FAKE_CUSTOMERS)
        value_date = REFERENCE_DATE - timedelta(days=rng.randint(0, 60))
        scenarios.append(
            builders.build_direct_neft_bypass(
                rng, next_scenario_id(), order_id, amount, customer_name, value_date
            )
        )

    for use_real in _real_flags("tds_short_payment", CLASS_COUNTS["tds_short_payment"]):
        tds_rate = rng.choice([Decimal("0.02"), Decimal("0.10")])
        if use_real:
            real_payment = next(real_payments)
            # The real payment's amount is what actually arrived, i.e. already net of
            # TDS — derive the invoice it was short against, in Decimal (NFR-1).
            invoice_amount = gross_before_tds_paise(real_payment.amount_paise, tds_rate)
            scenario = builders.build_tds_short_payment(
                rng, next_scenario_id(), real_payment.order_id, invoice_amount, tds_rate,
                fee_rate, tax_rate, payment=real_payment,
            )
        else:
            invoice_amount = random_amount_paise(rng)
            scenario = builders.build_tds_short_payment(
                rng, next_scenario_id(), next(order_ids), invoice_amount, tds_rate, fee_rate, tax_rate
            )
        scenarios.append(scenario)

    for _ in range(CLASS_COUNTS["split_payment"]):
        order_id = next(order_ids)
        share_a, share_b = random_amount_paise(rng), random_amount_paise(rng)
        scenarios.append(
            builders.build_split_payment(rng, next_scenario_id(), order_id, share_a, share_b, fee_rate, tax_rate)
        )

    for use_real in _real_flags("refund_netted", CLASS_COUNTS["refund_netted"]):
        if use_real:
            real_payment = next(real_payments)
            net_available = real_payment.amount_paise - real_payment.fee_paise - real_payment.tax_paise
            refund_amount = max(150, net_available // 3)
            scenario = builders.build_refund_netted(
                rng, next_scenario_id(), real_payment.order_id, real_payment.amount_paise,
                refund_amount, fee_rate, tax_rate, payment=real_payment,
            )
        else:
            order_id = next(order_ids)
            gross = random_amount_paise(rng)
            refund_amount = max(150, gross // 4)
            scenario = builders.build_refund_netted(
                rng, next_scenario_id(), order_id, gross, refund_amount, fee_rate, tax_rate
            )
        scenarios.append(scenario)

    for _ in range(CLASS_COUNTS["duplicate_payment"]):
        order_id = next(order_ids)
        amount = random_amount_paise(rng)
        scenarios.append(
            builders.build_duplicate_payment(rng, next_scenario_id(), order_id, amount, fee_rate, tax_rate)
        )

    for _ in range(CLASS_COUNTS["unmatchable"]):
        order_id = next(order_ids)
        amount = random_amount_paise(rng)
        scenarios.append(
            builders.build_unmatchable(rng, next_scenario_id(), order_id, amount, fee_rate, tax_rate)
        )

    # Counterparty classes. `customer_index` cycles so each customer recurs with the same
    # terms; the stratified split then puts some of each customer's cases in the pool (where
    # a resolution may be deposited) and some in the held-out test set (where only a
    # retrieved precedent can resolve them). That pairing is what makes the learning curve
    # measurable rather than decorative.
    for index in range(CLASS_COUNTS["negotiated_rebate"]):
        scenarios.append(
            builders.build_negotiated_rebate(
                rng, next_scenario_id(), next(order_ids), random_amount_paise(rng),
                index, fee_rate, tax_rate,
            )
        )

    for index in range(CLASS_COUNTS["advance_adjusted"]):
        # Large enough that netting a 250-750 rupee advance leaves a sane positive payment.
        invoice = random_amount_paise(rng) + 200_000
        scenarios.append(
            builders.build_advance_adjusted(
                rng, next_scenario_id(), next(order_ids), invoice, index, fee_rate, tax_rate,
            )
        )

    return _assign_pool_or_test(scenarios, rng)


def write_dataset(scenarios: list[Scenario]) -> None:
    payments, bank_lines, ledger_entries = {}, {}, {}
    gold_rows = []

    for s in scenarios:
        for p in s.payments:
            payments[p.payment_id] = asdict(p)
        for b in s.bank_lines:
            bank_lines[b.line_id] = asdict(b)
        for l in s.ledger_entries:
            ledger_entries[l.entry_id] = asdict(l)
        gold_rows.append({
            "scenario_id": s.scenario_id,
            "kind": s.kind,
            "is_exception": s.is_exception,
            "expected_reason_code": s.expected_reason_code,
            "notes": s.notes,
            "uses_real_payment": s.uses_real_payment,
            "pool_or_test": s.pool_or_test,
            "counterparty": s.counterparty,
            "occurrence_index": s.occurrence_index,
            "payment_ids": [p.payment_id for p in s.payments],
            "bank_line_ids": [b.line_id for b in s.bank_lines],
            "ledger_entry_ids": [l.entry_id for l in s.ledger_entries],
        })

    (_OUTPUT_DIR / "payments.json").write_text(json.dumps(list(payments.values()), indent=2) + "\n")
    (_OUTPUT_DIR / "bank_lines.json").write_text(json.dumps(list(bank_lines.values()), indent=2) + "\n")
    (_OUTPUT_DIR / "ledger_entries.json").write_text(json.dumps(list(ledger_entries.values()), indent=2) + "\n")
    with open(_GOLD_PATH, "w") as f:
        for row in gold_rows:
            f.write(json.dumps(row) + "\n")


def _print_summary(scenarios: list[Scenario]) -> None:
    by_kind = defaultdict(int)
    real_by_kind = defaultdict(int)
    pool_count = test_count = 0
    for s in scenarios:
        by_kind[s.kind] += 1
        if s.uses_real_payment:
            real_by_kind[s.kind] += 1
        if s.pool_or_test == "pool":
            pool_count += 1
        elif s.pool_or_test == "test":
            test_count += 1

    print(f"Generated {len(scenarios)} records:")
    for kind, count in by_kind.items():
        print(f"  {kind:22s} {count:4d}  (real: {real_by_kind[kind]})")
    n_exceptions = sum(1 for s in scenarios if s.is_exception)
    print(f"\nExceptions: {n_exceptions} (pool={pool_count}, test={test_count})")
    print(f"Resolved by rules alone: {len(scenarios) - n_exceptions}")


if __name__ == "__main__":
    dataset = generate_dataset()
    write_dataset(dataset)
    _print_summary(dataset)
