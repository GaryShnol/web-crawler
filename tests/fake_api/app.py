"""In-process test double for the fetch API — not an implementation of the
service, see CLAUDE.md. GET /fetch?url=<encoded> returns a JSON envelope
{statusCode, headers, body}, body encoded via crawler.models.encode_body (the
same assumed encoding fetch/client.py decodes with — see DESIGN.md for why
it's an assumption). A route is a response *sequence*: call N for a URL
returns routes[url][min(N, len-1)], so the last entry sticks once exhausted.
Fresh app per test — no reset endpoint.

Fault injection is env-driven and seeded, so a run is reproducible:
FAKE_API_SEED (default 0), FAKE_API_FAULT_RATE (default 0, 0..1 chance per
call). A triggered fault overrides the URL's own sequence for that call.

`create_app`'s optional `capture` list, when given, gets one dict of the
request headers appended per call — for tests asserting on what a client
actually sent (e.g. If-None-Match), without standing up a second fake app.
"""

import asyncio
import os
import random
from typing import NamedTuple

from aiohttp import web

from crawler.models import encode_body

_FAULT_KINDS = ("429_retry_after", "429", "500", "403", "slow")


class FakeResponse(NamedTuple):
    status_code: int
    headers: dict[str, str]
    body: bytes | None


def _fault_response(kind: str, rng: random.Random) -> FakeResponse | None:
    if kind == "429_retry_after":
        return FakeResponse(429, {"Retry-After": str(rng.randint(1, 5))}, None)
    if kind == "429":
        return FakeResponse(429, {}, None)
    if kind == "500":
        return FakeResponse(500, {}, None)
    if kind == "403":
        return FakeResponse(403, {}, None)
    return None  # "slow" is a delay, not a replacement — handled by the caller


def _envelope(resp: FakeResponse) -> dict:
    return {
        "statusCode": resp.status_code,
        "headers": resp.headers,
        "body": encode_body(resp.body),
    }


def create_app(
    routes: dict[str, list[FakeResponse]], capture: list[dict[str, str]] | None = None
) -> web.Application:
    rng = random.Random(int(os.environ.get("FAKE_API_SEED", "0")))
    fault_rate = float(os.environ.get("FAKE_API_FAULT_RATE", "0"))
    calls: dict[str, int] = {}

    async def fetch(request: web.Request) -> web.Response:
        url = request.query.get("url")
        if url is None:
            return web.Response(status=400, text="missing url")

        if capture is not None:
            capture.append(dict(request.headers))

        if rng.random() < fault_rate:
            kind = rng.choice(_FAULT_KINDS)
            fault = _fault_response(kind, rng)
            if kind == "slow":
                await asyncio.sleep(0.05)
            else:
                return web.json_response(_envelope(fault))

        sequence = routes.get(url, [FakeResponse(404, {}, None)])
        n = calls.get(url, 0)
        calls[url] = n + 1
        return web.json_response(_envelope(sequence[min(n, len(sequence) - 1)]))

    app = web.Application()
    app.router.add_get("/fetch", fetch)
    return app
