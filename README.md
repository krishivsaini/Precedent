# Precedent

**An exception-resolution agent whose knowledge base is written by its own operation.**

Razorpay AI Buildathon, Track 04 (AI Finance Controller). Solo build.

In every existing reconciliation, ticketing, or RPA system, resolving an exception destroys the
knowledge that resolution produced. The insight is consumed and discarded; the next similar
exception is escalated to a human again.

Precedent inverts this. Each confirmed resolution is written back as a structured, retrievable
precedent. The corpus is authored by the system's own operation, and autonomous resolution rate
on **unseen** exceptions rises as the corpus grows.

---

## Honest data provenance

| Layer | Provenance |
|---|---|
| Payments, Orders, Refunds | **Real** Razorpay test-mode API calls |
| `payment.captured` / `refund.processed` webhooks | **Real** delivery, signature-verified, deduped |
| Settlement grouping + fee/tax schedule | **Synthetic.** `GET /v1/settlements` returns an empty collection in test mode. |
| Bank statement lines | **Synthetic**, generated from real payment data |
| Internal order ledger | **Synthetic** |
| Precedent corpus | **Authored by the system**, seeded with ~40 hand-written entries |

Synthetic *input* is sanctioned by the track brief. Synthetic *outputs* are disqualifying and do
not appear anywhere in this project.

### What "real" means here, precisely

The table above is the standard this project holds itself to. Two details it does not by itself
make explicit, stated here rather than left for a reviewer to discover:

- **21 payments are genuinely real** — created as real test-mode orders via the Razorpay API and
  paid through real test-mode checkouts, then fetched back through `GET /v1/payments/{id}`. They
  carry Razorpay's own `fee` and `tax` values. They are committed verbatim to
  [`evals/dataset/real_payments.json`](evals/dataset/real_payments.json) so the eval is
  reproducible from a clone with no Razorpay credentials.
- **The remaining payments in the eval dataset are synthetic** and tagged `source='synthetic'` in
  the `payments` table, which the schema distinguishes explicitly. Razorpay offers no server-side
  way to create a captured payment — only the hosted Checkout does — so reaching the 240-record
  scale the eval needs by hand was not feasible. The synthetic payments' fee and tax rates are
  *calibrated from the 21 real ones* rather than invented.

No metric, chart, or claim in this repo is produced from anything but a committed result file.

---

## Status

Ring 0 complete. The deterministic baseline is measured and committed, which is the gate the spec
sets before any LLM code may exist in the repo.

| Baseline | Autonomous resolution rate | On held-out test set |
|---|---|---|
| Deterministic rules alone | **49.2%** (118/240) | **0.0%** |

Full result: [`evals/results/`](evals/results/). The rules baseline resolves clean 1:1 matches
(108) and fee/tax rounding deltas (10), and escalates the other 122 records — including 10 that
are **genuinely unmatchable by construction**. Any system here reporting 100% coverage would be
lying; the exception list is a permanent, mandatory output.

## Layout

```
src/precedent/
  domain/      pure, zero I/O — money (integer paise), matching, reason codes, confidence
  adapters/    razorpay/ (client + webhook signature), storage/ (SQLite schema + repositories)
  api/         FastAPI app, webhook receiver
evals/
  dataset/     seeded generator, committed 240-record dataset, real payment fixture
  gold.jsonl   hand-authored gold labels, one per record
  runner.py    scores the deterministic baseline against gold
  results/     every run, committed — including the bad ones
docs/          PRECEDENT_SPEC.md, ARCHITECTURE.md, requirements, product design, implementation plan
FAILURES.md    opened on the first commit, not the last
```

## Running it

```bash
uv sync
uv run pytest                        # 178 tests
uv run python -m evals.dataset.generate   # regenerate the dataset (deterministic, fixed seed)
uv run python -m evals.runner             # re-measure the baseline, write to evals/results/
```

Razorpay calls need test-mode credentials — copy `.env.example` to `.env` and fill it in. Nothing
in the eval or the test suite requires them.
