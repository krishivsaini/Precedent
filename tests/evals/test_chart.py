"""The learning-curve chart.

Generated from the same JSON as the report's tables, so the tests are mostly about that
coupling: a chart drawn from a separate source can drift from the number beside it and nobody
notices, because they render independently.
"""

import re

import pytest

from evals.chart import render_curve_svg
from evals.report import latest


@pytest.fixture(scope="module")
def curve():
    result = latest("learning-curve-*.json")
    assert result, "no learning-curve result committed"
    return result


class TestItPlotsTheCommittedNumbers:
    def test_it_draws_all_three_series(self, curve):
        # Treatment, control, and the counterparty subset. Dropping the control would remove
        # the only line that says whether the other one means anything.
        assert render_curve_svg(curve).count("<polyline") == 3

    def test_a_point_is_plotted_for_every_snapshot(self, curve):
        svg = render_curve_svg(curve)
        first = svg.split("<polyline")[1]
        assert first.count(",") == len(curve["curve"])

    def test_the_x_axis_is_labelled_with_the_committed_deposit_counts(self, curve):
        svg = render_curve_svg(curve)
        for point in curve["curve"]:
            assert f">{point['deposits']}</text>" in svg

    def test_the_rules_baseline_is_overlaid_when_given(self, curve):
        # Spec §9 Ring 3.6 asks for the Ring 0/1 baselines on the chart, not just in prose.
        svg = render_curve_svg(curve, rules_baseline=0.492)
        assert "rules only (49.2%)" in svg
        assert "stroke-dasharray" in svg

    def test_it_omits_the_baseline_rather_than_inventing_one(self, curve):
        assert "rules only" not in render_curve_svg(curve, rules_baseline=None)


class TestDegenerateInput:
    def test_a_single_snapshot_is_not_drawn_as_a_curve(self, curve):
        one = {"curve": curve["curve"][:1]}
        assert "<svg" not in render_curve_svg(one)
        assert "Not enough snapshots" in render_curve_svg(one)

    def test_an_empty_result_does_not_raise(self):
        assert "Not enough snapshots" in render_curve_svg({"curve": []})

    def test_a_missing_subset_series_is_skipped_not_zeroed(self, curve):
        # Plotting a missing counterparty rate as 0% would draw a line asserting the system
        # failed every case, which is a different claim from not having measured it.
        stripped = {
            "curve": [
                {**p, "retrieved": {**p["retrieved"], "counterparty_resolution_rate": None}}
                for p in curve["curve"]
            ]
        }
        svg = render_curve_svg(stripped)
        counterparty_line = svg.split('stroke="#1a7f4b"')[1].split("/>")[0]
        assert "points=\"\"" in counterparty_line or counterparty_line.count(",") == 0


class TestSelfContained:
    def test_it_is_inline_svg_with_no_external_asset(self, curve):
        # `xmlns="http://www.w3.org/2000/svg"` is a namespace identifier, never fetched, so
        # the check has to be for things that actually load: sources, links, references.
        svg = render_curve_svg(curve)
        assert svg.startswith("<svg")
        assert not re.search(r'\b(src|href|xlink:href)\s*=', svg)
        assert "url(" not in svg
        assert not re.search(r"<(img|image|script|link|use)\b", svg)

    def test_it_carries_an_accessible_label(self, curve):
        svg = render_curve_svg(curve)
        assert 'role="img"' in svg
        assert "aria-label" in svg

    def test_it_scales_rather_than_fixing_a_pixel_width(self, curve):
        # The report is read at whatever width the reader has.
        assert 'width="100%"' in render_curve_svg(curve)
        assert "viewBox" in render_curve_svg(curve)


class TestReportIntegration:
    def test_the_report_embeds_the_chart(self):
        from evals.report import build_report

        report = build_report()
        assert "<svg" in report
        assert report.count("<polyline") == 3

    def test_the_chart_and_the_table_come_from_one_file(self, curve):
        # The coupling that makes drift impossible: every rate drawn is also a rate printed.
        from evals.report import build_report

        report = build_report()
        for point in curve["curve"]:
            assert f"{point['retrieved']['resolution_rate']:.1%}" in report
            assert f"{point['random_control']['resolution_rate']:.1%}" in report
