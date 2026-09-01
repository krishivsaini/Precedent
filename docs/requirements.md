# Precedent — Requirements

Source of truth: [`PRECEDENT_SPEC.md`](PRECEDENT_SPEC.md). This document restates that spec as
testable requirements, organized for build tracking. Where this document and the spec disagree,
the spec wins — update this file, not the other way around.

**Except where marked ⚠️.** Two places where the spec is internally inconsistent or was falsified
by what the Razorpay API actually permits are flagged inline. Those are not silent deviations;
each states the conflict, the resolution taken, and its consequence.

---

## 1. Problem statement

Existing reconciliation/ticketing/RPA systems discard the knowledge produced by resolving an
exception. Precedent must write each confirmed resolution back as a structured, retrievable
precedent, so that autonomous resolution rate on **unseen** exceptions measurably rises as the
corpus grows. The deliverable is that learning curve, not a demo.

## 2. Scope

### 2.1 In scope

- Three-way reconciliation: PSP records vs. bank statement lines vs. internal order ledger.
- A deterministic matcher that clears the confident majority of exceptions.
- A retrieval-grounded LangGraph investigation agent for the residual exceptions.
- A human-in-the-loop approval gate that confirms, corrects, or rejects proposed resolutions.
- A precedent deposit loop that writes confirmed/corrected resolutions back into a retrievable
  corpus.
- An eval harness that replays a held-out test set against corpus snapshots and produces the
  learning-curve chart, plus supporting metrics and baselines.
- Bounded remediation (Ring 5): issuing a real test-mode refund under a human gate.

### 2.2 Explicitly out of scope

- Any dashboard beyond a static HTML eval report and a minimal approval UI.
- Auth, multi-tenancy, or user accounts.
- A chat interface over the ledger.
- Live-mode (real-money) operation of any kind.
- Cash forecasting.
- Multi-agent orchestration — one investigation graph, not a team of agents.

### 2.3 Data provenance requirements

Every data layer's provenance must be stated verbatim in the README, per this table:

| Layer | Required provenance |
|---|---|
| Payments, Orders, Refunds | Real Razorpay test-mode API calls |
| `payment.captured` / `refund.processed` webhooks | Real delivery, signature-verified, deduped |
| Settlement grouping + fee/tax schedule | Synthetic (test-mode `GET /v1/settlements` is empty) |
| Bank statement lines | Synthetic, generated from real payment data |
| Internal order ledger | Synthetic |
| Precedent corpus | Authored by the system, seeded with ~40 hand-written entries |

**Hard constraint:** synthetic *inputs* are sanctioned by the track brief; synthetic *outputs*
(fabricated results, metrics, or claims) are disqualifying and must never appear.

> ⚠️ **As-built reality, and why the table alone would mislead.** Razorpay exposes **no
> server-side way to create a captured payment** — only the hosted Checkout produces one, one
> browser interaction at a time. Reaching 240 records of genuinely real payments by hand is not
> feasible. **21 payments are real** (real orders via the API, paid through real test-mode
> checkouts, fetched back with their true `fee`/`tax`, committed to
> `evals/dataset/real_payments.json`); the rest are synthetic, tagged `source='synthetic'` in the
> schema, with fee/tax rates *calibrated from those 21*. The README must state this alongside the
> verbatim table, because "Payments: Real" read literally would overclaim.

## 3. Functional requirements

### FR-1 — Ingestion & webhooks
- FR-1.1: Ingest Payments, Orders, and Refunds via real Razorpay test-mode API calls. Order
  creation and payment fetch are exercised; refund *creation* is Ring 5 (FR-10), not ingestion.
- FR-1.2: Receive `payment.captured` and `refund.processed` webhooks with raw-body signature
  verification against fixture bytes.
- FR-1.3: Deduplicate webhook deliveries by `event_id` (primary key), using `INSERT OR IGNORE`,
  return `200` immediately, and process from storage — never process directly off the wire.
- FR-1.4: Generate synthetic bank statement lines from real payment data, and a synthetic internal
  order ledger, per a fixed, committed, reproducible seed.
- FR-1.5: The receiver must return `200` for **every** delivery it has durably recorded or
  deliberately ignored — including an invalid signature, an unknown event type, and a malformed or
  non-UTF-8 body. Any non-2xx invites a Razorpay retry-storm over input already captured. An
  invalid signature is recorded (`signature_valid=False`), never trusted, and never silently
  dropped.

### FR-2 — Deterministic matching (Ring 0 baseline)
- FR-2.1: Exact 1:1 match between payment, bank line, and ledger entry.
- FR-2.2: Tolerance-band match for fee/tax rounding deltas (±₹1). **Resolved by the matcher, not
  escalated** — see the ⚠️ note under FR-3.1.
- FR-2.3: Date-window match for timing drift between settlement and bank credit.
- FR-2.4: Netted-group detection (many payments → one bank credit, minus fees). The *checker* is a
  domain primitive; automatic discovery of which payments form a group is the agent's job, not the
  matcher's.
- FR-2.5: Must not double-match a duplicate customer payment against the same ledger entry.
- FR-2.6: Everything the deterministic matcher cannot confidently resolve is emitted as an
  `exception` record, not silently dropped — symmetrically: an orphaned payment, a ledger entry
  with no payment behind it, and a bank credit nobody claims.
- FR-2.7: This matcher must run, and be measured, **before any LLM code exists** in the repo.
- FR-2.8: Amount comparison must respect that **the bank never sees the gross figure and the
  ledger never sees the fee deduction**: a bank credit is compared against the payment *net of fee
  and tax*, while the ledger's expected amount is compared against the payment's *gross*. These
  are two distinct comparisons, not one three-way equality.

### FR-3 — Exception classification & the exception dataset
- FR-3.1: The exception generator (`evals/dataset/`) is seeded, fixed, committed, and reproducible,
  producing **240 records yielding 122 exceptions** across the 9 classes in spec §5.

  > ⚠️ **Deviation from the spec's "~130 exceptions", taken deliberately.** The spec's arithmetic
  > counts all eight non-clean-match classes as exceptions, including fee/tax rounding delta (4%).
  > But the deterministic matcher's tolerance tier already resolves a ±₹1 delta correctly and
  > confidently; escalating it to an LLM to hit a round number would be worse engineering.
  > Rounding-delta records are therefore rules-resolved. **Consequence: 122 exceptions, not ~130,
  > and a corpus pool of 62, not ~70.** The held-out test set is unchanged at exactly 60. The
  > rules-only baseline is correspondingly *stronger* (49.2%, not the implied 45%), making it a
  > harder floor for the agent to beat, not an easier one.

- FR-3.2: Gold labels live in `evals/gold.jsonl`, one per record. They are **authored by
  construction**: each scenario is deliberately built to have a known correct answer, and the label
  records it. This is stronger than post-hoc labelling (the answer cannot be mistaken) but must be
  described accurately — the exception mix reflects the spec's chosen distribution, not an
  organically observed one.
- FR-3.3: The corpus pool (62 exceptions) and test set (60 exceptions) are a fixed, disjoint,
  held-out split, stratified so every exception class appears in both. Resolutions from the test
  set are **never** deposited as precedents, at any point in the system's lifetime.
- FR-3.4: A "genuinely unmatchable" class (4% share) must exist in the dataset and must be
  reachable by the system as a valid terminal outcome — a 100%-resolved run is a bug report, not
  a result.

### FR-4 — Precedent retrieval
- FR-4.1: Precedent corpus seeded with ~40 hand-written entries conforming to the `Precedent`
  schema (spec §4). Seed precedents carry no `derived_from_resolution` — they are authored
  directly, so that column must be nullable.
- FR-4.2: Hybrid retrieval combining BM25 (`rank_bm25`) and dense vector search (`sqlite-vec`) via
  LangChain retrievers.
- FR-4.3: Retrieval must be swappable/measurable in three configurations — BM25-only, dense-only,
  hybrid — for the ablation in FR-9.5.
- FR-4.4: A random-precedent retrieval mode must exist as a first-class, selectable mode (not a
  one-off script) to serve as the negative control (FR-9.3).

### FR-5 — Investigation graph (LangGraph)
- FR-5.1: Implement the node sequence in spec §7: `detect_exception → classify_kind →
  retrieve_precedents → investigate → propose_resolution → verify → (revise ≤2) → route → gate →
  commit → audit → deposit_precedent`.
- FR-5.2: `investigate` is a bounded tool loop, max 5 calls, over: `fetch_payment`,
  `fetch_ledger_entry`, `fetch_bank_lines`, `fetch_refunds`, `compute_expected_amount`,
  `search_prior_resolutions`.
- FR-5.3: `propose_resolution` output must be Pydantic-validated structured output — no
  free-text resolution accepted downstream.
- FR-5.4: `verify` must check (a) arithmetic closes to the paise, and (b) cited precedents
  actually apply to the case. Failure triggers `revise`, bounded to 2 attempts.
- FR-5.5: `route` sends confidence ≥ threshold to `auto_resolve`, confidence < threshold to
  `escalate_with_draft`, and two failed verifications to `escalate_raw` + exception list.
- FR-5.6: **Every** LLM call path (parse failure, low confidence, model unavailable) must
  terminate in escalation with a reason code — the system must never guess and never stall.

### FR-6 — Human approval gate
- FR-6.1: State-changing resolutions pause at a durable `interrupt` gate; state is checkpointed so
  a paused resolution survives a process restart. This must be proven by an actual restart test,
  not asserted from the design.
- FR-6.2: A minimal approval UI presents: the proposed resolution, rationale, cited precedents,
  confidence, and the arithmetic check, and accepts exactly one of **confirm / correct / reject**.
- FR-6.3: On `confirm` or `correct`, the resolution is committed and audited. On `reject`, it is
  routed to the exception list, not silently discarded.

### FR-7 — Precedent deposit
- FR-7.1: `deposit_precedent` fires only on human `confirmed` or `corrected` outcomes — never on
  an unreviewed or rejected resolution.
- FR-7.2: A `corrected` resolution deposits the corrected payload, not the original proposal —
  corrections are higher-value precedents than confirmations.
- FR-7.3: Deposited precedents carry full provenance (`derived_from` → `resolution_id`) and a
  `corpus_version` for snapshot replay.
- FR-7.4: Precedent-authoring prompts are versioned (`prompts/deposit/v1.md`, `v2.md`, ...), and
  each version's retrieval quality is measurable independently (spec §4).
- FR-7.5: **A deposit is atomic.** Recording the human action, appending the audit row, and
  inserting the precedent either all commit or none do. A partial deposit leaves a precedent with
  no audit trail (violating NFR-5) or a confirmed resolution that never deposited — both silently
  corrupt the corpus the entire thesis rests on.

### FR-8 — Auditability & idempotency
- FR-8.1: Every graph stage (`detected, retrieved, decided, verified, gated, acted`) writes an
  `audit_log` row with actor, input/output digests, model, latency, tokens, and reason.
- FR-8.2: Orders/Payments idempotency is self-enforced (unique `receipt` + `idempotency` table) —
  the README must state plainly that Razorpay provides no native header here.
- FR-8.3: Refunds use the real `X-Refund-Idempotency` header (min 10 chars) and handle `409
  Conflict`.
- FR-8.4: Webhook idempotency is enforced by `event_id` PK + `INSERT OR IGNORE` (see FR-1.3).
  Because SQLite applies `OR IGNORE` to CHECK violations exactly as to duplicate keys — silently,
  with no exception — validity checks that must be loud (e.g. `event_type`) cannot rely on the
  CHECK constraint alone.

### FR-9 — Evaluation harness (primary deliverable)
- FR-9.1: Replay the frozen 60-exception test set against corpus snapshots and produce the
  resolution-rate-vs-corpus-size chart.

  > ⚠️ **The spec's snapshot points are unreachable, and this needs a decision before Ring 3.**
  > Spec §6 specifies snapshots at **0, 50, 100, 150, 200** deposited precedents. But spec §5 sizes
  > the depositable corpus pool at ~70 exceptions and the seed corpus at ~40 — a hard ceiling of
  > ~110 precedents, since one resolution deposits one precedent. With the as-built numbers the
  > ceiling is **102** (40 seed + 62 pool). **Snapshots at 150 and 200 cannot be reached**, in this
  > build or under the spec's own sizing. Recommended resolution: rescale to **0 / 25 / 50 / 75 /
  > 100**, which the corpus genuinely supports and which still yields five points on the curve.
  > Whatever is chosen must be stated in `docs/ARCHITECTURE.md` — silently plotting a shorter curve
  > against the spec's stated protocol would be exactly the kind of unexamined claim §6 forbids.

- FR-9.2: Held-out test exceptions are never deposited — enforced structurally, not by convention.
- FR-9.3: Run a random-precedent negative control at the same batch and *k* as the real run,
  unprompted, every time the headline curve is reported.
- FR-9.4: Measure precedent precision separately — of precedents cited in a resolution, the
  fraction that actually applied.
- FR-9.5: Report BM25-only vs. dense-only vs. hybrid retrieval results to justify the hybrid
  choice with data.
- FR-9.6: Report all four baselines every run: deterministic rules alone, zero-shot LLM (empty
  corpus), random-precedent control, retrieval-grounded agent.
- FR-9.7: Compute rationale faithfulness via Ragas, calibrated against 15 hand-scored items, and
  report the correlation.
- FR-9.8: Report false-resolution cost in ₹ (not counts), escalation rate, p50/p95 latency, tokens
  per exception, and the exception list with reason codes.
- FR-9.9: Every eval run is committed to `evals/results/YYYY-MM-DD-HHMM.json`, including runs that
  show no improvement or a regression. No cherry-picking.
- FR-9.10: The harness scores **one scenario per matcher invocation**, never the pooled dataset.
  `match_batch` searches all currently-available credit lines, so pooling admits cross-scenario
  collisions — one scenario's bank line satisfying another's payment, stealing a match and
  producing a spurious exception elsewhere. Per-scenario isolation removes the class of error.
- FR-9.11: The harness must independently re-derive each scenario's outcome and compare it against
  its gold label, failing loudly on any disagreement. If the dataset and the matcher drift apart,
  every downstream metric is built on sand.

### FR-10 — Bounded remediation (Ring 5)
- FR-10.1: Issue a real test-mode refund through the Razorpay API under an explicit human gate,
  distinct from the resolution gate — it authorizes movement of (test-mode) money.
- FR-10.2: Use `X-Refund-Idempotency` on every remediation call.
- FR-10.3: Enforce a remediation ceiling and a stopping rule — remediation must be bounded, not
  open-ended, and proven by driving the ceiling to exhaustion in a test.

## 4. Non-functional requirements

- NFR-1 (**Money correctness**): all monetary values are integer paise; floats are never used for
  money anywhere, including in LLM-facing prompts and structured outputs. Enforced at three layers
  rather than by convention: `money.rupees_to_paise` and `money.apply_rate_paise` reject `float` at
  the domain boundary, `apply_rate_paise` is the single choke point every rate calculation passes
  through, and the schema carries `CHECK (typeof(...) = 'integer')` on every paise column.
- NFR-2 (**Determinism first**): the deterministic core is authoritative wherever it can decide;
  the LLM operates only at the edges it cannot reach.
- NFR-3 (**Reproducibility**): dataset generation, seeding, and eval runs must be deterministic
  given a fixed seed, and re-runnable by a reviewer within ~60 seconds of cloning, with no external
  provisioning and **no Razorpay credentials** (drives the `sqlite-vec`/SQLite choice, and requires
  the real-payment fixture to be committed rather than fetched).
- NFR-4 (**Model-agnosticism**): the LLM adapter must not hard-couple to a single vendor — Gemini
  2.5 Flash is primary, Ollama is a real fallback path, not aspirational.
- NFR-5 (**Auditability**): every state-changing action must be reconstructable from `audit_log`
  alone.
- NFR-6 (**Graceful failure**): no unhandled exception path may leave the system silently
  incorrect; every failure mode routes to escalation with a reason code, and this path is
  demonstrable on video.
- NFR-7 (**Honesty of claims**): no metric, chart, or claim may be presented without the
  corresponding committed result file; the exception list is mandatory output, not an
  embarrassment to hide.
- NFR-8 (**Transactional integrity**): repositories never commit; the caller owns the transaction
  boundary. Any multi-write unit of work commits as one unit or not at all (see FR-7.5).
- NFR-9 (**No import-time side effects**): importing a module must never create files, open
  network connections, or touch a database. Schema creation belongs to application startup.

## 5. Constraints

- Solo build, fixed track (Razorpay AI Buildathon, Track 04 — AI Finance Controller).
- Stack is fixed per spec §3 (FastAPI, LangGraph, LangChain + `sqlite-vec` + `rank_bm25`, Pydantic
  v2, Ragas, SQLite, Gemini 2.5 Flash / Ollama) — deviating requires updating the spec's rejection
  rationale, not silently swapping libraries.
- Ring 1 carries a **kill criterion**: if the retrieval-grounded agent does not beat zero-shot LLM
  on the ablation, the precedent-retrieval thesis is falsified and the project falls back to plain
  adjudication. This must be checked honestly before continuing to Ring 2.
- Razorpay cannot create captured payments server-side (see ⚠️ under §2.3). Any requirement
  assuming payment volume at dataset scale is bounded by this.

## 6. Acceptance criteria

The project is feature-complete for submission when:

1. FR-1 through FR-9 are implemented and independently testable per spec §10. FR-10 (Ring 5) is
   explicitly optional — the thesis stands without it (see the implementation plan's execution
   ordering).
2. The Ring 1 kill criterion has been checked and passed (or the documented fallback taken).
3. `evals/results/` contains committed, timestamped runs showing the learning curve across all
   corpus snapshots (per FR-9.1's resolution), the negative control, and all four baselines.
4. `docs/ARCHITECTURE.md` covers the six sections in spec §11, including limits and known failure
   modes.
5. `FAILURES.md` exists and has been maintained since the first commit — not backfilled.
6. The README states data provenance verbatim per §2.3, *and* the real-vs-synthetic payment split.
