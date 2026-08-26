"""Tests for src/crawler/models.py's header and body codecs."""

from datetime import UTC, datetime

from crawler.models import decode_body, encode_body, parse_retry_after

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


class TestBodyCodec:
    def test_round_trips_bytes(self):
        assert decode_body(encode_body(b"hello")) == b"hello"

    def test_none_stays_none(self):
        assert encode_body(None) is None
        assert decode_body(None) is None

    def test_empty_bytes_round_trip_not_none(self):
        assert decode_body(encode_body(b"")) == b""


class TestParseRetryAfter:
    def test_missing_header_is_none(self):
        assert parse_retry_after({}, _NOW) is None

    def test_seconds_form(self):
        assert parse_retry_after({"Retry-After": "120"}, _NOW) == 120.0

    def test_case_insensitive_lookup(self):
        assert parse_retry_after({"retry-after": "5"}, _NOW) == 5.0

    def test_http_date_form(self):
        headers = {"Retry-After": "Thu, 01 Jan 2026 00:02:00 GMT"}
        assert parse_retry_after(headers, _NOW) == 120.0

    def test_http_date_in_the_past_clamps_to_zero(self):
        headers = {"Retry-After": "Wed, 31 Dec 2025 00:00:00 GMT"}
        assert parse_retry_after(headers, _NOW) == 0.0

    def test_negative_seconds_value_clamps_to_zero(self):
        assert parse_retry_after({"Retry-After": "-5"}, _NOW) == 0.0

    def test_garbage_value_is_none(self):
        assert parse_retry_after({"Retry-After": "not-a-date-or-number"}, _NOW) is None
