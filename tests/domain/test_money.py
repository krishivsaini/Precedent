from decimal import Decimal

import pytest

from precedent.domain.money import (
    ROUNDING_TOLERANCE_PAISE,
    apply_rate_paise,
    compute_tds_paise,
    gross_before_tds_paise,
    net_of_tds_paise,
    rupees_to_paise,
    within_tolerance,
)


class TestRupeesToPaise:
    def test_converts_whole_rupees(self):
        assert rupees_to_paise("100") == 10_000

    def test_converts_fractional_rupees(self):
        assert rupees_to_paise("99.99") == 9_999

    def test_accepts_decimal_input(self):
        assert rupees_to_paise(Decimal("1.23")) == 123

    def test_accepts_int_input(self):
        assert rupees_to_paise(5) == 500

    def test_rejects_float_input(self):
        # NFR-1: floats are never used for money, anywhere. Catch it at the boundary
        # rather than let a float silently propagate into a paise column.
        with pytest.raises(TypeError):
            rupees_to_paise(1.1)

    def test_rejects_sub_paise_precision(self):
        with pytest.raises(ValueError):
            rupees_to_paise("1.005")


class TestWithinTolerance:
    def test_identical_amounts_are_within_tolerance(self):
        assert within_tolerance(10_000, 10_000) is True

    def test_exactly_at_the_tolerance_edge_is_inclusive(self):
        assert within_tolerance(10_000, 10_000 + ROUNDING_TOLERANCE_PAISE) is True

    def test_one_paise_beyond_the_edge_fails(self):
        assert within_tolerance(10_000, 10_000 + ROUNDING_TOLERANCE_PAISE + 1) is False

    def test_direction_does_not_matter(self):
        assert within_tolerance(10_100, 10_000) is True

    def test_custom_tolerance_is_respected(self):
        assert within_tolerance(10_000, 10_050, tolerance_paise=50) is True
        assert within_tolerance(10_000, 10_051, tolerance_paise=50) is False

    def test_rejects_negative_tolerance(self):
        with pytest.raises(ValueError):
            within_tolerance(10_000, 10_000, tolerance_paise=-1)


class TestComputeTdsPaise:
    def test_two_percent_exact(self):
        assert compute_tds_paise(5_000, Decimal("0.02")) == 100

    def test_ten_percent_exact(self):
        assert compute_tds_paise(5_000, Decimal("0.10")) == 500

    def test_rounds_half_up_at_the_half_paise_boundary(self):
        # 25 paise * 2% = 0.5 paise exactly -> rounds up to 1
        assert compute_tds_paise(25, Decimal("0.02")) == 1

    def test_rounds_down_below_the_half_paise_boundary(self):
        # 15 paise * 2% = 0.3 paise -> rounds down to 0
        assert compute_tds_paise(15, Decimal("0.02")) == 0

    def test_zero_gross_yields_zero_tds(self):
        assert compute_tds_paise(0, Decimal("0.10")) == 0

    def test_rejects_negative_gross(self):
        with pytest.raises(ValueError):
            compute_tds_paise(-100, Decimal("0.02"))

    def test_rejects_negative_rate(self):
        with pytest.raises(ValueError):
            compute_tds_paise(100, Decimal("-0.02"))


class TestApplyRatePaise:
    def test_applies_a_decimal_rate(self):
        assert apply_rate_paise(10_000, Decimal("0.0236")) == 236

    def test_rounds_half_up(self):
        # 25 * 0.02 = 0.5 exactly -> rounds up
        assert apply_rate_paise(25, Decimal("0.02")) == 1

    def test_rejects_a_float_rate(self):
        # NFR-1's choke point: a float rate applied to money is exactly the leak this
        # function exists to stop, so it refuses rather than silently coercing.
        with pytest.raises(TypeError):
            apply_rate_paise(10_000, 0.0236)

    def test_rejects_a_negative_amount(self):
        with pytest.raises(ValueError):
            apply_rate_paise(-1, Decimal("0.02"))


class TestGrossBeforeTdsPaise:
    def test_reconstructs_the_invoice_from_a_short_payment(self):
        # 2% TDS on ₹1000 -> ₹980 received; from ₹980 we should recover ₹1000.
        assert gross_before_tds_paise(98_000, Decimal("0.02")) == 100_000

    def test_round_trips_with_net_of_tds_within_a_paise(self):
        for net in (98_000, 45_600, 12_345, 1):
            for rate in (Decimal("0.02"), Decimal("0.10")):
                gross = gross_before_tds_paise(net, rate)
                assert abs(net_of_tds_paise(gross, rate) - net) <= 1

    def test_rejects_a_float_rate(self):
        with pytest.raises(TypeError):
            gross_before_tds_paise(98_000, 0.02)

    def test_rejects_a_rate_of_one_or_more(self):
        with pytest.raises(ValueError):
            gross_before_tds_paise(98_000, Decimal("1"))


class TestNetOfTdsPaise:
    def test_deducts_computed_tds_from_gross(self):
        # gross 5000, 2% TDS = 100 -> net 4900
        assert net_of_tds_paise(5_000, Decimal("0.02")) == 4_900

    def test_net_plus_tds_reconstructs_gross_within_rounding(self):
        gross = 5_000
        rate = Decimal("0.10")
        net = net_of_tds_paise(gross, rate)
        tds = compute_tds_paise(gross, rate)
        assert net + tds == gross
