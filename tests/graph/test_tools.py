import pytest

from precedent.domain.case import ReconciliationCase
from precedent.graph.tools import TOOL_NAMES, CaseWorkspace
from tests.domain.test_case import credit, ledger, payment


@pytest.fixture
def workspace():
    case = ReconciliationCase("c1", [payment()], [credit()], [ledger()])
    with CaseWorkspace(case) as ws:
        yield ws


class TestRegistry:
    def test_exposes_exactly_the_six_tools_the_spec_names(self, workspace):
        assert set(workspace.registry()) == set(TOOL_NAMES)
        assert len(TOOL_NAMES) == 6

    def test_every_registered_tool_is_callable_with_no_arguments(self, workspace):
        # The model may call any of these with an empty argument object; none may require
        # a parameter it might not supply.
        for name, tool in workspace.registry().items():
            assert isinstance(tool(), dict), name

    def test_every_tool_returns_json_serialisable_output(self, workspace):
        import json

        for name, tool in workspace.registry().items():
            json.dumps(tool()), name


class TestIsolation:
    def test_a_workspace_sees_only_its_own_case(self):
        # FR-9.10, enforced structurally rather than by convention: a tool physically
        # cannot reach another scenario's records, so a model cannot "solve" a case by
        # matching against one that belongs to a different scenario.
        mine = ReconciliationCase("c1", [payment("order_mine")], [], [])
        theirs = ReconciliationCase("c2", [payment("order_theirs")], [], [])
        with CaseWorkspace(mine) as a, CaseWorkspace(theirs) as b:
            a_ids = [p["order_id"] for p in a.fetch_payment()["payments"]]
            b_ids = [p["order_id"] for p in b.fetch_payment()["payments"]]
            assert a_ids == ["order_mine"]
            assert b_ids == ["order_theirs"]

    def test_records_are_loaded_through_the_real_repositories(self):
        # Reading back by id proves the record went through the schema — CHECK constraints
        # included — rather than being served from an in-memory list.
        case = ReconciliationCase("c1", [payment()], [], [])
        with CaseWorkspace(case) as ws:
            found = ws.fetch_payment(payment_id="pay_order_1")
            assert found["count"] == 1


class TestFetchPayment:
    def test_reports_gross_fee_and_what_it_settles_to(self, workspace):
        found = workspace.fetch_payment()["payments"][0]
        assert found["gross_paise"] == 100_000
        assert found["fee_paise"] == 2_000
        assert found["settles_to_paise"] == 100_000 - 2_000 - 360

    def test_filters_by_order(self):
        case = ReconciliationCase("c1", [payment("o1"), payment("o2")], [], [])
        with CaseWorkspace(case) as ws:
            assert ws.fetch_payment(order_id="o1")["count"] == 1

    def test_an_unknown_id_returns_empty_rather_than_raising(self, workspace):
        assert workspace.fetch_payment(payment_id="pay_nope")["count"] == 0


class TestFetchBankLines:
    def test_filters_to_credits(self):
        from precedent.adapters.storage.records import BankLineRecord

        debit = BankLineRecord("line_d", "2026-01-03", 5_000, "debit", "reversal", "synthetic")
        case = ReconciliationCase("c1", [payment()], [credit(), debit], [ledger()])
        with CaseWorkspace(case) as ws:
            assert ws.fetch_bank_lines(direction="credit")["count"] == 1
            assert ws.fetch_bank_lines()["count"] == 2


class TestFetchRefunds:
    def test_reports_the_absence_of_a_refund_record_as_a_fact(self, workspace):
        # The absence is the answer, not missing data: a refund netted into a same-day
        # settlement leaves no processor record at all, which is what makes the class hard.
        result = workspace.fetch_refunds()
        assert result["processor_refund_records"] == []
        assert "no such record" in result["note"].lower() or "No processor refund" in result["note"]

    def test_surfaces_the_unexplained_shortfall_that_a_refund_would_explain(self):
        # payment settles to 97,640; only 92,640 arrived — 5,000 unaccounted for.
        case = ReconciliationCase(
            "c1", [payment()], [credit(amount=100_000 - 2_000 - 360 - 5_000)], [ledger()]
        )
        with CaseWorkspace(case) as ws:
            assert ws.fetch_refunds()["unexplained_shortfall_paise"] == 5_000

    def test_reports_no_shortfall_when_the_credit_is_whole(self, workspace):
        assert workspace.fetch_refunds()["unexplained_shortfall_paise"] == 0


class TestComputeExpectedAmount:
    def test_nets_fees_per_payment_rather_than_once_per_batch(self):
        case = ReconciliationCase("c1", [payment("o1"), payment("o2")], [], [])
        with CaseWorkspace(case) as ws:
            result = ws.compute_expected_amount()
            assert result["settles_to_paise"] == 2 * (100_000 - 2_000 - 360)
            assert result["total_fee_and_tax_paise"] == 2 * 2_360

    def test_can_be_restricted_to_a_subset_of_payments(self):
        case = ReconciliationCase("c1", [payment("o1"), payment("o2")], [], [])
        with CaseWorkspace(case) as ws:
            result = ws.compute_expected_amount(payment_ids=["pay_o1"])
            assert result["payments_considered"] == ["pay_o1"]
            assert result["settles_to_paise"] == 100_000 - 2_000 - 360

    def test_reconstructs_a_gross_invoice_from_a_net_receipt(self):
        # The withholding case: 90,000 received at 10% withheld implies a 100,000 invoice.
        paid = payment(amount=90_000, fee=0, tax=0)
        case = ReconciliationCase("c1", [paid], [], [ledger(expects=100_000)])
        with CaseWorkspace(case) as ws:
            result = ws.compute_expected_amount(deduction_rate="0.10")
            assert result["reconstructed_gross_at_rate_paise"] == 100_000
            assert result["matches_ledger_expectation"] is True

    def test_a_wrong_rate_does_not_match_the_ledger(self):
        paid = payment(amount=90_000, fee=0, tax=0)
        case = ReconciliationCase("c1", [paid], [], [ledger(expects=100_000)])
        with CaseWorkspace(case) as ws:
            result = ws.compute_expected_amount(deduction_rate="0.02")
            assert result["matches_ledger_expectation"] is False

    def test_a_malformed_rate_is_reported_rather_than_raising(self, workspace):
        # A tool that raises would take down the whole investigation for a bad argument.
        result = workspace.compute_expected_amount(deduction_rate="ten percent")
        assert "deduction_rate_error" in result

    def test_rejects_a_rate_the_domain_layer_rejects(self, workspace):
        # gross_before_tds_paise requires 0 <= rate < 1; the tool must surface that rather
        # than inventing its own arithmetic to cope.
        result = workspace.compute_expected_amount(deduction_rate="1.5")
        assert "deduction_rate_error" in result


class TestSearchPriorResolutions:
    def test_delegates_group_matching_to_the_deterministic_matcher(self):
        # The agent, the matcher and the verifier must share one definition of "nets".
        payments = [payment("o1"), payment("o2")]
        settled = sum(p.amount_paise - p.fee_paise - p.tax_paise for p in payments)
        case = ReconciliationCase("c1", payments, [credit(amount=settled)], [])
        with CaseWorkspace(case) as ws:
            findings = ws.search_prior_resolutions()["findings"]
            assert findings[0]["all_payments_net_to_this_credit"] is True

    def test_reports_a_non_matching_group_as_such(self):
        payments = [payment("o1"), payment("o2")]
        case = ReconciliationCase("c1", payments, [credit(amount=1)], [])
        with CaseWorkspace(case) as ws:
            findings = ws.search_prior_resolutions()["findings"]
            assert findings[0]["all_payments_net_to_this_credit"] is False

    def test_ignores_debit_lines(self):
        from precedent.adapters.storage.records import BankLineRecord

        debit = BankLineRecord("line_d", "2026-01-03", 5_000, "debit", "x", "synthetic")
        case = ReconciliationCase("c1", [payment()], [debit], [])
        with CaseWorkspace(case) as ws:
            assert ws.search_prior_resolutions()["credits_examined"] == 0
