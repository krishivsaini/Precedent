# Precedent — Product Design

Companion to [`requirements.md`](requirements.md) and [`implementation_plan.md`](implementation_plan.md).
This document covers *who the system serves, what it shows them, and why it's shaped the way it
is* — not the eng sequencing.

---

## 1. Thesis, restated as a product claim

Every reconciliation tool a finance team already owns treats an exception as a one-off ticket:
someone investigates, resolves it, and the reasoning evaporates. The next structurally-identical
exception gets escalated again, forever, at the same human cost.

Precedent's product claim: **the system should get measurably better at reconciliation the more
it reconciles**, because every human-confirmed resolution is retained as a reusable precedent, not
just a closed ticket. The product surface exists to make that claim checkable — every screen a
reviewer sees either produces a precedent or explains why one wasn't produced.

## 2. Users

There is exactly one operator persona, seen in two moments:

| Moment | What they're doing | What they need from the UI |
|---|---|---|
| **Reviewing** | An exception has a proposed resolution and needs a decision | Enough context to confirm/correct/reject in under a minute, without re-deriving the investigation themselves |
| **Auditing** | Someone (the operator, or a panel reviewer) wants to know if the system is actually learning | A static report that shows the curve, the controls, and the failure list — not a sales chart |

There is no end-customer-facing surface, no multi-user permissioning, and no chat interface — all
explicitly out of scope (spec §2). The panel/judge is a second reader of the audit surface, not a
distinct persona with distinct needs — the same report serves both.

## 3. Core flows

### 3.1 Background flow — ingestion → matching → exception detection

No UI. Real payments/orders/refunds come in via Razorpay test-mode + webhooks; synthetic bank
lines and ledger entries are generated alongside them. The deterministic matcher clears everything
it can with confidence. What's left becomes an `exception` row. This flow is invisible by design —
the product's job starts at the point a human is needed, not before.

### 3.2 The investigation → gate flow (the core loop)

This is the one interactive surface that matters, and it should be built to make the system's
reasoning legible, not to impress:

1. **Exception surfaces** with its classified kind (netted settlement, TDS short-payment, split
   payment, etc. — spec §5) and its `member_refs` (the payment/bank-line/ledger rows in play).
2. **Precedents retrieved** for this exception are shown alongside the exception, before the
   proposed resolution — the reviewer should be able to sanity-check *what the system thought was
   similar* independent of what it concluded.
3. **Proposed resolution** is shown as a structured object, not prose: what happened, the reason
   code, the arithmetic (to the paise), the confidence score, and which cited precedents actually
   informed it.

   The arithmetic must be shown **the way reconciliation actually works, not as one number**: the
   bank credit is the payment *net of PSP fee and tax*, while the ledger's expected amount is the
   *gross* order value. Those are two different comparisons, and collapsing them into a single
   "amount matches / doesn't" line is what makes reconciliation UIs untrustworthy — the reviewer
   cannot tell a genuine shortfall from a fee deduction. Show the chain: gross → less fee → less
   tax → expected credit, against what actually landed.
4. **Verification result** is visible, not hidden — if arithmetic didn't close or a cited
   precedent didn't actually apply, and the system revised, the reviewer sees that it revised and
   why. A system that silently retries looks more confident than it is; this one shouldn't.
5. **Gate**: exactly three actions — **Confirm**, **Correct** (edit the resolution payload before
   committing), **Reject**. No fourth "snooze" or "escalate later" — an exception that isn't
   resolved is explicitly routed to the exception list, not left ambiguous.
6. **Outcome feedback**: after the decision, the UI confirms whether a precedent was deposited
   (confirm/correct → yes; reject → no) and shows the resolution's `resolution_id` for audit
   traceability.

Design principle: **the gate screen should make it obvious that a correction is more valuable than
a rubber-stamp confirm**, since corrections deposit higher-value precedents (spec §7). This can be
a single line of copy near the Correct action rather than a mechanic — the point is reviewer
understanding, not gamification.

### 3.2a Why the gate screen carries unusual weight

In most review UIs, a careless approval costs one wrong record. Here it costs more, and the design
has to reflect that: **a confirmed wrong resolution is deposited as a precedent, and then retrieved
to justify future wrong resolutions.** Corpus poisoning is the system's most serious failure mode
(see `docs/ARCHITECTURE.md` §6), and this screen is the only thing standing between it and the
corpus.

Two consequences for the design:

- **Confirm should require having seen the evidence, not just the verdict.** The cited precedents
  and the arithmetic chain are not collapsible detail to be skipped past — they are what the
  reviewer is actually attesting to.
- **The screen should say what a confirm will do**: that it deposits a precedent which will be
  retrieved for similar future cases. A reviewer who knows their click becomes durable knowledge
  behaves differently from one who thinks they are closing a ticket.

### 3.2b Failure states the reviewer will actually see

The system escalates rather than guesses (spec §7), so these are normal, not edge cases, and each
needs a distinct presentation — a reviewer who cannot tell them apart cannot act on them:

| State | What the reviewer sees | What they can do |
|---|---|---|
| `escalate_with_draft` — low confidence | The full proposal, marked as below the auto-resolve threshold | Confirm / Correct / Reject as usual |
| `escalate_raw` — verification failed twice | The investigation trace and *both* failed verify attempts, with no proposal presented as trustworthy | Resolve manually; the draft is evidence, not a recommendation |
| Model unavailable / parse failure | An explicit "the model could not be reached / returned unusable output" state with its reason code — **never** a blank or a spinner | Retry, or resolve manually |

A system that renders "no proposal" identically whether it was thinking, unreachable, or refused to
guess teaches its reviewer to distrust all three.

### 3.3 The eval report flow (static HTML)

This is the actual submission artifact and should be designed accordingly — a reviewer with no
context on the codebase should be able to open one HTML file and understand whether the thesis
holds. Sections, in order:

1. **The headline curve** — autonomous resolution rate vs. corpus size, plotted against the
   random-precedent negative control on the same axes. If the two lines diverge, retrieval is doing
   something; if they don't, that's shown too, not omitted. (Snapshot points are pending the
   rescale decision in `requirements.md` FR-9.1 — the corpus tops out at 102 precedents, so the
   spec's 150 and 200 are unreachable.)
2. **All four baselines**, same chart or an adjacent bar comparison: deterministic rules alone
   (currently 49.2%), zero-shot LLM, random-precedent control, retrieval-grounded agent. The rules
   baseline resolves clean matches *and* fee/tax rounding deltas, so it is a genuinely harder floor
   than the spec's implied 45%; the report should say so rather than quietly benefiting from it.
3. **False-resolution cost in ₹** — the number a finance controller actually cares about, given
   top billing, not buried under accuracy.
4. **Precedent precision** — retrieval quality in isolation from end-to-end accuracy.
5. **Rationale faithfulness (Ragas)**, with the calibration correlation against the 15 hand-scored
   items stated next to it, so the judge score isn't presented as ground truth.
6. **Retrieval ablation** — BM25-only / dense-only / hybrid, justifying the hybrid choice.
7. **Cost/latency** — p50/p95 latency, tokens per exception.
8. **The exception list** — every case the system could not resolve, with its reason code. This
   section is not an appendix; it is placed with the same visual weight as the headline chart,
   because a 100%-resolved report would be a red flag, not a win.

   **It must be split into two groups, because they mean opposite things:**

   - **Correctly terminal** — `unmatchable_no_counterpart`. No valid counterpart exists; refusing
     to resolve these is the system working exactly as designed. In the current dataset that is 10
     records, deliberately constructed. A system that "resolved" them would be fabricating.
   - **Genuine limitations** — everything else: escalated for low confidence, failed verification
     twice, model unavailable, or simply not figured out.

   Reporting a single undifferentiated count invites the reader to read the whole list as failure,
   which both understates the system (the first group is a correctness guarantee) and overstates it
   (burying the second group inside the first). The spec calls the unmatchable class "load-bearing"
   precisely because it proves the system can decline; that argument only lands if the report
   distinguishes it.

Design principle: **no chart appears without a linked, committed result file** (`evals/results/*.json`).
The report should render directly from committed JSON, not from hand-transcribed numbers, so
regenerating it is a reproducibility check, not a rewrite.

### 3.4 Bounded remediation flow (Ring 5, if reached)

A confirmed resolution that requires a real refund passes through a second, explicit gate before
the test-mode refund API call fires — this is a distinct confirmation from the resolution gate in
3.2, because it authorizes movement of (test-mode) money, not just a data correction. The UI
should show the remediation ceiling and how much of it has been used, so "bounded" is visible, not
just enforced server-side.

## 4. Information architecture

```
Exception Queue (list)
 └─ Exception Detail
     ├─ Exception summary (kind, members, detected_at)
     ├─ Retrieved precedents (ranked, with retrieval mode indicator)
     ├─ Investigation trace (tool calls made, capped at 5)
     ├─ Proposed resolution (structured: what / reason code / arithmetic / confidence)
     ├─ Verification result (pass, or revise history up to 2)
     └─ Gate actions: Confirm / Correct / Reject

Eval Report (static HTML, single page, anchor-linked sections)
 ├─ Headline curve + negative control
 ├─ Four baselines
 ├─ False-resolution cost (₹)
 ├─ Precedent precision
 ├─ Rationale faithfulness (Ragas + calibration)
 ├─ Retrieval ablation
 ├─ Cost/latency
 └─ Exception list (reason codes)
     ├─ Correctly terminal (unmatchable — no counterpart exists)
     └─ Genuine limitations (low confidence / verify failed / model unavailable)
```

## 5. Design principles

1. **Show the reasoning, not just the answer.** Every proposed resolution surfaces its cited
   precedents and its arithmetic — the reviewer is confirming a chain of evidence, not trusting a
   black box.
2. **Never guess, never stall, always say why.** Any failure mode (parse failure, low confidence,
   model down, verification failed twice) surfaces on the exception list with a reason code — this
   is a product commitment, not just an engineering fallback (spec §7).
3. **A correction is not a failure state.** The UI and copy should treat Correct as a first-class,
   expected action that produces a *better* precedent than Confirm — never present it as the
   system having gotten it wrong in a way that discourages use.
4. **The unmatchable case is a valid outcome.** Nothing in the UI should imply 100% resolution is
   the goal; the exception list is a designed, permanent surface, not a bug tray. But *declining
   correctly* and *failing to figure it out* must never render identically (§3.3) — a system that
   presents them the same way is asking to be misread in whichever direction flatters it.
5. **Distinguish "don't know" from "broken".** Low confidence, failed verification, and an
   unreachable model are three different situations with three different reviewer responses. A
   shared empty state collapses them and teaches distrust (§3.2b).
6. **The eval report is the product.** Every other screen serves the loop that produces the curve;
   the curve is what gets judged.

## 6. Non-goals (restated from spec §2 for this document's scope)

No dashboard beyond the static eval report and the approval UI above. No auth/accounts. No chat
interface over the ledger. No live-mode operation. No cash forecasting. No multi-agent "team of
agents" — one investigation graph, presented as one coherent trace per exception, not a swarm of
independent actors the reviewer has to reconcile themselves.
