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

### Rings 1–2 — the kill criterion, and what tools do to it

Re-measured on `nvidia/nemotron-3-super-120b-a12b` against the current 194-exception dataset,
so these numbers sit on the same footing as the learning curve below. Three arms over the 134
pool exceptions, same model, same prompt, same *k*=5, corpus of 42 seed precedents. Only the
contents of the precedent block differ.

| Engine | Zero-shot | Random control | Retrieval-grounded | Value at risk (grounded) |
|---|---|---|---|---|
| Ring 1 chain | 29.1% | 43.3% | 52.2% | ₹155,631 |
| **Ring 2 graph** | **48.5%** | **48.5%** | **58.2%** | **₹123,680** |

**Ring 2's gate — the graph must not regress against the chain — is met with room to spare:**
grounded goes 52.2% → 58.2% and value at risk falls by ₹32,000. The original measurement had
both engines at 100% on an easier dataset, where "no regression" was the only thing the gate
could say. With headroom, it says something.

**The interesting number is what the tools do to the *control*.** The gain over zero-shot
decomposes into an effect of having precedents at all and an effect of their relevance:

| Engine | From having precedents at all | From their relevance |
|---|---|---|
| chain | **+14.2pp** | +9.0pp |
| graph | **+0.0pp** | **+9.7pp** |

On the chain, a *random* precedent is worth 14 points — format, priming, a worked example of
the reasoning style, none of it to do with retrieval. On the graph that effect vanishes
entirely: the control lands on exactly zero-shot's rate, 48.5% against 48.5%, 7W–7L at p=1.0.
The investigation tools already supply whatever a random precedent was supplying, so what
survives is pure relevance — and it survives at essentially the same size across both engines,
+9.0 and +9.7 points.

Both grounded comparisons on the graph are significant (15W–2L, p=0.0023 against each of
zero-shot and the control). That is the claim Ring 1 could not make: there, relevance was
+6.5pp at p=0.125, and the honest verdict was "a direction, not a demonstrated effect".

**The cost side, which the headline hides.** Grounding roughly halves escalation on the graph
(29.8% → 8.2%) and converts those escalations into *both* correct answers and expensive wrong
ones. Outright errors rise 21.6% → 33.6% and value at risk rises ₹76,646 → ₹123,680. On spec
§6's own framing — the false-resolution cost is "the number that gets someone fired" — that
trade is not self-evidently good. It is the strongest argument for Ring 4: the confidence
threshold that decides when to escalate is still a placeholder `0.8` that nothing set from
data.

**Retrieval quality, measured alone** (no model involved), on the same 134 pool exceptions:
BM25 reaches 53.7% top-3 against the random control's 12.7%, with all seven derivable classes
fully covered at k=5 and the two counterparty classes at **0/30 and 0/24** — unreachable from
seeds by construction, which is the headroom the learning curve is measured in.

### Ring 3 — the learning curve

The held-out 60-exception test set, replayed unchanged against the corpus at five sizes. The
test set is never deposited. A **simulated reviewer** works each pool case the way an operator
would: confirming when the agent is right, correcting when it is wrong, resolving escalations,
and rejecting 15% outright. Of 134 pool exceptions that produced 66 confirmations, 44
corrections and 24 rejections — **109 precedents, not 134**, because a corpus that grows one
precedent per case is the best case the mechanism can possibly have.

`nvidia/nemotron-3-super-120b-a12b`, k=5, 18 counterparty cases in the held-out set.

| Deposits | Corpus | Resolved | Random control | Counterparty subset | Escalated | Precedent precision |
|---|---|---|---|---|---|---|
| 0 | 42 | 70.0% | 56.7% | **0.0%** | 8.3% | 81.8% |
| 33 | 70 | 78.3% | 58.3% | 50.0% | 18.3% | 98.9% |
| 67 | 96 | 81.7% | 66.7% | 66.7% | 18.3% | 100.0% |
| 100 | 121 | 86.7% | 60.0% | 72.2% | 10.0% | 96.5% |
| **134** | **151** | **86.7%** | **61.7%** | **83.3%** | 13.3% | 100.0% |

Paired exact McNemar, first snapshot to last, over the same 60 cases:

| Comparison | | p | |
|---|---|---|---|
| Headline | 15W–5L | 0.041 | **significant** |
| Counterparty subset | 15W–0L | **0.00006** | **significant** |
| Random control | 10W–7L | 0.63 | not significant — flat |

**The control stays flat while the treatment rises.** It draws the same *k* precedents from
the same growing corpus, differing only in relevance, and moves from 56.7% to 61.7% —
indistinguishable from noise across a corpus that more than tripled. Whatever lifts the
treatment arm is not the presence of more text in the prompt.

**Every gain is a counterparty case; every loss is a derivable one.** The decomposition is
exact:

| | gained | lost | net |
|---|---|---|---|
| Counterparty (18 cases) | 15 | 0 | **+15** |
| Derivable (42 cases) | 0 | 5 | **−5** |

So the corpus is worth +15 where the answer cannot be worked out from the case, and costs −5
where it can — four `tds_short_payment` and one `direct_neft_bypass` that were correct against
the seed corpus alone and are wrong against a corpus of 151. That is the same distraction the
control measures, and it is a real price rather than a rounding error: **a third of the gross
gain is spent on cases the corpus should never have been consulted for.**

The honest form of the claim, then:

> A precedent corpus improves resolution **only for knowledge that cannot be derived from the
> case in front of the agent** — and mildly degrades the cases that can be. Where the answer is
> computable, an investigation tool derives it and the corpus is a distraction.

Escalation did not fall monotonically (8.3% → 13.3%, peaking at 18.3%), which is worth noting
against the spec's expectation that it should decline: the system became more accurate without
becoming more willing to answer.

**What the precedent-precision breakdown cannot tell us.** Spec §7 asserts that a corrected
resolution is the higher-value precedent. With 44 corrections and 66 confirmations in the
corpus that is finally checkable — and the measurement is saturated: seed, confirmed and
corrected precedents all score 100% precision at the last snapshot. Corrections are cited 35
times against confirmations' 62, roughly in proportion to their share of the corpus. **The
claim is neither supported nor refuted here.** The proxy (agreement between the cited
precedent's reason code and the case's) is too coarse to separate them, and saying so is
better than reading a tie as agreement.

**Still not a forecast.** The 15% rejection rate is a stated assumption with no data behind
it; the reviewer always corrects to the right answer, where a real one would sometimes be
wrong; and the counterparty task is recall of a customer's standing terms, which is genuine
institutional knowledge but not deep generalisation.

**Model provenance.** These numbers come from `nemotron-3-super-120b-a12b`. Every result
committed before 2026-09-03 came from `openai/gpt-oss-120b`, which reached end of life at
08:00 UTC that day — mid-run. Those results stand as the record of what was measured and are
**not comparable** with these.

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
- **Retrieval score is not a relevance signal.** BM25 ranks well (90% same-class at top-3) but
  its scores do not separate relevant precedents from irrelevant ones — measured, not assumed:
  other-class hits sit *closer* to the top score than same-class hits do. So there is no
  score-based way to suppress precedents that do not apply, and every case pays the token cost
  of *k* precedents whether or not any of them help. See FAILURES.md.
- **No drift monitoring.** Nothing detects the corpus degrading or the exception mix shifting over
  time.
- **After Ring 2, no arm's advantage over another is statistically significant.** All three sit
  at 98–100% on the pool set. The precedent corpus's measured marginal contribution is one case.
  This is a ceiling effect rather than evidence against retrieval, but it means the project's
  central claim is currently **unproven on this dataset** rather than demonstrated.
- **The grounded arm scores 100% on the pool, which leaves no headroom there.** Ring 3's learning
  curve cannot be measured on the pool set — it is saturated — so the curve has to come from the
  held-out test set. A dataset calibrated to make a rules engine score 49% turns out not to be
  hard for a 120B model reasoning with good precedents.
- **The relevance effect is underpowered.** 4 discordant cases out of 62 (p = 0.125). The direction
  is consistent and the grounded arm never loses a case to the control, but confirming it needs
  more exceptions than the pool contains.
- **Retrieval query rendering is fitted to the nine known exception classes.** The observations that
  make a case retrievable were chosen knowing which classes exist. A tenth class would arrive with
  no observation phrased to distinguish it and would retrieve badly. No number here measures that.
- **Provider limits shaped what could be measured, not just how fast.** Gemini's free tier caps at
  20 requests/day against an ablation needing 186, and Groq's daily token budget ran out partway
  through. Results come from whichever provider could complete a run; models differ, so only the
  arms *within* one run are comparable. See FAILURES.md.
- **Corpus poisoning is a real risk.** A human who confirms a wrong resolution deposits a wrong
  precedent, which is then retrieved to justify future wrong resolutions. Corrections deposit too,
  which helps, but nothing currently detects or retracts a bad precedent after the fact.
- **Live webhook delivery has not been exercised end-to-end.** Signature verification and dedupe
  are tested against fixture bytes; a real Razorpay delivery over a public tunnel has not yet been
  run.
