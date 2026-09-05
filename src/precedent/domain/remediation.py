"""The remediation ceiling and its stopping rule (spec §9 Ring 5).

Everything above this module produces *explanations*. This is the first thing in the
project that moves money, and the difference is not one of degree: a wrong explanation is
corrected by the next reviewer, a wrong refund is a payment to someone who was not owed
one. The ceiling exists because "the agent decided to" is not an acceptable answer to
"why did fourteen refunds go out last night".

Three limits, because one is not enough:

* **`max_refunds`** — a count. Catches a loop that fires the same small refund repeatedly.
* **`max_total_paise`** — a sum. Catches many refunds each individually reasonable.
* **`max_single_paise`** — a per-call cap. Catches one refund with a misplaced decimal,
  which neither of the others would stop until after it had gone out.

A ceiling expressed only as a count would let ₹4,00,000 through in four calls; one
expressed only as a total would let a single ₹4,00,000 refund through as the first call.
The failure modes are different, so the limits are separate.

Nothing here does I/O. `CeilingUsage` is computed from storage by the caller, deliberately:
a ceiling tracked in a process-local counter resets when the process does, and a limit that
a restart clears is not a limit. Same discipline as the Ring 3 durable gate.
"""

import hashlib
from dataclasses import dataclass

#: Razorpay rejects a shorter key with HTTP 400 and `input_validation_failed` — measured
#: against the live test-mode API, not read off the docs. See `adapters/razorpay/refunds.py`
#: for the rest of that probe's findings.
MIN_IDEMPOTENCY_KEY_LENGTH = 10


@dataclass(frozen=True)
class RemediationCeiling:
    """What this agent is allowed to spend before a human has to widen the limit.

    The defaults are deliberately small. This is a hackathon system with one operator and
    no second approver, and the right posture for an autonomous refund budget under those
    conditions is one where the worst case is embarrassing rather than expensive.
    """

    max_refunds: int = 3
    max_total_paise: int = 500_00
    max_single_paise: int = 250_00

    def __post_init__(self) -> None:
        for name in ("max_refunds", "max_total_paise", "max_single_paise"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool):
                raise TypeError(f"{name} must be an int (money is integer paise), got {value!r}")
            if value < 0:
                raise ValueError(f"{name} must be non-negative, got {value}")
        if self.max_single_paise > self.max_total_paise:
            # Not a hard contradiction, but it means the per-call cap can never bind, which
            # is almost always a typo rather than an intention.
            raise ValueError(
                f"max_single_paise ({self.max_single_paise}) exceeds max_total_paise "
                f"({self.max_total_paise}); the per-call cap could never apply"
            )


@dataclass(frozen=True)
class CeilingUsage:
    """What has already been spent, read from storage.

    Counts everything **reserved**, not merely everything confirmed to have landed. A
    refund call that times out leaves the system unable to say whether money moved; the
    only safe accounting is to keep holding it against the ceiling. Erring here costs
    coverage, and erring the other way costs money.
    """

    refunds_made: int = 0
    total_paise: int = 0


@dataclass(frozen=True)
class CeilingDecision:
    allowed: bool
    reason: str
    remaining_refunds: int
    remaining_paise: int


def check_ceiling(
    ceiling: RemediationCeiling, usage: CeilingUsage, amount_paise: int
) -> CeilingDecision:
    """Whether one more refund of `amount_paise` is permitted.

    Returns a decision rather than raising, because a refusal is a normal outcome that the
    gate has to *display* — "3 of 3 refunds used, ₹120.00 of ₹500.00 remaining" is the most
    useful thing the screen can say, and an exception carries none of it.
    """
    if not isinstance(amount_paise, int) or isinstance(amount_paise, bool):
        raise TypeError(f"amount_paise must be an int, got {amount_paise!r}")
    if amount_paise <= 0:
        raise ValueError(f"amount_paise must be positive, got {amount_paise}")

    remaining_refunds = max(0, ceiling.max_refunds - usage.refunds_made)
    remaining_paise = max(0, ceiling.max_total_paise - usage.total_paise)

    def refuse(reason: str) -> CeilingDecision:
        return CeilingDecision(False, reason, remaining_refunds, remaining_paise)

    # Checked in this order so the message names the limit an operator can act on first.
    if usage.refunds_made >= ceiling.max_refunds:
        return refuse(
            f"refund ceiling reached: {usage.refunds_made} of {ceiling.max_refunds} used"
        )
    if amount_paise > ceiling.max_single_paise:
        return refuse(
            f"single-refund cap exceeded: {amount_paise} paise requested, "
            f"cap is {ceiling.max_single_paise}"
        )
    if amount_paise > remaining_paise:
        return refuse(
            f"total ceiling would be exceeded: {amount_paise} paise requested, "
            f"{remaining_paise} remaining of {ceiling.max_total_paise}"
        )
    return CeilingDecision(
        True,
        f"within ceiling: {remaining_refunds - 1} refunds and "
        f"{remaining_paise - amount_paise} paise would remain",
        remaining_refunds,
        remaining_paise,
    )


def refund_idempotency_key(resolution_id: str, payment_id: str, amount_paise: int) -> str:
    """A key that is the same for the same intent and different for a different one.

    Derived rather than random, because the point of the key is to survive the case it
    exists for: a process that crashes after sending the request and retries with a *new*
    random key refunds twice. Everything that changes what would be sent goes into the
    digest, so a retry of the same intent reuses the key and a changed amount does not.

    The measured behaviour this is built against (live probe, test mode):
    same key + byte-identical body returns HTTP 200 and the original `rfnd_` id; same key +
    different body returns 409. So reusing the key is safe exactly when the request is
    unchanged, which is the condition this derivation enforces.
    """
    if amount_paise <= 0:
        raise ValueError("amount_paise must be positive")
    digest = hashlib.blake2b(
        f"{resolution_id}|{payment_id}|{amount_paise}".encode("utf-8"), digest_size=8
    ).hexdigest()
    key = f"rem-{digest}"
    if len(key) < MIN_IDEMPOTENCY_KEY_LENGTH:  # pragma: no cover - arithmetic makes it 20
        raise ValueError(f"derived key {key!r} is shorter than Razorpay's 10-char minimum")
    return key
