"""Tests for src/crawler/errors.py — pure classification, no I/O, no timing."""

import aiohttp
import pytest

from crawler.errors import Classification, ErrorKind, classify, classify_exception
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
        assert result == Classification(Outcome.TEMPORARY_FAILURE, ErrorKind.TRUNCATED_BODY)


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


class TestClassifyException:
    def test_timeout_is_temporary(self):
        result = classify_exception(TimeoutError())
        assert result == Classification(Outcome.TEMPORARY_FAILURE, ErrorKind.TIMEOUT)

    def test_connection_error_is_temporary(self):
        result = classify_exception(aiohttp.ClientConnectionError())
        assert result == Classification(Outcome.TEMPORARY_FAILURE, ErrorKind.CONNECTION_ERROR)

    def test_connection_error_subclass_is_temporary(self):
        # ClientOSError is what aiohttp actually raises on a reset connection.
        result = classify_exception(aiohttp.ClientOSError())
        assert result == Classification(Outcome.TEMPORARY_FAILURE, ErrorKind.CONNECTION_ERROR)

    def test_server_timeout_error_is_timeout_not_connection_error(self):
        # ServerTimeoutError subclasses both TimeoutError and
        # ClientConnectionError — the timeout check must win.
        result = classify_exception(aiohttp.ServerTimeoutError())
        assert result == Classification(Outcome.TEMPORARY_FAILURE, ErrorKind.TIMEOUT)

    def test_unrelated_exception_is_reraised_not_swallowed(self):
        with pytest.raises(TypeError):
            classify_exception(TypeError("bug in this codebase, not the network"))
