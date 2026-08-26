"""One shared token bucket, AIMD-paced. There's one fetch gateway behind
every worker, so there's one bucket: a per-worker hold would just let the
other workers keep hitting a gateway that's already complaining.
"""

import asyncio
from collections.abc import Awaitable, Callable
from time import monotonic

from ..config import Config


class RateLimiter:
    """Async-safe. `acquire()` blocks until pacing allows one fetch;
    `report()` feeds back what happened after it — never the reverse, since
    a caller asking permission hasn't fetched yet and has no outcome to
    report. One instance, shared by every worker.

    `now`/`sleep` default to the real clock; a caller supplies its own to
    control time deterministically — a production engine never needs to.
    """

    def __init__(
        self,
        config: Config,
        now: Callable[[], float] = monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._config = config
        self._now = now
        self._sleep = sleep
        self._rate = config.requests_per_second
        self._tokens = self._capacity()
        self._last_refill = self._now()
        self._blocked_until = 0.0
        self._success_streak = 0
        self._lock = asyncio.Lock()

    @property
    def current_rate(self) -> float:
        """The AIMD ceiling in effect right now — what `acquire()` is
        currently pacing to, not necessarily what's actually happening if
        there's little work queued.
        """
        return self._rate

    def _capacity(self) -> float:
        """Burst allowance: one second of the *current* rate, floor 1. If
        this didn't shrink with the rate, a multiplicative cut wouldn't bite
        until the leftover burst from before the 429 was spent — exactly the
        second that matters.
        """
        return max(self._rate, 1.0)

    def _refill(self, now: float) -> None:
        self._tokens = min(self._capacity(), self._tokens + (now - self._last_refill) * self._rate)
        self._last_refill = now

    async def acquire(self) -> None:
        while True:
            async with self._lock:
                now = self._now()
                self._refill(now)
                wait = max(self._blocked_until - now, 0.0)
                if wait == 0.0 and self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                if wait == 0.0:
                    wait = (1.0 - self._tokens) / self._rate
            await self._sleep(wait)

    def report(self, throttled: bool, retry_after: float | None = None) -> None:
        """No `await` in this function, on purpose: asyncio never preempts a
        sync function mid-body, so it can mutate the shared state safely
        without the lock `acquire()` needs around its own suspend points.

        `throttled` means a 429 — the gateway pushing back, not a target
        page 404ing or 500ing, which is a different kind of trouble that
        fetch/retry.py already slows down on its own. Only a 429 cuts the
        rate or breaks the recovery streak; everything else advances it, so
        one dead page can't hold the whole crawl at the floor.
        """
        if throttled:
            self._success_streak = 0
            if retry_after is not None:
                self._blocked_until = max(self._blocked_until, self._now() + retry_after)
            else:
                self._rate = max(
                    self._config.rate_limit_min_rps,
                    self._rate * self._config.rate_limit_decrease_factor,
                )
            return

        self._success_streak += 1
        if self._success_streak >= self._config.rate_limit_recovery_successes:
            self._rate = min(
                self._config.requests_per_second, self._rate + self._config.rate_limit_increase_rps
            )
            self._success_streak = 0
