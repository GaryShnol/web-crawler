"""Smoke tests for the fake API test double itself, not the crawler."""

import base64

from aiohttp.test_utils import TestClient, TestServer
from fake_api import site
from fake_api.app import FakeResponse, create_app


async def _get(client, url):
    resp = await client.get("/fetch", params={"url": url})
    return await resp.json()


async def test_sequence_advances_then_holds_on_last():
    routes = {"u": [FakeResponse(429, {}, None), FakeResponse(200, {}, b"ok")]}
    async with TestClient(TestServer(create_app(routes))) as client:
        first = await _get(client, "u")
        second = await _get(client, "u")
        third = await _get(client, "u")
    assert first["statusCode"] == 429
    assert second["statusCode"] == 200
    assert third["statusCode"] == 200  # held on the last entry


async def test_body_is_base64_encoded():
    routes = {"u": [FakeResponse(200, {}, b"hello")]}
    async with TestClient(TestServer(create_app(routes))) as client:
        envelope = await _get(client, "u")
    assert base64.b64decode(envelope["body"]) == b"hello"


async def test_null_body_stays_null():
    routes = {"u": [FakeResponse(404, {}, None)]}
    async with TestClient(TestServer(create_app(routes))) as client:
        envelope = await _get(client, "u")
    assert envelope["body"] is None


async def test_unmapped_url_returns_404():
    async with TestClient(TestServer(create_app({}))) as client:
        envelope = await _get(client, "http://nowhere/x")
    assert envelope["statusCode"] == 404


async def test_missing_url_param_is_400():
    async with TestClient(TestServer(create_app({}))) as client:
        resp = await client.get("/fetch")
    assert resp.status == 400


async def test_fault_injection_is_deterministic_under_a_seed(monkeypatch):
    monkeypatch.setenv("FAKE_API_SEED", "7")
    monkeypatch.setenv("FAKE_API_FAULT_RATE", "1")
    routes = {"u": [FakeResponse(200, {}, b"ok")]}

    async with TestClient(TestServer(create_app(routes))) as client:
        first_run = [(await _get(client, "u"))["statusCode"] for _ in range(20)]

    async with TestClient(TestServer(create_app(routes))) as client:
        second_run = [(await _get(client, "u"))["statusCode"] for _ in range(20)]

    assert first_run == second_run
    assert any(code != 200 for code in first_run)  # fault_rate=1: something must override


async def test_seed_page_links_all_resolve_in_the_route_table():
    routes = site.build_routes()
    linked = [
        site.QUERY_ONLY,
        site.CYCLE_A,
        site.CYCLE_B,
        site.OFFDOMAIN_PAGE,
        site.OFFHOST_IMAGE,
        site.IMAGE,
        site.PDF,
        site.VIDEO,
        site.DRIFTING,
    ]
    assert site.SEED in routes
    assert all(url in routes for url in linked)
