"""recover_expired_leases against a real database clock.

There's no clock to inject here on purpose: the query drives off
Postgres's own now(), not this process's (see claim_batch's docstring in
frontier.py) — a crashed worker's lease goes stale on the database's
clock, not on any clock this test controls. Forcing that scenario means
backdating lease_until with a direct UPDATE, not fast-forwarding a fake
clock the way fetch/retry.py's tests do.
"""

from crawler.config import Config
from crawler.store.frontier import claim_batch, recover_expired_leases


def _config() -> Config:
    return Config(
        seed_url="http://fixture.local/",
        database_url="postgresql://unused/unused",
        fetch_api_url="http://unused/unused",
    )


class TestRecoverExpiredLeases:
    async def test_expired_lease_is_reclaimable_without_double_counting_attempts(self, pool):
        row = await pool.fetchrow(
            "INSERT INTO urls (normalized_url, raw_url) VALUES ($1, $1) RETURNING id",
            "http://fixture.local/crash",
        )
        url_id = row["id"]
        config = _config()

        [claimed] = await claim_batch(pool, 1, config)
        assert claimed.id == url_id
        assert claimed.attempt_no == 1

        # Stand-in for a worker that died mid-fetch: no exception handler
        # runs, so the lease is simply never renewed. Backdating it directly
        # is how that's simulated without waiting out lease_seconds for real.
        await pool.execute(
            "UPDATE urls SET lease_until = now() - interval '1 second' WHERE id = $1",
            url_id,
        )

        recovered = await recover_expired_leases(pool)
        assert recovered == 1

        attempts_after_recovery = await pool.fetchval(
            "SELECT attempts FROM urls WHERE id = $1", url_id
        )
        assert attempts_after_recovery == 1  # recovery itself never touches attempts

        [reclaimed] = await claim_batch(pool, 1, config)
        assert reclaimed.id == url_id
        assert reclaimed.attempt_no == 2  # bumped once, by this claim — not by recovery
