import re

from precedent.corpus.seed import (
    SEED_CORPUS_VERSION,
    load_seed_precedents,
    seed_precedent_records,
)
from precedent.domain.precedent import ESCALATION_REASON_CODES
from precedent.domain.reasons import ReasonCode

#: The nine exception classes of spec §5 map onto these codes. A seed corpus that leaves a
#: class uncovered guarantees the Ring 1.3 grounded arm has nothing to retrieve for it.
REQUIRED_REASON_CODES = {
    ReasonCode.EXACT_MATCH,
    ReasonCode.TOLERANCE_ROUNDING,
    ReasonCode.DATE_WINDOW_TIMING,
    ReasonCode.NETTED_SETTLEMENT,
    ReasonCode.DIRECT_NEFT_BYPASS,
    ReasonCode.TDS_SHORT_PAYMENT,
    ReasonCode.SPLIT_PAYMENT,
    ReasonCode.REFUND_NETTED,
    ReasonCode.DUPLICATE_PAYMENT_REJECTED,
    ReasonCode.UNMATCHABLE_NO_COUNTERPART,
}


class TestSeedCorpusContents:
    def test_every_entry_parses_and_validates(self):
        # The assertion is that load() did not raise; Precedent's validators are the check.
        assert load_seed_precedents()

    def test_is_the_roughly_forty_entries_the_spec_calls_for(self):
        assert 35 <= len(load_seed_precedents()) <= 50

    def test_covers_every_reason_code_the_dataset_can_produce(self):
        covered = {p.reason_code for p in load_seed_precedents()}
        assert REQUIRED_REASON_CODES <= covered, REQUIRED_REASON_CODES - covered

    def test_carries_no_escalation_precedents(self):
        codes = {p.reason_code for p in load_seed_precedents()}
        assert not (codes & ESCALATION_REASON_CODES)

    def test_every_agent_resolvable_class_has_more_than_one_precedent(self):
        # A single precedent per class means retrieval has nothing to discriminate between,
        # and top-k degenerates into "return the one entry for this class".
        agent_classes = {
            ReasonCode.NETTED_SETTLEMENT, ReasonCode.DIRECT_NEFT_BYPASS,
            ReasonCode.TDS_SHORT_PAYMENT, ReasonCode.SPLIT_PAYMENT,
            ReasonCode.REFUND_NETTED, ReasonCode.DUPLICATE_PAYMENT_REJECTED,
            ReasonCode.UNMATCHABLE_NO_COUNTERPART,
        }
        for code in agent_classes:
            count = sum(1 for p in load_seed_precedents() if p.reason_code == code)
            assert count >= 3, f"{code.value} has only {count} seed precedent(s)"

    def test_no_seed_claims_provenance_from_a_resolution(self):
        assert all(p.derived_from is None for p in load_seed_precedents())

    def test_situations_are_distinct(self):
        situations = [p.situation for p in load_seed_precedents()]
        assert len(situations) == len(set(situations))

    def test_amount_signatures_are_distinct(self):
        # Two seeds sharing a signature are near-duplicates dressed up as coverage.
        signatures = [p.amount_signature for p in load_seed_precedents()]
        assert len(signatures) == len(set(signatures))

    def test_no_situation_leaks_an_eval_scenario_identifier(self):
        # Scenario ids look like `scn_0001`; a seed mentioning one would make the Ring 1.3
        # ablation measure leakage instead of retrieval.
        pattern = re.compile(r"\bscn_\d+\b")
        assert not [p for p in load_seed_precedents() if pattern.search(p.situation)]

    def test_every_seed_states_a_resolution_of_substance(self):
        assert all(len(p.resolution) >= 80 for p in load_seed_precedents())

    def test_confidence_spans_a_real_range_rather_than_being_uniform(self):
        # Uniform confidence gives Ring 4's calibration nothing to work with, and is a
        # sign the values were filled in rather than judged.
        values = [p.confidence_at_deposit for p in load_seed_precedents()]
        assert max(values) - min(values) >= 0.2


class TestSeedRecords:
    def test_ids_are_deterministic_and_unique(self):
        first = [r.precedent_id for r in seed_precedent_records()]
        second = [r.precedent_id for r in seed_precedent_records()]
        assert first == second
        assert len(first) == len(set(first))
        assert first[0] == "prec_seed_0001"

    def test_every_record_is_at_corpus_version_zero(self):
        assert all(r.corpus_version == SEED_CORPUS_VERSION for r in seed_precedent_records())

    def test_records_have_no_embedding_yet(self):
        # Embeddings are attached by the dense retriever (Ring 1.2), not authored by hand.
        assert all(r.embedding is None for r in seed_precedent_records())

    def test_records_insert_cleanly_against_the_real_schema(self, conn):
        from precedent.adapters.storage.repositories import PrecedentsRepository

        repo = PrecedentsRepository(conn)
        for record in seed_precedent_records():
            repo.insert(record)
        stored = repo.list_as_of_corpus_version(SEED_CORPUS_VERSION)
        assert len(stored) == len(seed_precedent_records())
