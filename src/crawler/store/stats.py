"""Read-only aggregate queries for `crawler stats` -- nothing here writes,
and nothing in the live crawl calls it. status_counts is frontier.py's own;
reused, not duplicated.
"""

from dataclasses import dataclass
from datetime import datetime

import asyncpg

from . import frontier


@dataclass(frozen=True, slots=True)
class Stats:
    status_counts: dict[str, int]
    failure_reasons: dict[str, int]
    attempts_total: int
    urls_attempted: int
    bytes_by_type: dict[str, int]
    dedup_total: int
    dedup_distinct: int
    changed_count: int


async def gather(conn: asyncpg.Connection, since: datetime | None = None) -> Stats:
    """One read per line of the report -- small, independent aggregates
    over urls/fetch_attempts/contents, not one query trying to say
    everything. `since` narrows only `changed_count`; every other figure
    describes the crawl's state as it stands right now, not a window.
    """
    status_counts = await frontier.status_counts(conn)

    failure_rows = await conn.fetch(
        "SELECT error_kind, count(*) AS n FROM urls WHERE status = 'failed' "
        "GROUP BY error_kind ORDER BY n DESC"
    )
    failure_reasons = {r["error_kind"]: r["n"] for r in failure_rows}

    pressure = await conn.fetchrow(
        "SELECT count(*) AS attempts, count(DISTINCT url_id) AS urls FROM fetch_attempts"
    )

    bytes_rows = await conn.fetch(
        "SELECT content_type, sum(byte_size) AS n FROM contents GROUP BY content_type ORDER BY n DESC"
    )
    bytes_by_type = {r["content_type"]: r["n"] for r in bytes_rows}

    dedup = await conn.fetchrow(
        "SELECT count(*) AS total, count(DISTINCT content_hash) AS distinct_hashes "
        "FROM urls WHERE status = 'done'"
    )

    if since is None:
        changed_count = await conn.fetchval(
            "SELECT count(*) FROM urls WHERE content_changed_at IS NOT NULL"
        )
    else:
        changed_count = await conn.fetchval(
            "SELECT count(*) FROM urls WHERE content_changed_at IS NOT NULL AND content_changed_at > $1",
            since,
        )

    return Stats(
        status_counts=status_counts,
        failure_reasons=failure_reasons,
        attempts_total=pressure["attempts"],
        urls_attempted=pressure["urls"],
        bytes_by_type=bytes_by_type,
        dedup_total=dedup["total"],
        dedup_distinct=dedup["distinct_hashes"],
        changed_count=changed_count,
    )
