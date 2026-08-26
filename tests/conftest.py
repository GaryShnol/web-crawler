"""Shared fixtures for tests that need a real Postgres — no mocked
transport, no mocked database, on purpose (see AI_WORKLOG.md's rejections:
races like claim_batch's SKIP LOCKED are exactly what a mock would hide).

Points at TEST_DATABASE_URL if set; otherwise the docker container these
sessions have been developed against (postgres:test@localhost:55432). A
clean clone with no Postgres running skips these tests rather than failing
the whole run — set TEST_DATABASE_URL to actually exercise them.
"""

import os
from pathlib import Path

import asyncpg
import pytest

from crawler.store.db import run_migrations

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://postgres:test@localhost:55432/crawler"
)


@pytest.fixture
async def pool():
    """A pool sized for real concurrency (tests spawn more than the
    asyncpg default of 10 connections), migrated, and truncated to a clean
    slate before the test runs.
    """
    try:
        db_pool = await asyncpg.create_pool(TEST_DATABASE_URL, min_size=12, max_size=12)
    except OSError as exc:
        pytest.skip(
            f"no Postgres reachable at {TEST_DATABASE_URL} ({exc}); "
            "set TEST_DATABASE_URL to run the db-backed tests"
        )
    try:
        await run_migrations(db_pool, MIGRATIONS_DIR)
        await db_pool.execute(
            "TRUNCATE TABLE fetch_attempts, links, urls, content_metadata, contents "
            "RESTART IDENTITY CASCADE"
        )
        yield db_pool
    finally:
        await db_pool.close()
