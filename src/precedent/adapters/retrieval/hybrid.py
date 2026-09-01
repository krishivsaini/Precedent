"""Hybrid retrieval: BM25 and dense, fused on rank.

Fusion is by Reciprocal Rank Fusion rather than a weighted sum of scores. BM25 scores are
unbounded and corpus-dependent; cosine similarities sit in [-1, 1]. Adding them requires a
normalisation nothing in the data justifies, and the normalisation constant then silently
becomes a tuned hyperparameter that no eval accounts for. RRF needs only the orderings, and
its one constant has a standard value.

Spec §6 requires reporting BM25-only, dense-only and hybrid separately, so the hybrid must
never be the only thing measurable — hence all three are exported as peer retrievers.
"""

from collections import defaultdict

from precedent.adapters.retrieval.base import RetrievedPrecedent
from precedent.adapters.retrieval.bm25 import BM25Retriever
from precedent.adapters.retrieval.dense import DenseRetriever, Embedder
from precedent.adapters.storage.records import PrecedentRecord

#: The standard RRF damping constant (Cormack et al., 2009). It flattens the difference
#: between adjacent high ranks so one retriever's confident first place cannot dominate the
#: other's ordering outright.
RRF_K = 60

#: How deep into each retriever's list to look before fusing. Wider than the final k, so a
#: precedent ranked mid-list by both can still surface above one ranked first by only one.
FUSION_DEPTH = 20


def reciprocal_rank_fusion(
    ranked_lists: list[list[str]], damping: int = RRF_K
) -> list[tuple[str, float]]:
    """Fuse ranked id lists into one, best first. Ties break on id for reproducibility."""
    scores: dict[str, float] = defaultdict(float)
    for ranked in ranked_lists:
        for position, key in enumerate(ranked):
            scores[key] += 1.0 / (damping + position + 1)
    return sorted(scores.items(), key=lambda pair: (-pair[1], pair[0]))


class HybridRetriever:
    def __init__(
        self,
        records: list[PrecedentRecord],
        embedder: Embedder | None = None,
        fusion_depth: int = FUSION_DEPTH,
    ):
        self._by_id = {record.precedent_id: record for record in records}
        self._bm25 = BM25Retriever(records)
        self._dense = DenseRetriever(records, embedder=embedder)
        self._fusion_depth = fusion_depth

    def retrieve(self, query: str, k: int) -> list[RetrievedPrecedent]:
        if not self._by_id or k <= 0:
            return []
        depth = max(k, self._fusion_depth)
        lists = [
            [hit.record.precedent_id for hit in self._bm25.retrieve(query, depth)],
            [hit.record.precedent_id for hit in self._dense.retrieve(query, depth)],
        ]
        fused = reciprocal_rank_fusion(lists)
        return [
            RetrievedPrecedent(self._by_id[precedent_id], score)
            for precedent_id, score in fused[:k]
        ]

    def close(self) -> None:
        self._dense.close()
