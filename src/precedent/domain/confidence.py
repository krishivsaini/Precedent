"""Confidence thresholds and calibration bins.

`DEFAULT_AUTO_RESOLVE_THRESHOLD` was a placeholder 0.8 through Rings 0-3 and is now set from
measured outcomes (`evals/calibration.py`, Ring 4).
"""

#: Set from the threshold sweep in `evals/results/calibration-*.json`, not chosen.
#:
#: Against the 0.8 placeholder, on the graph's grounded arm over 134 pool exceptions, 0.90
#: gives up **0.75 percentage points** of resolution rate — one case — and removes
#: **INR 23,739** of false-resolution exposure, a 19% cut in the number spec §6 calls "the
#: number that gets someone fired". 0.85 is free but removes only INR 5,107; past 0.90 the
#: trade turns sharply (0.92 costs 6pp for INR 11k more).
#:
#: Preferring the priced cutoff over the free one is a judgement about this domain rather
#: than something the numbers settle: in reconciliation a false accept is the failure that
#: matters and a point of coverage is cheap against it.
#:
#: **This number is not portable.** The agent is badly miscalibrated and non-monotonically so
#: — it is right 69.6% of the time when it says 0.90 and only 39.4% when it says 0.95, so its
#: most confident answers are among its least reliable. A cutoff fitted to that shape is
#: evidence about this model and this prompt and nothing else. Re-derive it when either
#: changes; `evals/calibration.py` does so from committed results without a new run.
DEFAULT_AUTO_RESOLVE_THRESHOLD = 0.90

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
