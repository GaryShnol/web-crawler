"""Conditional-revisit coverage: what a second visit to the same url does
to the frontier and to disk. mark_unchanged runs directly against real
Postgres, no clock to inject -- same reasoning as
test_frontier_recovery.py: it's a plain UPDATE, nothing here waits on
anything. The other two run worker.process_one end to end against real
Postgres and a real listening fake_api server, same as test_worker.py --
the thing worth testing is what actually lands in the frontier and on
disk, not what a mock was told to return.
"""

from contextlib import asynccontextmanager

from aiohttp.test_utils import TestServer
from fake_api.app import FakeResponse, create_app

from crawler.config import Config
from crawler.fetch.client import FetchClient
from crawler.fetch.rate_limiter import RateLimiter
from crawler.store import frontier
from crawler.worker import process_one


def _config(fetch_api_url: str, tmp_path, **overrides) -> Config:
    return Config(
        seed_url="http://fixture.local/",
        database_url="postgresql://unused/unused",
        fetch_api_url=fetch_api_url,
        output_dir=tmp_path,
        **overrides,
    )


@asynccontextmanager
async def _running(routes: dict[str, list[FakeResponse]]):
    server = TestServer(create_app(routes))
    await server.start_server()
    try:
        yield server
    finally:
        await server.close()


async def _row(pool, url_id: int):
    return await pool.fetchrow("SELECT * FROM urls WHERE id = $1", url_id)


async def _process_pending(pool, url_id: int, config: Config, fetch_client: FetchClient) -> None:
    """Claims whatever's pending (there's only ever one row in these
    tests) and runs it through process_one, the same pipeline a worker
    uses -- not a shortcut around it.
    """
    async with pool.acquire() as conn:
        [claimed] = await frontier.claim_batch(conn, 1, config)
    assert claimed.id == url_id
    await process_one(
        pool,
        claimed,
        fetch_client=fetch_client,
        rate_limiter=RateLimiter(config),
        config=config,
        seed_host="fixture.local",
    )


class TestMarkUnchanged:
    async def test_moves_last_seen_at_and_nothing_else(self, pool):
        await pool.execute(
            "INSERT INTO contents (content_hash, content_type, byte_size, storage_path) "
            "VALUES ($1, $2, $3, $4)",
            "priorhash",
            "text/html",
            42,
            "/tmp/prior.html",
        )
        row = await pool.fetchrow(
            """
            INSERT INTO urls (normalized_url, raw_url, content_type, content_length,
                               content_hash, etag, last_seen_at)
            VALUES ($1, $1, $2, $3, $4, $5, now() - interval '1 hour')
            RETURNING id, last_seen_at
            """,
            "http://fixture.local/revisit",
            "text/html",
            42,
            "priorhash",
            '"etag1"',
        )
        url_id, prior_seen_at = row["id"], row["last_seen_at"]
        config = Config(
            seed_url="http://fixture.local/",
            database_url="postgresql://unused/unused",
            fetch_api_url="http://unused/unused",
        )

        async with pool.acquire() as conn:
            [claimed] = await frontier.claim_batch(conn, 1, config)
            await frontier.mark_unchanged(conn, claimed.id, claimed.lease_token)

        after = await _row(pool, url_id)
        assert after["status"] == "done"
        assert after["last_seen_at"] > prior_seen_at
        assert after["content_type"] == "text/html"
        assert after["content_length"] == 42
        assert after["content_hash"] == "priorhash"
        assert after["etag"] == '"etag1"'
        assert after["content_changed_at"] is None  # never touched, not just left null


class TestConditionalHit:
    async def test_matching_etag_on_empty_body_is_not_modified_with_no_blob_write(self, pool, tmp_path):
        page = "http://fixture.local/cached"
        row = await pool.fetchrow(
            "INSERT INTO urls (normalized_url, raw_url, etag) VALUES ($1, $1, $2) RETURNING id",
            page,
            '"same"',
        )
        url_id = row["id"]
        routes = {page: [FakeResponse(200, {"ETag": '"same"'}, None)]}

        async with _running(routes) as server:
            config = _config(str(server.make_url("/fetch")), tmp_path)
            async with FetchClient(config) as fetch_client:
                await _process_pending(pool, url_id, config, fetch_client)

        after = await _row(pool, url_id)
        assert after["status"] == "done"
        assert after["content_hash"] is None  # no body ever arrived to hash
        assert list(tmp_path.iterdir()) == []  # NOT_MODIFIED never reaches blobs.write


class TestUnchangedFullBody:
    async def test_same_hash_revisit_writes_no_new_blob_and_leaves_content_changed_at_alone(
        self, pool, tmp_path
    ):
        page = "http://fixture.local/stable"
        body = b"<html><body>stable content</body></html>"
        routes = {page: [FakeResponse(200, {"Content-Type": "text/html"}, body)]}
        row = await pool.fetchrow(
            "INSERT INTO urls (normalized_url, raw_url) VALUES ($1, $1) RETURNING id", page
        )
        url_id = row["id"]

        async with _running(routes) as server:
            config = _config(str(server.make_url("/fetch")), tmp_path)
            async with FetchClient(config) as fetch_client:
                await _process_pending(pool, url_id, config, fetch_client)  # first fetch

                first = await _row(pool, url_id)
                assert first["content_hash"] is not None
                assert first["content_changed_at"] is None  # baseline, not a change
                blobs_after_first = list((tmp_path / "pages").iterdir())
                assert len(blobs_after_first) == 1

                # Simulate a later re-crawl finding the row pending again --
                # same reasoning as recover_expired_leases's own tests: this
                # is standing in for machinery this feature doesn't need.
                await pool.execute(
                    "UPDATE urls SET status = 'pending', next_attempt_at = now() WHERE id = $1",
                    url_id,
                )
                await _process_pending(pool, url_id, config, fetch_client)  # revisit, same body

        second = await _row(pool, url_id)
        assert second["content_hash"] == first["content_hash"]
        assert second["content_changed_at"] is None  # same hash: never counted as a change
        assert list((tmp_path / "pages").iterdir()) == blobs_after_first  # no new file written
