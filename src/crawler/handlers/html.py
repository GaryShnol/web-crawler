"""HTML: extracts outbound `a[href]` links only, this session — no title,
no `content_metadata` payload, no `<base href>` support. Those, and the
other three handlers, come in later steps (see CLAUDE.md). Link resolution
always uses the page's own url as the base.
"""

from selectolax.parser import HTMLParser

from ..store.frontier import DiscoveredLink
from ..url_tools import normalize
from .base import Handler, HandlerResult, register

# HTML has no fixed magic number the way a PNG or a PDF does, so this is a
# WHATWG-mimesniff-lite check: after a BOM and leading ASCII whitespace,
# does a recognizable tag open near the start? Good enough to catch both a
# genuine page and a body mislabeled as something else (site.py's
# LYING_CONTENT_TYPE fixture is exactly this), without scanning the whole
# body for a coincidental "<html" anywhere in it.
_SNIFF_MARKERS = (b"<!doctype html", b"<html", b"<head", b"<body")
_SNIFF_WINDOW = 512


def _looks_like_html(body: bytes) -> bool:
    stripped = body.lstrip(b"\xef\xbb\xbf").lstrip(b" \t\n\r\x0c")
    head = stripped[:_SNIFF_WINDOW].lower()
    return any(marker in head for marker in _SNIFF_MARKERS)


@register("text/html")
class HtmlHandler(Handler):
    kind = "page"
    directory = "pages"
    extension = "html"

    def sniff(self, body: bytes) -> bool:
        return _looks_like_html(body)

    def handle(self, body: bytes, url: str) -> HandlerResult:
        links: list[DiscoveredLink] = []
        for node in HTMLParser(body).css("a[href]"):
            href = (node.attributes.get("href") or "").strip()
            if not href:
                continue
            anchor_text = node.text(deep=True).strip() or None
            links.append(
                DiscoveredLink(
                    raw_url=href, normalized_url=normalize(href, base=url), anchor_text=anchor_text
                )
            )
        return HandlerResult(metadata=None, links=links)
