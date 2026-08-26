"""Maps a fetch response onto the closed set of outcomes, once, so nothing
else in src/ reads a status code. No retries, no backoff, no timing — those
live in fetch/retry.py and fetch/client.py, driven by what classify() returns.
"""

import enum
from dataclasses import dataclass

from aiohttp import ClientConnectionError

from .models import FetchResponse, Outcome

_PERMANENT_STATUSES = {404, 403}
_TEMPORARY_STATUSES = {429, 500}


class ErrorKind(enum.Enum):
    """Why a failure happened. Unset on SUCCESS and NOT_MODIFIED."""

    NOT_FOUND = "not_found"
    FORBIDDEN = "forbidden"
    RATE_LIMITED = "rate_limited"
    SERVER_ERROR = "server_error"
    EMPTY_BODY = "empty_body"
    TRUNCATED_BODY = "truncated_body"
    UNEXPECTED_STATUS = "unexpected_status"
    TIMEOUT = "timeout"
    CONNECTION_ERROR = "connection_error"


@dataclass(frozen=True, slots=True)
class Classification:
    outcome: Outcome
    error_kind: ErrorKind | None


def classify(response: FetchResponse, prev_etag: str | None = None) -> Classification:
    """The one place a status code gets read. Body and headers settle what a
    status code alone can't: a truncated transfer, and a conditional hit,
    which only exists as a 200 with an empty body and a matching ETag — an
    empty body without that match is the service being flaky, not a real
    "unchanged", so it's TEMPORARY_FAILURE and gets retried like any other.
    """
    status = response.status_code

    if status in _PERMANENT_STATUSES:
        kind = ErrorKind.NOT_FOUND if status == 404 else ErrorKind.FORBIDDEN
        return Classification(Outcome.PERMANENT_FAILURE, kind)

    if status in _TEMPORARY_STATUSES:
        kind = ErrorKind.RATE_LIMITED if status == 429 else ErrorKind.SERVER_ERROR
        return Classification(Outcome.TEMPORARY_FAILURE, kind)

    if status != 200:
        return Classification(Outcome.TEMPORARY_FAILURE, ErrorKind.UNEXPECTED_STATUS)

    if not response.body:
        etag = response.header("ETag")
        if etag is not None and etag == prev_etag:
            return Classification(Outcome.NOT_MODIFIED, None)
        return Classification(Outcome.TEMPORARY_FAILURE, ErrorKind.EMPTY_BODY)

    declared = response.header("Content-Length")
    if declared is not None:
        try:
            mismatch = int(declared) != len(response.body)
        except ValueError:
            mismatch = False
        if mismatch:
            return Classification(Outcome.TEMPORARY_FAILURE, ErrorKind.TRUNCATED_BODY)

    return Classification(Outcome.SUCCESS, None)


def classify_exception(exc: TimeoutError | ClientConnectionError) -> Classification:
    """Same closed outcome set for a fetch that never produced a response.

    Only the two kinds fetch/client.py actually catches are named here. An
    exception of any other type is a bug, not network flakiness, and gets
    re-raised rather than folded into TEMPORARY_FAILURE — that would turn a
    bug in this codebase into a retry loop that looks like a flaky network
    and never surfaces as a failure to find.
    """
    if isinstance(exc, TimeoutError):
        return Classification(Outcome.TEMPORARY_FAILURE, ErrorKind.TIMEOUT)
    if isinstance(exc, ClientConnectionError):
        return Classification(Outcome.TEMPORARY_FAILURE, ErrorKind.CONNECTION_ERROR)
    raise exc
