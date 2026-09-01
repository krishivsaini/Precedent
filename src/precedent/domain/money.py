"""Integer-paise money arithmetic.

Money is an integer number of paise everywhere in this codebase, per NFR-1. Nothing in
this module accepts or returns a float — `rupees_to_paise` raises `TypeError` on one
rather than silently truncating it, because a float that slips past this boundary is a
bug that will otherwise surface as an unexplained few-paise reconciliation mismatch much
later, and much harder to trace.
"""

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

PAISE_PER_RUPEE = 100

# Default matcher tolerance for fee/tax rounding deltas (spec §5: "Fee/tax rounding
# delta ±₹1").
ROUNDING_TOLERANCE_PAISE = 100


def rupees_to_paise(amount: str | int | Decimal) -> int:
    """Convert a rupee amount to integer paise.

    Accepts str, int, or Decimal — never float. Raises ValueError if the amount carries
    sub-paise precision (more than 2 decimal places), since that precision cannot be
    represented and silently truncating it would be a silent data-loss bug.
    """
    if isinstance(amount, float):
        raise TypeError(
            "rupees_to_paise() does not accept float — money is integer paise. "
            "Pass a str, int, or Decimal instead."
        )
    try:
        decimal_amount = Decimal(amount) if not isinstance(amount, Decimal) else amount
    except InvalidOperation as exc:
        raise ValueError(f"Not a valid decimal rupee amount: {amount!r}") from exc

    paise = decimal_amount * PAISE_PER_RUPEE
    if paise != paise.to_integral_value():
        raise ValueError(
            f"Amount {amount!r} has sub-paise precision and cannot be represented exactly."
        )
    return int(paise)


def within_tolerance(
    a_paise: int, b_paise: int, tolerance_paise: int = ROUNDING_TOLERANCE_PAISE
) -> bool:
    """Whether two paise amounts are within `tolerance_paise` of each other, inclusive.

    Used for fee/tax rounding-delta matching (spec §5). The edge itself counts as a
    match — a boundary that excludes the edge would make the configured tolerance a lie.
    """
    if tolerance_paise < 0:
        raise ValueError("tolerance_paise must be non-negative")
    return abs(a_paise - b_paise) <= tolerance_paise


def apply_rate_paise(amount_paise: int, rate: Decimal) -> int:
    """Apply a fractional `rate` to a paise amount, rounding half up to the nearest paise.

    The single place any percentage touches money — PSP fees, GST on those fees, TDS. It
    takes `Decimal` and refuses `float` so NFR-1 holds at the one choke point every rate
    calculation has to pass through, rather than relying on each caller to remember.
    """
    if isinstance(rate, float):
        raise TypeError(
            "apply_rate_paise() does not accept a float rate — money arithmetic is "
            "Decimal-only. Pass a Decimal (e.g. Decimal('0.02'))."
        )
    if amount_paise < 0:
        raise ValueError("amount_paise must be non-negative")
    if rate < 0:
        raise ValueError("rate must be non-negative")
    return int((Decimal(amount_paise) * rate).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def compute_tds_paise(gross_paise: int, rate: Decimal) -> int:
    """TDS amount, in paise, deducted from a gross paise amount at `rate`.

    `rate` is a fraction (e.g. `Decimal("0.02")` for 2%, `Decimal("0.10")` for 10%),
    matching the TDS short-payment exception class in spec §5.
    """
    return apply_rate_paise(gross_paise, rate)


def net_of_tds_paise(gross_paise: int, rate: Decimal) -> int:
    """Gross paise amount minus the TDS computed on it at `rate`."""
    return gross_paise - compute_tds_paise(gross_paise, rate)


def gross_before_tds_paise(net_paise: int, rate: Decimal) -> int:
    """Reconstruct the pre-TDS invoice value from what was actually received.

    The inverse of `net_of_tds_paise`: given a payment short by `rate`, what was the
    invoice? Used to build TDS short-payment scenarios around a *real* payment, where the
    received amount is fixed and the invoice has to be derived from it. Rounding means
    this is not an exact inverse for every input — `net_of_tds_paise(gross_before_tds_paise(n))`
    can land a paise away from `n`, which is precisely the kind of drift that would be
    invisible in float arithmetic.
    """
    if isinstance(rate, float):
        raise TypeError(
            "gross_before_tds_paise() does not accept a float rate — money arithmetic is "
            "Decimal-only. Pass a Decimal (e.g. Decimal('0.02'))."
        )
    if net_paise < 0:
        raise ValueError("net_paise must be non-negative")
    if not (0 <= rate < 1):
        raise ValueError("rate must be within [0, 1) to reconstruct a gross amount")
    gross = Decimal(net_paise) / (Decimal(1) - rate)
    return int(gross.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
