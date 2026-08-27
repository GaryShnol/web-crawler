"""Maps a fetch response onto the closed set of outcomes, once, so nothing
else in src/ reads a status code. No retries, no backoff, no timing — those
live in fetch/retry.py and fetch/client.py, driven by what classify() returns.
"""

import enum
from dataclasses import dataclass

from aiohttp import ClientConnectionError, ClientPayloadError

from .models import FetchResponse, Outcome, find_header

_PERMANENT_STATUSES = {404, 403}
_TEMPORARY_STATUSES = {429, 500}
_DETAIL_MAX = 500  # error_message is unbounded TEXT, but a stray huge repr shouldn't fill the row


class ErrorKind(enum.Enum):
    """Why a failure happened. Unset on SUCCESS and NOT_MODIFIED."""

    NOT_FOUND = "not_found"
    FORBIDDEN = "forbidden"
    RATE_LIMITED = "rate_limited"
    SERVER_ERROR = "server_error"
    REDIRECT = "redirect"
    EMPTY_BODY = "empty_body"
    TRUNCATED_BODY = "truncated_body"
    BODY_TOO_LARGE = "body_too_large"
    UNEXPECTED_STATUS = "unexpected_status"
    TIMEOUT = "timeout"
    CONNECTION_ERROR = "connection_error"
    UNPARSEABLE_CONTENT = "unparseable_content"
    MALFORMED_RESPONSE = "malformed_response"
    INTERNAL_ERROR = "internal_error"


@dataclass(frozen=True, slots=True)
class Classification:
    outcome: Outcome
    error_kind: ErrorKind | None
    detail: str | None = None


def _describe(exc: BaseException) -> str:
    """The one place an exception becomes an error_message (see DESIGN.md)."""
    name, message = type(exc).__name__, str(exc)
    return (f"{name}: {message}" if message else name)[:_DETAIL_MAX]


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

    if 300 <= status < 400:
        # Off the documented statusCode set (see CLAUDE.md), unlike the
        # fallback below: a 3xx is a deterministic answer about this url, not
        # this API's ordinary flakiness, so retrying it meets the same
        # redirect every time — PERMANENT on purpose (see DESIGN.md).
        # Location is folded into detail here, not carried on FetchResult,
        # since nothing else ever needs it once a failure is recorded.
        location = find_header(response.headers, "Location")
        detail = f"redirect to {location}" if location else "redirect with no Location header"
        return Classification(Outcome.PERMANENT_FAILURE, ErrorKind.REDIRECT, detail)

    if status != 200:
        return Classification(Outcome.TEMPORARY_FAILURE, ErrorKind.UNEXPECTED_STATUS)

    if not response.body:
        etag = find_header(response.headers, "ETag")
        if etag is not None and etag == prev_etag:
            return Classification(Outcome.NOT_MODIFIED, None)
        return Classification(Outcome.TEMPORARY_FAILURE, ErrorKind.EMPTY_BODY)

    declared = find_header(response.headers, "Content-Length")
    if declared is not None:
        try:
            mismatch = int(declared) != len(response.body)
        except ValueError:
            mismatch = False
        if mismatch:
            detail = f"declared {declared} bytes, got {len(response.body)}"
            return Classification(Outcome.TEMPORARY_FAILURE, ErrorKind.TRUNCATED_BODY, detail)

    return Classification(Outcome.SUCCESS, None)


def classify_oversized_body(max_body_bytes: int, bytes_read: int) -> Classification:
    """The one shape a body-too-large refusal can take. fetch/client.py stops
    reading once the response has run past `max_body_bytes` — before a
    status code or headers are even fully received, so there's nothing left
    to interpret the way classify() interprets a complete response. PERMANENT
    because a retry meets the same oversized body.
    """
    detail = f"exceeded max_body_bytes={max_body_bytes} (read at least {bytes_read} bytes)"
    return Classification(Outcome.PERMANENT_FAILURE, ErrorKind.BODY_TOO_LARGE, detail)


def classify_unparseable_content(exc: Exception) -> Classification:
    """A body that matched a handler's `sniff` but broke while being parsed —
    truncated, corrupt, or (for a PDF) encrypted. TEMPORARY, unlike
    classify_oversized_body()'s PERMANENT: the fetch API can return
    different bytes for the same url on a later attempt (see CLAUDE.md's
    "can return different things ... on different attempts"), so a retry
    isn't guaranteed to meet the same broken body the way it's guaranteed
    to meet the same size.
    """
    return Classification(Outcome.TEMPORARY_FAILURE, ErrorKind.UNPARSEABLE_CONTENT, _describe(exc))


def classify_malformed_response(exc: Exception) -> Classification:
    """The one shape a fetch API envelope that isn't well-formed can take:
    invalid JSON, a missing `statusCode`, or a `body` that isn't valid
    base64 — three exceptions, one fact (this call produced garbage), so
    one kind covers all three; nothing downstream tells them apart anyway.

    TEMPORARY, same as classify_internal_error, but a different kind on
    purpose: INTERNAL_ERROR exists to separate "this codebase has a bug"
    from "the remote side is unreliable" (see DESIGN.md), and a malformed
    envelope is squarely the second one. Filing it as INTERNAL_ERROR would
    make that split lie — fetch/client.py classifies it here, at the fetch
    layer, before process_one's outer `except` (the real backstop, for an
    actual bug) ever gets a chance to see it.
    """
    return Classification(Outcome.TEMPORARY_FAILURE, ErrorKind.MALFORMED_RESPONSE, _describe(exc))


def classify_internal_error(exc: Exception) -> Classification:
    """TEMPORARY on purpose, not PERMANENT (see DESIGN.md) -- a wasted
    retry costs less than a url a pool blip buries forever.
    """
    return Classification(Outcome.TEMPORARY_FAILURE, ErrorKind.INTERNAL_ERROR, _describe(exc))


def classify_exception(
    exc: TimeoutError | ClientConnectionError | ClientPayloadError,
) -> Classification:
    """Same closed outcome set for a fetch that never produced a response.

    Only the kinds fetch/client.py actually catches are named here. An
    exception of any other type is a bug, not network flakiness, and gets
    re-raised rather than folded into TEMPORARY_FAILURE — that would turn a
    bug in this codebase into a retry loop that looks like a flaky network
    and never surfaces as a failure to find. ClientPayloadError (the
    connection dying mid-body) is grouped with ClientConnectionError: both
    are the transport failing, not a status the service returned.
    """
    if isinstance(exc, TimeoutError):
        return Classification(Outcome.TEMPORARY_FAILURE, ErrorKind.TIMEOUT, _describe(exc))
    if isinstance(exc, (ClientConnectionError, ClientPayloadError)):
        return Classification(Outcome.TEMPORARY_FAILURE, ErrorKind.CONNECTION_ERROR, _describe(exc))
    raise exc
