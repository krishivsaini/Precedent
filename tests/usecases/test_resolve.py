import json

import pytest

from precedent.adapters.llm.base import LLMUnavailable
from precedent.adapters.llm.scripted import ScriptedLLM
from precedent.adapters.retrieval.base import RetrievedPrecedent
from precedent.corpus.seed import seed_precedent_records
from precedent.domain.case import ReconciliationCase
from precedent.domain.reasons import ReasonCode
from precedent.usecases.resolve import (
    ProposedResolution,
    extract_json,
    load_prompt,
    render_precedents,
    resolve_case,
)
from tests.domain.test_case import credit, ledger, payment


@pytest.fixture
def case():
    return ReconciliationCase("case_1", [payment()], [credit()], [ledger()])


@pytest.fixture
def hits():
    return [RetrievedPrecedent(record, 1.0) for record in seed_precedent_records()[:3]]


def answer(**overrides):
    body = {
        "reason_code": "exact_match",
        "confidence": 0.94,
        "rationale": "Credit equals the payment net of fee and tax.",
        "cited_precedent_ids": [],
    }
    body.update(overrides)
    return json.dumps(body)


class TestPrompt:
    def test_splits_into_a_system_and_user_half(self):
        system, user = load_prompt()
        assert "reconciliation analyst" in system
        assert "{case_summary}" in user
        assert "{precedents}" in user

    def test_the_system_half_lists_every_resolvable_reason_code(self):
        system, _ = load_prompt()
        for code in ReasonCode:
            if code.value.startswith("escalated_"):
                continue
            assert code.value in system, code.value

    def test_the_system_half_forbids_self_escalation(self):
        system, _ = load_prompt()
        assert "Do not return an `escalated_*` code" in system


class TestRenderPrecedents:
    def test_an_empty_corpus_still_renders_a_precedent_block(self, ):
        # The arms must differ in the *content* of this block, not in the shape of the
        # prompt. A missing section would confound grounding with a format change.
        assert "corpus is empty" in render_precedents([])

    def test_renders_the_fields_a_reader_needs_to_judge_applicability(self, hits):
        text = render_precedents(hits)
        for hit in hits:
            assert hit.record.precedent_id in text
            assert hit.record.situation in text
            assert hit.record.reason_code in text


class TestExtractJson:
    def test_passes_through_bare_json(self):
        assert json.loads(extract_json('{"a": 1}')) == {"a": 1}

    def test_recovers_json_from_a_fenced_block(self):
        assert json.loads(extract_json('```json\n{"a": 1}\n```')) == {"a": 1}

    def test_recovers_json_wrapped_in_prose(self):
        assert json.loads(extract_json('Here you go:\n{"a": 1}\nHope that helps.')) == {"a": 1}

    def test_returns_the_text_unchanged_when_there_is_no_object(self):
        assert extract_json("no json here") == "no json here"


class TestProposedResolutionSchema:
    def test_rejects_an_escalation_code_from_the_model(self):
        # Escalation is this system's decision about the answer, never an answer the model
        # may give — otherwise a model could opt out of being scored at will.
        with pytest.raises(ValueError):
            ProposedResolution(
                reason_code=ReasonCode.ESCALATED_LOW_CONFIDENCE,
                confidence=0.9, rationale="not sure",
            )

    def test_rejects_extra_keys(self):
        with pytest.raises(ValueError):
            ProposedResolution.model_validate(
                json.loads(answer()) | {"invented_field": "x"}
            )

    def test_rejects_a_confidence_outside_zero_to_one(self):
        with pytest.raises(ValueError):
            ProposedResolution.model_validate(json.loads(answer(confidence=1.4)))


class TestResolveCaseHappyPath:
    def test_returns_the_models_reason_code_and_confidence(self, case):
        outcome = resolve_case(case, ScriptedLLM([answer()]))
        assert outcome.reason_code is ReasonCode.EXACT_MATCH
        assert outcome.confidence == 0.94
        assert outcome.escalated is False

    def test_records_the_model_and_its_token_usage(self, case):
        outcome = resolve_case(case, ScriptedLLM([answer()]))
        assert outcome.model == "scripted-test-double"
        assert outcome.prompt_tokens == 0

    def test_puts_the_case_summary_and_precedents_into_the_prompt(self, case, hits):
        llm = ScriptedLLM([answer()])
        resolve_case(case, llm, precedents=hits)
        sent = llm.last_user_prompt
        assert case.summarize() in sent
        assert hits[0].record.precedent_id in sent
        assert "{case_summary}" not in sent
        assert "{precedents}" not in sent

    def test_records_what_was_retrieved_even_when_nothing_is_cited(self, case, hits):
        outcome = resolve_case(case, ScriptedLLM([answer()]), precedents=hits)
        assert outcome.retrieved_precedent_ids == [h.record.precedent_id for h in hits]
        assert outcome.cited_precedent_ids == []


class TestHallucinatedCitations:
    def test_flags_a_citation_that_was_never_retrieved(self, case, hits):
        outcome = resolve_case(
            case,
            ScriptedLLM([answer(cited_precedent_ids=["prec_seed_9999"])]),
            precedents=hits,
        )
        assert outcome.hallucinated_citations == ["prec_seed_9999"]

    def test_a_real_citation_is_not_flagged(self, case, hits):
        cited = hits[0].record.precedent_id
        outcome = resolve_case(
            case, ScriptedLLM([answer(cited_precedent_ids=[cited])]), precedents=hits
        )
        assert outcome.hallucinated_citations == []


class TestEveryFallbackPath:
    """Spec §7 and Ring 2.4: each fallback is exercised directly rather than trusted to
    fire during a demo. A fallback nobody has run is a fallback that does not work."""

    def test_model_unavailable_escalates_without_raising(self, case):
        outcome = resolve_case(case, ScriptedLLM([LLMUnavailable("connection refused")]))
        assert outcome.escalated
        assert outcome.reason_code is ReasonCode.ESCALATED_MODEL_UNAVAILABLE
        assert "connection refused" in outcome.rationale

    def test_an_exhausted_client_escalates_as_unavailable(self, case):
        outcome = resolve_case(case, ScriptedLLM([]))
        assert outcome.reason_code is ReasonCode.ESCALATED_MODEL_UNAVAILABLE

    def test_unparseable_output_escalates_as_a_parse_failure(self, case):
        outcome = resolve_case(case, ScriptedLLM(["I think this is a TDS case, honestly."]))
        assert outcome.escalated
        assert outcome.reason_code is ReasonCode.ESCALATED_PARSE_FAILURE

    def test_valid_json_that_violates_the_schema_escalates_as_a_parse_failure(self, case):
        outcome = resolve_case(case, ScriptedLLM(['{"reason_code": "invented_code"}']))
        assert outcome.reason_code is ReasonCode.ESCALATED_PARSE_FAILURE

    def test_a_model_that_tries_to_self_escalate_is_a_parse_failure(self, case):
        outcome = resolve_case(
            case, ScriptedLLM([answer(reason_code="escalated_low_confidence")])
        )
        assert outcome.reason_code is ReasonCode.ESCALATED_PARSE_FAILURE

    def test_confidence_below_the_threshold_escalates(self, case):
        outcome = resolve_case(
            case, ScriptedLLM([answer(confidence=0.4)]), confidence_threshold=0.8
        )
        assert outcome.escalated
        assert outcome.reason_code is ReasonCode.ESCALATED_LOW_CONFIDENCE
        assert outcome.confidence == 0.4

    def test_confidence_exactly_at_the_threshold_resolves(self, case):
        outcome = resolve_case(
            case, ScriptedLLM([answer(confidence=0.8)]), confidence_threshold=0.8
        )
        assert outcome.escalated is False

    def test_no_input_can_make_resolve_case_raise(self, case):
        # The load-bearing property: a batch that dies part-way through loses every result
        # already computed, so there is no path out of here that raises.
        for response in ["", "null", "[]", "{}", '{"reason_code": null}', "\x00",
                         '{"reason_code": "exact_match"}']:
            outcome = resolve_case(case, ScriptedLLM([response]))
            assert outcome.escalated
