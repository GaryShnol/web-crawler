"""The bounded worker pool, the lease supervisor alongside it, and shutdown.
`run(config)` seeds the frontier, starts `config.max_concurrency` workers, a
lease supervisor, and a drain watcher, and returns once they've all stopped —
either because the crawl finished on its own or because it was told to.

Shutdown, either way, is: stop claiming, let whatever's in flight finish, and
exit 0 — or 1 if a worker, the supervisor, or the progress task ended with an
exception instead of finishing cleanly; that's process health, not crawl
outcome, so a url that gave up after max_attempts never changes this (see
`crawler stats` for that). A SIGINT or SIGTERM stops claiming; a second one
skips the grace period and cancels immediately — worker.run()'s own
CancelledError handler releases whatever lease that task was holding, which
is what turns "still running when the grace expired" into a `pending` row
again rather than a lease the next recovery sweep has to catch. Whichever of
signal, drain, or a dead supervisor/drain-watcher ends the run first is
logged, once, on the way out, alongside the terminal status counts.
"""

import asyncio
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
    """Recovers expired leases every `interval`, until `stop_claiming` is
    set — by a signal, by `_watch_drain` noticing an empty frontier, or by
    this task's own death (see `_force_shutdown_on_death`). Stops
    immediately once that happens: nothing is claiming once shutdown
    begins, so there's nothing to recover, and a sweep mid-drain would push
    a row that's actively being processed back to `pending`.

    Deliberately not the task that notices a finished crawl — `interval`
    here is `lease_seconds`-derived, sized for how long a lease may go
    stale, which has nothing to do with how quickly an empty frontier
    should be noticed. Conflating the two used to mean a crawl that
    finished in two seconds could still take a full lease-recovery cycle to
    exit; see `_watch_drain` and DESIGN.md.
    """
    while not stop_claiming.is_set():
        async with pool.acquire() as conn:
            recovered = await frontier.recover_expired_leases(conn)
            if recovered:
                logger.info(f"recovered {recovered} expired lease(s)")
        await _wait(stop_claiming, interval, sleep)


async def _watch_drain(
    pool: asyncpg.Pool,
    stop_claiming: asyncio.Event,
    interval: float,
    record_reason: Callable[[str], None],
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Checks for a finished crawl on its own cadence — independent of
    lease recovery's, see `_supervise` — and sets `stop_claiming` the
    moment it finds one. A count query against two partial indexes costs
    nothing to run often; DESIGN.md has the argument for why this check is
    race-free against a worker that's about to enqueue.
    """
    while not stop_claiming.is_set():
        async with pool.acquire() as conn:
            if await frontier.crawl_complete(conn):
                record_reason("drain")
                stop_claiming.set()
                return
        await _wait(stop_claiming, interval, sleep)


async def _progress(
    pool: asyncpg.Pool,
    rate_limiter: RateLimiter,
    stop_claiming: asyncio.Event,
    interval: float,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Logs the done/pending/in_progress/failed counts and the measured
    rate against the permitted one every `interval` -- measured is done's
    delta since the last tick, over `interval`; the frontier already
    counts that, nothing new to track.
    """
    last_done = 0
    while not stop_claiming.is_set():
        async with pool.acquire() as conn:
            counts = await frontier.status_counts(conn)
        done = counts.get("done", 0)
        measured, last_done = (done - last_done) / interval, done
        rate = f"{measured:.1f}/s (limit {rate_limiter.current_rate:.1f}/s)"
        stats = {s: counts.get(s, 0) for s in ("pending", "in_progress", "done", "failed")}
        logger.info("progress", extra={"context": {**stats, "rate": rate}})
        await _wait(stop_claiming, interval, sleep)


async def _drain(
    tasks: list[asyncio.Task],
    force_stop: asyncio.Event,
    grace_seconds: float,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> bool:
    """Waits for every worker task to finish on its own — the normal case,
    since `process_one` always runs to completion — up to `grace_seconds`
    or a second signal (`force_stop`), whichever comes first; past that,
    cancels whatever's still running and waits for the cancellation to land.

    Returns True if a task ended with a real exception (not CancelledError)
    — process_one already contains an ordinary bug (see DESIGN.md), so this
    means the worker loop itself broke.
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

    failed = False
    for task, result in zip(tasks, all_done.result(), strict=True):
        if not isinstance(result, BaseException) or isinstance(result, asyncio.CancelledError):
            continue
        logger.error(f"{task.get_name()} failed", exc_info=result)
        failed = True
    return failed


def _force_shutdown_on_death(
    task: asyncio.Task,
    stop_claiming: asyncio.Event,
    record_reason: Callable[[str], None],
    name: str,
) -> None:
    """A dead supervisor stops lease recovery silently, and a dead drain
    watcher stops the crawl from ever noticing it finished at all (see
    DESIGN.md) — this fires the moment either happens, not once the crawl
    stalls.
    """

    def on_done(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error(f"{name} task died; forcing shutdown", exc_info=exc)
            record_reason(f"{name} died")
            stop_claiming.set()

    task.add_done_callback(on_done)


def _install_signal_handlers(
    stop_claiming: asyncio.Event,
    force_stop: asyncio.Event,
    record_reason: Callable[[str], None],
) -> None:
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
            record_reason("signal")
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
    if config.seed_url is None:
        raise ValueError("seed_url is required to crawl")

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

        # First writer wins: whichever of drain/signal/a dead support task
        # sets stop_claiming first is the one the final log line reports.
        shutdown_reason: dict[str, str] = {}

        def record_reason(reason: str) -> None:
            shutdown_reason.setdefault("reason", reason)

        _install_signal_handlers(stop_claiming, force_stop, record_reason)

        interval = config.lease_recovery_interval_seconds or config.lease_seconds / 2

        async with FetchClient(config) as fetch_client:
            rate_limiter = RateLimiter(config)
            supervisor = asyncio.create_task(_supervise(pool, stop_claiming, interval, sleep))
            _force_shutdown_on_death(supervisor, stop_claiming, record_reason, "supervisor")
            drain_watcher = asyncio.create_task(
                _watch_drain(
                    pool, stop_claiming, config.drain_check_interval_seconds, record_reason, sleep
                )
            )
            _force_shutdown_on_death(drain_watcher, stop_claiming, record_reason, "drain watcher")
            progress = asyncio.create_task(
                _progress(
                    pool, rate_limiter, stop_claiming, config.progress_interval_seconds, sleep
                )
            )
            workers = [
                asyncio.create_task(
                    worker.run(
                        i, pool, fetch_client, rate_limiter, config, seed_host, stop_claiming, sleep
                    ),
                    name=f"worker-{i}",
                )
                for i in range(config.max_concurrency)
            ]

            await stop_claiming.wait()
            worker_failed = await _drain(workers, force_stop, config.shutdown_grace_seconds, sleep)

            support_failed = False
            support_tasks = (
                ("supervisor", supervisor),
                ("drain watcher", drain_watcher),
                ("progress", progress),
            )
            for name, task in support_tasks:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    logger.exception(f"{name} task failed")
                    support_failed = True

            async with pool.acquire() as conn:
                counts = await frontier.status_counts(conn)
            logger.info(
                "crawl stopped",
                extra={
                    "context": {
                        "reason": shutdown_reason.get("reason", "unknown"),
                        **{
                            s: counts.get(s, 0)
                            for s in ("pending", "in_progress", "done", "failed", "skipped")
                        },
                    }
                },
            )
    finally:
        await pool.close()

    return 1 if (worker_failed or support_failed) else 0
