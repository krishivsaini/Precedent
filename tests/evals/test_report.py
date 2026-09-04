"""The eval report. Rendered from committed JSON, so these tests are mostly about that.

The design constraint is that **no number in the report is typed in** — every one is read
from a result file. A report with transcribed figures drifts from the run that produced them
the first time anyone edits it, and the drift is silent. These tests are what stop that
becoming true by accident.
"""

import json
import re

import pytest

from evals.report import build_report, latest, pct


class TestFormatting:
    def test_a_missing_metric_renders_as_a_dash_not_zero(self):
        # Reporting an unmeasured metric as 0.0% would be a false claim about the system.
        assert pct(None) == "—"
        assert pct(0.0) == "0.0%"


class TestRendersFromCommittedFiles:
    @staticmethod
    @pytest.fixture(scope="class")
    def report():
        return build_report()

    def test_it_renders_without_a_result_file_present(self, tmp_path, monkeypatch):
        # A clone that has not run the evals should still get a page, not a traceback.
        monkeypatch.setattr("evals.report.RESULTS_DIR", tmp_path)
        page = build_report()
        assert "No learning-curve result committed yet" in page

    def test_every_curve_number_comes_from_the_committed_json(self, report):
        curve = latest("learning-curve-*.json")
        assert curve, "no learning-curve result committed"
        for point in curve["curve"]:
            rendered = f"{point['retrieved']['resolution_rate']:.1%}"
            assert rendered in report, f"{rendered} missing from the report"
            assert str(point["corpus_size"]) in report

    def test_the_significance_values_come_from_the_json(self, report):
        curve = latest("learning-curve-*.json")
        for test in (curve.get("significance") or {}).values():
            assert f"{test['p_value']:.5f}" in report

    def test_the_model_is_named(self, report):
        curve = latest("learning-curve-*.json")
        assert curve["model"] in report

    def test_the_baseline_is_the_committed_rules_only_figure(self, report):
        baseline = latest("2026-09-01-0838.json")
        assert baseline, "the Ring 0 rules baseline should be committed"
        assert f"{baseline['metrics']['autonomous_resolution_rate']:.1%}" in report


class TestItLeadsWithTheLimitations:
    """A report that has to be read carefully to find its own caveats is a marketing
    document. These assert the constraining facts are present and prominent."""

    @staticmethod
    @pytest.fixture(scope="class")
    def report():
        return build_report()

    def test_the_random_control_is_shown_beside_every_curve_point(self, report):
        curve = latest("learning-curve-*.json")
        for point in curve["curve"]:
            assert f"{point['random_control']['resolution_rate']:.1%}" in report

    def test_the_control_verdict_matches_the_committed_numbers(self, report):
        # The sentence that rules out the prompt-length explanation must be *derived*. An
        # earlier version hard-coded "it falls" — true of one run and false of the next,
        # which is precisely the silent drift this report exists to prevent.
        curve = latest("learning-curve-*.json")
        points = curve["curve"]
        delta = (points[-1]["random_control"]["resolution_rate"]
                 - points[0]["random_control"]["resolution_rate"])
        moved = (curve["significance"]["control_first_to_last"]["significant_at_05"]
                 or abs(delta) > 0.10)
        if not moved:
            assert "does not move" in report
        elif delta < 0:
            assert "falls" in report
        else:
            assert "rises too" in report

    def test_it_states_the_narrow_form_of_the_claim(self, report):
        assert "cannot be derived" in report
        assert "every case that regressed was a derivable one" in report

    def test_every_caveat_from_the_result_file_is_carried_through(self, report):
        curve = latest("learning-curve-*.json")
        for caveat in curve["caveats"]:
            # Escaped in the HTML, so compare on a distinctive fragment.
            assert caveat.split(".")[0][:50] in report

    def test_it_says_the_test_set_was_never_deposited(self, report):
        assert "never deposited" in report

    def test_it_carries_the_ablation_effect_decomposition(self, report):
        # "Grounding gives +14.5pp" over-claims by more than 2x; the split is the honest form.
        assert "from having precedents" in report
        assert "their relevance" in report


class TestSelfContained:
    @staticmethod
    @pytest.fixture(scope="class")
    def report():
        return build_report()

    def test_it_loads_from_a_clone_with_no_network(self, report):
        # No external stylesheet, script or image: the report must open offline.
        assert "http://" not in report
        assert "https://" not in report
        assert not re.search(r"<(script|link|img)\b", report)

    def test_it_says_how_to_regenerate_itself(self, report):
        assert "evals.report" in report

    def test_values_are_escaped(self, report):
        # Caveats and model names are interpolated into HTML; an unescaped angle bracket
        # would silently break the page.
        assert "<script>" not in report
