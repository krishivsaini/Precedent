# Precedent — Implementation Plan

Companion to [`requirements.md`](requirements.md) and [`product_design.md`](product_design.md).
This document sequences the build. It follows the ring structure in `PRECEDENT_SPEC.md` §9 —
each ring is independently submittable, and no ring's tasks start before the previous ring's gate
is met.

**Standing rule for every ring:** money is integer paise, everywhere, no exceptions. Any change
that introduces a float for a monetary value is a bug, not a style issue.

## Status

| Ring | State | Gate |
|---|---|---|
| **0** — deterministic baseline | ✅ **complete** | ✅ **met** — rules-only baseline committed at 49.2%, before any LLM code exists |
| **1** — precedent retrieval + kill criterion | ✅ **complete** | ✅ **PASS** — grounded 100% vs zero-shot 85.5% vs random control 93.5% |
| **2** — LangGraph investigation graph | ✅ **complete** | ✅ **met** — grounded 100% through both chain and graph, no regression |
| **3** — deposit loop, gate, learning curve | 🟨 **3.1, 3.3–3.5 complete** | ✅ **curve committed** — 81.7% → 95.0% (p=0.021), counterparty 0/9 → 8/9 (p=0.0078), control *falls* |
| **4** — calibration | ⬜ | |
| **5** — bounded remediation | ⬜ | |

**Current state:** 550 tests passing. 240-record dataset generated and committed. Rules-only
baseline and the retrieval eval are both in `evals/results/`. Deferred from Ring 0 by explicit
decision: live webhook delivery over a public tunnel (signature verification and dedupe are tested
against fixture bytes instead).

**Model:** `openai/gpt-oss-120b` via NVIDIA NIM, temperature 0. Every result file records both
provider and model — numbers from different models are not comparable, and mixing them inside one
comparison is the error the eval discipline exists to prevent. Gemini and Groq were both tried and
both ran out of free-tier quota mid-run; see FAILURES.md. `evals/.cache/` makes a run resumable and
a re-run free, which is what lets spec §6's "re-run before any claim of improvement" actually be
followed.

**Snapshot scale — decided.** The spec's replay protocol asks for corpus snapshots at
0/50/100/150/200 deposited precedents. The corpus cannot exceed **140** (42 hand-written seeds
plus 98 depositable pool exceptions, one precedent per resolution), so 150 and 200 are
arithmetically unreachable — a contradiction in the source spec, not in this build; even at the
spec's own ~70-pool/~40-seed sizing the ceiling was ~110. **Rescaled to
`0 / 25 / 50 / 75 / 98`**: five points as the spec intends, at quarters of the depositable pool,
with the last being everything available rather than a round number that happens to fit. Recorded
in `evals/replay.py` beside the constant.

> ⚠️ **Open — the pool set is saturated for the derivable classes.** All three arms score 98-100%
> on the seven classes that can be worked out from the evidence, so no growing corpus can show an
> effect there. The learning curve is measured on the **counterparty classes**, where the seed
> corpus reaches 0/15 and 0/12 at k=5 and only a deposit can help. That is 36 cases across nine
> customers, each with its first sighting in the pool and at least one in the held-out test set.

> ⚠️ **Open — the deposit prompt is unproven.** Ring 2.5 demonstrated the effect with a precedent
> written by hand. An LLM-authored one may be materially worse, and the prompt has already been
> wrong once for exactly this class of knowledge. 3.3 must measure authored-vs-hand-written
> before the curve is trusted.

---

## Ring 0 — Deterministic baseline (no LLM code exists yet) ✅

**Gate:** rules-only baseline committed, and its accuracy on the exception dataset measured and on
disk. A hard gate — Ring 1 depends on having a baseline to beat. **Met:** 49.2% (118/240), 0.0% on
the held-out test set, committed to `evals/results/`.

### 0.1 — Pure domain layer (tests first, zero I/O, zero network) ✅
- `domain/money.py` — integer-paise arithmetic, tolerance bands, TDS. `apply_rate_paise` is the
  single choke point through which any percentage touches money, and it rejects `float` rates;
  `gross_before_tds_paise` reconstructs an invoice from a short payment.
- `domain/matching.py` — exact, tolerance, date-window, and netted-group matching. Tests cover the
  duplicate-payment false-accept case explicitly (FR-2.5) — a correctness trap, not a happy path.
- `domain/reasons.py` — the closed `ReasonCode` enum, defined early so nothing downstream invents
  ad hoc strings.
- `domain/confidence.py` — threshold constants and calibration bins (placeholders; Ring 4 tunes
  them from data).

### 0.2 — Data model & storage ✅
- SQLite schema for all tables in spec §4, with `CHECK (typeof(...) = 'integer')` on every paise
  column and a CHECK on `precedents.reason_code` mirroring the domain enum.
- `adapters/storage/` — thin repositories, parameterized SQL, no ORM. **Repositories never
  commit**; the caller owns the transaction boundary (NFR-8), because Ring 3.3's deposit must be
  atomic across three tables.

### 0.3 — Real ingestion (Razorpay test-mode) ✅
- `adapters/razorpay/client.py` — order creation and payment fetch. Refund *creation* is Ring 5,
  deliberately not here.
- `api/webhooks.py` — signature verification over the raw body, `INSERT OR IGNORE` on `event_id`,
  always-200 (FR-1.5), process from storage afterward.
- Self-enforced idempotency for Orders/Payments: unique `receipt` + `idempotency` table.
- **21 real payments collected** via real test-mode checkouts and committed to
  `evals/dataset/real_payments.json`, so the eval reproduces from a clone with no credentials.
- Deferred: live webhook delivery over a tunnel.

### 0.4 — Synthetic layers, generated from real data ✅
- Bank-line and ledger-entry generators, seeded, operating on any `PaymentRecord` (real or
  synthetic). Fee/tax rates for synthetic payments are *calibrated from the 21 real ones*, not
  invented.

### 0.5 — Exception dataset & generator ✅
- `evals/dataset/` — seeded generator producing the fixed 240-record set yielding **122
  exceptions** across the 9 classes in spec §5 (see FR-3.1 for why 122, not ~130).
- `evals/gold.jsonl` — one gold label per record, authored by construction.
- Split encoded as committed data, stratified so every exception class appears in both pool (62)
  and test (60).

### 0.6 — Eval harness (v0: rules-only) ✅
- `evals/runner.py` — one `match_batch` call per scenario (never pooled, FR-9.10), scored against
  gold, with an independent gold/matcher agreement check that fails loudly on disagreement
  (FR-9.11).
- Baseline #1 measured and committed before any LLM code exists.

### 0.7 — `FAILURES.md` ✅
- Opened early, not backfilled. Carries the real defects found during the build.

**Ring 0 exit checklist:** ✅ domain unit tests green · ✅ webhook signature + dedupe verified
against fixture bytes (⬜ live delivery deferred) · ✅ dataset reproducible from a clone · ✅
`evals/results/` holds the rules-only baseline.

---

## Ring 1 — Precedent retrieval + kill-criterion check

**Gate:** grounded (retrieval-augmented) resolution must beat zero-shot LLM on the ablation. If it
doesn't, the precedent-retrieval thesis is dead — fall back to plain adjudication and say so in
`docs/ARCHITECTURE.md` rather than continuing to build on a falsified premise.

### 1.1 — Precedent schema & seed corpus ✅
- `domain/precedent.py` — the Pydantic model of spec §4, plus three validators that exist because
  an unconstrained model produces precedents that are syntactically valid and permanently useless:
  no concrete record id in `situation` (it is the retrieval target, and an id pins it to one past
  case), `amount_signature` must stay a snake_case key rather than a second prose field, and
  escalation reason codes are rejected outright — they record the agent giving up, and a corpus
  that accumulates them teaches itself to escalate.
- Added the `confidence_at_deposit` column Ring 0's schema had omitted. See FAILURES.md: the
  storage round-trip tests could not have caught it, because they tested the schema against itself.
- **42 seed precedents**, all ten reason codes covered, at least three per agent-resolvable class.
  General reconciliation knowledge, deliberately *not* descriptions of the eval scenarios —
  otherwise the 1.3 ablation would measure leakage rather than retrieval.
- `prompts/deposit/v1.md`. The `situation` field is the hardest prompt-engineering problem here
  (generalise enough to match a future case, stay specific enough to remain true) — expect to
  iterate it in Ring 3+ once real deposits flow.

### 1.2 — Hybrid retrieval ✅
- `adapters/retrieval/` — BM25, dense (`sqlite-vec`), hybrid (reciprocal rank fusion), and the
  random control, all peers behind one `Retriever` protocol so the control differs from the real
  thing in exactly one respect: which precedents get chosen.
- `domain/case.py` — `ReconciliationCase.retrieval_query()`, the computed observations a rules
  engine can derive, kept separate from `summarize()`. Two bugs found here, both in FAILURES.md:
  the full record dump retrieved *worse* than the observations alone, and the shortfall arithmetic
  conflated the customer's withholding with the processor's fee.
- `evals/retrieval_eval.py` — the spec §6 comparison, committed. **BM25 90.3% top-3 and 100% top-5;
  random control 21.0%.** Hybrid scores 61.3% — *worse* than BM25 alone, because the credential-free
  `HashingEmbedder` is not semantic and its noisy ranking drags the fusion down. The spec asks this
  comparison to justify the hybrid choice with data; on the current embedder the data does not
  justify it, so the ablation runs BM25 and the result file records why.
- k=5 is the operating point, chosen because it is where lexical retrieval covers every class —
  a threshold set from the measurement rather than asserted.

### 1.3 — Zero-shot vs. grounded ablation ✅

**Result: the kill criterion PASSES**, and Ring 2 proceeds. Full write-up in
[`ARCHITECTURE.md`](ARCHITECTURE.md) §5; the numbers are in `evals/results/ablation-*.json`.

| Arm | Resolved | False resolutions | Value at risk |
|---|---|---|---|
| Zero-shot | 85.5% | 4 | ₹24,845 |
| Random control | 93.5% | 4 | ₹17,158 |
| **Grounded** | **100.0%** | **0** | **₹0** |

Two things the headline hides, both of which belong in any honest report of this:

1. **Only about half the gain is retrieval.** The random control shares the grounded arm's prompt
   shape and precedent count, so the +14.5pp over zero-shot decomposes into **+8.1pp from having
   precedents at all** and **+6.5pp from their relevance**. Quoting +14.5 as retrieval's value
   over-claims by more than 2x.
2. **The relevance leg is not statistically significant.** Paired exact McNemar: grounded vs
   zero-shot p=0.0039 (significant); grounded vs random control 4W–0L, p=0.125 (**not**
   significant). Consistent in direction, never losing a case, but four cases out of 62 cannot be
   separated from noise. A direction, not yet a demonstrated effect.

Infrastructure this ring forced, all of it in FAILURES.md: retry/quota handling that distinguishes
per-minute from per-day limits, a client-side token throttle, a circuit breaker that refuses to
write a result degraded by provider outages, and a response cache that makes re-runs free.
- Minimal LLM adapter (`adapters/llm/`) — Gemini 2.5 Flash primary, Ollama fallback, behind a
  vendor-agnostic interface.
- A thin, non-graph resolution path (LangGraph is Ring 2) that either resolves zero-shot with no
  precedent context, or injects top-k retrieved precedents.
- Run both against the **62 pool exceptions** (never the test set), score against gold, commit.
- **This is the kill-criterion checkpoint.** Compare honestly before writing a line of Ring 2.

**Ring 1 exit checklist:** ablation result committed; kill criterion evaluated and its outcome
(pass, or the documented fallback) stated plainly in `docs/ARCHITECTURE.md`.

---

## Ring 2 — LangGraph investigation graph

**Gate:** re-measure against Ring 1's ablation. The full graph should not regress relative to the
simple grounded prompt; if it does, that's a finding to report, not to hide.

**Result: gate met, and a finding that matters more than the gate.** Grounded scores 100.0%
through both the Ring 1 chain and the Ring 2 graph — no regression. But the graph lifted the
*ungrounded* arms to within one case of it:

| Arm | Ring 1 chain | Ring 2 graph |
|---|---|---|
| Zero-shot | 85.5% | **98.4%** |
| Random control | 93.5% | **98.4%** |
| Grounded | 100.0% | 100.0% |

Grounding beat zero-shot by 14.5pp at p=0.0039 in Ring 1; in Ring 2 it beats it by 1.6pp — one
case — at p=1.0. **No comparison is significant any more.** Two causes, both real: the tools let
the agent *derive* what a precedent would have *told* it, and a verifier fix found in the
zero-shot arm's errors lifted that arm from 88.7% to 98.4%. The fix helped the baseline, not the
treatment — the honest direction, and the reason both rings' numbers must be read together.

This is a ceiling effect, not evidence against retrieval: at 98–100% with 62 cases, no
comparison *could* reach significance. See [`ARCHITECTURE.md`](ARCHITECTURE.md) §5.

### 2.1 — Graph skeleton ✅
- `graph/state.py`, `graph/nodes.py`, `graph/investigation.py` — one function per node in spec §7,
  wired with the `verify → revise (max 2) → verify` cycle and the three-way `route`.

### 2.2 — Investigation tools ✅
- The six tools bound to `investigate`, capped at 5 calls. Each is a thin wrapper over the storage
  and domain layers built in Ring 0 — **no new business logic inside a tool**. In particular,
  netted-group reasoning must call `matching.is_netted_group_match`, not reimplement it.

### 2.3 — Structured output & verification ✅
- `propose_resolution` returns a Pydantic-validated object; reject and retry on schema violation.
- `verify` checks arithmetic closure to the paise (reusing `domain/money.py`, including the
  net-vs-gross distinction in FR-2.8) and whether cited precedents actually apply.

### 2.4 — Fallback discipline ✅
- Every LLM call site has an explicit fallback to escalation with a reason code on parse failure,
  low confidence, or model unavailability. **Test each fallback path directly** — do not rely on it
  firing by accident during a demo.

**Ring 2 exit checklist:** full graph run end-to-end with a visible trace; re-measured accuracy
committed; regressions documented, not hidden.

---

## Ring 3 — Deposit loop, human gate, learning curve ← the submission

This is what the buildathon submission is built around. Do not rush it to reach Rings 4–5.

### 3.1 — `interrupt` gate
- LangGraph `interrupt` at the `gate` node with checkpointed state. **Prove durability with an
  actual process-restart test** — a paused resolution must survive it. A design note is not proof.
- `api/approvals.py` — endpoints backing the approval UI: fetch pending exception + proposal,
  submit confirm/correct/reject.

### 3.2 — Minimal approval UI
- The screen in `product_design.md` §3.2 and §4. No auth, no multi-user routing — single operator.

### 3.3 — Deposit loop
- `usecases/deposit.py` — fires only on `confirmed` or `corrected`; never on `rejected` or
  unreviewed. Corrected payloads deposit the corrected version.
- **One transaction** covering the human action, the audit row, and the precedent insert (FR-7.5).
  The `db.transaction()` boundary exists for this.
- Each deposit stamps `corpus_version`, `derived_from`, `deposited_at`.

### 3.4 — Corpus snapshotting & replay
- ⚠️ **Resolve the snapshot scale first** (see Status). Recommended 0/25/50/75/100.
- Snapshot by `corpus_version` cutoff, not wall-clock, so replay is exact.
- `evals/replay.py` — the frozen 60-exception test set against each snapshot.

### 3.5 — Negative control & precedent precision
- Random-precedent control at the same batch and *k*, run **automatically** on every replay so it
  cannot be skipped under time pressure.
- Precedent precision: of precedents cited, the fraction that actually applied — reusing `verify`'s
  applicability check as the scoring function.

### 3.6 — The learning-curve chart + eval report v1
- The headline chart: resolution rate vs. corpus size, real vs. negative control, with the Ring 0/1
  baselines overlaid.
- Static HTML eval report rendering **directly from committed `evals/results/*.json`** — never
  hand-transcribed numbers, so regenerating it is a reproducibility check.

**Ring 3 exit checklist:** learning curve committed with both lines; precedent precision reported;
report renders from committed files; `FAILURES.md` updated.

---

## Ring 4 — Calibration

**Gate:** re-measure after threshold changes; confirm precision-at-coverage moved in the intended
direction, not merely that accuracy rose.

- **4.1** Calibration curve from accumulated outcomes — does a 0.8-confidence resolution actually
  get confirmed ~80% of the time?
- **4.2** Set the `route` threshold from that curve rather than the placeholder constant in
  `domain/confidence.py`; document the operating point and its precision/coverage tradeoff.
- **4.3** Precision-at-coverage as a standing metric in the report.

**Exit:** the threshold change is traceable to calibration data, not intuition; before/after
committed.

---

## Ring 5 — Bounded remediation

**Gate:** re-measure; ceiling and stopping rule demonstrably enforced, not merely coded.

- **5.1** `usecases/remediate.py` — real test-mode refund via the Razorpay API. Real
  `X-Refund-Idempotency` on every call; handle `409 Conflict` explicitly. **Verify the SDK's
  header-passing shape against a live call before relying on it** — this was deliberately left
  unimplemented in Ring 0 rather than guessed at.
- **5.2** A second gate distinct from the resolution gate, showing ceiling and usage. Prove the
  stopping rule by driving the ceiling to exhaustion in a test, not by code review.

**Exit:** a real test-mode refund fired end-to-end under the gate; ceiling test passes; final
report re-run and committed.

---

## Cross-cutting, ongoing

- **`docs/ARCHITECTURE.md`** — written incrementally per spec §11, never retrofitted. Already
  carries Ring 0's decisions and limits.
- **Tests** — risk-based per spec §10: decision-logic boundaries, idempotent webhook replay,
  raw-body signature verification, LLM parse-failure/timeout paths, verify-node arithmetic closure,
  and the graceful-failure path shown on video.
- **Eval discipline** — re-run before any claim of improvement; commit every result file, including
  bad ones; never report a cherry-picked case.
- **`FAILURES.md`** — log defects as they surface, with how they were caught. Its value is that it
  is unflattering.

## Execution ordering under a time-box

Rings are ordered by dependency, not calendar. Plug in the real deadline and work backward, but
preserve this weighting:

1. **Ring 0 is larger than it looks** — it is where money-precision and idempotency bugs are caught
   cheaply, before an LLM can paper over them. (Borne out: it surfaced a net-vs-gross matching bug,
   an orphan-detection gap, and a transaction-boundary defect.)
2. **Ring 1's kill criterion is a real decision point.** Budget time to *act* on a failing result —
   falling back to plain adjudication — rather than assuming it passes.
3. **Ring 3 produces the actual submission artifact.** Protect its budget even at the cost of
   Rings 4–5.
4. **Rings 4 and 5 are genuinely optional** relative to the thesis. If time runs short, submit after
   Ring 3 with an honest note about what wasn't reached, rather than rushing them and weakening the
   curve.
