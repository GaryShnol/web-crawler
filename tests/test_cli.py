"""cli.py's pure pieces: the stats text formatter and the --since parser.
No database, no process -- _run_stats itself is a thin connect-gather-print
wrapper around store/stats.py, already covered by test_stats.py.
"""

from datetime import UTC, datetime

from crawler.cli import _format_stats, _parse_since
from crawler.store.stats import Stats


def _report(**overrides) -> Stats:
    defaults = {
        "status_counts": {"done": 2, "pending": 1},
        "failure_reasons": {},
        "attempts_total": 3,
        "urls_attempted": 3,
        "bytes_by_type": {"text/html": 100},
        "dedup_total": 2,
        "dedup_distinct": 2,
        "changed_count": 0,
    }
    return Stats(**{**defaults, **overrides})


class TestParseSince:
    def test_naive_timestamp_is_assumed_utc(self):
        assert _parse_since("2026-01-01T00:00:00") == datetime(2026, 1, 1, tzinfo=UTC)

    def test_timestamp_with_an_offset_keeps_it(self):
        parsed = _parse_since("2026-01-01T00:00:00+02:00")
        assert parsed.utcoffset().total_seconds() == 2 * 3600


class TestFormatStats:
    def test_reports_every_status_present(self):
        text = _format_stats(_report(status_counts={"done": 5, "failed": 2}), since=None)
        assert "done         5" in text
        assert "failed       2" in text

    def test_no_failures_says_so_rather_than_an_empty_section(self):
        text = _format_stats(_report(failure_reasons={}), since=None)
        assert "failure reasons (terminal only)\n  none" in text

    def test_attempts_line_shows_retries_as_the_gap(self):
        text = _format_stats(_report(attempts_total=10, urls_attempted=7), since=None)
        assert "attempts: 10 across 7 urls attempted (3 retried)" in text

    def test_dedup_hit_rate_is_the_shared_hash_fraction(self):
        text = _format_stats(_report(dedup_total=4, dedup_distinct=3), since=None)
        assert "25.0% hit rate" in text  # (4 - 3) / 4

    def test_no_stored_urls_avoids_a_divide_by_zero(self):
        text = _format_stats(_report(dedup_total=0, dedup_distinct=0), since=None)
        assert "no urls stored yet" in text

    def test_changed_count_labels_the_window(self):
        no_window = _format_stats(_report(changed_count=3), since=None)
        assert "content changed (ever): 3 urls" in no_window

        since = datetime(2026, 1, 1, tzinfo=UTC)
        windowed = _format_stats(_report(changed_count=1), since=since)
        assert f"content changed (since {since.isoformat()}): 1 urls" in windowed
