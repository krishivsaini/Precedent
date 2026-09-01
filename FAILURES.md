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
