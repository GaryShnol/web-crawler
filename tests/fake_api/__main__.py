"""`python -m fake_api` serves the canonical fixture site (site.py) on
HOST/PORT -- what docker-compose's fake-api service runs. Still the same
test double (see app.py, CLAUDE.md): a stand-in for offline development,
not an implementation of the real service.
"""

import os

from aiohttp import web

from .app import create_app
from .site import build_routes


def main() -> None:
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8080"))
    web.run_app(create_app(build_routes()), host=host, port=port)


if __name__ == "__main__":
    main()
