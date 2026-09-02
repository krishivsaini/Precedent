# Failures

A running log of things that broke, wrong assumptions that got caught, and edge cases
that slipped through — opened on the first commit, not backfilled at the end. See
`docs/PRECEDENT_SPEC.md` §11 (Limits) and §10 (Tests) for the discipline this supports.

Format per entry: what broke, how it was caught, what changed as a result.

---

## 2026-08-31 — `INSERT OR IGNORE` swallows CHECK violations, not just duplicate keys

**What broke:** `webhook_events.event_type` has a `CHECK (event_type IN (...))` constraint
meant to reject a malformed event type. `WebhookEventsRepository.insert_if_new` uses
`INSERT OR IGNORE` for dedupe-by-primary-key. A test assumed an invalid `event_type`
would raise `sqlite3.IntegrityError`; instead it silently returned rowcount 0 — SQLite's
`OR IGNORE` conflict resolution applies to CHECK constraints exactly the same way it
applies to a duplicate PRIMARY KEY, so a malformed event and a harmless replay were
indistinguishable from the caller's side.

**How it was caught:** wrote the test expecting `IntegrityError`, it failed, checked
SQLite's actual `OR IGNORE` semantics with a throwaway script before "fixing" the test to
match the wrong behavior.

**What changed:** `insert_if_new` now validates `event_type` against the two real
webhook types explicitly before the `INSERT`, raising `ValueError` on anything else. The
CHECK constraint stays as defense-in-depth for any caller that writes to the table
directly, but the repo can no longer rely on it alone.

## 2026-08-31 — SQLite connection crossed threads under FastAPI's TestClient

**What broke:** the webhook endpoint tests failed with `sqlite3.ProgrammingError:
SQLite objects created in a thread can only be used in that same thread.` The
per-request connection (opened and closed within one `Depends(get_connection)` call) was
still constructed and used across a thread boundary — Starlette's `TestClient` proxies
sync request handling through an anyio thread portal, so "the thread that opened the
connection" and "the thread FastAPI runs the handler in" aren't guaranteed to be the same
one even for a single request.

**How it was caught:** ran the full suite after wiring the webhook endpoint; three of
four webhook tests failed with the same traceback.

**What changed:** `adapters/storage/db.connect` now passes `check_same_thread=False`.
Safe here specifically because every caller still gets its own connection, opened and
closed within a single request/test — this does not mean a connection is ever shared
across concurrent requests.

## 2026-09-01 — `tests/evals/__init__.py` shadowed the real `evals` package

**What broke:** added `evals/dataset/generators.py` at the repo root and a test at
`tests/evals/dataset/test_generators.py`, with `__init__.py` files under
`tests/evals/` mirroring the pattern used for `tests/adapters/`. Collection failed with
`ModuleNotFoundError: No module named 'evals.dataset.generators'`, even though the file
existed and `pythonpath = ["."]` was set in `pyproject.toml`.

**How it was caught:** pytest's package-root detection walks up from a test file through
every directory that has an `__init__.py`, and inserts the first directory *without* one
into `sys.path`. `tests/` itself has no `__init__.py`, so `tests/evals/` and
`tests/evals/dataset/` having one made pytest insert `tests/` into `sys.path` and import
the test as part of a package named `evals.dataset` — which then shadowed the real,
repo-root `evals` package of the same name for the rest of that test run.

**What changed:** removed `tests/evals/__init__.py` and `tests/evals/dataset/__init__.py`.
The `tests/adapters/` pattern was safe only because nothing at the repo root is
importable as a bare top-level `adapters` — `evals` collided because both a source
package and a test directory share that exact name. Rule going forward: a `tests/<x>/`
directory must not get an `__init__.py` if `<x>` is also a real top-level package name.

## 2026-09-01 — `match_batch` only ever flagged orphaned payments, not orphaned ledger
entries or bank lines

**What broke:** designing the Ring 0.5 exception dataset surfaced that `match_batch`
(Ring 0.1) had an asymmetric blind spot. It swept unresolved *payments* into exceptions
at the end, but a `ledger_entry` with no payment candidate just hit `continue` and
vanished, and a credit `bank_line` nobody consumed was never checked at all. The
`direct_neft_bypass` class (spec §5 — a real bank transfer that never touched the PSP,
so there is no payment record at all) would have silently produced zero exceptions
under the old code, violating FR-2.6 ("everything the matcher cannot resolve is emitted
as an exception, not silently dropped").

**How it was caught:** while designing how to construct a `direct_neft_bypass` scenario
for the dataset generator, tracing what `match_batch` would actually do with
"ledger entry + bank line, no payment" showed the ledger-entry branch just skips ahead
with no record left behind — caught during design, before any scenario data was built on
top of the gap.

**What changed:** `match_batch` now emits an `UnmatchedException` for a ledger entry with
no payment candidates, and for any credit `bank_line` still unconsumed at the end — the
same treatment orphaned payments already got. Two new tests
(`test_ledger_entry_with_no_payment_is_an_exception_not_a_silent_gap`,
`test_unclaimed_bank_line_is_an_exception_not_a_silent_gap`) pin this down.

## 2026-09-01 — review pass over Ring 0: four defects, one of them not a defect at all

A deliberate re-read of everything built so far, looking for what the build missed rather
than adding features. Four findings, recorded honestly including the one that turned out
to be less serious than it first looked.

**1. Floats in money paths (latent, not active).** The eval generator computed money with
float arithmetic — `round(amount_paise / (1 - tds_rate))` and `round(amount_paise * fee_rate)`
— directly contradicting the project's most emphatic rule ("no floats, ever"). Fixed by
routing every rate through a new `money.apply_rate_paise` choke point that refuses `float`,
plus `money.gross_before_tds_paise` for the TDS reconstruction. **Regenerating the dataset
changed zero values** — float and Decimal agreed on every input in this dataset. So this
was a latent violation of a stated invariant, not a bug producing wrong numbers, and it is
recorded that way rather than dressed up as a catch.

**2. Repositories committed on every write; `db.transaction()` was dead code.** Eleven
`commit()` calls meant no caller could compose an atomic multi-write unit. Ring 3's deposit
must write the human action, the audit row, and the precedent together — under autocommit a
crash midway leaves a precedent with no audit trail, silently corrupting the corpus and
breaking NFR-5. Repositories now never commit; the caller owns the boundary, and an API
request is one transaction. Caught by noticing `transaction()` had zero call sites.

**3. Importing `precedent.api.main` created a database file.** `create_app()` called
`init_db` directly, and uvicorn's entrypoint is a module-level `app = create_app()` — so
merely importing the module wrote a stray `precedent.db` into whatever the CWD happened to
be. Moved to a FastAPI lifespan. The existing webhook tests had been quietly depending on
this side effect, and had to start entering the `TestClient` context — which is what
production does anyway.

**4. The webhook receiver returned 500 on a non-UTF-8 body**, contradicting its own
docstring's always-ack promise and inviting a Razorpay retry-storm. Fixed — and the
regression test then found a *second* crash on the same input: `json.loads()` on bytes
sniffs the encoding, so a UTF-16-looking BOM raises `UnicodeDecodeError`, which is **not** a
`JSONDecodeError` and so slipped past the existing `except`. Writing the test found the bug
the fix had missed.

Post-refactor, the baseline result reproduces byte-identically (ignoring `run_id`),
confirming the changes were behavior-preserving.

## 2026-09-01 — `is_exact_match`/`is_tolerance_match` compared the bank line against the
gross payment amount, not the net-of-fee amount

**What broke:** `build_clean_match` and `build_rounding_delta` (Ring 0.5) produced
scenarios that should resolve cleanly, but ran through `match_batch` with **zero**
matches every time — every payment and its own freshly-generated bank line ended up as
an orphan-bank-line exception instead. `is_exact_match` required
`payment.amount_paise == bank_line.amount_paise == ledger_entry.expected_amount_paise`
— a single three-way equality. But `generate_bank_line_for_payment` (Ring 0.4) correctly
nets out fee and tax before crediting, matching how Razorpay actually settles money to a
bank account — so `bank_line.amount_paise` was never going to equal the gross
`payment.amount_paise` whenever a payment carried a real fee, which is every payment with
`fee_paise > 0`.

**How it was caught:** every original `is_exact_match`/`is_tolerance_match` test in
`test_matching.py` used the default `fee_paise=0`, under which gross and net amounts are
identical — so the bug was invisible until Ring 0.5's builder tests ran a *fee-bearing*
payment through the full `match_batch` pipeline and asserted a match that never came.
Two rings' worth of code (0.1's matcher, 0.4's bank-line generator) were each internally
consistent but disagreed with each other about what a bank line represents, and nothing
caught it until they were exercised together end-to-end.

**What changed:** `is_exact_match`/`is_tolerance_match` now compare `bank_line.amount_paise`
against the payment's *net* amount (gross minus fee minus tax) and separately compare
`ledger_entry.expected_amount_paise` against the *gross* payment amount — two different
comparisons, not one three-way equality, because the bank never sees the gross figure and
the ledger never sees the fee deduction. Added a regression test with non-zero
`fee_paise`/`tax_paise` directly to `TestIsExactMatch`, so a zero-fee default can't mask
this class of bug again.

## 2026-09-01 — Ring 0's `precedents` table could not store a spec-mandated field

**What broke:** spec §4 types the core artifact with `confidence_at_deposit: float`. Ring 0's
`precedents` table (and `PrecedentRecord`) omitted the column entirely. Every Ring 0 storage
test passed, because they tested the schema against itself — `test_precedents_repo.py`
round-tripped a `PrecedentRecord` and asserted the fields came back, so a field that existed
in neither the dataclass nor the table was invisible to the test.

**How it was caught:** writing the Pydantic `Precedent` model in Ring 1.1 directly against
spec §4's field list, rather than against the storage dataclass. Transcribing from the schema
would have propagated the omission.

**What changed:** added `confidence_at_deposit REAL NOT NULL CHECK (… BETWEEN 0.0 AND 1.0)` to
the table, the field to `PrecedentRecord`, and the mapping to the insert and row reader. The
wider lesson: a round-trip test proves the layers agree with *each other*, never that either
agrees with the spec. Fields specified externally need a test that names them.

## 2026-09-01 — A rate limit masqueraded as a failed kill criterion

**What broke:** the first smoke run of the Ring 1.3 ablation reported `zero_shot` 83.3%,
`grounded` 0.0%, `random_control` 0.0%, and printed **"FAIL — the retrieval thesis is not
supported"**. Taken at face value that is the finding that kills the project.

It was throttling. The zero-shot arm ran first and exhausted the free-tier per-minute quota;
the two precedent-carrying arms then received HTTP 429 on every call. `GeminiClient` treated
any non-2xx as terminal, so all 62 cases in those arms escalated as
`escalated_model_unavailable` — and an escalated case correctly scores as unresolved. Every
individual component behaved as written. The composite claim was nonsense.

**How it was caught:** the shape of the result was wrong in a way a real thesis failure
would not be. Grounding being *unhelpful* is plausible; grounding scoring exactly 0.0% while
zero-shot scored 83.3% on the same model and cases is not a difference in reasoning quality.
Re-running one grounded case printed the rationale: `Gemini returned HTTP 429`.

**What changed:** `GeminiClient` now separates transient failures (408, 429, 5xx, transport
errors) from permanent ones (bad key, unknown model, malformed request). Transient failures
retry with exponential backoff plus jitter, honouring `Retry-After`; permanent ones fail
immediately rather than spending quota to receive the same refusal. `attempts` is carried
through `LLMResponse` and `ResolutionOutcome` into the eval as `retry_attempts`, so a run
degraded by throttling is visible in the result file rather than silently wrong.

The wider lesson, and the reason this is the most valuable entry in this file so far: **the
eval harness cannot distinguish "the system failed" from "the measurement failed".** Both
arrive as a low score. An infrastructure fault that correlates with the arm under test —
here, arm ordering against a per-minute quota — produces a clean, plausible, entirely false
result. Any number this project reports needs a sanity check on its *shape*, not only on its
value.

## 2026-09-01 — Withholding and processor fees conflated in the case renderer

**What broke:** `ReconciliationCase.observations` computed the shortfall as
`ledger expected − bank credit`. Two different deductions sit between those two figures:
what the customer kept back (between invoice and payment) and the processor's fee plus tax
on it (between payment and bank credit). Measuring across both in one step means a clean 2%
or 10% withholding no longer lands on a round rate, so the renderer described every
withholding case as "an arbitrary amount". `tds_short_payment` retrieved at **0/10**.

This is precisely the error the hand-written `tds_and_psp_fee_stacked` seed precedent exists
to warn about — written earlier the same day, in this repo, by the same hand.

**How it was caught:** the retrieval eval's per-class breakdown. The aggregate top-3 number
was a respectable 83.9% and hid it completely; one class sitting at exactly zero is only
visible per class.

**What changed:** the two levels are computed separately and reported as separate
observations. `tds_short_payment` went 0/10 → 10/10 and overall top-3 83.9% → 90.3%. Direct
tests now pin the arithmetic at each level. Aggregate metrics get a per-class breakdown
beside them from here on — a mean over classes hides exactly the failure worth finding.

## 2026-09-01 — The retrieval query was drowning its own signal

**What broke:** `summarize()` was used as both the LLM's case context and the retrieval
query, on the reasoning that retrieving against one description while reasoning about
another would make precedent-precision meaningless. But the full record dump is mostly
boilerplate every case shares — "processor fee", "invoice", "terms", "value date" — and
those tokens matched the wrong precedents strongly enough to bury the few sentences that
actually discriminate. Three exception classes retrieved at exactly zero.

**How it was caught:** printing the ranked list for one netted-settlement case. The top two
hits were `exact_match` and `split_payment`, both scoring on fee-and-invoice boilerplate.

**What changed:** `retrieval_query()` is now the computed observations only, separate from
`summarize()`. Both derive from the same records deterministically, which is the property
that actually has to hold — identical text was never the requirement. Top-3 went 38.7% →
83.9% on the same corpus and the same retriever.

## 2026-09-01 — Free-tier daily quota makes the ablation unrunnable as designed

**What broke:** the full ablation needs 186 model calls (62 pool exceptions × 3 arms).
`gemini-3.5-flash` on the free tier allows **20 requests per day** — confirmed directly from
the API's own error body:

```
Quota exceeded for metric: generativelanguage.googleapis.com/generate_content_free_tier_requests,
limit: 20, model: gemini-3.5-flash
quotaId: GenerateRequestsPerDayPerProjectPerModel-FreeTier
```

The run was killed after a long wall-clock time without completing an arm, though that
duration is not evidence of anything on its own — the machine slept partway through. The
quota figure is the hard fact.

**The latent defect it exposed:** the retry logic added earlier the same day (to handle a
genuine *per-minute* limit) does not distinguish a per-minute cap from a per-day one. Both
arrive as HTTP 429 carrying a `retryDelay` of about a minute, but that hint only applies to
the per-minute case. Backing off six times against a daily cap waits for something that will
not happen until the quota resets, and every case in the batch pays that cost before
escalating.

**What changed:**
1. `DailyQuotaExhausted` (a subclass of `LLMUnavailable`, so existing call sites still
   escalate correctly without knowing the type exists) is raised immediately when a 429 body
   names a per-day quota. No backoff against a wall.
2. The ablation prints progress every 10 cases per arm. Previously the report printed only
   at the end, so a run making steady progress and a run wedged in backoff looked identical
   from outside.
3. A circuit breaker: if more than 15% of an arm's cases could not reach the model, the run
   raises `AblationAborted` and **writes nothing**. This is the structural fix for the
   rate-limit-as-finding failure logged above — a degraded measurement should be unable to
   reach the result file at all, rather than depending on someone noticing its shape.

**The pattern across all of today's eval failures:** each produced a *plausible number*
rather than an error. A retry storm, a conflated deduction, a query full of boilerplate —
none of them crashed anything. That is what makes eval infrastructure harder than
application code: the failure mode is a result you believe.

## 2026-09-01 — Three providers, three different quota walls

**What broke:** the Ring 1.3 ablation needs 186 model calls. It took three providers to land one
run.

- **Gemini.** `gemini-2.5-flash`, which the plan specified, is retired for new users and returns
  404 while still appearing in `ListModels`. `gemini-3.5-flash` works but the free tier allows
  **20 requests per day**.
- **Groq.** 1000 requests/day and 8000 tokens/minute (both read from response headers), plus an
  undocumented per-day *token* budget the headers do not expose. Two runs died on it, the second
  after 62 of 62 zero-shot and 62 of 62 grounded cases, with 36 of the grounded unreachable.
- **NVIDIA NIM.** Completed the run. But of 82 models the key can list, most do not serve:
  `meta/llama-3.3-70b-instruct` returns 410 Gone, `llama-3.1-nemotron-ultra-253b` and `qwen3-235b`
  return 404, the deepseek endpoints time out. `openai/gpt-oss-120b` answers in about two seconds.

**The recurring lesson, which cost three separate detours:** *a model appearing in a provider's
catalogue does not mean it serves requests.* Gemini listed a retired model, Groq advertised one it
does not host, NVIDIA lists 82 and serves a handful. Probing with one real request costs a second
and would have saved every one of those detours. The `list_models()` docstring now says so.

**What changed:** request handling for OpenAI-compatible providers was extracted into
`adapters/llm/openai_compatible.py` before adding the third. What varies between providers is a
URL, a default model, a rate limit, and the wording of "out of quota" — none of it logic. What must
not vary is the single `LLMUnavailable` failure mode, the transient/permanent split, the immediate
stop on a daily quota, and the token pacing, every one of which was learned from a run that
produced a wrong number rather than an error. A copy-pasted third adapter is precisely where those
drift apart.

**A bug the refactor nearly introduced, and one it did.** Nearly: two `DailyQuotaExhausted` classes
(one in `gemini.py`, one shared) would have silently broken `isinstance`, so the circuit breaker
would stop recognising quota exhaustion from one provider. Actually introduced, and caught only
because `openai/gpt-oss-120b` is served by *both* Groq and NVIDIA: the response cache keyed on model
but not provider, so one provider's answers would have been replayed for the other — exactly the
cross-provider comparison every result file here declares invalid. The provider is now part of the
key.

## 2026-09-01 — The kill criterion passed, and the headline still over-claimed

**What broke:** nothing, which is the point. The Ring 1.3 ablation returned grounded 100.0%,
zero-shot 85.5%, random control 93.5% — a clean PASS. The obvious sentence to write is "retrieval
grounding improves autonomous resolution by 14.5 points."

That sentence is wrong by more than a factor of two. The random-precedent control carries the same
prompt shape and the same five precedents, differing *only* in whether they are relevant. So the
gain decomposes: **+8.1 points from having precedents at all** (format, priming, worked examples of
the reasoning style) and **+6.5 points from their relevance**. Retrieval contributes a little under
half of what the headline suggests.

**A second over-claim underneath it.** Grounded beats the random control 4W–0L across 62 paired
cases. Exact McNemar puts that at **p = 0.125** — not significant. Grounded vs zero-shot is 9W–0L,
p = 0.0039, which is solid. So the criterion's headline leg holds and its stricter, more
interesting leg is a consistent direction that the sample cannot yet confirm.

**How it was caught:** by running the control spec §6 mandates and then actually subtracting, rather
than comparing the grounded arm to zero-shot and stopping. The control is only useful if its number
is used.

**What changed:** the ablation now computes paired exact McNemar between every pair of arms and an
explicit effect decomposition, both written into every result file, and the verdict carries a
caveat pointing at them. Point estimates alone are how a four-case margin becomes a claim.

## 2026-09-02 — `verify` passed a wrong answer because the arithmetic closed

**What broke:** the Ring 2 graph's `verify` node checked that a proposal's arithmetic closes to
the paise. For `exact_match` it compared what the payments settle to against what the bank
credited. On a **split payment** those two figures agree exactly — one payment, one credit — so
`exact_match` passed verification while leaving a second invoice open on the same order. The
customer has paid in full and is then chased for the remainder.

Five of the six false resolutions in the graph run's zero-shot arm were exactly this, proposed at
0.85–0.95 confidence: ₹34,063 of value at risk, all of it waved through by a verifier that was
working as written.

**How it was caught:** reading the `false_resolutions` list in the result file rather than
stopping at the headline rate. The arm's accuracy had *improved* over Ring 1 (85.5% → 88.7%)
while its false-resolution cost *rose* (₹24,845 → ₹34,063) — accuracy and cost moving in
opposite directions is the signal worth chasing, and it is only visible because spec §6 insists
the cost be reported in rupees beside the rate.

**What changed:** `verify` now rejects `exact_match` whenever more than one ledger entry is open
on the order. This is a definitional rule — an exact match is one payment against one invoice —
not a threshold fitted to the observed failures. Zero-shot went 88.7% → 98.4% and its
false-resolution cost ₹34,063 → ₹0, at a cost of six new model calls, the rest replayed from
cache.

**The uncomfortable part, recorded deliberately.** The fix was found by inspecting the
*ungrounded* arm's mistakes and it disproportionately helped the *ungrounded* arm. It took the
measured advantage of retrieval from 14.5 points (p=0.0039) down to 1.6 points (p=1.0). Making
the baseline stronger is the honest direction to be wrong in, but it means the Ring 1 and Ring 2
numbers cannot be quoted selectively: the more flattering one is the earlier and weaker one.

**The general lesson:** a verifier that checks a *necessary* condition and treats it as
*sufficient* is worse than no verifier, because it launders a wrong answer as a checked one.
Arithmetic closure is necessary for `exact_match` and nowhere near sufficient.

## 2026-09-02 — A prompt "improvement" that improved nothing, and nearly shipped

**What broke:** deposit prompt v1 authored precedents carrying concrete rupee amounts, one
case's order id, and — in one instance — a rebate rate with the processor's fee folded into
it, recording 3.25% as "about 5.8%". Wrong knowledge, deposited permanently.

v2 fixed every one of those. Authoring success 7/9 → 9/9, rupee amounts in situations 4/9 →
0/9, deposit retrieval 78% → 100%, and the headline resolution rate rose 56% → 63%.

**That last number is the failure.** Paired exact McNemar over the same 27 sightings: 9W-7L,
**p = 0.80**. Noise. Underneath it, two customers that v1 resolved 3/3 had collapsed to 0/3 —
a real regression, invisible in an aggregate that moved the right way.

**How it was caught:** reading the authored text rather than the metric. Every v2 failure was
`escalated_low_confidence`, never a wrong answer — the agent recognised the case and could not
get confident. The cause was an over-correction in v2's own instructions. Its situations became
clean observations that named nothing:

> ...the settled amount remains approximately three and a quarter percent lower than the invoice

against v1's, on the same customer, which resolved 3/3:

> ...the shortfall aligns with Kavery Textiles' negotiated 3.25% rebate

Both retrieve. Only one tells the reader **what they have found**. "State the observable
signature, not the conclusion" is right for *finding* a precedent and wrong for *believing* it,
and v2 pushed it until the situation carried no claim at all. v3 keeps v2's bans and restores
the claim: recognisable first, named second. 9/9 authored, 100% retrieved, 78% resolved.

**A second, smaller trap inside the first.** The eval recorded only the `situation`, so the
regression was nearly misdiagnosed as a `resolution` problem. The resolutions were fine
throughout and named the right rate. The two halves fail independently and have opposite
remedies, so both are now recorded.

**What is and is not established, stated because the temptation is to quote the 78%.**
Authoring success, the absence of amounts, and retrieval are near-deterministic properties of
the text and can be read directly. The resolution lead cannot: v3 over v1 is 10W-4L at
**p = 0.18** on 27 sightings, and per-customer variance is high — Chenab Steel is 3/3 under v1
and 1/3 under v3. v3 is adopted on the criteria that are established, not on the headline.

**The general lesson, and the reason spec §4 asks for prompt versions with numbers attached:**
a prompt change that fixes exactly what it targeted can still make the system no better, and an
aggregate moving in the intended direction is the easiest possible thing to mistake for
evidence. Without the per-case breakdown, v2 ships.

## 2026-09-02 — The gate approved cases nobody was asked about, and swallowed empty approvals

**What broke:** two defects in the `interrupt` gate, both found by writing the tests the plan
demanded rather than the ones that would pass.

**1. Escalated cases stopped at the gate.** `finalize` emits both the auto-resolved proposal
*and* the low-confidence escalation, and the edge `finalize -> gate` did not distinguish them.
So a case the system had explicitly routed to a human blocked at a second human step, waiting
for an approval nobody knew to give. Fixed with a conditional edge: only a non-escalated
proposal reaches the gate.

**2. An empty resume was silently ignored.** LangGraph treats a falsy `Command(resume=...)`
value as *no resume at all* — the graph pauses again and `invoke` returns a state that looks
acted-on. The node-level validation could never fire, because the node never ran. At a gate
whose entire purpose is a durable human decision, the difference between "your approval was
rejected" and "your approval vanished" is the whole point, so validation moved to
`resume_gate`, which fails loudly before invoking. A correction carrying no
`corrected_reason_code` is refused there too: it would deposit the agent's original answer
under the label of a human correction, which is the worst of both.

**On proving durability.** Spec §7 justifies `interrupt` as surviving a process restart, and
the plan says a design note is not proof. The test therefore **starts a second Python
interpreter**: the first pauses a case at the gate and exits, the second — which never ran the
graph — reads the pending decision off disk and resumes it. The second process is given a
`ScriptedLLM` with *no queued responses*, so if resuming re-ran any model call it would raise;
a clean resume proves the work was restored rather than recomputed. `MemorySaver` cannot do
this, which is why `durable_graph()` uses `SqliteSaver`, and a control test asserts the
in-memory checkpointer indeed loses the paused case.
