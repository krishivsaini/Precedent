"""Lexical retrieval over the precedent corpus (Okapi BM25).

The strong half of the hybrid in this domain, and worth being explicit about why: precedents
turn on exact, rare tokens — "NEFT", "TDS", "RTGS", "withholding", "suspense". Those are the
terms that discriminate one exception class from another, and BM25's IDF weighting is built
to reward exactly that. A dense retriever tends to map the whole corpus into one tight
"payment reconciliation" region where those distinctions blur.

The index is built over `Precedent.retrieval_text()` — situation, amount signature, entities
— and deliberately *not* over the resolution narrative. See `domain/precedent.py`.
"""

from rank_bm25 import BM25Okapi

from precedent.adapters.retrieval.base import RetrievedPrecedent, tokenize
from precedent.adapters.storage.records import PrecedentRecord
from precedent.domain.precedent import Precedent


class BM25Retriever:
    def __init__(self, records: list[PrecedentRecord]):
        self._records = list(records)
        self._texts = [Precedent.from_record(r).retrieval_text() for r in self._records]
        corpus = [tokenize(text) for text in self._texts]
        # BM25Okapi divides by the corpus average document length and cannot be built empty.
        self._bm25 = BM25Okapi(corpus) if corpus else None

    def retrieve(self, query: str, k: int) -> list[RetrievedPrecedent]:
        if self._bm25 is None or k <= 0:
            return []
        scores = self._bm25.get_scores(tokenize(query))
        ranked = sorted(
            zip(self._records, scores),
            # Tie-break on precedent_id so a corpus with many zero-scoring entries still
            # returns a stable, reproducible list rather than relying on sort stability.
            key=lambda pair: (-float(pair[1]), pair[0].precedent_id),
        )
        return [RetrievedPrecedent(record, float(score)) for record, score in ranked[:k]]

    def indexed_text_for(self, precedent_id: str) -> str:
        """The exact text indexed for one precedent. Exists so tests can assert what is —
        and is not — in the index, rather than inferring it from ranking behaviour."""
        for record, text in zip(self._records, self._texts):
            if record.precedent_id == precedent_id:
                return text
        raise KeyError(precedent_id)
