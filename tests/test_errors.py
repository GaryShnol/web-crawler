"""Tests for src/crawler/errors.py — pure classification, no I/O, no timing."""

import aiohttp
import pytest

from crawler.errors import (
    Classification,
    ErrorKind,
    classify,
    classify_exception,
    classify_oversized_body,
    classify_unparseable_content,
)
from crawler.models import FetchResponse, Outcome


def _response(status_code, headers=None, body=None):
    return FetchResponse(status_code=status_code, headers=headers or {}, body=body)


class TestPermanentFailures:
    def test_404_is_permanent_not_found(self):
        result = classify(_response(404))
        assert result == Classification(Outcome.PERMANENT_FAILURE, ErrorKind.NOT_FOUND)

    def test_403_is_permanent_forbidden(self):
        result = classify(_response(403))
        assert result == Classification(Outcome.PERMANENT_FAILURE, ErrorKind.FORBIDDEN)


class TestRedirect:
    # A 3xx is off the documented statusCode set the same way a 418 is (see
    # TestTemporaryFailures.test_unexpected_status_is_temporary...), but it's
    # PERMANENT rather than TEMPORARY: it's a deterministic answer about this
    # url, not this API's ordinary flakiness, so a retry meets the same
    # redirect every time (see DESIGN.md).
    def test_302_with_location_is_permanent_redirect(self):
        result = classify(_response(302, {"Location": "http://fixture.local/final"}))
        assert result.outcome is Outcome.PERMANENT_FAILURE
        assert result.error_kind is ErrorKind.REDIRECT
        assert result.detail == "redirect to http://fixture.local/final"

    def test_location_lookup_is_case_insensitive(self):
        result = classify(_response(301, {"location": "http://fixture.local/final"}))
        assert result.detail == "redirect to http://fixture.local/final"

    def test_redirect_with_no_location_is_still_permanent_redirect(self):
        result = classify(_response(307))
        assert result.outcome is Outcome.PERMANENT_FAILURE
        assert result.error_kind is ErrorKind.REDIRECT
        assert result.detail == "redirect with no Location header"


class TestTemporaryFailures:
    def test_429_is_temporary_rate_limited(self):
        result = classify(_response(429, {"Retry-After": "2"}))
        assert result == Classification(Outcome.TEMPORARY_FAILURE, ErrorKind.RATE_LIMITED)

    def test_429_without_retry_after_is_still_rate_limited(self):
        result = classify(_response(429))
        assert result == Classification(Outcome.TEMPORARY_FAILURE, ErrorKind.RATE_LIMITED)

    def test_500_is_temporary_server_error(self):
        result = classify(_response(500))
        assert result == Classification(Outcome.TEMPORARY_FAILURE, ErrorKind.SERVER_ERROR)

    def test_unexpected_status_is_temporary_and_does_not_raise(self):
        result = classify(_response(418))
        assert result == Classification(Outcome.TEMPORARY_FAILURE, ErrorKind.UNEXPECTED_STATUS)


class TestSuccess:
    def test_200_with_body_is_success(self):
        result = classify(_response(200, {"Content-Type": "text/html"}, b"<html></html>"))
        assert result == Classification(Outcome.SUCCESS, None)

    def test_matching_content_length_is_success(self):
        body = b"hello"
        result = classify(_response(200, {"Content-Length": str(len(body))}, body))
        assert result == Classification(Outcome.SUCCESS, None)

    def test_content_length_header_case_insensitive(self):
        body = b"hello"
        result = classify(_response(200, {"content-length": str(len(body))}, body))
        assert result == Classification(Outcome.SUCCESS, None)

    def test_unparseable_content_length_is_ignored_not_failed(self):
        result = classify(_response(200, {"Content-Length": "not-a-number"}, b"hello"))
        assert result == Classification(Outcome.SUCCESS, None)


class TestTruncatedBody:
    def test_content_length_mismatch_is_temporary_truncated(self):
        result = classify(_response(200, {"Content-Length": "99999"}, b"short"))
        assert result.outcome is Outcome.TEMPORARY_FAILURE
        assert result.error_kind is ErrorKind.TRUNCATED_BODY
        assert result.detail == "declared 99999 bytes, got 5"


class TestEmptyBodyAndConditionalHit:
    def test_empty_body_with_no_etag_is_temporary_empty_body(self):
        result = classify(_response(200, {}, None))
        assert result == Classification(Outcome.TEMPORARY_FAILURE, ErrorKind.EMPTY_BODY)

    def test_empty_body_with_etag_but_no_prior_etag_is_temporary_empty_body(self):
        result = classify(_response(200, {"ETag": '"abc"'}, None))
        assert result == Classification(Outcome.TEMPORARY_FAILURE, ErrorKind.EMPTY_BODY)

    def test_empty_body_with_mismatched_etag_is_temporary_empty_body(self):
        result = classify(_response(200, {"ETag": '"abc"'}, b""), prev_etag='"xyz"')
        assert result == Classification(Outcome.TEMPORARY_FAILURE, ErrorKind.EMPTY_BODY)

    def test_empty_body_with_matching_etag_is_not_modified(self):
        result = classify(_response(200, {"ETag": '"abc"'}, None), prev_etag='"abc"')
        assert result == Classification(Outcome.NOT_MODIFIED, None)

    def test_empty_string_body_with_matching_etag_is_not_modified(self):
        result = classify(_response(200, {"ETag": '"abc"'}, b""), prev_etag='"abc"')
        assert result == Classification(Outcome.NOT_MODIFIED, None)


class TestBodyTooLarge:
    def test_oversized_body_is_permanent(self):
        result = classify_oversized_body(max_body_bytes=100, bytes_read=8500)
        assert result.outcome is Outcome.PERMANENT_FAILURE
        assert result.error_kind is ErrorKind.BODY_TOO_LARGE
        assert result.detail == "exceeded max_body_bytes=100 (read at least 8500 bytes)"


class TestUnparseableContent:
    def test_unparseable_content_is_temporary(self):
        # unlike an oversized body, a retry isn't guaranteed to see the same
        # broken bytes — the fetch API can return something different next time.
        result = classify_unparseable_content(ValueError("bad chunk stream"))
        assert result.outcome is Outcome.TEMPORARY_FAILURE
        assert result.error_kind is ErrorKind.UNPARSEABLE_CONTENT
        assert result.detail == "ValueError: bad chunk stream"


class TestClassifyException:
    def test_timeout_is_temporary(self):
        result = classify_exception(TimeoutError())
        assert result.outcome is Outcome.TEMPORARY_FAILURE
        assert result.error_kind is ErrorKind.TIMEOUT

    def test_connection_error_is_temporary(self):
        result = classify_exception(aiohttp.ClientConnectionError())
        assert result.outcome is Outcome.TEMPORARY_FAILURE
        assert result.error_kind is ErrorKind.CONNECTION_ERROR

    def test_payload_error_is_temporary_connection_error(self):
        # a body that dies mid-stream is the transport failing, not a status
        # the service returned — grouped with ClientConnectionError.
        result = classify_exception(aiohttp.ClientPayloadError())
        assert result.outcome is Outcome.TEMPORARY_FAILURE
        assert result.error_kind is ErrorKind.CONNECTION_ERROR

    def test_connection_error_subclass_is_temporary(self):
        # ClientOSError is what aiohttp actually raises on a reset connection.
        result = classify_exception(aiohttp.ClientOSError())
        assert result.outcome is Outcome.TEMPORARY_FAILURE
        assert result.error_kind is ErrorKind.CONNECTION_ERROR

    def test_server_timeout_error_is_timeout_not_connection_error(self):
        # ServerTimeoutError subclasses both TimeoutError and
        # ClientConnectionError — the timeout check must win.
        result = classify_exception(aiohttp.ServerTimeoutError())
        assert result.outcome is Outcome.TEMPORARY_FAILURE
        assert result.error_kind is ErrorKind.TIMEOUT

    def test_detail_distinguishes_connect_from_read_timeout(self):
        # Both subclass ServerTimeoutError/TimeoutError identically for
        # classification purposes -- detail is the only place connect vs.
        # read survives, via the exception's own class name.
        connect = classify_exception(aiohttp.ConnectionTimeoutError())
        read = classify_exception(aiohttp.SocketTimeoutError())
        assert "ConnectionTimeoutError" in connect.detail
        assert "SocketTimeoutError" in read.detail

    def test_unrelated_exception_is_reraised_not_swallowed(self):
        with pytest.raises(TypeError):
            classify_exception(TypeError("bug in this codebase, not the network"))
