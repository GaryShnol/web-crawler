"""The canonical fixture graph: named URLs covering the crawl's hard paths.
build_routes() feeds app.create_app(); the constants are what tests assert on.
"""

from .app import FakeResponse, MalformedResponse
from .payloads import tiny_pdf, tiny_png, tiny_video_no_duration

SEED = "http://fixture.local/"
QUERY_ONLY = "http://fixture.local/page?token=abc123"
CYCLE_A = "http://fixture.local/a"
CYCLE_B = "http://fixture.local/b"
OFFDOMAIN_PAGE = "http://other.local/"
OFFHOST_IMAGE = "http://cdn.local/logo.png"

LYING_CONTENT_TYPE = "http://fixture.local/lying-type"
LYING_CONTENT_LENGTH = "http://fixture.local/lying-length"
MISSING_CONTENT_TYPE = "http://fixture.local/no-content-type.png"
REDIRECT = "http://fixture.local/redirected"

STATUS_404 = "http://fixture.local/missing"
STATUS_403 = "http://fixture.local/forbidden"
STATUS_500 = "http://fixture.local/broken"
STATUS_429_WITH_RETRY_AFTER = "http://fixture.local/limited-with-retry-after"
STATUS_429_WITHOUT_RETRY_AFTER = "http://fixture.local/limited-no-retry-after"
STATUS_429_THEN_SUCCESS = "http://fixture.local/limited-then-ok"
MALFORMED_ENVELOPE = "http://fixture.local/malformed"

IMAGE = "http://fixture.local/hero.png"
PDF = "http://fixture.local/doc.pdf"
VIDEO = "http://fixture.local/clip.mp4"

# A different body on each of its first three calls, then repeats its
# last -- app.py's own sequence semantics (call N -> routes[url][min(N,
# len-1)]). Exists for a revisit to have something to actually see change;
# nothing here re-triggers a fetch on its own -- that still takes an
# external nudge back to 'pending', same as test_revisit.py's.
DRIFTING = "http://fixture.local/drifting"


def _html(body: str) -> FakeResponse:
    return FakeResponse(200, {"Content-Type": "text/html"}, body.encode())


def build_routes() -> dict[str, list[FakeResponse | MalformedResponse]]:
    seed_html = (
        f'<html><body><a href="{QUERY_ONLY}">query</a>'
        f'<a href="{CYCLE_A}">cycle</a>'
        f'<a href="{OFFDOMAIN_PAGE}">offdomain</a>'
        f'<img src="{OFFHOST_IMAGE}">'
        f'<a href="{IMAGE}">image</a>'
        f'<a href="{PDF}">pdf</a>'
        f'<a href="{VIDEO}">video</a>'
        f'<a href="{DRIFTING}">drifting</a>'
        f'<a href="{MISSING_CONTENT_TYPE}">no content type</a>'
        f'<a href="{REDIRECT}">redirected</a>'
        f'<a href="{STATUS_404}">404</a>'
        f'<a href="{STATUS_403}">403</a>'
        f'<a href="{STATUS_500}">500</a>'
        f'<a href="{STATUS_429_WITH_RETRY_AFTER}">429 with retry-after</a>'
        f'<a href="{STATUS_429_THEN_SUCCESS}">429 then ok</a>'
        f'<a href="{MALFORMED_ENVELOPE}">malformed</a></body></html>'
    )
    return {
        SEED: [_html(seed_html)],
        QUERY_ONLY: [_html("<html><body>only reachable by query string</body></html>")],
        CYCLE_A: [_html(f'<html><body><a href="{CYCLE_B}">b</a></body></html>')],
        CYCLE_B: [_html(f'<html><body><a href="{CYCLE_A}">a</a></body></html>')],
        OFFDOMAIN_PAGE: [_html("<html><body>off-domain, never enqueued</body></html>")],
        OFFHOST_IMAGE: [FakeResponse(200, {"Content-Type": "image/png"}, tiny_png())],
        LYING_CONTENT_TYPE: [
            FakeResponse(200, {"Content-Type": "image/png"}, b"<html>not a png</html>")
        ],
        LYING_CONTENT_LENGTH: [
            FakeResponse(
                200, {"Content-Type": "text/html", "Content-Length": "99999"}, b"short"
            )
        ],
        STATUS_404: [FakeResponse(404, {}, None)],
        STATUS_403: [FakeResponse(403, {}, None)],
        STATUS_500: [FakeResponse(500, {}, None)],
        # Retry-After overrides the backoff formula entirely (fetch/retry.py's
        # next_attempt), so config's tiny retry_base/max_seconds can't shrink
        # this wait the way it shrinks a formula-driven one. "1" is the
        # smallest legal value -- RFC 7231's delay-seconds is 1*DIGIT, no
        # fractional seconds, and parse_retry_after's float() parse is more
        # lenient than the spec, so a value like "0.05" would parse without
        # ever proving the fixture sends something a real service would.
        STATUS_429_WITH_RETRY_AFTER: [FakeResponse(429, {"Retry-After": "1"}, None)],
        STATUS_429_WITHOUT_RETRY_AFTER: [FakeResponse(429, {}, None)],
        STATUS_429_THEN_SUCCESS: [
            FakeResponse(429, {"Retry-After": "1"}, None),
            FakeResponse(429, {}, None),
            _html("<html><body>ok on the third attempt</body></html>"),
        ],
        IMAGE: [FakeResponse(200, {"Content-Type": "image/png"}, tiny_png())],
        # No Content-Type at all -- resolve() still has to route this by
        # sniff alone, and the real bug this drives is downstream of
        # routing: contents.content_type must never end up storing this
        # None, since that column is NOT NULL (handlers/base.py's registry).
        MISSING_CONTENT_TYPE: [FakeResponse(200, {}, tiny_png())],
        # Off the documented statusCode set entirely (see CLAUDE.md) --
        # permanent by construction, not retried: see errors.py's classify()
        # and DESIGN.md for why a 3xx isn't treated like an ordinary flake.
        REDIRECT: [FakeResponse(302, {"Location": "http://fixture.local/redirect-target"}, None)],
        # Not JSON at all -- drives fetch/client.py's own envelope-parsing
        # except clause, not process_one's outer one. See classify_malformed_response.
        MALFORMED_ENVELOPE: [MalformedResponse(b"{not json")],
        PDF: [FakeResponse(200, {"Content-Type": "application/pdf"}, tiny_pdf())],
        VIDEO: [FakeResponse(200, {"Content-Type": "video/mp4"}, tiny_video_no_duration())],
        DRIFTING: [
            _html("<html><body>drifting: version one</body></html>"),
            _html("<html><body>drifting: version two</body></html>"),
            _html("<html><body>drifting: version three</body></html>"),
        ],
    }
