"""worker.py's structured completion logging: bound context (worker, url,
attempt) merged with the one per-outcome `extra={"context": {...}}` into
exactly one JSON line per url -- through logging.py's real _ContextFilter
and _JsonFormatter, not a mock of either. See test_logging.py for that
machinery's own unit coverage; this covers what worker.py actually feeds
it.
"""

import json
import logging
from contextlib import asynccontextmanager

from aiohttp.test_utils import TestServer
from fake_api.app import FakeResponse, create_app

from crawler.config import Config
from crawler.fetch.client import FetchClient
from crawler.fetch.rate_limiter import RateLimiter
from crawler.logging import _ContextFilter, _JsonFormatter, bind
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


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


async def _insert_pending(pool, url: str) -> int:
    row = await pool.fetchrow(
        "INSERT INTO urls (normalized_url, raw_url) VALUES ($1, $1) RETURNING id", url
    )
    return row["id"]


async def _process_and_capture(
    pool, url_id: int, config: Config, fetch_client: FetchClient, *, worker_id: int = 7
) -> list[dict]:
    """Claims whatever's pending (there's only ever one row here) and runs
    it through process_one with worker_id bound the way run() binds it,
    capturing every JSON line worker.py's logger actually emitted.
    """
    logger = logging.getLogger("crawler.worker")
    logger.setLevel(logging.INFO)  # root defaults to WARNING; nothing calls configure_logging() here
    handler = _Capture()
    handler.addFilter(_ContextFilter())
    logger.addHandler(handler)
    try:
        async with pool.acquire() as conn:
            [claimed] = await frontier.claim_batch(conn, 1, config)
        assert claimed.id == url_id
        with bind(worker_id=worker_id):
            await process_one(
                pool,
                claimed,
                fetch_client=fetch_client,
                rate_limiter=RateLimiter(config),
                config=config,
                seed_host="fixture.local",
            )
    finally:
        logger.removeHandler(handler)

    formatter = _JsonFormatter()
    return [json.loads(formatter.format(r)) for r in handler.records]


class TestCompletionLogging:
    async def test_html_success_logs_exactly_one_line_with_bound_context(self, pool, tmp_path):
        page = "http://fixture.local/page"
        routes = {page: [FakeResponse(200, {"Content-Type": "text/html"}, b"<html></html>")]}
        url_id = await _insert_pending(pool, page)

        async with _running(routes) as server:
            config = _config(str(server.make_url("/fetch")), tmp_path)
            async with FetchClient(config) as fetch_client:
                lines = await _process_and_capture(pool, url_id, config, fetch_client, worker_id=3)

        assert len(lines) == 1  # one line for the whole url, not one per step
        [payload] = lines
        assert payload["message"] == "url completed"
        assert payload["worker_id"] == 3
        assert payload["url"] == page
        assert payload["attempt"] == 1
        assert payload["outcome"] == "done"
        assert "hash_changed" not in payload  # first-ever fetch, nothing to compare against

    async def test_revisit_with_changed_body_reports_hash_changed_true(self, pool, tmp_path):
        page = "http://fixture.local/drifting"
        routes = {
            page: [
                FakeResponse(200, {"Content-Type": "text/html"}, b"<html><body>one</body></html>"),
                FakeResponse(200, {"Content-Type": "text/html"}, b"<html><body>two</body></html>"),
            ]
        }
        url_id = await _insert_pending(pool, page)

        async with _running(routes) as server:
            config = _config(str(server.make_url("/fetch")), tmp_path)
            async with FetchClient(config) as fetch_client:
                await _process_and_capture(pool, url_id, config, fetch_client)
                await pool.execute(
                    "UPDATE urls SET status = 'pending', next_attempt_at = now() WHERE id = $1", url_id
                )
                [payload] = await _process_and_capture(pool, url_id, config, fetch_client)

        assert payload["outcome"] == "done"
        assert payload["hash_changed"] is True

    async def test_revisit_with_same_body_reports_hash_changed_false(self, pool, tmp_path):
        page = "http://fixture.local/stable"
        routes = {page: [FakeResponse(200, {"Content-Type": "text/html"}, b"<html><body>same</body></html>")]}
        url_id = await _insert_pending(pool, page)

        async with _running(routes) as server:
            config = _config(str(server.make_url("/fetch")), tmp_path)
            async with FetchClient(config) as fetch_client:
                await _process_and_capture(pool, url_id, config, fetch_client)
                await pool.execute(
                    "UPDATE urls SET status = 'pending', next_attempt_at = now() WHERE id = $1", url_id
                )
                [payload] = await _process_and_capture(pool, url_id, config, fetch_client)

        assert payload["outcome"] == "done"
        assert payload["hash_changed"] is False

    async def test_unmatched_content_type_logs_skipped(self, pool, tmp_path):
        page = "http://fixture.local/asset.bin"
        routes = {page: [FakeResponse(200, {"Content-Type": "image/png"}, b"not really a png")]}
        url_id = await _insert_pending(pool, page)

        async with _running(routes) as server:
            config = _config(str(server.make_url("/fetch")), tmp_path)
            async with FetchClient(config) as fetch_client:
                [payload] = await _process_and_capture(pool, url_id, config, fetch_client)

        assert payload["outcome"] == "skipped"

    async def test_matching_etag_on_empty_body_logs_unchanged(self, pool, tmp_path):
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
                [payload] = await _process_and_capture(pool, url_id, config, fetch_client)

        assert payload["outcome"] == "unchanged"

    async def test_retryable_failure_logs_retrying_not_failed(self, pool, tmp_path):
        page = "http://fixture.local/broken"
        routes = {page: [FakeResponse(500, {}, None)]}
        url_id = await _insert_pending(pool, page)

        async with _running(routes) as server:
            config = _config(str(server.make_url("/fetch")), tmp_path, max_attempts=5)
            async with FetchClient(config) as fetch_client:
                [payload] = await _process_and_capture(pool, url_id, config, fetch_client)

        assert payload["outcome"] == "retrying"
        assert payload["error_kind"] == "server_error"

    async def test_exhausted_attempts_logs_failed(self, pool, tmp_path):
        page = "http://fixture.local/always-broken"
        routes = {page: [FakeResponse(500, {}, None)]}
        url_id = await _insert_pending(pool, page)

        async with _running(routes) as server:
            config = _config(str(server.make_url("/fetch")), tmp_path, max_attempts=1)
            async with FetchClient(config) as fetch_client:
                [payload] = await _process_and_capture(pool, url_id, config, fetch_client)

        assert payload["outcome"] == "failed"
        assert payload["error_kind"] == "server_error"
