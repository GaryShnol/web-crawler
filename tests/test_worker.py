"""worker.process_one against a real Postgres (conftest.py's pool fixture)
and a real listening fake_api server — no mocked transport, no mocked
database, same reasoning as test_frontier_concurrency.py and
test_fetch_client.py: the thing worth testing is what actually lands in
the frontier, not what a mock was told to return.
"""

import json
from contextlib import asynccontextmanager
from pathlib import Path

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


async def _insert_pending(pool, url: str, *, depth: int = 0, etag: str | None = None) -> int:
    row = await pool.fetchrow(
        "INSERT INTO urls (normalized_url, raw_url, depth, etag) VALUES ($1, $1, $2, $3) RETURNING id",
        url,
        depth,
        etag,
    )
    return row["id"]


async def _claim(pool, url_id: int, config: Config) -> frontier.ClaimedUrl:
    async with pool.acquire() as conn:
        [claimed] = await frontier.claim_batch(conn, 10, config)
        assert claimed.id == url_id
        return claimed


async def _row(pool, url_id: int):
    return await pool.fetchrow("SELECT * FROM urls WHERE id = $1", url_id)


async def _process(pool, url: str, routes: dict, tmp_path, *, depth: int = 0, etag=None, **overrides):
    async with _running(routes) as server:
        config = _config(str(server.make_url("/fetch")), tmp_path, **overrides)
        url_id = await _insert_pending(pool, url, depth=depth, etag=etag)
        claimed = await _claim(pool, url_id, config)
        async with FetchClient(config) as fetch_client:
            await process_one(
                pool,
                claimed,
                fetch_client=fetch_client,
                rate_limiter=RateLimiter(config),
                config=config,
                seed_host="fixture.local",
            )
        return url_id, claimed


class TestSuccessHtml:
    async def test_matched_html_is_stored_and_its_in_scope_link_enqueued(self, pool, tmp_path):
        page = "http://fixture.local/page"
        in_scope_link = "http://fixture.local/child"
        off_host_link = "http://other.local/"
        routes = {
            page: [
                FakeResponse(
                    200,
                    {"Content-Type": "text/html", "ETag": '"v1"'},
                    f'<html><body><a href="{in_scope_link}">c</a>'
                    f'<a href="{off_host_link}">o</a></body></html>'.encode(),
                )
            ]
        }
        url_id, _claimed = await _process(pool, page, routes, tmp_path, depth=0)

        row = await _row(pool, url_id)
        assert row["status"] == "done"
        assert row["content_type"] == "text/html"
        assert row["etag"] == '"v1"'
        assert row["content_hash"] is not None

        content_row = await pool.fetchrow(
            "SELECT * FROM contents WHERE content_hash = $1", row["content_hash"]
        )
        assert content_row is not None
        assert Path(content_row["storage_path"]).read_bytes().startswith(b"<html>")

        metadata_row = await pool.fetchrow(
            "SELECT kind, payload FROM content_metadata WHERE content_hash = $1", row["content_hash"]
        )
        assert metadata_row["kind"] == "page"
        assert json.loads(metadata_row["payload"]) == {"title": None, "link_count": 2}

        child = await pool.fetchrow(
            "SELECT status, depth FROM urls WHERE normalized_url = $1", in_scope_link
        )
        assert child is not None
        assert child["status"] == "pending"
        assert child["depth"] == 1

        off_host = await pool.fetchrow("SELECT 1 FROM urls WHERE normalized_url = $1", off_host_link)
        assert off_host is None  # off-host page: never enqueued

        edges = await pool.fetch("SELECT dst_id FROM links WHERE src_id = $1", url_id)
        assert len(edges) == 1  # only the in-scope link got an edge

    async def test_max_depth_stops_enqueueing_new_links(self, pool, tmp_path):
        page = "http://fixture.local/deep"
        child_link = "http://fixture.local/too-deep"
        routes = {page: [FakeResponse(200, {"Content-Type": "text/html"}, f'<a href="{child_link}">c</a>'.encode())]}

        await _process(pool, page, routes, tmp_path, depth=0, max_depth=0)

        child = await pool.fetchrow("SELECT 1 FROM urls WHERE normalized_url = $1", child_link)
        assert child is None

    async def test_lying_content_type_is_routed_by_body_not_header(self, pool, tmp_path):
        page = "http://fixture.local/lying"
        routes = {
            page: [FakeResponse(200, {"Content-Type": "image/png"}, b"<html>not a png</html>")]
        }
        url_id, _ = await _process(pool, page, routes, tmp_path)

        row = await _row(pool, url_id)
        assert row["status"] == "done"  # matched by sniffing, not by the declared header
        assert row["content_hash"] is not None


class TestSkipped:
    async def test_unmatched_content_type_is_skipped_not_failed(self, pool, tmp_path):
        page = "http://fixture.local/asset.bin"
        body = b"\x89PNG\r\nnot really parsed, just unmatched this session"
        routes = {page: [FakeResponse(200, {"Content-Type": "image/png"}, body)]}
        url_id, _ = await _process(pool, page, routes, tmp_path)

        row = await _row(pool, url_id)
        assert row["status"] == "skipped"
        assert row["content_type"] == "image/png"
        assert row["content_length"] == len(body)
        assert row["content_hash"] is None
        assert row["error_kind"] is None

        assert list(tmp_path.iterdir()) == []  # nothing was written to disk


class TestUnparseableContent:
    async def test_sniff_matching_but_corrupt_body_is_retried_not_a_crash(self, pool, tmp_path):
        # Real PNG magic bytes (passes ImageHandler.sniff), but no valid PNG
        # chunk stream behind them — Pillow raises inside handler.handle().
        page = "http://fixture.local/corrupt.png"
        body = b"\x89PNG\r\n\x1a\n" + b"not a real png chunk stream"
        routes = {page: [FakeResponse(200, {"Content-Type": "image/png"}, body)]}
        url_id, _ = await _process(pool, page, routes, tmp_path, max_attempts=5)

        row = await _row(pool, url_id)
        assert row["status"] == "pending"  # temporary failure: retried, not given up on
        assert row["error_kind"] == "unparseable_content"
        assert row["error_message"] is not None  # the handler exception's own repr, not rebuilt
        assert row["content_hash"] is None  # handler.handle() raised before blobs.write ran
        assert row["next_attempt_at"] is not None

        assert list(tmp_path.iterdir()) == []  # nothing written to disk
        assert await pool.fetchval("SELECT count(*) FROM contents") == 0


class TestFailures:
    async def test_404_is_a_permanent_failure(self, pool, tmp_path):
        page = "http://fixture.local/missing"
        routes = {page: [FakeResponse(404, {}, None)]}
        url_id, _ = await _process(pool, page, routes, tmp_path)

        row = await _row(pool, url_id)
        assert row["status"] == "failed"
        assert row["error_kind"] == "not_found"

        attempt = await pool.fetchrow("SELECT * FROM fetch_attempts WHERE url_id = $1", url_id)
        assert attempt["status_code"] == 404
        assert attempt["error_kind"] == "not_found"

    async def test_500_retries_with_a_future_next_attempt(self, pool, tmp_path):
        page = "http://fixture.local/broken"
        routes = {page: [FakeResponse(500, {}, None)]}
        url_id, _ = await _process(pool, page, routes, tmp_path, max_attempts=5)

        row = await _row(pool, url_id)
        assert row["status"] == "pending"
        assert row["error_kind"] == "server_error"
        assert row["next_attempt_at"] is not None

    async def test_exhausted_attempts_gives_up(self, pool, tmp_path):
        page = "http://fixture.local/always-broken"
        routes = {page: [FakeResponse(500, {}, None)]}
        # attempt_no comes from claim_batch's own bump, already at max_attempts=1.
        url_id, _ = await _process(pool, page, routes, tmp_path, max_attempts=1)

        row = await _row(pool, url_id)
        assert row["status"] == "failed"


class TestNotModified:
    async def test_matching_etag_on_empty_body_only_touches_last_seen(self, pool, tmp_path):
        page = "http://fixture.local/cached"
        routes = {page: [FakeResponse(200, {"ETag": '"same"'}, None)]}
        url_id, _ = await _process(pool, page, routes, tmp_path, etag='"same"')

        row = await _row(pool, url_id)
        assert row["status"] == "done"
        assert row["last_seen_at"] is not None


class TestInternalError:
    async def test_unhandled_exception_is_contained_and_recorded(self, pool, tmp_path, monkeypatch):
        # _fetch raising is a stand-in for any bug reaching process_one's
        # outer boundary uncaught -- json.loads in fetch/client.py, a bad
        # header lookup, anything. process_one returning at all (rather
        # than this test erroring out on a propagated ValueError) is the
        # containment claim; the row below is the recording claim.
        page = "http://fixture.local/boom"
        routes = {page: [FakeResponse(200, {"Content-Type": "text/html"}, b"<html></html>")]}

        async def _raise(*_args, **_kwargs):
            raise ValueError("boom")

        monkeypatch.setattr("crawler.worker._fetch", _raise)

        url_id, _ = await _process(pool, page, routes, tmp_path, max_attempts=5)

        row = await _row(pool, url_id)
        assert row["status"] == "pending"  # temporary failure: retried, not buried
        assert row["error_kind"] == "internal_error"
        assert row["error_message"] == "ValueError: boom"
        assert row["next_attempt_at"] is not None
