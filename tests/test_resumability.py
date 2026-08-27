"""SIGKILL resumability: crash a real crawler subprocess mid-crawl, restart
it against the same database, and prove nothing is lost, nothing already
done gets re-fetched, and whatever was in flight gets picked back up --
not just that the code contains a lease-recovery path, but that it
actually resumes.

`proc.kill()` is SIGKILL on POSIX and `TerminateProcess` on Windows -- both
are the harsh, uncatchable kill this test wants, not the graceful one, and
that's verified empirically rather than assumed: a probe child with a
`finally`, an `atexit` handler, and a `signal.signal(SIGTERM, ...)` handler,
killed the same way this test kills, ran none of them on this platform. No
skipif, no POSIX-only path -- one test, same as everything else here.

This is the only test in the suite that runs the crawler as a real OS
process rather than in-process asyncio. Nothing here can inject a clock into
it: `retry_base_seconds`/`retry_max_seconds`/`lease_seconds`/
`lease_recovery_interval_seconds` are all real wall-clock waits inside the
subprocess, tuned small in `_ENV` for exactly that reason.
"""

import asyncio
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from aiohttp.test_utils import TestServer
from conftest import TEST_DATABASE_URL
from fake_api import site
from fake_api.app import create_app

from crawler.config import Config

_REPO_ROOT = Path(__file__).resolve().parent.parent
_MAX_ATTEMPTS = Config(database_url="unused", fetch_api_url="unused").max_attempts

# These four are the fixture graph's own designed-to-retry routes -- each
# occupies a worker's lease for a real backoff or Retry-After wait, seconds
# long, versus a normal url's lease being held for one HTTP round trip. That
# makes them more likely, not less, to still be in_progress at an arbitrary
# kill moment, purely because they hold their lease longer. A url from this
# set doesn't get "retried exactly once" after a crash -- it may have several
# of its own retry attempts left, by design -- so it gets the weaker, still
# true assertion below instead.
_RETRYING_ROUTES = frozenset(
    {site.STATUS_500, site.MALFORMED_ENVELOPE, site.STATUS_429_WITH_RETRY_AFTER, site.STATUS_429_THEN_SUCCESS}
)

# Reachable from SEED per site.build_routes(): everything the seed page (or
# a page it leads to) links, minus OFFDOMAIN_PAGE (filtered by in_scope
# before enqueue) and STATUS_429_WITHOUT_RETRY_AFTER (not linked at all).
_REACHABLE_URL_COUNT = 17
_DONE_THRESHOLD = 7  # ~40% of _REACHABLE_URL_COUNT


@asynccontextmanager
async def _running(routes):
    server = TestServer(create_app(routes))
    await server.start_server()
    try:
        yield server
    finally:
        await server.close()


def _env(fetch_api_url: str, output_dir: Path) -> dict:
    return {
        **os.environ,
        "PYTHONPATH": str(_REPO_ROOT / "src"),  # crawler isn't installed; pytest's own pythonpath ini option is the only reason `import crawler` works anywhere else in this suite
        "DATABASE_URL": TEST_DATABASE_URL,
        "FETCH_API_URL": fetch_api_url,
        "OUTPUT_DIR": str(output_dir),
        "MAX_CONCURRENCY": "8",
        # Production defaults (120s / 60s) would make the second run wait up
        # to two real minutes for the killed run's stale lease to even
        # become eligible for recovery -- load-bearing for this test
        # finishing at all, not a speed tuning. LEASE_SECONDS=6, not smaller:
        # found by running this test repeatedly and watching it flake --
        # under this test's real concurrent load (8 real workers, a real
        # subprocess, real network hops through one single-threaded fake
        # server), a live worker's own genuine processing time occasionally
        # exceeds 2s. recover_expired_leases doesn't know the difference
        # between that and a dead one, so a too-short lease gets reclaimed
        # out from under a worker that's still actively, correctly working
        # it -- a live double-claim race with nothing crashed at all, and a
        # false positive for exactly the resumption story this test exists
        # to check. Don't shrink this without re-running the suite enough
        # times to see the flake again.
        "LEASE_SECONDS": "6",
        "LEASE_RECOVERY_INTERVAL_SECONDS": "1",
        "RETRY_BASE_SECONDS": "0.01",
        "RETRY_MAX_SECONDS": "0.05",
        "DRAIN_CHECK_INTERVAL_SECONDS": "0.1",
        "POLL_INTERVAL_SECONDS": "0.05",
        "LOG_LEVEL": "INFO",
    }


def _spawn(env: dict) -> subprocess.Popen:
    # seed is a CLI arg, not SEED_URL: cli.py builds Config(seed_url=args.seed)
    # explicitly, which wins over any env var for that field regardless.
    return subprocess.Popen(
        [sys.executable, "-m", "crawler.cli", "crawl", site.SEED], cwd=_REPO_ROOT, env=env
    )


async def _wait_then_kill(pool, proc: subprocess.Popen, timeout: float) -> dict[str, tuple[str, int]]:
    """Polls real DB state in a tight cooperative loop -- no fixed sleep --
    until there's genuine progress (>= _DONE_THRESHOLD done) AND at least one
    non-retrying url is actually in flight, so the one assertion that cares
    about "in flight at kill time" is never vacuously satisfied by only the
    four slow routes happening to be what's still running. Kills right here,
    as the very next statement after the snapshot query that decided to --
    not returned up through an asyncio.wait_for wrapper first -- because the
    subprocess keeps running in real time regardless of what this test's own
    event loop does next; every extra `await` between "snapshot says X is
    in_progress" and the actual kill is real wall-clock time for that url to
    finish on its own and stop being true. Not eliminated, only minimized:
    the per-url assertions downstream have to tolerate "it raced to
    completion anyway" as a real, non-buggy outcome.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        assert proc.poll() is None, "crawler exited before the kill trigger ever fired"
        assert asyncio.get_running_loop().time() < deadline, "kill trigger condition never fired"
        rows = await pool.fetch("SELECT normalized_url, status, attempts FROM urls")
        snapshot = {r["normalized_url"]: (r["status"], r["attempts"]) for r in rows}
        done = sum(1 for status, _ in snapshot.values() if status == "done")
        has_simple_in_progress = any(
            status == "in_progress" and url not in _RETRYING_ROUTES for url, (status, _) in snapshot.items()
        )
        if done >= _DONE_THRESHOLD and has_simple_in_progress:
            proc.kill()
            return snapshot
        await asyncio.sleep(0)


async def _final_state(pool) -> dict[str, tuple[str, int]]:
    rows = await pool.fetch("SELECT normalized_url, status, attempts FROM urls")
    return {r["normalized_url"]: (r["status"], r["attempts"]) for r in rows}


async def _wait_for_exit(proc: subprocess.Popen, timeout: float) -> int:
    """Async stand-in for Popen.wait(). A blocking wait here would freeze
    this process's own event loop -- the one running the fake_api TestServer
    the subprocess is trying to reach -- so every fetch it makes while this
    test is "waiting" would go unanswered. Found by running this test for
    real: the first version called .wait() directly, and the second run
    failed every url with connection_error, not because of anything in
    src/, but because the server had no event loop left to answer on.
    """

    async def _poll() -> int:
        while proc.poll() is None:
            await asyncio.sleep(0)
        return proc.returncode

    return await asyncio.wait_for(_poll(), timeout=timeout)


class TestSigkillResumability:
    async def test_kill_mid_crawl_then_resume_loses_nothing(self, pool, tmp_path):
        routes = site.build_routes()

        # One fake_api server for both runs, deliberately: it's the same
        # remote service across the crash, and its per-url call sequences
        # (STATUS_429_THEN_SUCCESS's third-call recovery, in particular)
        # should keep advancing exactly as they would against a real one --
        # a fresh server per run would silently reset that state.
        async with _running(routes) as server:
            env = _env(str(server.make_url("/fetch")), tmp_path)

            first = _spawn(env)
            try:
                snapshot = await _wait_then_kill(pool, first, timeout=30)
            finally:
                if first.poll() is None:
                    first.kill()
            first_returncode = await _wait_for_exit(first, timeout=10)
            # -9 on POSIX, 1 (TerminateProcess) on Windows -- the number is
            # platform-specific, "didn't exit cleanly" isn't.
            assert first_returncode != 0

            second = _spawn(env)
            second_returncode = await _wait_for_exit(second, timeout=60)
            assert second_returncode == 0

        final = await _final_state(pool)

        # Nothing lost: every url known at kill time is still known.
        assert set(final) >= set(snapshot)

        # Fully drained: the second run didn't just exit, it finished.
        statuses = [status for status, _ in final.values()]
        assert statuses.count("pending") == 0
        assert statuses.count("in_progress") == 0

        saw_exact_retry = False
        for url, (status_at_kill, attempts_at_kill) in snapshot.items():
            final_status, final_attempts = final[url]

            if status_at_kill == "done":
                # Already done at kill time: untouched, not re-fetched.
                assert final_status == "done"
                assert final_attempts == attempts_at_kill
                continue

            if status_at_kill == "in_progress":
                assert final_status in ("done", "failed", "skipped")
                if url in _RETRYING_ROUTES:
                    # Weaker, still-true claim: it may have needed several
                    # more of its own retries, but never more than the
                    # policy allows.
                    assert final_attempts <= _MAX_ATTEMPTS
                else:
                    # Snapshotting "in_progress" and calling kill() aren't
                    # the same instant -- the subprocess keeps running in
                    # real time between them (see _wait_then_kill), so this
                    # url may have already finished for real just before the
                    # kill landed (attempts unchanged, no recovery needed
                    # for this one) or genuinely been caught mid-flight
                    # (attempts bumped by exactly the one extra claim that
                    # reclaimed it). Anything else -- more than one extra
                    # claim, or never reaching a terminal status -- would
                    # mean the crash cost this url more than the single
                    # wasted attempt it's allowed to cost.
                    assert final_attempts in (attempts_at_kill, attempts_at_kill + 1)
                    if final_attempts == attempts_at_kill + 1:
                        saw_exact_retry = True

        # The kill trigger guarantees at least one non-retrying url was
        # in_progress at the snapshot moment; this proves at least one of
        # them was still genuinely incomplete when the kill actually landed,
        # not just racing to finish anyway -- otherwise this run never
        # touched lease recovery at all and would trivially pass even with
        # that path broken.
        assert saw_exact_retry
