"""HTML: title, outbound links, and asset urls, resolved against the page's
own url. `a[href]` links to the next page, in- or out-of-scope alike --
worker.py decides which get enqueued. `img[src]`, `video[src]`, `source[src]`
and `embed[src]` are leaves -- a direct download regardless of host
(CLAUDE.md's off-host-assets decision) -- so they carry `is_asset=True` for
worker.py to bypass the scope check on. `<base href>` still isn't honoured;
resolution always uses the page's own url.
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
_ASSET_SELECTOR = "img[src], video[src], source[src], embed[src]"


def _looks_like_html(body: bytes) -> bool:
    stripped = body.lstrip(b"\xef\xbb\xbf").lstrip(b" \t\n\r\x0c")
    head = stripped[:_SNIFF_WINDOW].lower()
    return any(marker in head for marker in _SNIFF_MARKERS)


@register
class HtmlHandler(Handler):
    kind = "page"
    directory = "pages"
    extension = "html"
    content_type = "text/html"

    def sniff(self, body: bytes) -> bool:
        return _looks_like_html(body)

    def handle(self, body: bytes, url: str) -> HandlerResult:
        tree = HTMLParser(body)

        links: list[DiscoveredLink] = []
        for node in tree.css("a[href]"):
            href = (node.attributes.get("href") or "").strip()
            if not href:
                continue
            anchor_text = node.text(deep=True).strip() or None
            links.append(
                DiscoveredLink(
                    raw_url=href, normalized_url=normalize(href, base=url), anchor_text=anchor_text
                )
            )
        for node in tree.css(_ASSET_SELECTOR):
            src = (node.attributes.get("src") or "").strip()
            if not src:
                continue
            links.append(
                DiscoveredLink(
                    raw_url=src,
                    normalized_url=normalize(src, base=url),
                    anchor_text=None,
                    is_asset=True,
                )
            )

        title_node = tree.css_first("title")
        title = title_node.text(deep=True).strip() if title_node is not None else None
        metadata = {"title": title or None, "link_count": len(links)}
        return HandlerResult(metadata=metadata, links=links)
