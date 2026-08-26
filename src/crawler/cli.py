"""Entry point. `crawl <seed>` runs the crawler against that seed url, every
other setting coming from the environment (`.env`, docker-compose, etc.) —
nothing else lives here yet; stats is a later step.
"""

import argparse
import asyncio
import sys

from .config import Config
from .engine import run
from .logging import configure_logging


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="crawler")
    subparsers = parser.add_subparsers(dest="command", required=True)
    crawl = subparsers.add_parser("crawl", help="crawl a seed url")
    crawl.add_argument("seed", help="seed url to crawl")
    args = parser.parse_args(argv)

    config = Config(seed_url=args.seed)
    configure_logging(config.log_level)
    return asyncio.run(run(config))


if __name__ == "__main__":
    sys.exit(main())
