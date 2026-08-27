"""Tests for fetch/client.py against tests/fake_api — a real listening
server, no mocked transport, so connection failures are the real thing.
"""

from contextlib import asynccontextmanager

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from fake_api.app import FakeResponse, MalformedResponse, create_app

from crawler.config import Config
from crawler.fetch.client import FetchClient
from crawler.models import Outcome


def _config(fetch_api_url: str, **overrides) -> Config:
    return Config(
        seed_url="http://fixture.local/",
        database_url="postgresql://unused/unused",
        fetch_api_url=fetch_api_url,
        **overrides,
    )


@asynccontextmanager
async def _running(app: web.Application):
    server = TestServer(app)
    await server.start_server()
    try:
        yield server
    finally:
        await server.close()


class TestSuccessAndClassification:
    async def test_success_reads_body_and_headers(self):
        routes = {"u": [FakeResponse(200, {"Content-Type": "text/html"}, b"<html></html>")]}
        async with (
            _running(create_app(routes)) as server,
            FetchClient(_config(str(server.make_url("/fetch")))) as client,
        ):
            result = await client.fetch("u")

        assert result.outcome == Outcome.SUCCESS
        assert result.response.status_code == 200
        assert result.response.body == b"<html></html>"

    async def test_url_with_query_string_round_trips_as_the_route_key(self):
        url = "http://fixture.local/page?a=1&b=2"
        routes = {url: [FakeResponse(200, {}, b"ok")]}
        async with (
            _running(create_app(routes)) as server,
            FetchClient(_config(str(server.make_url("/fetch")))) as client,
        ):
            result = await client.fetch(url)

        assert result.response.body == b"ok"

    async def test_permanent_failure_for_404(self):
        routes = {"u": [FakeResponse(404, {}, None)]}
        async with (
            _running(create_app(routes)) as server,
            FetchClient(_config(str(server.make_url("/fetch")))) as client,
        ):
            result = await client.fetch("u")

        assert result.outcome == Outcome.PERMANENT_FAILURE
        assert result.response.status_code == 404

    async def test_temporary_failure_for_429(self):
        routes = {"u": [FakeResponse(429, {"Retry-After": "2"}, None)]}
        async with (
            _running(create_app(routes)) as server,
            FetchClient(_config(str(server.make_url("/fetch")))) as client,
        ):
            result = await client.fetch("u")

        assert result.outcome == Outcome.TEMPORARY_FAILURE

    async def test_not_modified_when_etag_matches(self):
        routes = {"u": [FakeResponse(200, {"ETag": '"abc"'}, None)]}
        async with (
            _running(create_app(routes)) as server,
            FetchClient(_config(str(server.make_url("/fetch")))) as client,
        ):
            result = await client.fetch("u", prev_etag='"abc"')

        assert result.outcome == Outcome.NOT_MODIFIED

    async def test_permanent_failure_for_redirect(self):
        routes = {"u": [FakeResponse(302, {"Location": "http://fixture.local/final"}, None)]}
        async with (
            _running(create_app(routes)) as server,
            FetchClient(_config(str(server.make_url("/fetch")))) as client,
        ):
            result = await client.fetch("u")

        assert result.outcome == Outcome.PERMANENT_FAILURE
        assert "http://fixture.local/final" in result.error_detail

    async def test_multiple_fetches_on_one_client_each_get_their_own_response(self):
        # Proves the client can be called repeatedly and routes/sequences
        # still advance correctly — not a claim about session internals.
        routes = {"u": [FakeResponse(200, {}, b"one"), FakeResponse(200, {}, b"two")]}
        async with (
            _running(create_app(routes)) as server,
            FetchClient(_config(str(server.make_url("/fetch")))) as client,
        ):
            first = await client.fetch("u")
            second = await client.fetch("u")

        assert first.response.body == b"one"
        assert second.response.body == b"two"


class TestMalformedEnvelope:
    async def test_invalid_json_is_temporary_malformed_response(self):
        routes = {"u": [MalformedResponse(b"{not json")]}
        async with (
            _running(create_app(routes)) as server,
            FetchClient(_config(str(server.make_url("/fetch")))) as client,
        ):
            result = await client.fetch("u")

        assert result.outcome == Outcome.TEMPORARY_FAILURE
        assert result.response is None  # nothing parsed far enough to build one
        assert result.error_detail is not None


class TestConditionalRequest:
    async def test_if_none_match_header_is_sent_when_prev_etag_given(self):
        capture: list[dict[str, str]] = []
        routes = {"u": [FakeResponse(200, {}, b"ok")]}
        async with (
            _running(create_app(routes, capture=capture)) as server,
            FetchClient(_config(str(server.make_url("/fetch")))) as client,
        ):
            await client.fetch("u", prev_etag='"xyz"')

        assert capture[-1].get("If-None-Match") == '"xyz"'

    async def test_no_if_none_match_header_when_prev_etag_is_none(self):
        capture: list[dict[str, str]] = []
        routes = {"u": [FakeResponse(200, {}, b"ok")]}
        async with (
            _running(create_app(routes, capture=capture)) as server,
            FetchClient(_config(str(server.make_url("/fetch")))) as client,
        ):
            await client.fetch("u")

        assert "If-None-Match" not in capture[-1]


class TestBodyTooLarge:
    async def test_oversized_body_is_permanent_with_no_response(self):
        # 20KB is comfortably past the streaming cap for max_body_bytes=100
        # (cap = envelope overhead + base64 blowup of ~101 bytes) without
        # needing a production-scale payload in the test.
        routes = {"u": [FakeResponse(200, {"Content-Type": "text/plain"}, b"x" * 20_000)]}
        async with _running(create_app(routes)) as server:
            config = _config(str(server.make_url("/fetch")), max_body_bytes=100)
            async with FetchClient(config) as client:
                result = await client.fetch("u")

        assert result.outcome == Outcome.PERMANENT_FAILURE
        # aborted mid-stream: no complete envelope to pull status/headers from
        assert result.response is None
        assert "max_body_bytes=100" in result.error_detail

    async def test_body_within_the_cap_is_untouched(self):
        routes = {"u": [FakeResponse(200, {}, b"x" * 50)]}
        async with _running(create_app(routes)) as server:
            config = _config(str(server.make_url("/fetch")), max_body_bytes=100)
            async with FetchClient(config) as client:
                result = await client.fetch("u")

        assert result.outcome == Outcome.SUCCESS
        assert result.response.body == b"x" * 50


class TestTransportFailures:
    async def test_connection_refused_is_temporary_failure(self):
        server = TestServer(create_app({}))
        await server.start_server()
        url = str(server.make_url("/fetch"))
        await server.close()  # nothing listens on `url` anymore

        async with FetchClient(_config(url)) as client:
            result = await client.fetch("u")

        assert result.outcome == Outcome.TEMPORARY_FAILURE
        assert result.response is None
        assert result.error_detail is not None  # the refused-connection exception's own repr


class TestUsageOutsideContextManager:
    async def test_fetch_before_aenter_raises(self):
        client = FetchClient(_config("http://unused/fetch"))
        with pytest.raises(AssertionError):
            await client.fetch("u")
