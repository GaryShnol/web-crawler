"""Tests for fetch/retry.py — a pure function, no I/O, no clock, no rng."""

from datetime import UTC, datetime, timedelta

import pytest

from crawler.config import Config
from crawler.fetch.retry import GiveUp, RetryAt, next_attempt
from crawler.models import Outcome

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _config(**overrides) -> Config:
    return Config(
        seed_url="http://fixture.local/",
        database_url="postgresql://unused/unused",
        fetch_api_url="http://fixture.local/fetch",
        **overrides,
    )


class TestPermanentFailure:
    def test_always_gives_up_regardless_of_attempt_no(self):
        for attempt_no in (1, 2, 100):
            result = next_attempt(Outcome.PERMANENT_FAILURE, attempt_no, {}, _NOW, _config())
            assert result == GiveUp()

    def test_gives_up_even_with_a_retry_after_header(self):
        result = next_attempt(
            Outcome.PERMANENT_FAILURE, 1, {"Retry-After": "5"}, _NOW, _config()
        )
        assert result == GiveUp()


class TestMaxAttempts:
    def test_gives_up_once_attempts_reach_the_configured_max(self):
        config = _config(max_attempts=3)
        result = next_attempt(Outcome.TEMPORARY_FAILURE, 3, {}, _NOW, config)
        assert result == GiveUp()

    def test_gives_up_past_the_configured_max(self):
        config = _config(max_attempts=3)
        result = next_attempt(Outcome.TEMPORARY_FAILURE, 4, {}, _NOW, config)
        assert result == GiveUp()

    def test_retries_the_attempt_before_max(self):
        config = _config(max_attempts=3)
        result = next_attempt(Outcome.TEMPORARY_FAILURE, 2, {}, _NOW, config, jitter=0.0)
        assert isinstance(result, RetryAt)


class TestExponentialBackoff:
    def test_jitter_zero_gives_the_lower_bound(self):
        config = _config(retry_base_seconds=1.0, retry_max_seconds=60.0, max_attempts=10)
        result = next_attempt(Outcome.TEMPORARY_FAILURE, 1, {}, _NOW, config, jitter=0.0)
        assert result == RetryAt(_NOW)

    def test_jitter_one_gives_the_exact_upper_bound(self):
        config = _config(retry_base_seconds=1.0, retry_max_seconds=60.0, max_attempts=10)
        result = next_attempt(Outcome.TEMPORARY_FAILURE, 3, {}, _NOW, config, jitter=1.0)
        # attempt_no=3 -> base * 2**(3-1) = 4.0
        assert result == RetryAt(_NOW + timedelta(seconds=4.0))

    def test_jitter_none_applies_no_reduction(self):
        config = _config(retry_base_seconds=1.0, retry_max_seconds=60.0, max_attempts=10)
        result = next_attempt(Outcome.TEMPORARY_FAILURE, 3, {}, _NOW, config)
        assert result == RetryAt(_NOW + timedelta(seconds=4.0))

    def test_ceiling_caps_the_exponential_growth(self):
        config = _config(retry_base_seconds=1.0, retry_max_seconds=3.0, max_attempts=10)
        result = next_attempt(Outcome.TEMPORARY_FAILURE, 5, {}, _NOW, config, jitter=1.0)
        # base * 2**4 = 16, capped to the 3.0 ceiling
        assert result == RetryAt(_NOW + timedelta(seconds=3.0))

    def test_first_attempt_uses_the_base_delay(self):
        config = _config(retry_base_seconds=2.0, retry_max_seconds=60.0, max_attempts=10)
        result = next_attempt(Outcome.TEMPORARY_FAILURE, 1, {}, _NOW, config, jitter=1.0)
        assert result == RetryAt(_NOW + timedelta(seconds=2.0))


class TestRetryAfterOverride:
    def test_seconds_form_overrides_the_backoff_formula(self):
        config = _config(retry_base_seconds=1.0, retry_max_seconds=60.0, max_attempts=10)
        result = next_attempt(
            Outcome.TEMPORARY_FAILURE, 5, {"Retry-After": "42"}, _NOW, config, jitter=1.0
        )
        assert result == RetryAt(_NOW + timedelta(seconds=42))

    def test_http_date_form_overrides_the_backoff_formula(self):
        config = _config(max_attempts=10)
        headers = {"Retry-After": "Thu, 01 Jan 2026 00:01:00 GMT"}
        result = next_attempt(Outcome.TEMPORARY_FAILURE, 1, headers, _NOW, config, jitter=1.0)
        assert result == RetryAt(_NOW + timedelta(seconds=60))

    def test_header_lookup_is_case_insensitive(self):
        config = _config(max_attempts=10)
        result = next_attempt(
            Outcome.TEMPORARY_FAILURE, 1, {"retry-after": "10"}, _NOW, config, jitter=1.0
        )
        assert result == RetryAt(_NOW + timedelta(seconds=10))

    def test_max_attempts_still_wins_over_a_retry_after_header(self):
        config = _config(max_attempts=1)
        result = next_attempt(
            Outcome.TEMPORARY_FAILURE, 1, {"Retry-After": "5"}, _NOW, config
        )
        assert result == GiveUp()


class TestMisuse:
    def test_success_is_not_a_valid_outcome_to_ask_about(self):
        with pytest.raises(AssertionError):
            next_attempt(Outcome.SUCCESS, 1, {}, _NOW, _config())

    def test_not_modified_is_not_a_valid_outcome_to_ask_about(self):
        with pytest.raises(AssertionError):
            next_attempt(Outcome.NOT_MODIFIED, 1, {}, _NOW, _config())
