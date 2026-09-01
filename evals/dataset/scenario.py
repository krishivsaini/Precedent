"""The unit of the Ring 0.5 exception dataset: one reconciliation scenario plus its gold
label. 240 of these get generated; ~122 are exceptions (spec §5/§6), the rest — clean
matches and fee/tax rounding deltas — the deterministic matcher (Ring 0.1) already
resolves on its own. See `generate.py` for why rounding-delta is *not* counted toward
the exception total here, deliberately deviating from the spec's literal "~130" figure.
"""

from dataclasses import dataclass, field

from precedent.adapters.storage.records import BankLineRecord, LedgerEntryRecord, PaymentRecord


@dataclass(frozen=True)
class Scenario:
    scenario_id: str
    kind: str  # one of the 9 classes in spec §5, snake_case
    is_exception: bool  # False only for clean_match and rounding_delta
    payments: list[PaymentRecord]
    bank_lines: list[BankLineRecord]
    ledger_entries: list[LedgerEntryRecord]
    expected_reason_code: str  # ReasonCode value the correct resolution should carry
    notes: str = ""
    uses_real_payment: bool = False
    pool_or_test: str | None = None  # "pool" | "test" | None (non-exceptions aren't split)
