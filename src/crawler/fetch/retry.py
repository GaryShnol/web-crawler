"""Given what happened to one attempt, decides when — or whether — a URL
gets tried again. Pure: `now` and `jitter` are parameters, never read or
drawn internally, so a caller gets exact, reproducible bounds without
seeding a global RNG or waiting on a clock.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from ..config import Config
from ..models import Outcome, parse_retry_after


@dataclass(frozen=True, slots=True)
class RetryAt:
    at: datetime


@dataclass(frozen=True, slots=True)
class GiveUp:
    """Either the outcome was PERMANENT_FAILURE, or `attempt_no` had already
    reached `config.max_attempts` — the caller already has both facts if it
    wants to say which.
    """


def next_attempt(
    outcome: Outcome,
    attempt_no: int,
    headers: dict[str, str],
    now: datetime,
    config: Config,
    jitter: float | None = None,
) -> GiveUp | RetryAt:
    """`outcome` must be PERMANENT_FAILURE or TEMPORARY_FAILURE — SUCCESS and
    NOT_MODIFIED never need a next attempt, so asking for one here is a
    caller bug, not a case this handles quietly.

    `attempt_no` counts attempts already made: the first failure calls this
    with `attempt_no=1`.

    A `Retry-After` on the response overrides the backoff formula entirely —
    the service naming the exact time beats a guess, and every worker is
    already held on that same header via the shared rate limiter, so
    honouring it here doesn't concentrate load anywhere.

    Without one, the delay is full jitter over
    `min(config.retry_max_seconds, config.retry_base_seconds * 2 ** (attempt_no - 1))`.
    `jitter` is the pre-drawn fraction in [0, 1] to scale that by — pass 0 or
    1 in a test for the exact bounds. Leaving it `None` applies no
    reduction (the full capped delay), since this function never draws its
    own randomness.
    """
    assert outcome in (Outcome.PERMANENT_FAILURE, Outcome.TEMPORARY_FAILURE)

    if outcome is Outcome.PERMANENT_FAILURE:
        return GiveUp()

    if attempt_no >= config.max_attempts:
        return GiveUp()

    retry_after = parse_retry_after(headers, now)
    if retry_after is not None:
        return RetryAt(now + timedelta(seconds=retry_after))

    max_delay = min(config.retry_max_seconds, config.retry_base_seconds * 2 ** (attempt_no - 1))
    fraction = 1.0 if jitter is None else jitter
    return RetryAt(now + timedelta(seconds=fraction * max_delay))
