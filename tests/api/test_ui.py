"""The approval screen.

Mostly assertions about the things product_design.md argues for rather than about markup:
the order sections appear in, that the arithmetic is shown as two comparisons rather than a
verdict, that the prominent action matches the warning above it, and that a money screen
never prints a raw paise figure.
"""

import json
import re

import pytest
from fastapi.testclient import TestClient

from precedent.adapters.llm.scripted import ScriptedLLM
from precedent.adapters.storage.db import connect, init_db
from precedent.adapters.storage.records import (
    BankLineRecord,
    ExceptionRecord,
    LedgerEntryRecord,
    PaymentRecord,
    PrecedentRecord,
    ResolutionRecord,
)
from precedent.adapters.storage.repositories import (
    BankLinesRepository,
    ExceptionsRepository,
    LedgerEntriesRepository,
    PaymentsRepository,
    PrecedentsRepository,
    ResolutionsRepository,
)
from precedent.api.main import create_app
from precedent.api.ui import depositing_llm

NOW = "2026-09-03T09:00:00+00:00"


#: What the deposit model is scripted to return. The reason code is overridden from the
#: confirmed resolution inside `author_precedent`, so the value here is deliberately not the
#: one the assertions depend on.
AUTHORED = json.dumps({
    "situation": (
        "Payments from this counterparty arrive short of the invoice by a proportion "
        "matching no statutory withholding band, with no refund or fee explaining it."
    ),
    "resolution": (
        "The counterparty settles under a negotiated rebate agreed in their supply "
        "contract. Reconstruct the invoice from the receipt and close it in full."
    ),
    "reason_code": "negotiated_rebate",
    "entities": ["Coral Textiles"],
    "amount_signature": "rebate_coral",
    "confidence_at_deposit": 0.93,
})


def seed(db, *, confidence=0.94, verified=True, rationale="the credit ties out",
         human_action=None, llm=None):
    conn = connect(str(db))
    init_db(conn)
    PaymentsRepository(conn).insert(PaymentRecord(
        payment_id="pay_1", order_id="order_1", amount_paise=167_800,
        captured_at=NOW, status="captured", source="synthetic",
        fee_paise=3_960, tax_paise=604,
    ))
    BankLinesRepository(conn).insert(BankLineRecord(
        line_id="line_1", value_date="2026-09-04", amount_paise=163_236,
        direction="credit", narration="NEFT-CR order_1", source="synthetic",
    ))
    LedgerEntriesRepository(conn).insert(LedgerEntryRecord(
        entry_id="led_1", order_id="order_1", expected_amount_paise=186_444,
        invoice_no="INV-5672", customer_name="Coral Textiles", terms="net_30",
    ))
    PrecedentsRepository(conn).insert(PrecedentRecord(
        precedent_id="prec_seed_0001",
        situation="A payment falls short of the invoice by a round percentage with no fee "
                  "or refund explaining the gap.",
        resolution="Reconstruct the gross invoice from the net receipt and match at gross.",
        reason_code="tds_short_payment", entities=["TDS"],
        amount_signature="short_by_10pct_tds", confidence_at_deposit=0.94,
        deposited_at=NOW, corpus_version=0,
    ))
    ExceptionsRepository(conn).insert(ExceptionRecord(
        exception_id="exc_1", batch_id="b1", kind="tds_short_payment",
        member_refs=["pay_1", "line_1", "led_1"], detected_at=NOW, status="open",
        correlation_id="corr_1",
    ))
    ResolutionsRepository(conn).insert(ResolutionRecord(
        resolution_id="res_1", exception_id="exc_1", proposed_by="agent",
        confidence=confidence, rationale=rationale,
        cited_precedents=["prec_seed_0001"], verified=verified,
    ))
    if human_action:
        ResolutionsRepository(conn).record_human_action(
            resolution_id="res_1", human_action=human_action, resolved_at=NOW,
        )
        if human_action in {"confirmed", "corrected"}:
            # A decision that deposits leaves a precedent behind it. Seeding the action
            # without one would model a state the gate is built to make unreachable, and the
            # screen reports what is actually in the corpus rather than what the action implies.
            PrecedentsRepository(conn).insert(PrecedentRecord(
                precedent_id="prec_0001", situation="A payment falls short of the invoice "
                "by a proportion no statutory band explains.",
                resolution="Close it under the counterparty's negotiated rebate.",
                reason_code="negotiated_rebate", entities=["Coral Textiles"],
                amount_signature="rebate_coral", confidence_at_deposit=0.93,
                deposited_at=NOW, corpus_version=1, derived_from_resolution="res_1",
            ))
    conn.commit()
    conn.close()
    app = create_app(db_path=str(db))
    # Never the network. The gate authors a precedent through this dependency, and a test
    # that reached a real vendor would be neither deterministic nor runnable from a clone.
    app.dependency_overrides[depositing_llm] = lambda: llm or ScriptedLLM([AUTHORED] * 4)
    return TestClient(app)


@pytest.fixture
def client(tmp_path):
    with seed(tmp_path / "ui.db") as c:
        yield c


class TestQueue:
    def test_it_lists_what_is_waiting(self, client):
        body = client.get("/").text
        assert "1 to review" in body
        assert "Coral Textiles" in body

    def test_it_shows_what_is_at_stake_in_rupees(self, client):
        assert "1,864.44" in client.get("/").text

    def test_a_decided_case_leaves_the_queue(self, tmp_path):
        with seed(tmp_path / "d.db", human_action="confirmed") as client:
            assert "Nothing to review" in client.get("/").text

    def test_the_empty_state_directs_rather_than_apologises(self, tmp_path):
        with seed(tmp_path / "e.db", human_action="rejected") as client:
            body = client.get("/").text
            assert "New exceptions appear here" in body
            assert "sorry" not in body.lower()


class TestTheArithmeticIsTwoComparisons:
    """The design doc's central requirement: the bank credit is net of the processor's fee
    and the invoice is gross, so collapsing them into one 'matches' line is what stops a
    reviewer separating a genuine shortfall from a fee deduction."""

    def test_both_columns_are_present(self, client):
        body = client.get("/exceptions/res_1").text
        assert "At the bank" in body and "Against the invoice" in body

    def test_the_chain_shows_each_deduction_not_just_a_total(self, client):
        body = client.get("/exceptions/res_1").text
        for figure in ("1,678.00", "(39.60)", "(6.04)", "1,632.36"):
            assert figure in body, figure

    def test_the_bank_side_and_the_invoice_side_reach_different_verdicts(self, client):
        # This case ties out at the bank and is short against the invoice — the exact
        # situation a single "matches" line would render as one misleading answer.
        body = client.get("/exceptions/res_1").text
        assert "The credit ties out." in body
        assert "The customer withheld part of the invoice." in body

    def test_negative_figures_are_set_in_parentheses(self, client):
        assert "(39.60)" in client.get("/exceptions/res_1").text

    def test_no_screen_prints_a_raw_paise_figure(self, client):
        # A money screen that shows 167800 where it means INR 1,678.00 is misreading waiting
        # to happen.
        for path in ("/", "/exceptions/res_1"):
            assert not re.search(r"\b\d{5,}\s*paise", client.get(path).text)


class TestOrderOfArgument:
    def test_precedents_appear_above_the_proposal(self, client):
        # Judge what the system thought was *similar* before seeing what it *concluded*;
        # verdict-first anchors the reviewer on the conclusion.
        body = client.get("/exceptions/res_1").text
        assert body.index("What the system had seen before") < body.index("What it proposes")

    def test_the_arithmetic_appears_above_the_precedents(self, client):
        body = client.get("/exceptions/res_1").text
        assert body.index("Where the money went") < body.index("What the system had seen")

    def test_precedents_are_shown_in_full_not_as_identifiers(self, client):
        body = client.get("/exceptions/res_1").text
        assert "Reconstruct the gross invoice" in body


class TestTheGate:
    def test_there_are_exactly_three_actions(self, client):
        body = client.get("/exceptions/res_1").text
        actions = set(re.findall(r'name="human_action" value="(\w+)"', body))
        assert actions == {"confirmed", "corrected", "rejected"}

    def test_it_says_what_a_confirm_does(self, client):
        # §3.2a: a reviewer who knows their click becomes durable knowledge behaves
        # differently from one who thinks they are closing a ticket.
        assert "writes this into the corpus" in client.get("/exceptions/res_1").text

    def test_it_says_a_correction_is_worth_more(self, client):
        assert "worth more than a confirmation" in client.get("/exceptions/res_1").text

    def test_a_trustworthy_draft_makes_confirm_the_primary_action(self, client):
        body = client.get("/exceptions/res_1").text
        assert 'class="confirm" name="human_action" value="confirmed"' in body

    def test_an_unverified_draft_demotes_confirm_and_opens_the_correction(self, tmp_path):
        # The screen told the reviewer to resolve it themselves and then offered Confirm as
        # the easiest click — the interface arguing against itself.
        with seed(tmp_path / "u.db", verified=False) as client:
            body = client.get("/exceptions/res_1").text
            assert "Confirm anyway" in body
            assert 'class="confirm" name="human_action" value="confirmed"' not in body
            assert 'details class="correct" open' in body

    def test_a_decided_case_shows_no_gate(self, tmp_path):
        with seed(tmp_path / "c.db", human_action="confirmed") as client:
            body = client.get("/exceptions/res_1").text
            assert "human_action" not in body
            assert "now in the corpus" in body


class TestFailureStatesAreDistinct:
    """§3.2b — rendering 'no proposal' identically whether the system was thinking,
    unreachable, or refusing to guess teaches the reviewer to distrust all three."""

    def test_low_confidence_is_named_as_such(self, tmp_path):
        with seed(tmp_path / "lc.db", confidence=0.55) as client:
            assert "Below the bar" in client.get("/exceptions/res_1").text

    def test_an_outage_is_not_presented_as_a_judgement(self, tmp_path):
        with seed(tmp_path / "o.db",
                  rationale="Gemini unavailable after 6 attempts") as client:
            body = client.get("/exceptions/res_1").text
            assert "could not be reached" in body
            assert "outage, not a judgement" in body

    def test_unusable_output_is_distinguished_from_an_outage(self, tmp_path):
        with seed(tmp_path / "p.db",
                  rationale="could not parse a resolution: ValidationError") as client:
            assert "unusable output" in client.get("/exceptions/res_1").text

    def test_a_failed_verification_says_the_draft_is_evidence_not_advice(self, tmp_path):
        with seed(tmp_path / "v.db", verified=False) as client:
            assert "not a recommendation" in client.get("/exceptions/res_1").text


class TestDecisions:
    def test_confirming_records_it_and_redirects(self, client):
        response = client.post("/exceptions/res_1/decide",
                               data={"human_action": "confirmed"}, follow_redirects=False)
        assert response.status_code == 303
        assert "now in the corpus" in client.get("/exceptions/res_1").text

    def test_a_correction_without_an_answer_is_refused(self, client):
        # Otherwise the agent's own answer is deposited labelled as a human correction.
        body = client.post("/exceptions/res_1/decide",
                           data={"human_action": "corrected"}).text
        assert "Choose what it should have been" in body
        assert "worst of both" in body

    def test_a_correction_with_an_answer_is_recorded(self, client):
        client.post("/exceptions/res_1/decide", data={
            "human_action": "corrected", "corrected_reason_code": "negotiated_rebate",
            "correction_note": "standing rebate",
        })
        assert "corrected" in client.get("/exceptions/res_1").text

    def test_an_unknown_action_is_refused(self, client):
        assert "was not recorded" in client.post(
            "/exceptions/res_1/decide", data={"human_action": "snooze"}
        ).text

    def test_an_unknown_case_does_not_500(self, client):
        assert client.get("/exceptions/res_nope").status_code == 200


class TestQualityFloor:
    def test_it_declares_a_language_and_a_viewport(self, client):
        body = client.get("/").text
        assert "<html lang='en'>" in body
        assert "width=device-width" in body

    def test_it_stacks_on_a_narrow_screen(self, client):
        assert "@media (max-width: 46rem)" in client.get("/exceptions/res_1").text

    def test_it_respects_reduced_motion(self, client):
        assert "prefers-reduced-motion" in client.get("/exceptions/res_1").text

    def test_keyboard_focus_stays_visible(self, client):
        assert ":focus-visible" in client.get("/exceptions/res_1").text

    def test_it_loads_with_no_external_asset(self, client):
        # Works from a clone with no network, like everything else in this repo. The bar is
        # that nothing is *fetched*, not that no script exists: the gate's pending state is
        # inline, ships in the same response, and needs no network to run.
        body = client.get("/exceptions/res_1").text
        assert not re.search(r"<script\b[^>]*\ssrc=", body)
        assert not re.search(r"<(link|img)\b", body)
        assert "url(" not in body

    def test_its_javascript_is_an_enhancement_rather_than_the_mechanism(self, client):
        # The gate must work with scripting off, so the buttons carry their own name and
        # value and the form posts on its own. If the decision ever depended on JS, a
        # reviewer with it disabled would click Confirm and silently record nothing.
        body = client.get("/exceptions/res_1").text
        assert 'name="human_action" value="confirmed"' in body
        assert '<form method="post" action="/exceptions/res_1/decide">' in body

    def test_every_gate_button_carries_its_own_name_and_value(self, client):
        # Two things depend on this. Without JS it is how the server learns which action was
        # chosen; with JS it is what the submit handler copies into a hidden input before it
        # disables the buttons, because a disabled button is not submitted.
        body = client.get("/exceptions/res_1").text
        buttons = re.findall(r"<button\b[^>]*>", body)
        assert buttons
        for button in buttons:
            assert 'name="human_action"' in button, button
            assert "value=" in button, button

    def test_the_gate_says_a_slow_click_is_working(self, client):
        # Confirming calls a model and takes seconds. A button that looks unpressed for that
        # long is one a reviewer clicks again.
        body = client.get("/exceptions/res_1").text
        assert "data-pending" in body
        assert 'class="pending"' in body
        assert "takes a few" in body

    def test_values_are_escaped(self, tmp_path):
        with seed(tmp_path / "x.db", rationale="<script>alert(1)</script>") as client:
            body = client.get("/exceptions/res_1").text
            assert "<script>alert(1)</script>" not in body
            assert "&lt;script&gt;" in body
