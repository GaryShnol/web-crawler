"""The connection pool, and a migration runner guarded by a session-level
advisory lock — under `docker compose up`, the crawler container can start
more than one process against a fresh database, and only one of them should
run `CREATE TABLE`. The loser blocks on the lock, not on a duplicate-object
error.
"""

import logging
from pathlib import Path

import asyncpg

from ..config import Config
from ..logging import bind

logger = logging.getLogger(__name__)

# Arbitrary, fixed for this application's lifetime — pg_advisory_lock keys
# are just a shared namespace both sides have to agree on ahead of time.
_MIGRATION_LOCK_KEY = 84_902_113_775

_SCHEMA_MIGRATIONS_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename   TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


async def create_pool(config: Config) -> asyncpg.Pool:
    return await asyncpg.create_pool(config.database_url)


async def run_migrations(pool: asyncpg.Pool, migrations_dir: Path) -> None:
    """Applies every `*.sql` file in `migrations_dir`, in filename order,
    that isn't already recorded in `schema_migrations`. Holds one connection
    for the advisory lock's whole session so a concurrent caller genuinely
    blocks until this one releases it, rather than racing to create
    `schema_migrations` itself.
    """
    async with pool.acquire() as conn:
        await conn.execute("SELECT pg_advisory_lock($1)", _MIGRATION_LOCK_KEY)
        try:
            await conn.execute(_SCHEMA_MIGRATIONS_TABLE)
            applied = {
                row["filename"]
                for row in await conn.fetch("SELECT filename FROM schema_migrations")
            }

            for path in sorted(migrations_dir.glob("*.sql")):
                if path.name in applied:
                    continue
                async with conn.transaction():
                    await conn.execute(path.read_text(encoding="utf-8"))
                    await conn.execute(
                        "INSERT INTO schema_migrations (filename) VALUES ($1)", path.name
                    )
                with bind(migration=path.name):
                    logger.info("applied migration")
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", _MIGRATION_LOCK_KEY)
