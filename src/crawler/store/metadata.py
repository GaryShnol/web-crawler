"""Persists what a blob is and what a handler found in it — both keyed by
content_hash, so two urls that hash the same share one write, not two.
Same connection-not-pool contract as frontier.py: both functions here run
inside worker.py's per-url transaction, on the connection it passes in.
"""

import json

import asyncpg


async def insert_content(
    conn: asyncpg.Connection,
    content_hash: str,
    content_type: str,
    byte_size: int,
    storage_path: str,
) -> None:
    """Registers a blob's hash the first time it's seen. `DO NOTHING` on a
    repeat: identical bytes from a second url are already on disk under
    this hash — see store/blobs.py — so there's nothing new to record.

    `content_type` is always the matched handler's own canonical type
    (`Handler.content_type`, e.g. "text/html"), never the raw response
    header — that header can be absent or carry parameters a handler's
    sniff already looked past, and `contents.content_type` is `NOT NULL`.
    The raw header, `None` included, lands on `urls.content_type` instead
    (store/frontier.py's mark_done/mark_skipped), which is nullable for
    exactly that reason.
    """
    await conn.execute(
        """
        INSERT INTO contents (content_hash, content_type, byte_size, storage_path)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (content_hash) DO NOTHING
        """,
        content_hash,
        content_type,
        byte_size,
        storage_path,
    )


async def insert_metadata(
    conn: asyncpg.Connection, content_hash: str, kind: str, payload: dict[str, object]
) -> None:
    """Registers one handler's extraction for a hash. `DO NOTHING` on a
    repeat, same reasoning as insert_content: the same bytes extract the
    same payload deterministically, so a second url with the same hash
    never needs this redone.
    """
    await conn.execute(
        """
        INSERT INTO content_metadata (content_hash, kind, payload)
        VALUES ($1, $2, $3::jsonb)
        ON CONFLICT (content_hash) DO NOTHING
        """,
        content_hash,
        kind,
        json.dumps(payload),
    )
