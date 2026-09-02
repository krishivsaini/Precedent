import pytest
from pydantic import ValidationError

from precedent.domain.precedent import Precedent
from precedent.domain.reasons import ReasonCode


def make_precedent(**overrides):
    base = dict(
        situation=(
            "A vendor payment lands short of the invoiced amount by a round percentage, "
            "with no fee or refund explaining the gap."
        ),
        resolution=(
            "Reconstruct the gross invoice from the net receipt and confirm the shortfall "
            "equals statutory TDS at that rate; match the payment to the invoice at gross."
        ),
        reason_code=ReasonCode.TDS_SHORT_PAYMENT,
        entities=["vendor", "invoice"],
        amount_signature="short_by_2pct_tds",
        confidence_at_deposit=0.91,
    )
    base.update(overrides)
    return Precedent(**base)


class TestPrecedentValidation:
    def test_accepts_a_well_formed_precedent(self):
        p = make_precedent()
        assert p.reason_code is ReasonCode.TDS_SHORT_PAYMENT
        assert p.derived_from is None

    def test_confidence_must_be_a_probability(self):
        for bad in (-0.01, 1.01):
            with pytest.raises(ValidationError):
                make_precedent(confidence_at_deposit=bad)

    def test_situation_must_be_substantial_enough_to_retrieve_on(self):
        # A two-word situation is not a retrieval target; it matches everything or nothing.
        with pytest.raises(ValidationError, match="situation"):
            make_precedent(situation="TDS thing")

    def test_situation_may_not_embed_a_concrete_payment_or_order_id(self):
        # The whole point of `situation` is that it generalises to a future case. An ID
        # pins it to exactly one past case, so it can never retrieve again. Spec §4 calls
        # this the hardest prompt-engineering problem in the project; this is the guard.
        with pytest.raises(ValidationError, match="identifier"):
            make_precedent(
                situation="Payment pay_TWTMWlUBiCYWtU arrived short by two percent of the invoice."
            )

    def test_resolution_may_reference_concrete_ids(self):
        # Only `situation` is the retrieval target. The resolution narrative is allowed to
        # be specific about what was actually done.
        p = make_precedent(resolution="Matched pay_TWTMWlUBiCYWtU to invoice INV-88 at gross.")
        assert "pay_TWTMWlUBiCYWtU" in p.resolution

    def test_amount_signature_is_a_snake_case_key_not_prose(self):
        # It is a groupable key used to compare like with like across the corpus, so it has
        # to have a stable shape rather than being a second free-text field.
        with pytest.raises(ValidationError, match="amount_signature"):
            make_precedent(amount_signature="short by 2% TDS")

    def test_rejects_an_escalation_reason_code(self):
        # An escalation records that the agent gave up; it is not knowledge about the world.
        # Depositing one would poison the corpus with precedents whose lesson is "give up".
        with pytest.raises(ValidationError, match="[Ee]scalation"):
            make_precedent(reason_code=ReasonCode.ESCALATED_LOW_CONFIDENCE)

    def test_entities_are_stripped_and_emptied_entries_dropped(self):
        p = make_precedent(entities=["  Acme Traders  ", "", "   "])
        assert p.entities == ["Acme Traders"]

    def test_rejects_unknown_fields(self):
        # Guards against an LLM inventing a field and it silently vanishing on parse.
        with pytest.raises(ValidationError):
            make_precedent(vendor_gstin="27AAAAA0000A1Z5")


class TestPrecedentRecordRoundTrip:
    def test_to_record_and_back_preserves_every_field(self):
        original = make_precedent(
            entities=["Acme Traders", "NEFT"], derived_from="res_0001"
        )
        record = original.to_record(
            precedent_id="prec_0001",
            deposited_at="2026-09-01T10:00:00+00:00",
            corpus_version=7,
        )
        assert record.precedent_id == "prec_0001"
        assert record.corpus_version == 7
        assert record.derived_from_resolution == "res_0001"
        assert record.entities == ["Acme Traders", "NEFT"]

        assert Precedent.from_record(record) == original

    def test_reason_code_is_stored_as_its_string_value(self):
        record = make_precedent().to_record("prec_1", "2026-09-01T10:00:00+00:00", 0)
        assert record.reason_code == "tds_short_payment"
        assert isinstance(record.reason_code, str)

    def test_confidence_survives_the_round_trip(self):
        record = make_precedent(confidence_at_deposit=0.735).to_record(
            "prec_1", "2026-09-01T10:00:00+00:00", 0
        )
        assert record.confidence_at_deposit == 0.735
        assert Precedent.from_record(record).confidence_at_deposit == 0.735

    def test_retrieval_text_joins_the_fields_a_retriever_indexes(self):
        p = make_precedent(entities=["Acme Traders"])
        text = p.retrieval_text()
        assert p.situation in text
        assert "Acme Traders" in text
        assert "short_by_2pct_tds" in text


class TestConcreteIdentifiersTheGeneratorActuallyMints:
    """The first version of this guard required an unbroken alphanumeric run after the
    prefix, so it missed every id containing an underscore — including `order_synth_0233`,
    the shape this project's own generator produces. A model authoring a deposit put one
    into a situation and the validator passed it. A guard that does not catch the ids the
    system actually mints is not a guard."""

    @pytest.mark.parametrize("identifier", [
        "order_synth_0233",     # the generator's own shape, missed by the first version
        "pay_TWTMWlUBiCYWtU",   # Razorpay
        "order_TWL7gT6zin2oN8",
        "led_ab12cd34",
        "prec_seed_0001",
        "line_9f2a1b3c",
        "INV-4471",             # an invoice number is an id without a telltale prefix
        "INV 4471",
    ])
    def test_it_is_rejected_in_a_situation(self, identifier):
        with pytest.raises(ValidationError, match="identifier"):
            make_precedent(
                situation=(
                    f"A payment referenced as {identifier} arrived short of the invoiced "
                    "amount with no fee or refund explaining the difference at all."
                )
            )

    @pytest.mark.parametrize("phrase", [
        "a customer paying under net_30 terms",
        "the counterparty is Konark Logistics",
        "an order reference shared by two entries",
    ])
    def test_ordinary_prose_is_not_mistaken_for_an_identifier(self, phrase):
        # The guard must not fire on the vocabulary a good situation actually uses —
        # counterparty names especially, which the deposit prompt now requires.
        make_precedent(
            situation=f"A reconciliation case where {phrase} and the credit falls short."
        )
