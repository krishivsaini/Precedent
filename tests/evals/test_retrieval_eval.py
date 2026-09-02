import pytest

from evals.retrieval_eval import K_VALUES, run_retrieval_eval


@pytest.fixture(scope="module")
def result():
    return run_retrieval_eval()


class TestScope:
    def test_scores_only_the_pool_exceptions(self, result):
        assert result["dataset"]["scenarios_scored"] == 98

    def test_runs_against_the_seed_corpus_at_version_zero(self, result):
        assert result["corpus"]["corpus_version"] == 0
        assert result["corpus"]["size"] == 42

    def test_reports_all_three_retrievers_plus_the_control(self, result):
        # spec §6 requires BM25-only vs dense-only vs hybrid reported separately, and the
        # random control run on every eval — unprompted.
        assert {arm["retriever"] for arm in result["arms"]} == {
            "bm25", "dense", "hybrid", "random_control"
        }

    def test_declares_that_the_embedder_is_not_semantic(self, result):
        # An unqualified dense/hybrid number here would be read as a claim about semantic
        # retrieval, which the hashing embedder cannot support.
        assert result["embedder"]["semantic"] is False
        assert result["embedder"]["caveat"]


class TestMetrics:
    def test_precision_is_monotonic_in_k(self, result):
        for arm in result["arms"]:
            precision = arm["precedent_class_precision"]
            values = [precision[f"top_{k}"] for k in sorted(K_VALUES)]
            assert values == sorted(values), arm["retriever"]

    def test_every_pool_class_is_accounted_for(self, result):
        bm25 = next(a for a in result["arms"] if a["retriever"] == "bm25")
        assert set(bm25["per_class"]) == {
            "netted_settlement", "direct_neft_bypass", "tds_short_payment", "split_payment",
            "refund_netted", "duplicate_payment", "unmatchable",
            "negotiated_rebate", "advance_adjusted",
        }

    def test_the_best_retriever_beats_the_random_control(self, result):
        # The kill-criterion-shaped check for retrieval alone: if relevant precedents help
        # no more than random ones, retrieval is doing nothing.
        assert result["verdict"]["beats_random_control"]

    def test_the_control_is_not_trivially_zero(self, result):
        # A control scoring 0 would mean the metric is degenerate rather than that
        # retrieval is good — with 42 precedents over 7 classes, random should land
        # sometimes, and it must be measured rather than assumed.
        assert result["verdict"]["random_control_top_3"] > 0.0

    #: Resolvable from the evidence, so the seed corpus covers them by design.
    DERIVABLE = {
        "netted_settlement", "direct_neft_bypass", "tds_short_payment", "split_payment",
        "refund_netted", "duplicate_payment", "unmatchable",
    }
    #: Counterparty knowledge. The seed corpus deliberately says nothing about these
    #: customers, so retrieval *must* fail on them at corpus_version 0 — that failure is
    #: the headroom the learning curve is measured in.
    COUNTERPARTY = {"negotiated_rebate", "advance_adjusted"}

    def test_lexical_retrieval_covers_every_derivable_class_at_the_operating_k(self, result):
        # Justifies k=5, asserted rather than eyeballed so a corpus or renderer change
        # that breaks it fails the build.
        bm25 = next(a for a in result["arms"] if a["retriever"] == "bm25")
        for kind, counts in bm25["per_class"].items():
            if kind in self.DERIVABLE:
                hits, total = counts["top_5"].split("/")
                assert hits == total, f"{kind}: {counts['top_5']}"

    def test_the_seed_corpus_cannot_resolve_the_counterparty_classes(self, result):
        # The load-bearing property of Ring 2.5. If a seed precedent could answer these,
        # they would be derivable after all and the learning curve would measure nothing.
        bm25 = next(a for a in result["arms"] if a["retriever"] == "bm25")
        for kind in self.COUNTERPARTY:
            hits, _total = bm25["per_class"][kind]["top_5"].split("/")
            assert hits == "0", f"{kind} should be unreachable from seeds, got {hits}"


class TestReproducibility:
    def test_two_runs_agree_on_every_metric(self):
        first, second = run_retrieval_eval(), run_retrieval_eval()
        assert first["arms"] == second["arms"]
