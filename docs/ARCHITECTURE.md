# Architecture

Written incrementally from Ring 0, per spec §11 — not retrofitted at the end. Sections 5 and 6
fill in as the rings that produce them land; what is written below is what has actually been
built and measured, not what is planned.

---

## 1. The problem

Reconciliation exceptions are resolved and then forgotten. A finance analyst works out that a
payment is short by exactly 2% because the customer deducted TDS, closes the ticket, and the
reasoning evaporates with it. The next structurally identical exception arrives a week later and
is escalated again, at the same cost. Every reconciliation, ticketing, and RPA product in this
space treats an exception as a unit of work to be closed, not as a unit of knowledge to be kept.

Precedent's claim is that the resolution *is* the knowledge, and that capturing it in a
retrievable form makes the system measurably better at its own job over time. The submission is
not the agent; it is the curve showing autonomous resolution rate on **unseen** exceptions rising
as the corpus grows.

## 2. The loop

```
ingest (real Razorpay test-mode payments + webhooks)
   → deterministic matcher clears the confident majority
   → residual exceptions go to a retrieval-grounded investigation graph
   → verify (arithmetic closes to the paise; cited precedents actually apply)
   → human gate: confirm / correct / reject
   → deposit precedent  ──────┐
        ▲                     │
        └── retrieval ────────┘   (the corpus feeding the next investigation)
```

The loop closes at `deposit_precedent`, and only on human confirmation or correction. That closure
is the whole thesis: without it, this is an ordinary adjudication agent.

## 3. Deterministic core, agent at the edges

The deterministic matcher (`domain/matching.py`) is authoritative wherever it can decide. It runs
four tiers — exact match, date-window match, tolerance-band match, and a netted-group checker —
and is measured **before any LLM code exists in the repo** (spec's Ring 0 gate).

What it deliberately does *not* do is as important. It does not attempt combinatorial discovery of
which payments net into which settlement credit; that needs grouping, not joining, and belongs to
the investigation graph. Everything it cannot confidently resolve is emitted as an exception
record rather than silently dropped — including a ledger entry with no payment behind it and a
bank credit nobody claims, which are as much unresolved cases as an orphaned payment.

**What happens when the model is wrong** (from Ring 2 onward): every LLM call path — parse
failure, low confidence, model unavailable, verification failed twice — terminates in escalation
with a reason code. The system never guesses and never stalls. The exception list is a designed,
permanent surface, not a bug tray.

## 4. Decisions, and rejected alternatives

The stack table (FastAPI over Flask, LangGraph over a plain chain, `sqlite-vec` over a hosted
vector DB, and so on) is spec §3 and is not restated here. What follows is decisions taken during
implementation, with the reasoning that produced them.

### Money is integer paise, enforced at three layers

The rule is absolute, so it is enforced where violations actually happen rather than by
convention:

1. `money.rupees_to_paise` raises `TypeError` on a `float` input rather than coercing it.
2. `money.apply_rate_paise` is the single choke point through which any percentage — PSP fee, GST
   on that fee, TDS — touches money, and it likewise refuses `float` rates. Every rate calculation
   has to pass through it, so no caller has to remember the rule individually.
3. The SQLite schema carries `CHECK (typeof(amount_paise) = 'integer')` on every paise column, so
   even a caller bypassing the domain layer cannot persist a float.

The eval dataset generator originally computed fees and TDS reconstruction in float arithmetic —
`round(amount_paise / (1 - tds_rate))`. On this dataset it happened to produce values identical to
the Decimal implementation, so no committed number changed when it was fixed; it was a latent
violation, not an active defect. It was fixed anyway, because the invariant is only worth stating
if it holds everywhere.

### Repositories never commit; the caller owns the transaction

Every repository method originally committed on its own. That is fine until a single logical
operation spans several writes — and Ring 3's deposit does exactly that: record the human action,
append the audit row, insert the precedent. Under per-method autocommit, a crash midway leaves a
precedent with no audit trail, or a confirmed resolution that never deposited. Either silently
corrupts the corpus the entire thesis rests on, and breaks NFR-5 (every state change
reconstructable from `audit_log` alone).

Repositories now only execute; the caller owns the boundary via `db.transaction(conn)`, and an API
request is itself one transaction (`api/deps.get_connection`).

### Self-enforced idempotency where Razorpay has none

Stated plainly, because it is easy to overclaim:

- **Webhooks** have a real dedupe key. `x-razorpay-event-id` is the primary key of
  `webhook_events`; delivery is `INSERT OR IGNORE`, acked 200, and processed from storage
  afterwards — never off the wire.
- **Refunds** have real native support via the `X-Refund-Idempotency` header (min 10 chars, 409 on
  conflict). Not yet exercised — refund *creation* is Ring 5.
- **Orders and Payments have no native idempotency header at all.** This is self-enforced with a
  unique `receipt` plus the `idempotency` table. It is not the same guarantee and is not described
  as one.

A note on `INSERT OR IGNORE`: SQLite applies it to CHECK violations exactly as it does to
duplicate-key conflicts — silently, with `rowcount` 0 and no exception. A malformed `event_type`
was therefore indistinguishable from a harmless replay, so the repository validates `event_type`
explicitly before inserting. The CHECK constraint remains as defence in depth for direct writers.

### The webhook receiver always acks

Razorpay retries anything that is not 2xx. A signature failure, an unknown event type, or a body
that is not valid UTF-8 are all recorded (with `signature_valid=False` where relevant) and acked
200, because retry-storming ourselves over input we have already durably captured is strictly
worse than recording it. Nothing downstream reads that table without checking `signature_valid`
first — recording is not trusting.

### One deliberate deviation from the spec's numbers

The spec's arithmetic ("240 records yielding ~130 exceptions") counts all eight non-clean-match
classes as exceptions, including the 4% fee/tax rounding-delta class. But the deterministic
matcher's tolerance tier already resolves a ±₹1 delta correctly and confidently. Escalating that
to an LLM to hit a round number would be worse engineering, so rounding-delta records are
rules-resolved.

The consequences, stated rather than buried: **122 exceptions, not ~130**, and a corpus pool of
**62, not ~70**. The held-out test set is unchanged at exactly 60. The rules-only baseline is
correspondingly *stronger* than the spec's implied 45% floor — 49.2%, being 45% exact matches plus
4% tolerance matches — which makes it a harder baseline for the agent to beat, not an easier one.

### The eval scores each scenario in isolation

`match_batch` searches all currently-available credit lines when resolving a match, not just those
belonging to one scenario. Pooling all 240 scenarios into a single call would therefore admit a
rare cross-scenario collision — one scenario's bank line coincidentally satisfying another's
payment, stealing a match and producing a spurious exception elsewhere. With ~240 whole-rupee
amounts drawn from a few thousand buckets, that is not a negligible probability. The runner
evaluates one scenario per `match_batch` call, which removes the class of error by construction.

## 5. Results

Ring 0, deterministic rules alone — the baseline every later ring is measured against:

| Metric | Value |
|---|---|
| Autonomous resolution rate | 49.2% (118/240) |
| Resolution rate on held-out test set | 0.0% |
| Escalation rate | 50.8% (122 records) |
| Duplicate-payment false accepts | 0 |
| Gold/matcher label disagreements | 0 / 240 |

The 0.0% on the held-out test set is not a defect: every test-set record is an exception by
construction, so a rules-only system must score zero there. That is precisely the floor the
retrieval-grounded agent has to beat, and the reason the number is reported rather than omitted.

The runner independently re-derives each scenario's outcome from the matcher and compares it
against the gold label. If the dataset and the matcher ever disagree, the run fails loudly instead
of quietly producing metrics built on sand.

*The learning curve, the four baselines, and the negative control land here as Rings 1–3 complete.*

## 6. Limits

Known and stated up front, not discovered by a reviewer:

- **Bank statement lines and the internal ledger are synthetic**, generated from real payment data.
  Only 21 payments are genuinely real (see README). Razorpay provides no server-side way to create
  a captured payment, which caps how much of the dataset can be real at hand-clickable scale.
- **The dataset is constructed, so the gold labels are constructed too.** Each scenario's correct
  answer is known because the scenario was deliberately built to have it. That makes the labels
  reliable but also means the exception mix reflects the spec's chosen distribution, not an
  organically observed one.
- **Thresholds are not yet calibrated.** The confidence threshold is a placeholder constant until
  Ring 4 sets it from a calibration curve.
- **No drift monitoring.** Nothing detects the corpus degrading or the exception mix shifting over
  time.
- **Corpus poisoning is a real risk.** A human who confirms a wrong resolution deposits a wrong
  precedent, which is then retrieved to justify future wrong resolutions. Corrections deposit too,
  which helps, but nothing currently detects or retracts a bad precedent after the fact.
- **Live webhook delivery has not been exercised end-to-end.** Signature verification and dedupe
  are tested against fixture bytes; a real Razorpay delivery over a public tunnel has not yet been
  run.
