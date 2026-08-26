"""Tests for fetch/rate_limiter.py. No real sleeping: RateLimiter takes
`now`/`sleep` as constructor arguments (production leaves them at their real
defaults), so a test injects its own fake clock instead of waiting.
"""

import asyncio

import pytest

from crawler.config import Config
from crawler.fetch.rate_limiter import RateLimiter


def _config(**overrides) -> Config:
    return Config(
        seed_url="http://fixture.local/",
        database_url="postgresql://unused/unused",
        fetch_api_url="http://fixture.local/fetch",
        **overrides,
    )


class _FakeClock:
    """`sleep` never really waits — it advances `now` instantly and yields
    once (a real 0-second sleep), so concurrent `acquire()` calls still
    interleave the way they would under a real clock.
    """

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds
        await asyncio.sleep(0)


def _limiter(config: Config, start: float = 0.0) -> tuple[RateLimiter, _FakeClock]:
    clock = _FakeClock(start)
    return RateLimiter(config, now=clock.monotonic, sleep=clock.sleep), clock


class TestBurstAndPacing:
    async def test_first_burst_up_to_capacity_returns_without_waiting(self):
        limiter, clock = _limiter(_config(requests_per_second=3.0))

        for _ in range(3):
            await limiter.acquire()

        assert clock.now == 0.0

    async def test_acquire_beyond_capacity_waits_for_the_next_token(self):
        limiter, clock = _limiter(_config(requests_per_second=2.0))

        await limiter.acquire()
        await limiter.acquire()  # burst of 2 spent
        await limiter.acquire()  # needs a fresh token: 1 / 2.0s

        assert clock.now == pytest.approx(0.5)


class TestRetryAfterHold:
    async def test_hold_delays_the_next_acquire_until_the_deadline(self):
        limiter, clock = _limiter(_config(requests_per_second=100.0))

        limiter.report(throttled=True, retry_after=5.0)
        await limiter.acquire()

        assert clock.now == pytest.approx(5.0)

    async def test_hold_applies_globally_not_per_caller(self):
        # Tokens are never the bottleneck here (rps=100) — only the hold is.
        # If the hold were per-caller instead of one shared deadline, two
        # concurrent callers would land at 6.0 (each waiting its own 3.0),
        # not both landing at 3.0.
        limiter, clock = _limiter(_config(requests_per_second=100.0))

        limiter.report(throttled=True, retry_after=3.0)
        await asyncio.gather(limiter.acquire(), limiter.acquire())

        assert clock.now == pytest.approx(3.0)


class TestMultiplicativeDecrease:
    async def test_bare_429_cuts_the_rate(self):
        config = _config(requests_per_second=4.0, rate_limit_decrease_factor=0.5)
        limiter, clock = _limiter(config)

        limiter.report(throttled=True, retry_after=None)  # rate: 4.0 -> 2.0
        await limiter.acquire()  # 1 of the (now smaller) burst
        await limiter.acquire()  # 2nd of the burst
        await limiter.acquire()  # a 3rd token only exists at the old rate of 4.0

        assert clock.now > 0.0

    async def test_capacity_shrinks_with_the_cut_rate(self):
        # The full burst (4 tokens) is sitting there unspent when the cut
        # happens — if the cap on held tokens didn't shrink with it, the
        # cut wouldn't bite until that stale burst was drained.
        config = _config(requests_per_second=4.0, rate_limit_decrease_factor=0.5)
        limiter, clock = _limiter(config)

        limiter.report(throttled=True, retry_after=None)  # rate: 4.0 -> 2.0, before any acquire

        await limiter.acquire()
        await limiter.acquire()
        await limiter.acquire()  # a 3rd token exists only if the stale 4-token burst survived

        assert clock.now > 0.0

    async def test_decrease_never_goes_below_the_floor(self):
        config = _config(
            requests_per_second=8.0, rate_limit_decrease_factor=0.1, rate_limit_min_rps=1.0
        )
        limiter, clock = _limiter(config)

        for _ in range(10):
            limiter.report(throttled=True, retry_after=None)  # would asymptote toward 0 unfloored

        await limiter.acquire()  # drains the floor-sized burst (1 token)
        await limiter.acquire()  # next token needs exactly 1 / floor = 1.0s

        assert clock.now == pytest.approx(1.0)


class TestAdditiveIncrease:
    async def test_recovery_climbs_after_the_configured_streak(self):
        config = _config(
            requests_per_second=4.0,
            rate_limit_decrease_factor=0.5,
            rate_limit_recovery_successes=2,
            rate_limit_increase_rps=1.0,
        )
        limiter, clock = _limiter(config)

        limiter.report(throttled=True, retry_after=None)  # rate: 4.0 -> 2.0
        limiter.report(throttled=False)
        limiter.report(throttled=False)  # streak of 2 -> rate: 2.0 -> 3.0

        await limiter.acquire()
        await limiter.acquire()
        await limiter.acquire()  # a 3rd token exists only once the rate climbed back to 3.0

        assert clock.now == 0.0

    async def test_a_429_resets_the_streak(self):
        config = _config(
            requests_per_second=4.0,
            rate_limit_decrease_factor=0.5,
            rate_limit_recovery_successes=2,
            rate_limit_increase_rps=1.0,
        )
        limiter, clock = _limiter(config)

        limiter.report(throttled=True, retry_after=None)  # rate: 4.0 -> 2.0
        limiter.report(throttled=False)  # 1 of 2 toward recovery
        limiter.report(throttled=True, retry_after=None)  # resets the streak; rate: 2.0 -> 1.0
        limiter.report(throttled=False)  # only 1 of 2 again — recovery hasn't fired

        await limiter.acquire()  # the one token the rate of 1.0 makes available
        assert clock.now == 0.0

        # if the streak had wrongly survived the second 429, 2 successes would
        # have fired recovery and this token would already be sitting there
        await limiter.acquire()
        assert clock.now == pytest.approx(1.0)  # 1 / 1.0 — the rate is still 1.0, not higher

    async def test_recovery_never_exceeds_the_ceiling(self):
        config = _config(
            requests_per_second=2.0,
            rate_limit_decrease_factor=0.5,
            rate_limit_recovery_successes=1,
            rate_limit_increase_rps=10.0,  # deliberately oversized step
        )
        limiter, clock = _limiter(config)

        limiter.report(throttled=True, retry_after=None)  # rate: 2.0 -> 1.0
        limiter.report(throttled=False)  # would overshoot to 11.0 if uncapped

        await limiter.acquire()
        await limiter.acquire()
        await limiter.acquire()  # a 3rd token exists only if the ceiling of 2.0 was exceeded

        assert clock.now > 0.0


class TestNonThrottledFailuresAreNeutral:
    async def test_a_non_throttled_report_has_no_extra_arguments(self):
        # throttled=False is what the caller reports for a 404/500/timeout
        # too, not just a real success — the limiter can't tell the
        # difference by design (see report()'s docstring), so this is
        # already covered by the additive-increase tests above; this test
        # just pins that a bare `report(throttled=False)` (no other args)
        # is the whole call shape a non-429 result reports.
        config = _config(rate_limit_recovery_successes=1, rate_limit_increase_rps=0.0)
        limiter, _clock = _limiter(config)

        limiter.report(throttled=False)  # must not raise for the "success" shape


class TestDefaultClock:
    async def test_defaults_to_the_real_clock_without_injection(self):
        limiter = RateLimiter(_config(requests_per_second=1000.0))
        await limiter.acquire()  # tokens are plentiful; must not hang
