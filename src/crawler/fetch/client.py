"""Talks to the fetch API — never the target URL directly. See CLAUDE.md:
the API is a black box behind `GET /fetch?url=<encoded>`, unreliable and
rate-limited, returning `{statusCode, headers, body}` for whatever URL we
ask about. Retries, rate limiting, and the frontier are someone else's job;
this is url in, `FetchResult` out.
"""

import base64
import json
import math
import time
from typing import Self

import aiohttp

from ..config import Config
from ..errors import classify, classify_exception, classify_oversized_body
from ..models import FetchResponse, FetchResult, find_header

_BASE64_GROUP = 4  # base64 emits 4 output bytes per 3 input bytes
_ENVELOPE_OVERHEAD_BYTES = 8192  # room for statusCode, headers, and JSON punctuation


class _BodyTooLarge(Exception):
    """Signals the cap trip out of _read_capped with the numbers classify_oversized_body needs."""

    def __init__(self, bytes_read: int) -> None:
        self.bytes_read = bytes_read


async def _read_capped(http_response: aiohttp.ClientResponse, max_body_bytes: int) -> bytes:
    """Stream the raw response instead of buffering it whole — the point of
    the cap is to never hold more than roughly `max_body_bytes` in memory,
    and `await http_response.json()` reads the entire body first regardless
    of what a (possibly lying) Content-Length claims. Raises _BodyTooLarge,
    with the connection already closed, once the cap is exceeded.

    The body arrives base64-encoded inside a JSON envelope, so the raw bytes
    run ~4/3 larger than the asset they encode; `_ENVELOPE_OVERHEAD_BYTES`
    covers everything else in the envelope. The cap is therefore coarse, not
    byte-exact against the decoded body — the goal is bounding memory, not
    enforcing the limit to the byte.
    """
    cap = _ENVELOPE_OVERHEAD_BYTES + _BASE64_GROUP * math.ceil((max_body_bytes + 1) / 3)
    chunks: list[bytes] = []
    total = 0
    async for chunk in http_response.content.iter_chunked(65536):
        total += len(chunk)
        if total > cap:
            http_response.close()
            raise _BodyTooLarge(total)
        chunks.append(chunk)
    return b"".join(chunks)


class FetchClient:
    """Owns one `aiohttp.ClientSession` for its lifetime. The engine opens
    one of these and hands it to workers — not a module-level session, which
    a test can't close and a process can't have two of.
    """

    def __init__(self, config: Config) -> None:
        self._config = config
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> Self:
        timeout = aiohttp.ClientTimeout(
            connect=self._config.connect_timeout_seconds,
            sock_read=self._config.read_timeout_seconds,
        )
        self._session = aiohttp.ClientSession(timeout=timeout)
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        assert self._session is not None
        await self._session.close()
        self._session = None

    async def fetch(self, url: str, prev_etag: str | None = None) -> FetchResult:
        """One call to the fetch API for `url`. No retries: a failure comes
        back classified, once, for the caller to act on.
        """
        assert self._session is not None, "FetchClient must be used as `async with FetchClient(...)`"

        headers = {"If-None-Match": prev_etag} if prev_etag is not None else {}
        started = time.monotonic()

        try:
            async with self._session.get(
                self._config.fetch_api_url, params={"url": url}, headers=headers
            ) as http_response:
                raw = await _read_capped(http_response, self._config.max_body_bytes)
        except _BodyTooLarge as exc:
            classification = classify_oversized_body(self._config.max_body_bytes, exc.bytes_read)
            return FetchResult(
                outcome=classification.outcome,
                elapsed=time.monotonic() - started,
                resolved_url=None,
                response=None,
                error_kind=classification.error_kind,
                error_detail=classification.detail,
            )
        except (TimeoutError, aiohttp.ClientConnectionError, aiohttp.ClientPayloadError) as exc:
            classification = classify_exception(exc)
            return FetchResult(
                outcome=classification.outcome,
                elapsed=time.monotonic() - started,
                resolved_url=None,
                response=None,
                error_kind=classification.error_kind,
                error_detail=classification.detail,
            )

        elapsed = time.monotonic() - started

        envelope = json.loads(raw)
        encoded_body = envelope.get("body")
        # Assumed wire encoding for a `Buffer | null` body -- the real API's
        # own encoding is unverifiable (see DESIGN.md), so this is a guess:
        # base64, or null.
        body = base64.b64decode(encoded_body) if encoded_body is not None else None

        response = FetchResponse(
            status_code=envelope["statusCode"], headers=envelope.get("headers") or {}, body=body
        )
        classification = classify(response, prev_etag)

        return FetchResult(
            outcome=classification.outcome,
            elapsed=elapsed,
            resolved_url=find_header(response.headers, "Location"),
            response=response,
            error_kind=classification.error_kind,
            error_detail=classification.detail,
        )
