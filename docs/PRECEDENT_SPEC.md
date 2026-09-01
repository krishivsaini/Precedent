# Precedent — Build Spec

**An exception-resolution agent whose knowledge base is written by its own operation.**

Razorpay AI Buildathon, Track 04 (AI Finance Controller). Solo build.

---

## 1. Thesis

In every existing reconciliation, ticketing, or RPA system, resolving an exception destroys the
knowledge that resolution produced. The insight is consumed and discarded; the next similar
exception is escalated to a human again.

Precedent inverts this. Each confirmed resolution is written back as a structured, retrievable
precedent. The corpus is authored by the system's own operation, and autonomous resolution rate
on **unseen** exceptions rises as the corpus grows.

The submission is that curve.

---

## 2. Scope

### In scope — one workflow

Three-way reconciliation exception resolution: PSP records vs bank statement lines vs internal
order ledger. Deterministic matcher clears the confident majority; the residual goes to a
retrieval-grounded investigation agent; confirmed resolutions deposit precedents.

### Explicitly not building

- Dashboard beyond a static HTML eval report + a minimal approval UI
- Auth, multi-tenancy, user accounts
- Chat interface over the ledger
- Live-mode anything
- Cash forecasting
- Multi-agent orchestration (one investigation graph, not a team of agents)

### Honest data provenance — state this verbatim in the README

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

---

## 3. Stack

| Concern | Choice | Alternative rejected, and why |
|---|---|---|
| API | FastAPI | Flask — no native async, no Pydantic integration, no SSE story |
| Agent | LangGraph | Plain function chain — no cyclic verify/revise, no durable `interrupt` gate |
| Retrieval | LangChain retrievers + `sqlite-vec` + `rank_bm25` | Pinecone/Weaviate — reviewer must clone and run in 60s with no provisioning |
| Validation | Pydantic v2 | Hand-rolled dict checks — no schema at the model-output boundary |
| Faithfulness | Ragas | Unvalidated LLM judge — Ragas judge is itself calibrated against 15 hand-scored items |
| Storage | SQLite | Postgres — provisioning cost buys nothing at this volume |
| Model | Gemini 2.5 Flash primary, Ollama fallback | Single-vendor coupling; adapter keeps it model-agnostic |

**Money is integer paise everywhere. No floats. Ever.**

---

## 4. Data model

```
payments          payment_id PK, order_id, amount_paise, fee_paise, tax_paise,
                  captured_at, status, source ENUM('razorpay','synthetic')

bank_lines        line_id PK, value_date, amount_paise, direction, narration, source

ledger_entries    entry_id PK, order_id, invoice_no, customer_name,
                  expected_amount_paise, terms

webhook_events    event_id PK,          -- x-razorpay-event-id; PK IS the dedupe
                  event_type, raw_body TEXT, signature_valid,
                  received_at, processed_at
                  -- INSERT OR IGNORE, return 200, then process from storage

exceptions        exception_id PK, batch_id, kind, member_refs JSON,
                  detected_at, status, correlation_id

resolutions       resolution_id PK, exception_id FK, proposed_by ENUM('rule','agent'),
                  confidence REAL, rationale TEXT, cited_precedents JSON,
                  verified BOOL, human_action ENUM('confirmed','corrected','rejected'),
                  corrected_payload JSON NULL, resolved_at

precedents        precedent_id PK, situation TEXT, resolution TEXT, reason_code,
                  entities JSON, amount_signature TEXT, embedding BLOB,
                  derived_from_resolution FK, deposited_at, corpus_version INT,
                  times_retrieved INT, times_cited_correctly INT

audit_log         id PK, correlation_id, stage ENUM('detected','retrieved','decided',
                  'verified','gated','acted'), actor, input_digest, output_digest,
                  model, latency_ms, tokens, reason, created_at

idempotency       key PK, request_digest, response JSON, created_at
```

### Precedent schema — the core artifact

The `situation` field is the retrieval target and the hardest prompt-engineering problem in the
project: it must generalise enough to match a similar future case, while staying specific enough
to remain true.

```python
class Precedent(BaseModel):
    situation: str          # generalised description — the retrieval target
    resolution: str         # what was done and why
    reason_code: ReasonCode # closed enum
    entities: list[str]     # vendor/customer names, reference patterns
    amount_signature: str   # e.g. "short_by_2pct_tds", "netted_with_refund"
    confidence_at_deposit: float
    derived_from: str       # resolution_id — full provenance
```

Prompt versions for precedent authoring live in `prompts/deposit/v1.md`, `v2.md`, ... and the
eval measures whether v3 precedents retrieve better than v2. **Prompt engineering with a number
attached.**

### Idempotency — stated honestly

- **Refunds**: real header support, `X-Refund-Idempotency`, min 10 chars, handle 409 Conflict
- **Payouts**: `X-Payout-Idempotency`, mandatory — not used here, noted for completeness
- **Orders/Payments**: **no native idempotency header exists.** Self-enforced via unique `receipt`
  plus the `idempotency` table above. Do not overclaim this in the architecture doc.
- **Webhooks**: PK on `event_id`, INSERT OR IGNORE, process from storage

---

## 5. The exception batch

`evals/dataset/` — seeded generator, fixed seed, committed, reproducible. **240 records**
yielding ~130 exceptions. Gold labels hand-authored in `gold.jsonl`.

| Class | Share | Why it resists rules |
|---|---|---|
| Clean 1:1 match | 45% | Baseline must get all of these |
| Netted settlement (many payments → one credit, minus fees) | 15% | Grouping, not joining |
| Direct NEFT bypassing PSP, garbled narration | 10% | Fuzzy entity match on free text |
| TDS short-payment (2% / 10%) | 8% | Amount wrong by a computable rule |
| Split payment across two invoices | 6% | One credit, two counterparts |
| Refund netted into same-day credit | 5% | Reduces expected total |
| Fee/tax rounding delta ±₹1 | 4% | Tolerance-band judgement |
| Duplicate customer payment | 3% | Must **not** double-match — tests false accepts |
| **Genuinely unmatchable** | **4%** | **No valid counterpart exists** |

That last row is load-bearing: any system reporting 100% coverage is provably lying, including
this one. It makes the exception list mandatory rather than decorative.

### Held-out split

- **Corpus pool** (~70 exceptions) — resolutions from these may be deposited
- **Test set** (60 exceptions) — **never deposited**, replayed at every corpus snapshot

---

## 6. Eval design — the primary artifact

### The replay protocol

Freeze the 60-exception test set. Run it against corpus snapshots at **0, 50, 100, 150, 200**
deposited precedents. Same questions, growing knowledge, measured every time.

Output: autonomous resolution rate on unseen exceptions, plotted against corpus size. **This chart
is the submission.**

### Three controls that stop the curve being an artifact

1. **Held-out test set.** Test exceptions are never deposited. The system answers genuinely unseen
   cases from accumulated precedent, not its own recall.
2. **Random-precedent negative control.** Same batch, same *k*, but precedents sampled at random
   instead of retrieved. If random helps as much as relevant, retrieval is doing nothing and the
   curve is a prompt-length artifact. **Run this unprompted — it is the check a good panel
   engineer would ask for.**
3. **Precedent precision measured separately.** Of the precedents cited in a resolution, what
   fraction actually applied. Retrieval quality isolated, not hidden inside end-to-end accuracy.

### Metrics — reported on every run, committed to `evals/results/YYYY-MM-DD-HHMM.json`

| Metric | What it establishes |
|---|---|
| Autonomous resolution rate @ corpus size | The learning curve — headline |
| **False-resolution cost in ₹** | The number that gets someone fired. Rupees, not counts. |
| Precedent precision | Retrieval quality, isolated |
| Rationale faithfulness (Ragas) | Grounding. Judge calibrated against 15 hand-scored items; report correlation. |
| Escalation rate | Should fall while accuracy holds |
| BM25-only vs dense-only vs hybrid | Justifies the hybrid choice with data |
| p50 / p95 latency, tokens per exception | Cost discipline |
| Exception list | What it still cannot do, with reason codes |

### Baselines — all four, all committed

1. Deterministic rules alone (measured in Ring 0, **before any LLM code exists**)
2. Zero-shot LLM, empty corpus
3. Random-precedent control
4. Retrieval-grounded agent (the system)

### Discipline

Re-run before any claim of improvement. Commit every result file, including bad ones. Never report
a single cherry-picked case.

---

## 7. Investigation graph (LangGraph)

```
detect_exception
   → classify_kind
   → retrieve_precedents (hybrid: BM25 + dense)
   → investigate (tool loop, max 5 calls)
        tools: fetch_payment, fetch_ledger_entry, fetch_bank_lines,
               fetch_refunds, compute_expected_amount, search_prior_resolutions
   → propose_resolution (structured output, Pydantic-validated)
   → verify
        - does the arithmetic close to the paise?
        - do cited precedents actually apply?
        ├ fail → revise (max 2) → verify
        └ pass ↓
   → route
        ├ confidence ≥ threshold → auto_resolve
        ├ confidence < threshold → escalate_with_draft
        └ verify failed twice    → escalate_raw + exception list
   → gate  [interrupt: human confirms / corrects / rejects]
   → commit → audit → deposit_precedent
```

`deposit_precedent` fires only on human confirmation or correction. **Corrections deposit too** —
a corrected resolution is a higher-value precedent than a confirmed one, because it encodes a case
the system got wrong.

`interrupt` justification for the panel: durable human gate on a state-changing action, with
checkpointed state so a paused resolution survives a process restart. A chain gives neither.

**Fallback for every model call:** parse failure, low confidence, or model unavailable → escalate
to the exception list with a reason code. The system never guesses and never stalls.

---

## 8. Repo layout

```
src/precedent/
  domain/           # pure, zero I/O, unit-testable with no network
    money.py        # integer paise, tolerance bands
    matching.py     # deterministic rules
    reasons.py      # reason code enum
    confidence.py   # thresholds, calibration bins
  usecases/
    ingest.py  detect_exceptions.py  resolve.py  deposit.py  remediate.py
  adapters/
    razorpay/  llm/  retrieval/  storage/  clock.py
  graph/
    investigation.py  nodes.py  state.py
  api/
    main.py  webhooks.py  approvals.py  stream.py
prompts/
  investigate/v1.md  deposit/v1.md  verify/v1.md
evals/
  dataset/  gold.jsonl  runner.py  replay.py  results/
docs/
  ARCHITECTURE.md
FAILURES.md         # opened on the FIRST commit, not the last
```

---

## 9. Rings — repo submittable at the end of each

| Ring | Deliverable | Gate |
|---|---|---|
| **0** | FastAPI skeleton, real order → payment → verified webhook → dedupe, three-source ingestion, deterministic matcher, exception generator, eval harness | **Rules-only baseline committed before any LLM code** |
| **1** | Precedent schema, ~40 seeded, hybrid retrieval, zero-shot vs grounded ablation | **KILL CRITERION** — if grounded does not beat zero-shot, the thesis is dead. Fall back to plain adjudication. |
| **2** | LangGraph investigation graph, tools, verify/revise cycle, structured output | Re-measure |
| **3** | Deposit loop + `interrupt`. **Learning curve, corpus snapshots, held-out replay, negative control.** | **← APPLY HERE** |
| **4** | Calibration curve, threshold set from data, precision-at-coverage | Re-measure |
| **5** | Bounded remediation: refund via test-mode API, gate, `X-Refund-Idempotency`, ceiling, stopping rule | Re-measure |

---

## 10. Tests — risk-based, not coverage-driven

- Decision logic incl. boundary cases (tolerance bands, confidence thresholds)
- Idempotent replay of the same webhook event
- **Raw-body signature verification against fixture bytes** — the classic failure
- LLM parse-failure and timeout paths
- Verify-node arithmetic closure
- The graceful-failure path shown on video

---

## 11. Architecture doc — start now, do not retrofit

1. The problem, and why resolving exceptions destroys knowledge everywhere else
2. The loop, with diagram
3. Deterministic core, retrieval-grounded agent at the edges — and what happens when the model is wrong
4. Decisions and rejected alternatives (§3 table), integer paise, self-enforced idempotency where Razorpay has none
5. Results: the learning curve, all four baselines, the negative control
6. Limits: synthetic bank data, threshold tuned on 240 records, no drift monitoring, corpus poisoning risk if a human confirms a wrong resolution

---

## 12. Ring 0, task 1 — start here

Pure domain, no network, tests first:

- `domain/money.py` — integer paise arithmetic, tolerance bands, TDS deduction computation
- `domain/matching.py` — exact match, tolerance match, date-window match, netted-group detection
- `domain/reasons.py` — the reason code enum

Nothing else gets written until the deterministic baseline is measured and on disk.
