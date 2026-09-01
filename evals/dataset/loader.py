"""Loads the committed dataset (`gold.jsonl` + `payments/bank_lines/ledger_entries.json`)
back into records, grouped per scenario via the ID lists in `gold.jsonl`.
"""

import json
from dataclasses import dataclass
from pathlib import Path

from precedent.adapters.storage.records import BankLineRecord, LedgerEntryRecord, PaymentRecord

_DATASET_DIR = Path(__file__).parent
_GOLD_PATH = _DATASET_DIR.parent / "gold.jsonl"


@dataclass(frozen=True)
class LoadedScenario:
    scenario_id: str
    kind: str
    is_exception: bool
    expected_reason_code: str
    notes: str
    uses_real_payment: bool
    pool_or_test: str | None
    payments: list[PaymentRecord]
    bank_lines: list[BankLineRecord]
    ledger_entries: list[LedgerEntryRecord]


def load_dataset() -> list[LoadedScenario]:
    with open(_DATASET_DIR / "payments.json") as f:
        payments_by_id = {row["payment_id"]: PaymentRecord(**row) for row in json.load(f)}
    with open(_DATASET_DIR / "bank_lines.json") as f:
        bank_lines_by_id = {row["line_id"]: BankLineRecord(**row) for row in json.load(f)}
    with open(_DATASET_DIR / "ledger_entries.json") as f:
        ledger_entries_by_id = {row["entry_id"]: LedgerEntryRecord(**row) for row in json.load(f)}

    scenarios = []
    with open(_GOLD_PATH) as f:
        for line in f:
            row = json.loads(line)
            scenarios.append(
                LoadedScenario(
                    scenario_id=row["scenario_id"],
                    kind=row["kind"],
                    is_exception=row["is_exception"],
                    expected_reason_code=row["expected_reason_code"],
                    notes=row["notes"],
                    uses_real_payment=row["uses_real_payment"],
                    pool_or_test=row["pool_or_test"],
                    payments=[payments_by_id[pid] for pid in row["payment_ids"]],
                    bank_lines=[bank_lines_by_id[lid] for lid in row["bank_line_ids"]],
                    ledger_entries=[ledger_entries_by_id[eid] for eid in row["ledger_entry_ids"]],
                )
            )
    return scenarios
