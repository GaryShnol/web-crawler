"""The frontier: claim, finish, enqueue. Every write is one SQL statement —
the atomicity that matters (bump attempts and take the lease; new URLs vs
known ones) happens inside Postgres, not across a read and a write here.
See DESIGN.md for the SKIP LOCKED and snapshot-timing mechanics.

Every function here takes `conn: asyncpg.Connection`, never a `Pool` — a
`Pool` also has `.execute`/`.fetch`, which is exactly why it's the wrong
type: it would let this module silently open its own connection (and its
own implicit transaction) per call, hiding whether a write is the claim's
own short transaction or part of the caller's longer-lived one. The pool
stays at the edges (engine.py, cli.py); the caller decides which connection
a write lands on, and whether it shares one with any other write here.

Nothing here reads config.max_attempts — that's fetch/retry.py's call;
this module only writes the decision it's handed.

Every write that could land on a row this caller no longer owns — every
terminal status, and giving a lease back early — is fenced on the
`lease_token` claim_batch minted at claim time. See claim_batch's docstring
for why an id alone isn't enough.
"""

import logging
import uuid
from dataclasses import dataclass

import asyncpg

from ..config import Config
from ..errors import ErrorKind
from ..fetch.retry import GiveUp, RetryAt

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ClaimedUrl:
    id: int
    url: str
    depth: int
    attempt_no: int
    etag: str | None
    lease_token: uuid.UUID


@dataclass(frozen=True, slots=True)
class DiscoveredLink:
    raw_url: str
    normalized_url: str
    anchor_text: str | None


def _warn_if_lost_race(result: str, url_id: int, action: str) -> None:
    """A terminal write matching zero rows means this lease is no longer
    current — expired and reclaimed while this worker was still mid-flight,
    so the row now belongs to whoever holds the new lease_token. Not a
    bug: it's the race this fencing exists to lose safely instead of
    overwriting the new owner's work. Logged, not raised — there's nothing
    to retry or clean up, the row is already someone else's problem now.
    """
    if int(result.split()[-1]) == 0:
        logger.warning(f"{action}: lease race lost, no row updated (url_id={url_id})")


async def claim_batch(conn: asyncpg.Connection, limit: int, config: Config) -> list[ClaimedUrl]:
    """One statement, its own implicit transaction: SKIP LOCKED claims up to
    `limit` pending rows nobody else is touching, bumps `attempts`, sets the
    lease, and mints a fresh `lease_token` on exactly those rows in the
    same pass, committing the moment it returns — the lease has to be
    visible to every other worker and to the supervisor immediately, not
    held open behind whatever the caller does next.

    The token, not the id, is what a terminal write is fenced on:
    `recover_expired_leases` resets an expired row's status to `pending`
    without knowing whether the worker that held it is still running —
    still mid-fetch, unaware its lease is gone. That worker's own eventual
    mark_done/mark_failed would otherwise land on whatever's claimed the
    row since, silently overwriting a second worker's terminal write with
    a first worker's stale one. Minting a new token on every claim, always,
    regardless of the row's previous value, is what makes the stale
    worker's write fail to match instead.

    `pending` only — an `OR`'d-in `in_progress` branch forced a BitmapOr
    across both partial indexes, which can't preserve `next_attempt_at`
    order, so `ORDER BY` fell back to sorting every eligible row before
    `LIMIT` could cut it down (verified via EXPLAIN; see DESIGN.md). now()
    is Postgres's clock, not this process's, on purpose.
    """
    rows = await conn.fetch(
        """
        UPDATE urls
        SET status = 'in_progress',
            attempts = attempts + 1,
            lease_until = now() + make_interval(secs => $2),
            lease_token = gen_random_uuid(),
            updated_at = now()
        WHERE id IN (
            SELECT id FROM urls
            WHERE status = 'pending' AND next_attempt_at <= now()
            ORDER BY next_attempt_at
            FOR UPDATE SKIP LOCKED
            LIMIT $1
        )
        RETURNING id, normalized_url, depth, attempts, etag, lease_token
        """,
        limit,
        config.lease_seconds,
    )
    return [
        ClaimedUrl(
            id=r["id"],
            url=r["normalized_url"],
            depth=r["depth"],
            attempt_no=r["attempts"],
            etag=r["etag"],
            lease_token=r["lease_token"],
        )
        for r in rows
    ]


async def recover_expired_leases(conn: asyncpg.Connection) -> int:
    """Resets every `in_progress` row whose lease has lapsed back to
    `pending`, as its own statement run periodically — not folded into
    `claim_batch`, see its docstring. Doesn't touch `attempts`, same as
    `release`: only `claim_batch`'s own `UPDATE` ever increments it, so a
    row recovered here costs exactly one attempt on its next claim, no
    more, regardless of which of `release`/retry/this path sent it back to
    `pending`. Leaves `lease_token` alone — the stale worker still holds
    the old value in memory, and claim_batch always mints a new one on the
    next claim regardless of what's there now, which is all the fencing
    needs.
    """
    result = await conn.execute(
        """
        UPDATE urls
        SET status = 'pending', lease_until = NULL, updated_at = now()
        WHERE status = 'in_progress' AND lease_until < now()
        """
    )
    return int(result.split()[-1])


async def crawl_complete(conn: asyncpg.Connection) -> bool:
    """True once no url is `pending` or `in_progress` — the fixed point past
    which nothing could ever add more work: only an `in_progress` row's own
    commit can insert a new `pending` row (via `enqueue_many`), and if none
    are `in_progress` there's no such commit left to happen. The engine's
    supervisor polls this to recognize a finished crawl the same way it
    recognizes a signal — both end the run through the same stop event.
    """
    row = await conn.fetchrow(
        "SELECT NOT EXISTS (SELECT 1 FROM urls WHERE status IN ('pending', 'in_progress')) AS complete"
    )
    return row["complete"]


async def status_counts(conn: asyncpg.Connection) -> dict[str, int]:
    """Every status currently present in `urls`, with its row count. A
    status nothing has reached yet just doesn't appear — a caller that
    needs a fixed set (engine.py's progress line, `crawler stats`)
    defaults the ones it asks for.
    """
    rows = await conn.fetch("SELECT status, count(*) FROM urls GROUP BY status")
    return {r["status"]: r["count"] for r in rows}


async def mark_done(
    conn: asyncpg.Connection,
    url_id: int,
    lease_token: uuid.UUID,
    *,
    content_type: str | None,
    content_length: int | None,
    content_hash: str | None,
    etag: str | None,
) -> str | None:
    """Records what landed, and returns the row's own `content_hash` from
    immediately before this write — `None` on a url's first successful
    fetch, the prior hash on a revisit. The caller uses that to tell a
    first fetch from a revisit, and (comparing it to `content_hash`) an
    unchanged revisit from a changed one, entirely from this return value
    — no second query.

    `content_changed_at` moves to now() in the same statement, exactly
    when that prior hash is non-null and differs from the new one. A CTE
    reads the pre-update row once; the UPDATE's own SET could compare
    against it unqualified without one (Postgres evaluates SET against the
    pre-update row), but RETURNING only ever sees the row after — so
    getting the prior hash *out* needs the CTE regardless.

    `prior` is a snapshot taken at the start of this statement, in
    principle stale by the time the UPDATE locks the row — but that
    window can't actually land a wrong comparison here. `content_hash` is
    only ever written by this function, always gated on `lease_token`
    matching, and a given token is only ever valid for one caller at a
    time (freshly minted, once, by the claim_batch call that handed it
    out). Nothing else can be concurrently writing `content_hash` on this
    row under the same token while this statement runs, so the value
    `prior` captured is still exactly what's about to be overwritten.
    """
    row = await conn.fetchrow(
        """
        WITH prior AS (
            SELECT content_hash FROM urls WHERE id = $1 AND lease_token = $2
        )
        UPDATE urls
        SET status = 'done', content_type = $3, content_length = $4,
            content_hash = $5, etag = $6,
            content_changed_at = CASE
                WHEN prior.content_hash IS NOT NULL AND prior.content_hash IS DISTINCT FROM $5
                    THEN now()
                ELSE urls.content_changed_at
            END,
            lease_until = NULL, last_seen_at = now(), updated_at = now()
        FROM prior
        WHERE urls.id = $1 AND urls.lease_token = $2
        RETURNING prior.content_hash AS previous_hash
        """,
        url_id,
        lease_token,
        content_type,
        content_length,
        content_hash,
        etag,
    )
    if row is None:
        logger.warning(f"mark_done: lease race lost, no row updated (url_id={url_id})")
        return None
    return row["previous_hash"]


async def mark_unchanged(conn: asyncpg.Connection, url_id: int, lease_token: uuid.UUID) -> None:
    """A conditional hit: empty body, `ETag` matching what's already on the
    row. `content_type`/`content_length`/`content_hash`/`etag` from the
    prior visit are still correct, so only `last_seen_at` moves — see
    CLAUDE.md's conditional-request decision.
    """
    result = await conn.execute(
        """
        UPDATE urls
        SET status = 'done', lease_until = NULL, last_seen_at = now(), updated_at = now()
        WHERE id = $1 AND lease_token = $2
        """,
        url_id,
        lease_token,
    )
    _warn_if_lost_race(result, url_id, "mark_unchanged")


async def mark_skipped(
    conn: asyncpg.Connection,
    url_id: int,
    lease_token: uuid.UUID,
    *,
    content_type: str | None,
    content_length: int | None,
) -> None:
    """A url whose content landed outside every registered handler — not a
    failure (DESIGN.md: "unmatched content type"), so no error_kind, no
    retry, no body written. What was observed still lands on the row even
    though nothing was stored.
    """
    result = await conn.execute(
        """
        UPDATE urls
        SET status = 'skipped', content_type = $3, content_length = $4,
            lease_until = NULL, last_seen_at = now(), updated_at = now()
        WHERE id = $1 AND lease_token = $2
        """,
        url_id,
        lease_token,
        content_type,
        content_length,
    )
    _warn_if_lost_race(result, url_id, "mark_skipped")


async def mark_failed(
    conn: asyncpg.Connection,
    url_id: int,
    lease_token: uuid.UUID,
    decision: GiveUp | RetryAt,
    error_kind: ErrorKind,
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

    result = await conn.execute(
        """
        UPDATE urls
        SET status = $3,
            next_attempt_at = COALESCE($4, next_attempt_at),
            lease_until = NULL,
            error_kind = $5,
            error_message = $6,
            updated_at = now()
        WHERE id = $1 AND lease_token = $2
        """,
        url_id,
        lease_token,
        status,
        next_attempt_at,
        error_kind.value,
        error_message,
    )
    _warn_if_lost_race(result, url_id, "mark_failed")


async def record_attempt(
    conn: asyncpg.Connection,
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
    it doesn't need their atomicity, and doesn't need lease_token fencing
    either — an attempt log entry from a stale worker is still an accurate
    record of what that attempt actually did, unlike a terminal write.
    """
    await conn.execute(
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


async def release(conn: asyncpg.Connection, url_id: int, lease_token: uuid.UUID) -> None:
    """Gives back a claimed row immediately, for a worker that took a lease
    and never acted on it. Doesn't touch `attempts`.
    """
    result = await conn.execute(
        """
        UPDATE urls
        SET status = 'pending', lease_until = NULL, updated_at = now()
        WHERE id = $1 AND status = 'in_progress' AND lease_token = $2
        """,
        url_id,
        lease_token,
    )
    _warn_if_lost_race(result, url_id, "release")


async def enqueue_many(
    conn: asyncpg.Connection, links: list[DiscoveredLink], depth: int, src_id: int | None = None
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

    await conn.execute(
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
    rows = await conn.fetch(
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
