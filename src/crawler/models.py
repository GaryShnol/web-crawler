"""The fetch API's response shape, and what we track ourselves about a fetch attempt."""

import enum
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .errors import ErrorKind  # errors.py imports models.py; this side stays type-only


class Outcome(enum.Enum):
    """Closed set of fetch outcomes. errors.py maps status codes onto these; nothing else should."""

    SUCCESS = "success"
    NOT_MODIFIED = "not_modified"
    PERMANENT_FAILURE = "permanent_failure"
    TEMPORARY_FAILURE = "temporary_failure"


def find_header(headers: dict[str, str], name: str) -> str | None:
    """Case-insensitive header lookup — the API doesn't guarantee casing.
    The one place this loop is written. Takes a plain dict rather than a
    `FetchResponse`, because fetch/retry.py's `next_attempt` only ever has
    the raw headers (see DESIGN.md), not a `FetchResponse` to call a method
    on — `errors.py` and `fetch/client.py` call this the same way, on
    `response.headers`, rather than a `FetchResponse.header()` wrapper that
    would just be this loop reachable through a second name.
    """
    target = name.lower()
    for key, value in headers.items():
        if key.lower() == target:
            return value
    return None


def parse_retry_after(headers: dict[str, str], now: datetime) -> float | None:
    """Retry-After is delta-seconds ("120") or an HTTP-date ("Wed, 21 Oct
    2015 07:28:00 GMT") per RFC 7231. Both fetch/retry.py (this URL's next
    attempt) and whoever feeds fetch/rate_limiter.py (the shared pacing
    hold) need this, so it's parsed once, here, next to the other header
    reading this module already does — two parsers for one header is two
    bugs.

    `now` is a parameter, never read internally, so nothing that calls this
    for a "pure" result (fetch/retry.py's next_attempt) is lying about it.
    Returns seconds from `now`, never negative — an HTTP-date already in the
    past means "retry immediately," not "retry before now."
    """
    value = find_header(headers, "Retry-After")
    if value is None:
        return None

    try:
        return max(float(value), 0.0)
    except ValueError:
        pass

    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max((when - now).total_seconds(), 0.0)


@dataclass(frozen=True, slots=True)
class FetchResponse:
    """Mirrors the fetch API's response body exactly: three fields, nothing else.

    The API's own field is `statusCode`; the camelCase-to-snake_case translation
    happens in fetch/client.py when this is built, not here.
    """

    status_code: int
    headers: dict[str, str]
    body: bytes | None


@dataclass(frozen=True, slots=True)
class FetchResult:
    """What we measure ourselves about one fetch attempt, kept off FetchResponse.

    No `attempt` here — the client doesn't know which attempt this is, the
    worker does, off the row it claimed. A field only the caller can fill in
    correctly doesn't belong on the callee's return type.

    `error_kind` is `errors.classify*`'s own verdict, carried through rather
    than dropped: fetch/client.py already computes it to decide `outcome`,
    and it's the one thing a caller recording a failure (store/frontier.py's
    mark_failed, which requires it) can't safely re-derive — a `response`-less
    result (timeout, oversized body) has nothing left to reclassify from.
    `error_detail` is the same carry-through for `Classification.detail`
    (see DESIGN.md) — the specifics (declared vs. actual bytes, the cap
    that tripped, connect vs. read) live only where they were computed.
    """

    outcome: Outcome
    elapsed: float
    resolved_url: str | None
    response: FetchResponse | None
    error_kind: "ErrorKind | None" = None
    error_detail: str | None = None
