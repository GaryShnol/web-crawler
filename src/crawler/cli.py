"""Entry point. `crawl <seed>` runs the crawler against that seed url;
`stats` reports on whatever's already in the database. Every other
setting comes from the environment (`.env`, docker-compose, etc.).
"""

import argparse
import asyncio
import sys
from datetime import UTC, datetime

from .config import Config
from .engine import run
from .logging import configure_logging
from .store import db, stats


def _format_stats(report: stats.Stats, since: datetime | None) -> str:
    lines = ["status"]
    for status, count in sorted(report.status_counts.items()):
        lines.append(f"  {status:<12} {count}")

    lines.append("")
    lines.append("failure reasons (terminal only)")
    if report.failure_reasons:
        for kind, count in report.failure_reasons.items():
            lines.append(f"  {kind:<16} {count}")
    else:
        lines.append("  none")

    retried = report.attempts_total - report.urls_attempted
    lines.append("")
    lines.append(
        f"attempts: {report.attempts_total} across {report.urls_attempted} urls attempted"
        f" ({retried} retried)"
    )

    lines.append("")
    lines.append("bytes by content type")
    if report.bytes_by_type:
        for content_type, total_bytes in report.bytes_by_type.items():
            lines.append(f"  {content_type:<20} {total_bytes:,}")
    else:
        lines.append("  none")

    lines.append("")
    if report.dedup_total:
        hit_rate = 1 - report.dedup_distinct / report.dedup_total
        lines.append(
            f"dedup: {report.dedup_distinct} distinct hashes across {report.dedup_total} stored"
            f" urls ({hit_rate:.1%} hit rate)"
        )
    else:
        lines.append("dedup: no urls stored yet")

    lines.append("")
    window = f"since {since.isoformat()}" if since is not None else "ever"
    lines.append(f"content changed ({window}): {report.changed_count} urls")

    return "\n".join(lines)


async def _run_stats(config: Config, since: datetime | None) -> int:
    pool = await db.create_pool(config)
    try:
        async with pool.acquire() as conn:
            report = await stats.gather(conn, since)
    finally:
        await pool.close()
    print(_format_stats(report, since))
    return 0


def _parse_since(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="crawler")
    subparsers = parser.add_subparsers(dest="command", required=True)

    crawl = subparsers.add_parser("crawl", help="crawl a seed url")
    crawl.add_argument("seed", help="seed url to crawl")

    stats_cmd = subparsers.add_parser("stats", help="report on the current crawl state")
    stats_cmd.add_argument(
        "--since",
        type=_parse_since,
        default=None,
        help="only count content changes after this ISO-8601 timestamp",
    )

    args = parser.parse_args(argv)

    if args.command == "crawl":
        config = Config(seed_url=args.seed)
        configure_logging(config.log_level)
        return asyncio.run(run(config))

    # stats never crawls -- seed_url is a required Config field it never
    # reads, so this placeholder is all it needs.
    config = Config(seed_url="stats:unused")
    return asyncio.run(_run_stats(config, args.since))


if __name__ == "__main__":
    sys.exit(main())
