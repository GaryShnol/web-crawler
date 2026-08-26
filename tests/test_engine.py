"""engine.py: the supervisor's completion/recovery loop, the drain's
grace-period-vs-forced-cancel race, and one light end-to-end wiring check.
No real waiting anywhere — every timeout-bearing wait takes an injected
`sleep` that resolves immediately, the same `now`/`sleep` pattern
fetch/rate_limiter.py uses, so a "long" interval never actually costs
wall-clock time in a test.
"""

import asyncio
import logging
from contextlib import asynccontextmanager

from aiohttp.test_utils import TestServer
from conftest import TEST_DATABASE_URL
from fake_api.app import FakeResponse, create_app

from crawler.config import Config
from crawler.engine import _drain, _progress, _supervise, run
from crawler.fetch.rate_limiter import RateLimiter
from crawler.store import frontier


async def _instant_sleep(_seconds: float) -> None:
    return


async def _never_sleep(_seconds: float) -> None:
    await asyncio.Event().wait()


async def _insert_pending(pool, url: str) -> int:
    row = await pool.fetchrow(
        "INSERT INTO urls (normalized_url, raw_url) VALUES ($1, $1) RETURNING id", url
    )
    return row["id"]


@asynccontextmanager
async def _running(routes: dict[str, list[FakeResponse]]):
    server = TestServer(create_app(routes))
    await server.start_server()
    try:
        yield server
    finally:
        await server.close()


class TestSupervise:
    async def test_stops_claiming_once_the_frontier_drains(self, pool):
        stop_claiming = asyncio.Event()
        url_id = await _insert_pending(pool, "http://fixture.local/x")

        task = asyncio.create_task(
            _supervise(pool, stop_claiming, interval=999, sleep=_instant_sleep)
        )
        await asyncio.sleep(0)  # let the first tick run
        assert not stop_claiming.is_set()  # one pending row: not complete yet

        async with pool.acquire() as conn:
            [claimed] = await frontier.claim_batch(conn, 1, _config())
            await frontier.mark_done(
                conn,
                url_id,
                claimed.lease_token,
                content_type=None,
                content_length=None,
                content_hash=None,
                etag=None,
            )

        await asyncio.wait_for(task, timeout=5)
        assert stop_claiming.is_set()

    async def test_recovers_an_expired_lease_along_the_way(self, pool):
        stop_claiming = asyncio.Event()
        url_id = await _insert_pending(pool, "http://fixture.local/y")
        async with pool.acquire() as conn:
            [claimed] = await frontier.claim_batch(conn, 1, _config())
        assert claimed.id == url_id
        await pool.execute(
            "UPDATE urls SET lease_until = now() - interval '1 second' WHERE id = $1", url_id
        )

        task = asyncio.create_task(
            _supervise(pool, stop_claiming, interval=999, sleep=_instant_sleep)
        )
        await asyncio.wait_for(_wait_until(lambda: _status(pool, url_id), "pending"), timeout=5)
        stop_claiming.set()
        await asyncio.wait_for(task, timeout=5)


class TestProgress:
    async def test_logs_status_counts_and_the_done_delta_as_a_rate(self, pool, caplog):
        """A gated sleep, not the instant one _supervise's tests use — that
        lets the loop spin arbitrarily many ticks between any two awaits in
        this test, racing ahead of the write below. Gating it means a tick
        only ever advances when this test says so.
        """
        caplog.set_level(logging.INFO, logger="crawler.engine")
        stop_claiming = asyncio.Event()
        config = _config()
        rate_limiter = RateLimiter(config)
        gate = asyncio.Event()

        async def _gated_sleep(_seconds: float) -> None:
            await gate.wait()
            gate.clear()

        task = asyncio.create_task(
            _progress(pool, rate_limiter, stop_claiming, interval=2.0, sleep=_gated_sleep)
        )
        await asyncio.wait_for(_wait_for_progress_lines(caplog, 1), timeout=5)  # tick 1: nothing done yet

        done_id = await _insert_pending(pool, "http://fixture.local/x")
        await _insert_pending(pool, "http://fixture.local/y")  # stays pending
        async with pool.acquire() as conn:
            [claimed] = await frontier.claim_batch(conn, 1, config)
            assert claimed.id == done_id
            await frontier.mark_done(
                conn,
                done_id,
                claimed.lease_token,
                content_type=None,
                content_length=None,
                content_hash=None,
                etag=None,
            )

        gate.set()  # release tick 1's wait; the write above already landed
        await asyncio.wait_for(_wait_for_progress_lines(caplog, 2), timeout=5)  # tick 2: one done
        stop_claiming.set()
        gate.set()  # release tick 2's wait so the loop can see stop_claiming and exit
        await asyncio.wait_for(task, timeout=5)

        lines = [r for r in caplog.records if r.getMessage() == "progress"]
        assert lines[0].context["done"] == 0
        assert lines[1].context["done"] == 1
        assert lines[1].context["pending"] == 1
        assert "0.5/s" in lines[1].context["rate"]  # (1 - 0) done over interval=2.0
        assert f"limit {rate_limiter.current_rate:.1f}/s" in lines[1].context["rate"]


class TestDrain:
    async def test_returns_once_every_task_finishes_on_its_own(self):
        gates = [asyncio.Event() for _ in range(2)]

        async def worker(gate: asyncio.Event) -> None:
            await gate.wait()

        tasks = [asyncio.create_task(worker(g)) for g in gates]
        force_stop = asyncio.Event()

        drain = asyncio.create_task(_drain(tasks, force_stop, grace_seconds=999, sleep=_never_sleep))
        await asyncio.sleep(0)
        for g in gates:
            g.set()

        await asyncio.wait_for(drain, timeout=5)
        assert all(t.done() and not t.cancelled() for t in tasks)

    async def test_cancels_whatever_is_left_once_the_grace_period_expires(self):
        gate = asyncio.Event()

        async def hangs() -> None:
            await gate.wait()

        task = asyncio.create_task(hangs())
        force_stop = asyncio.Event()

        await asyncio.wait_for(
            _drain([task], force_stop, grace_seconds=0.001, sleep=_instant_sleep), timeout=5
        )
        assert task.cancelled()

    async def test_a_second_signal_forces_the_cancel_before_grace_expires(self):
        gate = asyncio.Event()

        async def hangs() -> None:
            await gate.wait()

        task = asyncio.create_task(hangs())
        force_stop = asyncio.Event()
        force_stop.set()  # already fired, as if a second SIGINT arrived first

        # grace_seconds is huge and sleep never resolves — only force_stop
        # being already set can end this, proving it wins the race.
        await asyncio.wait_for(
            _drain([task], force_stop, grace_seconds=10_000, sleep=_never_sleep), timeout=5
        )
        assert task.cancelled()


class TestRunEndToEnd:
    async def test_a_single_page_with_no_links_crawls_and_exits_cleanly(self, pool, tmp_path):
        seed = "http://fixture.local/"
        routes = {
            seed: [FakeResponse(200, {"Content-Type": "text/html"}, b"<html><body>no links</body></html>")]
        }
        async with _running(routes) as server:
            config = Config(
                seed_url=seed,
                database_url=TEST_DATABASE_URL,
                fetch_api_url=str(server.make_url("/fetch")),
                output_dir=tmp_path,
                max_concurrency=2,
            )
            exit_code = await asyncio.wait_for(run(config, sleep=_instant_sleep), timeout=15)

        assert exit_code == 0
        row = await pool.fetchrow("SELECT status FROM urls WHERE normalized_url = $1", seed)
        assert row["status"] == "done"


def _config() -> Config:
    return Config(
        seed_url="http://fixture.local/",
        database_url="postgresql://unused/unused",
        fetch_api_url="http://unused/unused",
    )


async def _status(pool, url_id: int) -> str:
    return await pool.fetchval("SELECT status FROM urls WHERE id = $1", url_id)


async def _wait_until(getter, expected) -> None:
    while await getter() != expected:
        await asyncio.sleep(0)


async def _wait_for_progress_lines(caplog, count: int) -> None:
    while len([r for r in caplog.records if r.getMessage() == "progress"]) < count:
        await asyncio.sleep(0)
