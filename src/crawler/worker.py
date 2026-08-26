"""One worker: claim a url, rate-limit, fetch it, route the response to a
handler, persist what came back, enqueue whatever links it found, and land
on a terminal status. Every database write for one url happens on one
connection — the claim is its own short transaction that commits the
instant it's taken (the lease has to be visible to every other worker and
to the supervisor right away), and nothing about the fetch itself holds a
transaction open. The write-up transaction opens only after the response
is back, and covers content, metadata, links, and the terminal status as
one commit or none of it.
"""

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import asyncpg

from .config import Config
from .errors import ErrorKind
from .fetch.client import FetchClient
from .fetch.rate_limiter import RateLimiter
from .fetch.retry import GiveUp, next_attempt
from .handlers import base as handlers
from .logging import bind
from .models import FetchResult, Outcome, find_header, parse_retry_after
from .store import blobs, frontier, metadata
from .url_tools import in_scope

logger = logging.getLogger(__name__)


async def _fetch(
    claimed: frontier.ClaimedUrl, fetch_client: FetchClient, rate_limiter: RateLimiter
) -> FetchResult:
    """Rate-limits, fetches, and feeds the outcome back to the shared
    limiter — a 429 is the gateway pushing back, everything else advances
    its recovery streak (see fetch/rate_limiter.py's report()).
    """
    await rate_limiter.acquire()
    result = await fetch_client.fetch(claimed.url, prev_etag=claimed.etag)

    throttled = result.error_kind is ErrorKind.RATE_LIMITED
    retry_after = None
    if throttled and result.response is not None:
        retry_after = parse_retry_after(result.response.headers, datetime.now(UTC))
    rate_limiter.report(throttled=throttled, retry_after=retry_after)

    return result


async def _persist_success(
    pool: asyncpg.Pool,
    claimed: frontier.ClaimedUrl,
    result: FetchResult,
    config: Config,
    seed_host: str,
) -> None:
    response = result.response
    assert response is not None and response.body
    content_type = find_header(response.headers, "Content-Type")
    handler = handlers.resolve(content_type, response.body)

    if handler is None:
        async with pool.acquire() as conn, conn.transaction():
            await frontier.mark_skipped(
                conn,
                claimed.id,
                claimed.lease_token,
                content_type=content_type,
                content_length=len(response.body),
            )
        logger.info("skipped: no handler matched")
        return

    handled = handler.handle(response.body, claimed.url)
    content_hash, storage_path = blobs.write(
        config.output_dir, handler.directory, handler.extension, claimed.url, response.body
    )
    in_scope_links = [
        link
        for link in handled.links
        if in_scope(link.normalized_url, seed_host, config.allow_subdomains)
    ]
    next_depth = claimed.depth + 1
    within_depth = config.max_depth is None or next_depth <= config.max_depth

    async with pool.acquire() as conn, conn.transaction():
        await metadata.insert_content(
            conn, content_hash, content_type, len(response.body), storage_path
        )
        if handled.metadata is not None:
            await metadata.insert_metadata(conn, content_hash, handler.kind, handled.metadata)
        if in_scope_links and within_depth:
            await frontier.enqueue_many(conn, in_scope_links, depth=next_depth, src_id=claimed.id)
        await frontier.mark_done(
            conn,
            claimed.id,
            claimed.lease_token,
            content_type=content_type,
            content_length=len(response.body),
            content_hash=content_hash,
            etag=find_header(response.headers, "ETag"),
        )
    logger.info("done", extra={"context": {"kind": handler.kind, "links": len(in_scope_links)}})


async def _persist_failure(
    pool: asyncpg.Pool, claimed: frontier.ClaimedUrl, result: FetchResult, config: Config
) -> None:
    assert result.error_kind is not None
    headers = result.response.headers if result.response is not None else {}
    decision = next_attempt(
        result.outcome, claimed.attempt_no, headers, datetime.now(UTC), config, jitter=random.random()
    )
    async with pool.acquire() as conn, conn.transaction():
        await frontier.mark_failed(conn, claimed.id, claimed.lease_token, decision, result.error_kind)
    verb = "failed" if isinstance(decision, GiveUp) else "retrying"
    logger.info(f"{verb}: {result.error_kind.value}")


async def process_one(
    pool: asyncpg.Pool,
    claimed: frontier.ClaimedUrl,
    *,
    fetch_client: FetchClient,
    rate_limiter: RateLimiter,
    config: Config,
    seed_host: str,
) -> None:
    """Runs one claimed url end to end. Never raises for a fetch or
    classification outcome — those all land as a terminal write; only a
    real bug (a database error, cancellation) propagates.
    """
    with bind(url=claimed.url, attempt=claimed.attempt_no):
        result = await _fetch(claimed, fetch_client, rate_limiter)

        async with pool.acquire() as conn:
            await frontier.record_attempt(
                conn,
                claimed.id,
                claimed.attempt_no,
                status_code=result.response.status_code if result.response else None,
                elapsed=result.elapsed,
                error_kind=result.error_kind,
            )

        if result.outcome is Outcome.SUCCESS:
            await _persist_success(pool, claimed, result, config, seed_host)
        elif result.outcome is Outcome.NOT_MODIFIED:
            async with pool.acquire() as conn, conn.transaction():
                await frontier.mark_unchanged(conn, claimed.id, claimed.lease_token)
            logger.info("unchanged")
        else:
            await _persist_failure(pool, claimed, result, config)


async def _wait(event: asyncio.Event, timeout: float, sleep: Callable[[float], Awaitable[None]]) -> None:
    """Waits for `event`, or `timeout` seconds via `sleep`, whichever comes
    first. A cancellable, clock-injectable stand-in for
    `asyncio.wait_for(event.wait(), timeout)`, which is bound to the real
    event loop clock and so can't be sped up in a test — see
    fetch/rate_limiter.py's `now`/`sleep` for the same reasoning.
    """
    event_wait = asyncio.ensure_future(event.wait())
    timer = asyncio.ensure_future(sleep(timeout))
    try:
        await asyncio.wait({event_wait, timer}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for future in (event_wait, timer):
            if not future.done():
                future.cancel()


async def run(
    worker_id: int,
    pool: asyncpg.Pool,
    fetch_client: FetchClient,
    rate_limiter: RateLimiter,
    config: Config,
    seed_host: str,
    stop_claiming: asyncio.Event,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """One worker task's whole life: claim one url at a time and process it
    until told to stop claiming. `limit=1` on every claim, on purpose — a
    bigger batch starts its lease clock on urls sitting in a queue for
    nothing, burning lease_seconds before a worker even looks at them.

    Cancellation (the engine's shutdown grace expiring) is only ever
    delivered while `process_one` is awaited below — a claimed row still
    `in_progress` with no terminal write ever made for it, so it's released
    back to `pending` here rather than left to lease expiry, which is
    exactly why release() exists.
    """
    with bind(worker_id=worker_id):
        while not stop_claiming.is_set():
            async with pool.acquire() as conn:
                claimed = await frontier.claim_batch(conn, 1, config)

            if not claimed:
                await _wait(stop_claiming, config.poll_interval_seconds, sleep)
                continue

            [claimed_url] = claimed
            try:
                await process_one(
                    pool,
                    claimed_url,
                    fetch_client=fetch_client,
                    rate_limiter=rate_limiter,
                    config=config,
                    seed_host=seed_host,
                )
            except asyncio.CancelledError:
                async with pool.acquire() as conn:
                    await frontier.release(conn, claimed_url.id, claimed_url.lease_token)
                raise
