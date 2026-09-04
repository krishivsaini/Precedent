"""A simulated reviewer at the gate.

The first learning curve treated every pool resolution as **confirmed at the gold reason
code**. That made the curve an upper bound rather than an estimate, and it also meant the
`corrected` path — which spec §7 calls the *higher-value* deposit, because a correction
encodes a case the system got wrong — was never exercised end to end. Dead code in practice,
tested only in isolation.

This models a reviewer who actually looks:

* The agent proposes. If it is **right**, the reviewer confirms.
* If it is **wrong**, the reviewer corrects it to the gold answer. The correction deposits,
  at the corrected reason code.
* Some proportion of cases the reviewer **rejects** outright — the resolution is unusable,
  the evidence is too thin, they do not have time. A rejection deposits nothing, and every
  rejection is a precedent that never exists.
* Cases the agent **escalated** are reviewed and resolved by the human, which deposits: an
  escalation is the system asking for help, and the answer to it is worth recording.

**Why a rejection rate at all, and why it is a parameter.** No real operator confirms
everything, and a corpus that grows one precedent per case is the best case the mechanism can
possibly have. Making the rate explicit turns "this is an upper bound" from a caveat into a
number that can be varied and reported. `REALISTIC_REJECTION_RATE` is a stated assumption,
not a measurement — there is no data here about how often a reconciliation lead would decline
to sign something off, and pretending otherwise would be worse than admitting the guess.

Seeded, so a curve is reproducible: the same cases are rejected on every run.
"""

import random
from dataclasses import dataclass

#: The share of otherwise-depositable resolutions a reviewer declines to sign off. A stated
#: assumption rather than a measurement — see the module docstring. Varying it is the honest
#: way to show how sensitive the curve is to reviewer behaviour.
REALISTIC_REJECTION_RATE = 0.15

REVIEWER_SEED = 20260903


@dataclass(frozen=True)
class Review:
    """One reviewer decision at the gate."""

    human_action: str  # confirmed | corrected | rejected
    reason_code: str  # what gets deposited; gold for confirm and correct
    correction_note: str = ""

    @property
    def deposits(self) -> bool:
        return self.human_action in {"confirmed", "corrected"}


class SimulatedReviewer:
    """Decides confirm / correct / reject for each case, deterministically.

    Keyed on `scenario_id` rather than call order, so the same case gets the same decision
    regardless of how many workers ran or in what sequence — a reviewer whose verdicts moved
    with thread scheduling would make the curve irreproducible.
    """

    def __init__(
        self,
        rejection_rate: float = REALISTIC_REJECTION_RATE,
        seed: int = REVIEWER_SEED,
    ):
        if not 0.0 <= rejection_rate <= 1.0:
            raise ValueError("rejection_rate must be a probability")
        self.rejection_rate = rejection_rate
        self._seed = seed

    def _rejects(self, scenario_id: str) -> bool:
        # Hashing the id with the seed gives a stable per-case draw, independent of order.
        rng = random.Random(f"{self._seed}:{scenario_id}")
        return rng.random() < self.rejection_rate

    def review(self, scenario_id: str, gold_reason_code: str, proposed: str | None,
               escalated: bool) -> Review:
        if self._rejects(scenario_id):
            return Review("rejected", gold_reason_code, "reviewer declined to sign this off")

        if escalated or proposed is None:
            # The system asked for help. The answer to that question is worth recording —
            # arguably the most worth recording, since nothing in the corpus covered it.
            return Review(
                "corrected", gold_reason_code,
                "escalated to the reviewer, who resolved it",
            )

        if proposed == gold_reason_code:
            return Review("confirmed", gold_reason_code)

        return Review(
            "corrected", gold_reason_code,
            f"the agent proposed {proposed}; corrected to {gold_reason_code}",
        )
