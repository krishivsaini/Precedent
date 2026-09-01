"""Confidence thresholds and calibration bins.

These are placeholder constants for Ring 0-3 (spec §12, §9 Ring 0.1) — Ring 4 replaces
`DEFAULT_AUTO_RESOLVE_THRESHOLD` with a value set from the calibration curve rather than
a guess, and reports the precision/coverage tradeoff of that operating point.
"""

# Placeholder for Ring 0-3. Ring 4 sets this from the calibration curve (spec §9 Ring 4).
DEFAULT_AUTO_RESOLVE_THRESHOLD = 0.8

_DEFAULT_BIN_COUNT = 10


def _validate_unit_interval(value: float, name: str = "confidence") -> None:
    if not (0.0 <= value <= 1.0):
        raise ValueError(f"{name} must be within [0.0, 1.0], got {value!r}")


def meets_auto_resolve_threshold(
    confidence: float, threshold: float = DEFAULT_AUTO_RESOLVE_THRESHOLD
) -> bool:
    """Whether `confidence` clears the auto-resolve bar (route node, spec §7: >= threshold)."""
    _validate_unit_interval(confidence)
    _validate_unit_interval(threshold, name="threshold")
    return confidence >= threshold


def default_calibration_bins(bin_count: int = _DEFAULT_BIN_COUNT) -> list[float]:
    """Equal-width bin edges spanning [0.0, 1.0], `bin_count` bins wide.

    E.g. bin_count=10 -> [0.0, 0.1, 0.2, ..., 1.0] (11 edges, 10 bins), used to build the
    calibration curve: does a resolution proposed at ~0.8 confidence actually get
    confirmed ~80% of the time (spec §9 Ring 4)?
    """
    if bin_count < 1:
        raise ValueError("bin_count must be at least 1")
    return [i / bin_count for i in range(bin_count + 1)]


def calibration_bin_index(confidence: float, bin_edges: list[float]) -> int:
    """Which bin `confidence` falls into, given `bin_edges` from `default_calibration_bins`.

    Bins are half-open on the low side, [edge_i, edge_i+1), except the last bin which is
    closed on both ends so that confidence == 1.0 has a home. A confidence sitting exactly
    on a shared edge belongs to the upper bin.
    """
    _validate_unit_interval(confidence)
    bin_count = len(bin_edges) - 1
    for i in range(bin_count):
        lower, upper = bin_edges[i], bin_edges[i + 1]
        is_last_bin = i == bin_count - 1
        if lower <= confidence < upper or (is_last_bin and confidence == upper):
            return i
    raise ValueError(f"confidence {confidence!r} did not fall within any bin edge in {bin_edges!r}")
