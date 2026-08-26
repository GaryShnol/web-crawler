"""The bounded worker pool, the lease supervisor alongside it, and shutdown.
`run(config)` seeds the frontier, starts `config.max_concurrency` workers and
one supervisor, and returns once they've all stopped — either because the
crawl finished on its own or because it was told to.

Shutdown, either way, is: stop claiming, let whatever's in flight finish, and
exit 0. A SIGINT or SIGTERM stops claiming; a second one skips the grace
period and cancels immediately — worker.run()'s own CancelledError handler
releases whatever lease that task was holding, which is what turns "still
running when the grace expired" into a `pending` row again rather than a
lease the next recovery sweep has to catch.
"""

import asyncio
import contextlib
import logging
import signal
from collections.abc import Awaitable, Callable
from pathlib import Path
from urllib.parse import urlsplit

import asyncpg

from . import worker
from .config import Config
from .fetch.client import FetchClient
from .fetch.rate_limiter import RateLimiter
from .store import db, frontier
from .url_tools import normalize
from .worker import _wait

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent.parent / "migrations"


async def _supervise(
    pool: asyncpg.Pool,
    stop_claiming: asyncio.Event,
    interval: float,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Recovers expired leases and checks for a finished crawl on the same
    tick, until `stop_claiming` is set — by a signal, or by this loop
    itself the moment it finds the frontier empty. Stops immediately once
    that happens: nothing is claiming once shutdown begins, so there's
    nothing to recover, and a sweep mid-drain would push a row that's
    actively being processed back to `pending`.
    """
    while not stop_claiming.is_set():
        async with pool.acquire() as conn:
            recovered = await frontier.recover_expired_leases(conn)
            if recovered:
                logger.info(f"recovered {recovered} expired lease(s)")
            if await frontier.crawl_complete(conn):
                stop_claiming.set()
                return
        await _wait(stop_claiming, interval, sleep)


async def _drain(
    tasks: list[asyncio.Task],
    force_stop: asyncio.Event,
    grace_seconds: float,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Waits for every worker task to finish on its own — the normal case,
    since `process_one` always runs to completion — up to `grace_seconds`
    or a second signal (`force_stop`), whichever comes first; past that,
    cancels whatever's still running and waits for the cancellation to land.
    """
    all_done = asyncio.gather(*tasks, return_exceptions=True)
    timer = asyncio.ensure_future(sleep(grace_seconds))
    forced = asyncio.ensure_future(force_stop.wait())
    try:
        await asyncio.wait({all_done, timer, forced}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for future in (timer, forced):
            if not future.done():
                future.cancel()

    if not all_done.done():
        for task in tasks:
            task.cancel()
        await all_done


def _install_signal_handlers(stop_claiming: asyncio.Event, force_stop: asyncio.Event) -> None:
    """First SIGINT/SIGTERM stops claiming; a second one of either forces an
    immediate cancel instead of waiting out the grace period.
    """
    signalled = False

    def handle() -> None:
        nonlocal signalled
        if signalled:
            force_stop.set()
        else:
            signalled = True
            stop_claiming.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, handle)
        except NotImplementedError:
            # Windows has no add_signal_handler; signal.signal's handler runs
            # on the main thread same as everywhere else, so this is safe.
            signal.signal(sig, lambda *_args: handle())


async def run(config: Config, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> int:
    pool = await db.create_pool(config)
    try:
        await db.run_migrations(pool, MIGRATIONS_DIR)

        seed_normalized = normalize(config.seed_url)
        seed_host = urlsplit(seed_normalized).hostname
        if seed_host is None:
            raise ValueError(f"seed url has no host: {config.seed_url}")

        async with pool.acquire() as conn:
            await frontier.enqueue_many(
                conn,
                [
                    frontier.DiscoveredLink(
                        raw_url=config.seed_url, normalized_url=seed_normalized, anchor_text=None
                    )
                ],
                depth=0,
            )

        stop_claiming = asyncio.Event()
        force_stop = asyncio.Event()
        _install_signal_handlers(stop_claiming, force_stop)

        interval = config.lease_recovery_interval_seconds or config.lease_seconds / 2

        async with FetchClient(config) as fetch_client:
            rate_limiter = RateLimiter(config)
            supervisor = asyncio.create_task(_supervise(pool, stop_claiming, interval, sleep))
            workers = [
                asyncio.create_task(
                    worker.run(
                        i, pool, fetch_client, rate_limiter, config, seed_host, stop_claiming, sleep
                    )
                )
                for i in range(config.max_concurrency)
            ]

            await stop_claiming.wait()
            await _drain(workers, force_stop, config.shutdown_grace_seconds, sleep)

            supervisor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await supervisor
    finally:
        await pool.close()

    return 0
