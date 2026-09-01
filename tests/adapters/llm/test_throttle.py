"""The token-rate throttle. Time is injected, so nothing here actually sleeps."""

import threading

import pytest

from precedent.adapters.llm.throttle import (
    WINDOW_SECONDS,
    TokenRateThrottle,
    estimate_tokens,
)


class FakeClock:
    """A monotonic clock that only advances when something sleeps on it."""

    def __init__(self):
        self.t = 1000.0
        self.slept: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds


@pytest.fixture
def clock():
    return FakeClock()


def build(clock, tokens_per_minute=1000, margin=1.0):
    return TokenRateThrottle(
        tokens_per_minute, safety_margin=margin, sleep=clock.sleep, now=clock.now
    )


class TestBudget:
    def test_applies_the_safety_margin_to_the_stated_limit(self):
        # Aiming at 100% of a limit whose window boundaries are the provider's, not ours,
        # guarantees periodic overshoot.
        assert TokenRateThrottle(8000, safety_margin=0.85).budget == 6800

    def test_rejects_a_non_positive_limit(self):
        with pytest.raises(ValueError):
            TokenRateThrottle(0)


class TestPacing:
    def test_requests_within_budget_never_wait(self, clock):
        throttle = build(clock, 1000)
        for _ in range(4):
            throttle.acquire(250)
        assert clock.slept == []

    def test_waits_once_the_window_is_full(self, clock):
        throttle = build(clock, 1000)
        throttle.acquire(600)
        throttle.acquire(400)
        throttle.acquire(300)  # would exceed 1000
        assert clock.slept, "expected the throttle to pace the third request"

    def test_the_window_rolls_forward(self, clock):
        throttle = build(clock, 1000)
        throttle.acquire(1000)
        clock.sleep(WINDOW_SECONDS + 1)
        clock.slept.clear()
        throttle.acquire(1000)
        assert clock.slept == [], "spend older than the window must not count"

    def test_a_request_larger_than_the_whole_budget_still_proceeds(self, clock):
        # Otherwise it waits forever for room that can never exist. Let the provider be the
        # one to refuse it — that is at least a real answer.
        throttle = build(clock, 100)
        throttle.acquire(5000)
        assert clock.slept == []

    def test_charges_before_the_call_not_after(self, clock):
        # A throttle that only recorded actual usage would let every concurrent worker pass
        # at once while the window still looked empty.
        throttle = build(clock, 1000)
        throttle.acquire(900)
        throttle.acquire(200)
        assert clock.slept, "the second request should have been paced by the first's charge"


class TestReconcile:
    def test_corrects_an_underestimate(self, clock):
        throttle = build(clock, 1000)
        throttle.acquire(100)
        throttle.reconcile(100, 900)
        throttle.acquire(200)  # window now holds 900, so this must wait
        assert clock.slept

    def test_corrects_an_overestimate(self, clock):
        throttle = build(clock, 1000)
        throttle.acquire(900)
        throttle.reconcile(900, 100)
        throttle.acquire(500)  # window now holds 100, so this must not wait
        assert clock.slept == []

    def test_never_drives_a_charge_negative(self, clock):
        throttle = build(clock, 1000)
        throttle.acquire(100)
        throttle.reconcile(100, 0)
        throttle.acquire(1000)
        assert clock.slept == []


class TestThreadSafety:
    def test_concurrent_acquires_are_serialised_without_corruption(self):
        # Real threads, against a budget large enough that nothing ever needs to wait: this
        # exercises the lock, not the pacing. Sizing the budget small here would make the
        # test spin — a no-op `sleep` paired with a real clock busy-waits for the window to
        # roll rather than advancing it.
        throttle = TokenRateThrottle(100_000, safety_margin=1.0, sleep=lambda s: None)
        errors = []

        def worker():
            try:
                for _ in range(20):
                    throttle.acquire(50)
            except Exception as error:  # pragma: no cover
                errors.append(error)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert not errors
        # Every charge landed exactly once: 8 workers x 20 acquires x 50 tokens.
        assert sum(tokens for _, tokens in throttle._spent) == 8 * 20 * 50


class TestEstimateTokens:
    def test_scales_with_prompt_length(self):
        assert estimate_tokens("a" * 400, "", expected_output=0) == 100

    def test_includes_an_allowance_for_the_response(self):
        assert estimate_tokens("", "", expected_output=600) == 600
