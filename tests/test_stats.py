"""store/stats.py's gather() against real Postgres -- aggregate reads over
whatever's already in the database, so plain inserts are enough setup; no
worker or fake_api needed.
"""

from datetime import UTC, datetime, timedelta

from crawler.store import stats


async def _insert_content(pool, content_hash: str, content_type: str, byte_size: int) -> None:
    await pool.execute(
        "INSERT INTO contents (content_hash, content_type, byte_size, storage_path) "
        "VALUES ($1, $2, $3, $4)",
        content_hash,
        content_type,
        byte_size,
        f"/tmp/{content_hash}",
    )


async def _insert_url(
    pool,
    url: str,
    *,
    status: str = "pending",
    error_kind: str | None = None,
    content_hash: str | None = None,
    content_changed_at: datetime | None = None,
) -> int:
    row = await pool.fetchrow(
        """
        INSERT INTO urls (normalized_url, raw_url, status, error_kind, content_hash, content_changed_at)
        VALUES ($1, $1, $2, $3, $4, $5)
        RETURNING id
        """,
        url,
        status,
        error_kind,
        content_hash,
        content_changed_at,
    )
    return row["id"]


class TestGather:
    async def test_status_and_failure_reason_counts(self, pool):
        await _insert_url(pool, "http://fixture.local/pending")
        await _insert_url(pool, "http://fixture.local/f1", status="failed", error_kind="not_found")
        await _insert_url(pool, "http://fixture.local/f2", status="failed", error_kind="not_found")
        await _insert_url(pool, "http://fixture.local/f3", status="failed", error_kind="server_error")

        async with pool.acquire() as conn:
            report = await stats.gather(conn)

        assert report.status_counts == {"pending": 1, "failed": 3}
        assert report.failure_reasons == {"not_found": 2, "server_error": 1}

    async def test_attempt_pressure_separates_retries_from_urls(self, pool):
        url_id = await _insert_url(pool, "http://fixture.local/x")
        for attempt_no in (1, 2, 3):
            await pool.execute(
                "INSERT INTO fetch_attempts (url_id, attempt_no, status_code, duration_ms) "
                "VALUES ($1, $2, 500, 10)",
                url_id,
                attempt_no,
            )

        async with pool.acquire() as conn:
            report = await stats.gather(conn)

        assert report.attempts_total == 3  # every attempt
        assert report.urls_attempted == 1  # one url absorbed all three

    async def test_bytes_by_type_and_dedup_hit_rate(self, pool):
        await _insert_content(pool, "hash1", "text/html", 100)
        await _insert_content(pool, "hash2", "text/html", 200)
        await _insert_content(pool, "hash3", "image/png", 500)
        await _insert_url(pool, "http://fixture.local/a", status="done", content_hash="hash1")
        await _insert_url(pool, "http://fixture.local/b", status="done", content_hash="hash1")  # dup
        await _insert_url(pool, "http://fixture.local/c", status="done", content_hash="hash3")

        async with pool.acquire() as conn:
            report = await stats.gather(conn)

        assert report.bytes_by_type == {"image/png": 500, "text/html": 300}  # contents, not urls -- dedup'd
        assert report.dedup_total == 3
        assert report.dedup_distinct == 2

    async def test_changed_count_respects_since(self, pool):
        two_days_ago = datetime.now(UTC) - timedelta(days=2)
        one_minute_ago = datetime.now(UTC) - timedelta(minutes=1)
        await _insert_url(pool, "http://fixture.local/old-change", status="done", content_changed_at=two_days_ago)
        await _insert_url(pool, "http://fixture.local/new-change", status="done", content_changed_at=one_minute_ago)
        await _insert_url(pool, "http://fixture.local/never-changed", status="done")

        async with pool.acquire() as conn:
            everything = await stats.gather(conn)
            windowed = await stats.gather(conn, since=datetime.now(UTC) - timedelta(hours=1))

        assert everything.changed_count == 2  # no window: both changes count
        assert windowed.changed_count == 1  # only the one inside the last hour
