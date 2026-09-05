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

## See it running

| | |
|---|---|
| **The system** | https://precedent-k5qf.onrender.com |
| **The argument** | https://precedent-3ob.pages.dev |

The app sleeps when idle; the first request takes about a minute to wake it. It is seeded with
real dataset cases, so the queue is populated on arrival.

**A three-minute path through it.** Open the queue, pick the duplicate-charge case, and read the
tie-out — two comparisons kept apart, because the bank credit is net of the processor's fee and
the invoice is gross. Retrieved precedents sit *above* the proposal, so you judge whether the
cases are alike before knowing what was concluded from them. Press **Confirm**: the system spends
about twelve seconds writing a precedent from your decision, and `/corpus` gains an entry under
*authored by operation*. The case then presents a second, separate gate for the refund — and
refuses to offer the button, because the amount is over the per-call ceiling.

That last refusal is the system working, not failing.

## Status

Rings 0 through 5 are built and measured. Every figure below is read from a committed file in
[`evals/results/`](evals/results/); none is typed in.

| | corpus of 42 | corpus of 151 | paired exact McNemar |
|---|---|---|---|
| **Autonomous resolution** | 70.0% | **86.7%** | p = 0.041, 15 gained / 5 lost |
| Random-precedent control | 56.7% | 61.7% | p = 0.629 — **not significant** |
| Cases only a precedent can answer | 0.0% | **83.3%** | p = 0.00006, 15 gained / **0 lost** |

The control draws the same number of precedents from the same corpus and differs only in whether
they are *relevant*. It stays flat. That is what rules out "more text in the prompt", and it is
the line that decides whether the other one means anything.

The bottom row is the claim. Those cases carry a shortfall matching no statutory band, with
nothing in the evidence saying why — a negotiated rebate, an advance already adjusted. They
cannot be worked out. They can only be remembered, which is precisely what a corpus authored by
the system's own operation provides and a better prompt does not.

Measured on `nvidia/nemotron-3-super-120b-a12b`. The deterministic baseline this had to clear is
**49.2%**, and **0.0%** on the held-out test set.

### What this does not show

- The reviewer in the learning-curve run is simulated: it confirms the agent when right, corrects
  it when wrong, and rejects 15% of cases outright. The curve is an estimate, not a ceiling.
- Seven of nine exception classes are derivable from the evidence and already sit at 98–100% with
  no corpus at all. Giving the agent investigation tools removes the "having precedents" effect
  entirely — **+0.0pp** — leaving **+9.7pp** attributable to their relevance. Reporting the total
  as retrieval would over-claim.
- The counterparty task is recall of a customer's standing terms. That is real institutional
  knowledge, and it is not deep generalisation.
- Precedent precision is approximated by reason-code agreement. A precedent of the right class can
  still be the wrong precedent.

[`FAILURES.md`](FAILURES.md) was opened on the first commit, not the last.

## Layout

```
src/precedent/
  domain/      pure, zero I/O — money (integer paise), matching, reason codes, confidence
  adapters/    razorpay/ (client + webhook signature), storage/ (SQLite schema + repositories)
  api/         FastAPI app — webhook receiver, JSON gates, and seven server-rendered screens
  graph/       the LangGraph investigation: classify -> retrieve -> investigate -> verify -> route
  usecases/    deposit (review and precedent in one transaction), remediate (the money gate)
evals/
  dataset/     seeded generator, committed 240-record dataset, real payment fixture
  gold.jsonl   hand-authored gold labels, one per record
  runner.py    scores the deterministic baseline against gold
  results/     every run, committed — including the bad ones
deploy/        entrypoint + Cloud Run script; render.yaml deploys the same image
docs/          PRECEDENT_SPEC.md, ARCHITECTURE.md, requirements, product design, DEPLOYMENT.md
FAILURES.md    opened on the first commit, not the last
```

## Running it

```bash
uv sync
uv run pytest                        # 809 tests
uv run python -m evals.dataset.generate   # regenerate the dataset (deterministic, fixed seed)
uv run python -m evals.runner             # re-measure the baseline, write to evals/results/

uv run python scripts/seed_demo.py        # populate a database with real dataset cases
uv run uvicorn precedent.api.main:app     # then open http://localhost:8000
```

Confirming a resolution authors a precedent, so the approval gate calls a model in the request
path and needs `NVIDIA_API_KEY` or `GROQ_API_KEY`. Without one the gate refuses a confirmation
rather than recording a review with nothing in the corpus behind it — the review and the deposit
are one transaction, deliberately.

Razorpay calls need test-mode credentials — copy `.env.example` to `.env` and fill it in. Nothing
in the eval or the test suite requires them.
