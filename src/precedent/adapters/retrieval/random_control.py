"""The random-precedent negative control (spec §6, control #2).

> Same batch, same *k*, but precedents sampled at random instead of retrieved. If random
> helps as much as relevant, retrieval is doing nothing and the curve is a prompt-length
> artifact. **Run this unprompted — it is the check a good panel engineer would ask for.**

This is a peer implementation of `Retriever`, not a test helper, and that placement is the
point. The control is only evidence if the controlled arm differs from the real one in
exactly one respect: *which* precedents are chosen. Same interface, same call site, same
count, same injection into the prompt — so a measured difference can only come from
relevance.

Its defining property, asserted in the tests: `retrieve` ignores `query` entirely. The moment
this becomes query-sensitive it stops being a control and the ablation proves nothing.
"""

import random

from precedent.adapters.retrieval.base import RetrievedPrecedent
from precedent.adapters.storage.records import PrecedentRecord


class RandomRetriever:
    def __init__(self, records: list[PrecedentRecord], seed: int):
        # Seeded, and a private Random instance rather than the module-level one, so a
        # committed control result reproduces exactly and cannot be perturbed by unrelated
        # code drawing from the global stream.
        self._records = list(records)
        self._seed = seed

    def retrieve(self, query: str, k: int) -> list[RetrievedPrecedent]:  # noqa: ARG002
        if not self._records or k <= 0:
            return []
        rng = random.Random(self._seed)
        sampled = rng.sample(self._records, min(k, len(self._records)))
        # Descending pseudo-scores so the shape matches a real retriever's output; they
        # carry no meaning and nothing downstream may treat them as relevance.
        return [
            RetrievedPrecedent(record, 1.0 - index / len(sampled))
            for index, record in enumerate(sampled)
        ]
