"""The hand-written seed corpus — `corpus_version = 0` (spec §3, Ring 1.1).

These are the only precedents in the system not authored by its own operation. They exist
so the first exception the agent ever sees has *something* to retrieve against; every
precedent after them is deposited from a confirmed or corrected resolution.

Two properties are load-bearing and enforced by tests:

* **No `derived_from`.** A seed predates every resolution, which is why the storage column
  is nullable. Anything at `corpus_version = 0` claiming provenance is a bug.
* **General, not dataset-specific.** The seeds encode reconciliation domain knowledge, not
  descriptions of the eval scenarios. If they described the scenarios, the Ring 1.3
  ablation would measure leakage rather than retrieval.

Seed identity is deterministic — `prec_seed_0001`, `prec_seed_0002`, … in file order — so a
corpus rebuilt from a clone is byte-identical to the one the committed eval results were
produced against.
"""

import json
from functools import lru_cache
from pathlib import Path

from precedent.adapters.storage.records import PrecedentRecord
from precedent.domain.precedent import Precedent

SEED_FILE = Path(__file__).with_name("seed_precedents.json")

#: Corpus genesis. Fixed rather than "now" so seed rows are reproducible across runs.
SEED_DEPOSITED_AT = "2026-09-01T00:00:00+00:00"

SEED_CORPUS_VERSION = 0


@lru_cache(maxsize=1)
def load_seed_precedents() -> tuple[Precedent, ...]:
    """Parse and validate the seed file. Raises on the first malformed entry."""
    raw = json.loads(SEED_FILE.read_text(encoding="utf-8"))
    return tuple(Precedent(**entry) for entry in raw)


def seed_precedent_id(index: int) -> str:
    """Deterministic id for the seed at `index` (0-based) in file order."""
    return f"prec_seed_{index + 1:04d}"


def seed_precedent_records() -> list[PrecedentRecord]:
    """The seed corpus as storage rows, ready to insert at `corpus_version = 0`."""
    return [
        precedent.to_record(
            precedent_id=seed_precedent_id(index),
            deposited_at=SEED_DEPOSITED_AT,
            corpus_version=SEED_CORPUS_VERSION,
        )
        for index, precedent in enumerate(load_seed_precedents())
    ]
