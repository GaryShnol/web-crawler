"""claim_batch and enqueue_many under real contention. A mocked pool can't
reproduce the row-locking behaviour DESIGN.md relies on, so these run
against a live database — see conftest.py's pool fixture.

frontier.py takes a connection, never the pool (store/frontier.py's own
docstring on why) — each concurrent call below acquires its own, the way
worker.py's per-url pipeline would.
"""

import asyncio

from crawler.config import Config
from crawler.store.frontier import (
    DiscoveredLink,
    claim_batch,
    enqueue_many,
    mark_done,
    recover_expired_leases,
)

NUM_URLS = 500
NUM_CLAIMERS = 12
BATCH_LIMIT = 5


def _config() -> Config:
    return Config(
        seed_url="http://fixture.local/",
        database_url="postgresql://unused/unused",
        fetch_api_url="http://unused/unused",
    )


async def _claim(pool, limit: int, config: Config):
    async with pool.acquire() as conn:
        return await claim_batch(conn, limit, config)


async def _mark_done(pool, url_id: int, lease_token) -> None:
    async with pool.acquire() as conn:
        await mark_done(
            conn,
            url_id,
            lease_token,
            content_type=None,
            content_length=None,
            content_hash=None,
            etag=None,
            previous_hash=None,
        )


async def _enqueue(pool, links: list[DiscoveredLink], depth: int, src_id: int | None = None):
    async with pool.acquire() as conn:
        return await enqueue_many(conn, links, depth, src_id)


async def _insert_url(pool, normalized_url: str) -> int:
    row = await pool.fetchrow(
        "INSERT INTO urls (normalized_url, raw_url) VALUES ($1, $1) RETURNING id",
        normalized_url,
    )
    return row["id"]


async def _seed(pool, n: int) -> set[int]:
    rows = await pool.fetch(
        """
        INSERT INTO urls (normalized_url, raw_url)
        SELECT 'http://fixture.local/' || g, 'http://fixture.local/' || g
        FROM generate_series(1, $1) AS g
        RETURNING id
        """,
        n,
    )
    return {row["id"] for row in rows}


async def _claim_until_drained(pool, config: Config) -> list[int]:
    """Claims and immediately marks each row done, closing the lifecycle
    for real rather than relying on "in_progress never returns to
    pending" holding true for the rest of the test.
    """
    claimed: list[int] = []
    while True:
        batch = await _claim(pool, BATCH_LIMIT, config)
        if not batch:
            return claimed
        for claimed_url in batch:
            await _mark_done(pool, claimed_url.id, claimed_url.lease_token)
            claimed.append(claimed_url.id)


class TestLeaseTokenFencing:
    async def test_reclaimed_row_rejects_the_original_claimer_s_terminal_write(self, pool):
        """The race lease_token exists for: a lease expires and is
        recovered while the original claimer is still mid-flight, unaware.
        Its eventual mark_done must not land on the row a second claimer
        has since taken — id alone can't tell the two apart, only the
        token minted at claim time can.
        """
        url_id = await _insert_url(pool, "http://fixture.local/contested")
        config = _config()

        [first] = await _claim(pool, 1, config)
        assert first.id == url_id

        # Stand-in for the first claimer's lease going stale — same
        # backdating trick as test_frontier_recovery.py.
        await pool.execute(
            "UPDATE urls SET lease_until = now() - interval '1 second' WHERE id = $1", url_id
        )
        async with pool.acquire() as conn:
            recovered = await recover_expired_leases(conn)
        assert recovered == 1

        [second] = await _claim(pool, 1, config)
        assert second.id == url_id
        assert second.lease_token != first.lease_token

        # The stale claimer, still in flight, finally tries to finish.
        await _mark_done(pool, url_id, first.lease_token)

        row = await pool.fetchrow("SELECT status, lease_token FROM urls WHERE id = $1", url_id)
        assert row["status"] == "in_progress"  # untouched — still the second claimer's
        assert row["lease_token"] == second.lease_token


class TestClaimBatchConcurrency:
    async def test_every_url_claimed_exactly_once(self, pool):
        expected_ids = await _seed(pool, NUM_URLS)
        config = _config()

        results = await asyncio.gather(
            *(_claim_until_drained(pool, config) for _ in range(NUM_CLAIMERS))
        )
        claimed_ids = [url_id for worker_result in results for url_id in worker_result]

        assert len(claimed_ids) == NUM_URLS
        assert set(claimed_ids) == expected_ids


class TestEnqueueManyIdempotency:
    async def test_same_existing_url_from_two_parents_concurrently(self, pool):
        """Both calls take the 'already there' path — ON CONFLICT DO
        NOTHING skips the insert entirely for a row that already exists.
        This is not the race enqueue_many's two-statement split was built
        for; see test_same_new_url_from_two_parents_concurrently below.
        """
        target_url = "http://fixture.local/target"
        target_id = await _insert_url(pool, target_url)
        # Pre-mark done, not pending: enqueue_many's insert is
        # ON CONFLICT DO NOTHING, so if this were left at the default
        # 'pending' a regression that overwrites status to 'pending'
        # would pass unnoticed — it was already 'pending'.
        [claimed_target] = await _claim(pool, 1, _config())
        await _mark_done(pool, target_id, claimed_target.lease_token)

        parent_a = await _insert_url(pool, "http://fixture.local/parent-a")
        parent_b = await _insert_url(pool, "http://fixture.local/parent-b")
        link = DiscoveredLink(raw_url=target_url, normalized_url=target_url, anchor_text=None)

        await asyncio.gather(
            _enqueue(pool, [link], depth=1, src_id=parent_a),
            _enqueue(pool, [link], depth=1, src_id=parent_b),
        )

        rows = await pool.fetch("SELECT id, status FROM urls WHERE normalized_url = $1", target_url)
        assert len(rows) == 1
        assert rows[0]["status"] == "done"

        edges = await pool.fetch("SELECT src_id FROM links WHERE dst_id = $1", target_id)
        assert {edge["src_id"] for edge in edges} == {parent_a, parent_b}

    async def test_same_new_url_from_two_parents_concurrently(self, pool):
        """The actual race from AI_WORKLOG.md's rejection #1: the url
        doesn't exist yet, two transactions both try to insert it. Under a
        single INSERT-then-read CTE, the loser's DO NOTHING skips it and
        its own snapshot (taken at statement start) can't see the
        winner's uncommitted row either — no id comes back, its links
        edge is dropped. enqueue_many's second statement takes its own
        snapshot after both inserts have had a chance to commit, so both
        callers resolve to the same row regardless of who wins.
        """
        target_url = "http://fixture.local/brand-new-target"
        parent_a = await _insert_url(pool, "http://fixture.local/new-parent-a")
        parent_b = await _insert_url(pool, "http://fixture.local/new-parent-b")
        link = DiscoveredLink(raw_url=target_url, normalized_url=target_url, anchor_text=None)

        result_a, result_b = await asyncio.gather(
            _enqueue(pool, [link], depth=1, src_id=parent_a),
            _enqueue(pool, [link], depth=1, src_id=parent_b),
        )

        assert target_url in result_a
        assert target_url in result_b
        assert result_a[target_url] == result_b[target_url]
        target_id = result_a[target_url]

        rows = await pool.fetch("SELECT id FROM urls WHERE normalized_url = $1", target_url)
        assert len(rows) == 1
        assert rows[0]["id"] == target_id

        edges = await pool.fetch("SELECT src_id FROM links WHERE dst_id = $1", target_id)
        assert {edge["src_id"] for edge in edges} == {parent_a, parent_b}
