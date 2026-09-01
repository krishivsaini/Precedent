"""A client-side token-rate throttle.

Groq's free tier allows 1000 requests per day but only **8000 tokens per minute**, and the
ablation's grounded prompts run about 2500 tokens each. The daily budget is comfortable; the
per-minute one is the binding constraint, and firing requests concurrently at it produces
nothing but 429s.

Retrying is the wrong tool for a limit you can predict. Backoff reacts *after* the provider
refuses, wasting a round trip each time and — with several workers — desynchronising into a
thundering herd that re-triggers the same limit. Spending the tokens at the rate the budget
allows means the refusal never happens.

Deliberately conservative in two ways, because being wrong in the other direction costs a
whole run: it charges the *estimate* up front and reconciles afterwards, and it works against
a fraction of the stated limit rather than the whole of it.
"""

import threading
import time

WINDOW_SECONDS = 60.0

#: Fraction of the provider's stated limit to actually use. The provider's accounting and
#: ours will not agree exactly — its window boundaries are its own, and token counts are only
#: known after a call — so aiming at 100% guarantees periodic overshoot.
DEFAULT_SAFETY_MARGIN = 0.85


class TokenRateThrottle:
    """Keeps token spend within `tokens_per_minute` over a rolling window.

    Thread-safe: the ablation runs several workers against one client, and an unsynchronised
    throttle would let them each independently conclude there was room.
    """

    def __init__(
        self,
        tokens_per_minute: int,
        safety_margin: float = DEFAULT_SAFETY_MARGIN,
        sleep=time.sleep,
        now=time.monotonic,
    ):
        if tokens_per_minute <= 0:
            raise ValueError("tokens_per_minute must be positive")
        self.budget = max(1, int(tokens_per_minute * safety_margin))
        self._sleep = sleep
        self._now = now
        self._lock = threading.Lock()
        self._spent: list[tuple[float, int]] = []  # (timestamp, tokens)

    def _prune(self, now: float) -> None:
        cutoff = now - WINDOW_SECONDS
        self._spent = [entry for entry in self._spent if entry[0] > cutoff]

    def _used(self, now: float) -> int:
        self._prune(now)
        return sum(tokens for _, tokens in self._spent)

    def acquire(self, estimated_tokens: int) -> None:
        """Block until `estimated_tokens` fit in the window, then charge them.

        Charging before the call rather than after is what makes concurrent workers safe: a
        throttle that only recorded actual usage would let every worker pass simultaneously
        while the window still looked empty.
        """
        estimated_tokens = max(0, estimated_tokens)
        while True:
            with self._lock:
                now = self._now()
                used = self._used(now)
                if used + estimated_tokens <= self.budget or not self._spent:
                    # The `not self._spent` clause admits a single request larger than the
                    # whole budget. It will be refused by the provider if genuinely too big,
                    # which is a real answer — waiting forever for room that can never exist
                    # is not.
                    self._spent.append((now, estimated_tokens))
                    return
                oldest = self._spent[0][0]
                wait = max(0.05, oldest + WINDOW_SECONDS - now)
            self._sleep(wait)

    def reconcile(self, estimated_tokens: int, actual_tokens: int) -> None:
        """Correct the most recent charge once the real usage is known."""
        delta = actual_tokens - max(0, estimated_tokens)
        if not delta:
            return
        with self._lock:
            if self._spent:
                timestamp, tokens = self._spent[-1]
                self._spent[-1] = (timestamp, max(0, tokens + delta))


def estimate_tokens(system: str, user: str, expected_output: int = 600) -> int:
    """Rough token count for a request, before the provider tells us the real one.

    Four characters per token is the usual English approximation. Precision is not the point
    — the throttle reconciles against actual usage after every call, so this only has to be
    close enough to pace the first request of a window.
    """
    return (len(system) + len(user)) // 4 + expected_output
