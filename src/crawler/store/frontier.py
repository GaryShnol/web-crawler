"""The frontier: claim, finish, enqueue. Every write is one SQL statement —
the atomicity that matters (bump attempts and take the lease; new URLs vs
known ones) happens inside Postgres, not across a read and a write here.
See DESIGN.md for the SKIP LOCKED and snapshot-timing mechanics.

Nothing here reads config.max_attempts — that's fetch/retry.py's call;
this module only writes the decision it's handed.
"""

from dataclasses import dataclass

import asyncpg

from ..config import Config
from ..errors import ErrorKind
from ..fetch.retry import GiveUp, RetryAt


@dataclass(frozen=True, slots=True)
class ClaimedUrl:
    id: int
    url: str
    attempt_no: int
    etag: str | None


@dataclass(frozen=True, slots=True)
class DiscoveredLink:
    raw_url: str
    normalized_url: str
    anchor_text: str | None


async def claim_batch(pool: asyncpg.Pool, limit: int, config: Config) -> list[ClaimedUrl]:
    """One statement: SKIP LOCKED claims up to `limit` pending rows nobody
    else is touching and bumps `attempts`/sets the lease on exactly those
    rows in the same pass. `pending` only — an `OR`'d-in `in_progress`
    branch forced a BitmapOr across both partial indexes, which can't
    preserve `next_attempt_at` order, so `ORDER BY` fell back to sorting
    every eligible row before `LIMIT` could cut it down (verified via
    EXPLAIN; see DESIGN.md). Recovering a dead worker's row is
    `recover_expired_leases`'s job, not this statement's. now() is
    Postgres's clock, not this process's, on purpose.
    """
    rows = await pool.fetch(
        """
        UPDATE urls
        SET status = 'in_progress',
            attempts = attempts + 1,
            lease_until = now() + make_interval(secs => $2),
            updated_at = now()
        WHERE id IN (
            SELECT id FROM urls
            WHERE status = 'pending' AND next_attempt_at <= now()
            ORDER BY next_attempt_at
            FOR UPDATE SKIP LOCKED
            LIMIT $1
        )
        RETURNING id, normalized_url, attempts, etag
        """,
        limit,
        config.lease_seconds,
    )
    return [
        ClaimedUrl(id=r["id"], url=r["normalized_url"], attempt_no=r["attempts"], etag=r["etag"])
        for r in rows
    ]


async def recover_expired_leases(pool: asyncpg.Pool) -> int:
    """Resets every `in_progress` row whose lease has lapsed back to
    `pending`, as its own statement run periodically — not folded into
    `claim_batch`, see its docstring. Doesn't touch `attempts`, same as
    `release`: only `claim_batch`'s own statement ever increments it, so a
    row recovered here costs exactly one attempt on its next claim, no
    more, regardless of which of `release`/retry/this path sent it back to
    `pending`.
    """
    result = await pool.execute(
        """
        UPDATE urls
        SET status = 'pending', lease_until = NULL, updated_at = now()
        WHERE status = 'in_progress' AND lease_until < now()
        """
    )
    return int(result.split()[-1])


async def mark_done(
    pool: asyncpg.Pool,
    url_id: int,
    *,
    content_type: str | None,
    content_length: int | None,
    content_hash: str | None,
    etag: str | None,
) -> None:
    """Records what landed. Whether `content_hash` matches a prior visit is
    the caller's comparison to make first; this only writes the result.
    """
    await pool.execute(
        """
        UPDATE urls
        SET status = 'done', content_type = $2, content_length = $3,
            content_hash = $4, etag = $5, lease_until = NULL,
            last_seen_at = now(), updated_at = now()
        WHERE id = $1
        """,
        url_id,
        content_type,
        content_length,
        content_hash,
        etag,
    )


async def mark_failed(
    pool: asyncpg.Pool, url_id: int, decision: GiveUp | RetryAt, error_kind: ErrorKind,
    error_message: str | None = None,
) -> None:
    """Writes the retry decision fetch/retry.py already made — GiveUp ends
    the row at 'failed', RetryAt sends it back to 'pending'. Doesn't touch
    `attempts` — only claim_batch's statement ever increments it.
    """
    if isinstance(decision, GiveUp):
        status, next_attempt_at = "failed", None
    else:
        status, next_attempt_at = "pending", decision.at

    await pool.execute(
        """
        UPDATE urls
        SET status = $2,
            next_attempt_at = COALESCE($3, next_attempt_at),
            lease_until = NULL,
            error_kind = $4,
            error_message = $5,
            updated_at = now()
        WHERE id = $1
        """,
        url_id,
        status,
        next_attempt_at,
        error_kind.value,
        error_message,
    )


async def record_attempt(
    pool: asyncpg.Pool,
    url_id: int,
    attempt_no: int,
    *,
    status_code: int | None,
    elapsed: float,
    error_kind: ErrorKind | None = None,
) -> None:
    """One row per fetch attempt — status_code and duration_ms, for
    diagnosing which attempt did what. Deliberately its own statement, not
    folded into mark_done/mark_failed: it's diagnostic, not crawl state, so
    it doesn't need their atomicity. A crash between the two calls loses a
    log row, never correctness.
    """
    await pool.execute(
        """
        INSERT INTO fetch_attempts (url_id, attempt_no, status_code, duration_ms, error_kind)
        VALUES ($1, $2, $3, $4, $5)
        """,
        url_id,
        attempt_no,
        status_code,
        round(elapsed * 1000),
        error_kind.value if error_kind else None,
    )


async def release(pool: asyncpg.Pool, url_id: int) -> None:
    """Gives back a claimed row immediately, for a worker that took a lease
    and never acted on it. Doesn't touch `attempts`.
    """
    await pool.execute(
        """
        UPDATE urls
        SET status = 'pending', lease_until = NULL, updated_at = now()
        WHERE id = $1 AND status = 'in_progress'
        """,
        url_id,
    )


async def enqueue_many(
    pool: asyncpg.Pool, links: list[DiscoveredLink], depth: int, src_id: int | None = None
) -> dict[str, int]:
    """Inserts whichever `links` aren't already known, records an edge from
    `src_id` to every one of them — new or already known — and returns
    every url's id. Two statements, not one: see DESIGN.md — a single
    INSERT-then-read CTE can miss a row a concurrent transaction commits
    mid-statement.
    """
    normalized = [link.normalized_url for link in links]
    raw = [link.raw_url for link in links]
    anchors = [link.anchor_text for link in links]

    await pool.execute(
        """
        INSERT INTO urls (normalized_url, raw_url, depth)
        SELECT DISTINCT ON (normalized_url) normalized_url, raw_url, $3
        FROM unnest($1::text[], $2::text[]) AS t (normalized_url, raw_url)
        ON CONFLICT (normalized_url) DO NOTHING
        """,
        normalized,
        raw,
        depth,
    )
    rows = await pool.fetch(
        """
        WITH input AS (
            SELECT * FROM unnest($1::text[], $2::text[]) AS t (normalized_url, anchor_text)
        ),
        resolved AS (
            SELECT DISTINCT ON (u.normalized_url) u.id, u.normalized_url
            FROM urls u JOIN input i ON i.normalized_url = u.normalized_url
        ),
        links_ins AS (
            INSERT INTO links (src_id, dst_id, anchor_text)
            SELECT $3, r.id, i.anchor_text
            FROM input i JOIN resolved r ON r.normalized_url = i.normalized_url
            WHERE $3::bigint IS NOT NULL
            ON CONFLICT (src_id, dst_id) DO NOTHING
        )
        SELECT id, normalized_url FROM resolved
        """,
        normalized,
        anchors,
        src_id,
    )
    return {row["normalized_url"]: row["id"] for row in rows}
