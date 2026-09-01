import pytest

from precedent.adapters.retrieval.base import RetrievedPrecedent
from precedent.adapters.retrieval.bm25 import BM25Retriever
from precedent.adapters.retrieval.dense import DenseRetriever, HashingEmbedder
from precedent.adapters.retrieval.hybrid import HybridRetriever, reciprocal_rank_fusion
from precedent.adapters.retrieval.random_control import RandomRetriever
from precedent.corpus.seed import seed_precedent_records

TDS_QUERY = (
    "The bank credit is smaller than the open invoice by almost exactly two percent "
    "of the invoice value, and no fee or refund accounts for the gap."
)
NEFT_QUERY = (
    "A credit with no payment record behind it and a garbled uppercase narration that "
    "looks like a truncated customer name."
)


@pytest.fixture(scope="module")
def records():
    return seed_precedent_records()


def reason_codes(hits: list[RetrievedPrecedent]) -> list[str]:
    return [hit.record.reason_code for hit in hits]


class TestBM25Retriever:
    def test_surfaces_the_right_class_for_a_lexically_obvious_query(self, records):
        hits = BM25Retriever(records).retrieve(TDS_QUERY, k=5)
        assert "tds_short_payment" in reason_codes(hits)

    def test_returns_at_most_k(self, records):
        assert len(BM25Retriever(records).retrieve(TDS_QUERY, k=3)) == 3

    def test_scores_are_ordered_descending(self, records):
        hits = BM25Retriever(records).retrieve(TDS_QUERY, k=8)
        assert hits == sorted(hits, key=lambda h: h.score, reverse=True)

    def test_is_deterministic(self, records):
        retriever = BM25Retriever(records)
        assert retriever.retrieve(TDS_QUERY, k=5) == retriever.retrieve(TDS_QUERY, k=5)

    def test_an_empty_corpus_returns_nothing_rather_than_raising(self):
        # Corpus size 0 is a real state: it is the first point on the learning curve.
        assert BM25Retriever([]).retrieve(TDS_QUERY, k=5) == []

    def test_indexes_the_situation_not_the_resolution(self, records):
        # `retrieval_text` deliberately excludes the resolution narrative, so a precedent
        # cannot surface on words from its own conclusion. Derived from the corpus rather
        # than asserted on a hardcoded word, so it stays true as the seeds change.
        from precedent.adapters.retrieval.base import tokenize

        retriever = BM25Retriever(records)
        indexed = set(tokenize(" ".join(retriever.indexed_text_for(r.precedent_id) for r in records)))
        resolution_tokens = set(tokenize(" ".join(r.resolution for r in records)))

        conclusion_only = sorted(resolution_tokens - indexed)
        assert conclusion_only, "fixture assumption: resolutions say something situations do not"

        # A query made purely of words the corpus says only in its conclusions must match
        # nothing at all — not merely rank it low.
        hits = retriever.retrieve(" ".join(conclusion_only), k=5)
        assert all(hit.score == 0.0 for hit in hits)

    def test_indexed_text_is_the_situation_and_never_the_resolution(self, records):
        retriever = BM25Retriever(records)
        for record in records:
            text = retriever.indexed_text_for(record.precedent_id)
            assert record.situation in text
            assert record.amount_signature in text
            assert record.resolution not in text


class TestHashingEmbedder:
    def test_produces_a_fixed_dimension_unit_vector(self):
        vector = HashingEmbedder(dimensions=64).embed("a settlement batch credit")
        assert len(vector) == 64
        assert abs(sum(component * component for component in vector) - 1.0) < 1e-6

    def test_is_deterministic_across_instances(self):
        assert HashingEmbedder(dimensions=64).embed("netted settlement") == (
            HashingEmbedder(dimensions=64).embed("netted settlement")
        )

    def test_similar_text_scores_higher_than_unrelated_text(self):
        embedder = HashingEmbedder(dimensions=256)
        anchor = embedder.embed("withholding tax deducted at two percent before payment")
        near = embedder.embed("two percent withholding tax deducted before payment")
        far = embedder.embed("garbled narration on a direct bank transfer")
        dot = lambda a, b: sum(x * y for x, y in zip(a, b))
        assert dot(anchor, near) > dot(anchor, far)

    def test_empty_text_yields_a_zero_vector_rather_than_raising(self):
        assert HashingEmbedder(dimensions=32).embed("   ") == [0.0] * 32


class TestDenseRetriever:
    def test_retrieves_semantically_related_precedents(self, records):
        retriever = DenseRetriever(records, embedder=HashingEmbedder(dimensions=256))
        hits = retriever.retrieve(TDS_QUERY, k=6)
        assert "tds_short_payment" in reason_codes(hits)

    def test_returns_at_most_k_ordered_by_score(self, records):
        retriever = DenseRetriever(records, embedder=HashingEmbedder(dimensions=256))
        hits = retriever.retrieve(NEFT_QUERY, k=4)
        assert len(hits) == 4
        assert hits == sorted(hits, key=lambda h: h.score, reverse=True)

    def test_is_deterministic(self, records):
        retriever = DenseRetriever(records, embedder=HashingEmbedder(dimensions=256))
        assert retriever.retrieve(NEFT_QUERY, k=5) == retriever.retrieve(NEFT_QUERY, k=5)

    def test_an_empty_corpus_returns_nothing(self):
        retriever = DenseRetriever([], embedder=HashingEmbedder(dimensions=64))
        assert retriever.retrieve(TDS_QUERY, k=5) == []

    def test_closes_its_vector_index(self, records):
        retriever = DenseRetriever(records, embedder=HashingEmbedder(dimensions=64))
        retriever.close()


class TestReciprocalRankFusion:
    def test_an_item_ranked_first_by_both_beats_one_ranked_first_by_one(self):
        fused = reciprocal_rank_fusion([["a", "b"], ["a", "c"]])
        assert fused[0][0] == "a"

    def test_an_item_in_only_one_list_still_appears(self):
        fused = dict(reciprocal_rank_fusion([["a"], ["b"]]))
        assert set(fused) == {"a", "b"}

    def test_agreement_across_lists_outweighs_a_single_top_rank(self):
        # RRF's whole point: BM25 scores and cosine scores are not on a comparable scale,
        # so fusing on rank avoids inventing a normalisation nothing justifies. "b" is
        # second in both lists; "a" is first in one and absent from the other.
        fused = [key for key, _ in reciprocal_rank_fusion([["a", "b"], ["c", "b"]])]
        assert fused[0] == "b"

    def test_output_is_ordered_by_fused_score_descending(self):
        fused = reciprocal_rank_fusion([["a", "b", "c"], ["b", "a", "c"]])
        assert [score for _, score in fused] == sorted(
            (score for _, score in fused), reverse=True
        )
        assert fused[-1][0] == "c"

    def test_empty_input_is_empty_output(self):
        assert reciprocal_rank_fusion([]) == []


class TestHybridRetriever:
    def test_combines_both_retrievers(self, records):
        retriever = HybridRetriever(records, embedder=HashingEmbedder(dimensions=256))
        hits = retriever.retrieve(TDS_QUERY, k=5)
        assert len(hits) == 5
        assert "tds_short_payment" in reason_codes(hits)

    def test_returns_no_duplicate_precedents(self, records):
        retriever = HybridRetriever(records, embedder=HashingEmbedder(dimensions=256))
        ids = [hit.record.precedent_id for hit in retriever.retrieve(NEFT_QUERY, k=8)]
        assert len(ids) == len(set(ids))

    def test_is_deterministic(self, records):
        retriever = HybridRetriever(records, embedder=HashingEmbedder(dimensions=256))
        assert retriever.retrieve(TDS_QUERY, k=5) == retriever.retrieve(TDS_QUERY, k=5)

    def test_an_empty_corpus_returns_nothing(self):
        retriever = HybridRetriever([], embedder=HashingEmbedder(dimensions=64))
        assert retriever.retrieve(TDS_QUERY, k=5) == []


class TestRandomRetriever:
    def test_returns_k_precedents(self, records):
        assert len(RandomRetriever(records, seed=1).retrieve(TDS_QUERY, k=5)) == 5

    def test_ignores_the_query_entirely(self, records):
        # The negative control's defining property: if this ever became query-sensitive it
        # would stop being a control and the ablation would prove nothing.
        retriever = RandomRetriever(records, seed=1)
        assert retriever.retrieve(TDS_QUERY, k=5) == retriever.retrieve(NEFT_QUERY, k=5)

    def test_same_seed_is_reproducible(self, records):
        assert RandomRetriever(records, seed=7).retrieve(TDS_QUERY, k=5) == (
            RandomRetriever(records, seed=7).retrieve(TDS_QUERY, k=5)
        )

    def test_different_seeds_generally_differ(self, records):
        a = RandomRetriever(records, seed=1).retrieve(TDS_QUERY, k=5)
        b = RandomRetriever(records, seed=2).retrieve(TDS_QUERY, k=5)
        assert a != b

    def test_never_returns_the_same_precedent_twice(self, records):
        ids = [h.record.precedent_id for h in RandomRetriever(records, seed=3).retrieve("", k=10)]
        assert len(ids) == len(set(ids))

    def test_caps_at_corpus_size_when_k_exceeds_it(self, records):
        hits = RandomRetriever(records, seed=3).retrieve("", k=len(records) + 20)
        assert len(hits) == len(records)
