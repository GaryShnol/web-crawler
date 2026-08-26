"""The canonical fixture graph: named URLs covering the crawl's hard paths.
build_routes() feeds app.create_app(); the constants are what tests assert on.
"""

from .app import FakeResponse
from .payloads import tiny_pdf, tiny_png, tiny_video_no_duration

SEED = "http://fixture.local/"
QUERY_ONLY = "http://fixture.local/page?token=abc123"
CYCLE_A = "http://fixture.local/a"
CYCLE_B = "http://fixture.local/b"
OFFDOMAIN_PAGE = "http://other.local/"
OFFHOST_IMAGE = "http://cdn.local/logo.png"

LYING_CONTENT_TYPE = "http://fixture.local/lying-type"
LYING_CONTENT_LENGTH = "http://fixture.local/lying-length"

STATUS_404 = "http://fixture.local/missing"
STATUS_403 = "http://fixture.local/forbidden"
STATUS_500 = "http://fixture.local/broken"
STATUS_429_WITH_RETRY_AFTER = "http://fixture.local/limited-with-retry-after"
STATUS_429_WITHOUT_RETRY_AFTER = "http://fixture.local/limited-no-retry-after"
STATUS_429_THEN_SUCCESS = "http://fixture.local/limited-then-ok"

IMAGE = "http://fixture.local/hero.png"
PDF = "http://fixture.local/doc.pdf"
VIDEO = "http://fixture.local/clip.mp4"


def _html(body: str) -> FakeResponse:
    return FakeResponse(200, {"Content-Type": "text/html"}, body.encode())


def build_routes() -> dict[str, list[FakeResponse]]:
    seed_html = (
        f'<html><body><a href="{QUERY_ONLY}">query</a>'
        f'<a href="{CYCLE_A}">cycle</a>'
        f'<a href="{OFFDOMAIN_PAGE}">offdomain</a>'
        f'<img src="{OFFHOST_IMAGE}">'
        f'<a href="{IMAGE}">image</a>'
        f'<a href="{PDF}">pdf</a>'
        f'<a href="{VIDEO}">video</a></body></html>'
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
        STATUS_429_WITH_RETRY_AFTER: [FakeResponse(429, {"Retry-After": "2"}, None)],
        STATUS_429_WITHOUT_RETRY_AFTER: [FakeResponse(429, {}, None)],
        STATUS_429_THEN_SUCCESS: [
            FakeResponse(429, {"Retry-After": "1"}, None),
            FakeResponse(429, {}, None),
            _html("<html><body>ok on the third attempt</body></html>"),
        ],
        IMAGE: [FakeResponse(200, {"Content-Type": "image/png"}, tiny_png())],
        PDF: [FakeResponse(200, {"Content-Type": "application/pdf"}, tiny_pdf())],
        VIDEO: [FakeResponse(200, {"Content-Type": "video/mp4"}, tiny_video_no_duration())],
    }
