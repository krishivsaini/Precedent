"""The retrieval interface every mode implements — including the negative control.

Spec §6 requires a random-precedent control run on *every* eval from Ring 3 onward: same
batch, same *k*, but precedents sampled at random rather than retrieved. If random helps as
much as relevant, retrieval is doing nothing and the learning curve is a prompt-length
artifact.

That control is only trustworthy if it goes through exactly the same code path as the real
retrievers — same interface, same call site, same top-k plumbing. So `RandomRetriever` is a
first-class implementation of this protocol, not a script that bypasses it.
"""

import re
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from precedent.adapters.storage.records import PrecedentRecord


@dataclass(frozen=True)
class RetrievedPrecedent:
    """One hit. `score` is comparable *within* a retriever, never across them — which is
    why `hybrid.py` fuses on rank rather than on these values."""

    record: PrecedentRecord
    score: float


@runtime_checkable
class Retriever(Protocol):
    def retrieve(self, query: str, k: int) -> list[RetrievedPrecedent]:
        """Top-`k` precedents for `query`, best first. Fewer than `k` if the corpus is
        smaller; empty for an empty corpus — which is a real state, being the first point
        on the learning curve, not an error."""
        ...


_TOKEN = re.compile(r"[a-z0-9]+")

#: Deliberately small. Aggressive stopword removal on a ~100-document corpus of similar
#: prose strips the discriminating terms along with the noise; these are the words that
#: appear in essentially every precedent, so they carry no signal at all.
STOPWORDS = frozenset(
    """
    a an and are as at be been but by for from has have in into is it its of on or that the
    them then there these this to was were which with
    """.split()
)


def tokenize(text: str) -> list[str]:
    """Lowercase alphanumeric tokens, stopwords dropped. Shared by every lexical path so
    the BM25 index and its queries can never disagree about what a token is."""
    return [token for token in _TOKEN.findall(text.lower()) if token not in STOPWORDS]
