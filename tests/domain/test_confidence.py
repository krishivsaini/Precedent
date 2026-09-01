import pytest

from precedent.domain.confidence import (
    DEFAULT_AUTO_RESOLVE_THRESHOLD,
    calibration_bin_index,
    default_calibration_bins,
    meets_auto_resolve_threshold,
)


class TestMeetsAutoResolveThreshold:
    def test_above_threshold_passes(self):
        assert meets_auto_resolve_threshold(0.95, threshold=0.8) is True

    def test_exactly_at_threshold_passes(self):
        # route (spec §7): "confidence >= threshold -> auto_resolve" — inclusive.
        assert meets_auto_resolve_threshold(0.8, threshold=0.8) is True

    def test_below_threshold_fails(self):
        assert meets_auto_resolve_threshold(0.79, threshold=0.8) is False

    def test_uses_module_default_threshold_when_unspecified(self):
        assert meets_auto_resolve_threshold(DEFAULT_AUTO_RESOLVE_THRESHOLD) is True
        assert meets_auto_resolve_threshold(DEFAULT_AUTO_RESOLVE_THRESHOLD - 0.01) is False

    @pytest.mark.parametrize("bad_confidence", [-0.01, 1.01])
    def test_rejects_confidence_outside_unit_interval(self, bad_confidence):
        with pytest.raises(ValueError):
            meets_auto_resolve_threshold(bad_confidence, threshold=0.5)


class TestCalibrationBins:
    def test_default_bins_span_the_unit_interval_in_ten_equal_steps(self):
        bins = default_calibration_bins()
        assert bins[0] == 0.0
        assert bins[-1] == 1.0
        assert len(bins) == 11  # 10 bins => 11 edges

    def test_bin_index_for_low_confidence(self):
        bins = default_calibration_bins()
        assert calibration_bin_index(0.05, bins) == 0

    def test_bin_index_for_high_confidence(self):
        bins = default_calibration_bins()
        assert calibration_bin_index(0.95, bins) == 9

    def test_bin_index_at_a_shared_edge_goes_to_the_upper_bin(self):
        # 0.5 sits on the boundary between bin 4 ([0.4,0.5)) and bin 5 ([0.5,0.6)).
        bins = default_calibration_bins()
        assert calibration_bin_index(0.5, bins) == 5

    def test_bin_index_at_exactly_one_goes_to_the_last_bin(self):
        bins = default_calibration_bins()
        assert calibration_bin_index(1.0, bins) == 9

    @pytest.mark.parametrize("bad_confidence", [-0.01, 1.01])
    def test_rejects_confidence_outside_unit_interval(self, bad_confidence):
        with pytest.raises(ValueError):
            calibration_bin_index(bad_confidence, default_calibration_bins())
