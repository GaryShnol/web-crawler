"""handlers/html.py: sniffing by magic bytes (never the declared header or
the url), link extraction (both `a[href]` and asset `src`) with resolution
against the page's own url, and the title/link_count metadata. Pure — no
I/O, so these run with no fixtures at all.
"""

from fake_api.payloads import tiny_pdf, tiny_png

from crawler.handlers.html import HtmlHandler

HANDLER = HtmlHandler()


class TestSniff:
    def test_plain_html_matches(self):
        assert HANDLER.sniff(b"<html><body>hi</body></html>")

    def test_doctype_prefix_matches(self):
        assert HANDLER.sniff(b"<!DOCTYPE html>\n<html><body>hi</body></html>")

    def test_leading_whitespace_and_bom_are_skipped(self):
        assert HANDLER.sniff(b"\xef\xbb\xbf\n  <html><body>hi</body></html>")

    def test_mislabeled_binary_does_not_match(self):
        # site.py's LYING_CONTENT_TYPE fixture inverted: real bytes, wrong
        # declared type is fetch/client.py's problem, not sniff's — this is
        # the html handler correctly staying out of someone else's content.
        assert not HANDLER.sniff(tiny_png())
        assert not HANDLER.sniff(tiny_pdf())

    def test_html_mislabeled_as_something_else_still_matches(self):
        # The actual "lying header" case: magic bytes are what routes it,
        # not the Content-Type the response declared.
        assert HANDLER.sniff(b"<html>not a png</html>")


class TestHandle:
    def test_extracts_absolute_and_relative_links(self):
        body = (
            b'<html><body>'
            b'<a href="http://fixture.local/a">a</a>'
            b'<a href="/b">b</a>'
            b"</body></html>"
        )
        result = HANDLER.handle(body, "http://fixture.local/page")

        urls = {link.normalized_url for link in result.links}
        assert urls == {"http://fixture.local/a", "http://fixture.local/b"}
        assert result.metadata["link_count"] == 2

    def test_anchor_text_is_captured_and_stripped(self):
        body = b'<html><body><a href="http://fixture.local/a">  hello world  </a></body></html>'
        [link] = HANDLER.handle(body, "http://fixture.local/").links
        assert link.anchor_text == "hello world"

    def test_empty_anchor_text_is_none(self):
        body = b'<html><body><a href="http://fixture.local/a"></a></body></html>'
        [link] = HANDLER.handle(body, "http://fixture.local/").links
        assert link.anchor_text is None

    def test_anchor_without_href_is_skipped(self):
        body = b'<html><body><a name="top">no href</a></body></html>'
        assert HANDLER.handle(body, "http://fixture.local/").links == []

    def test_relative_link_resolved_against_the_page_url_not_a_base_tag(self):
        # <base href> support is a later step — resolution always uses the
        # page's own url, even when a <base> tag is present.
        body = (
            b'<base href="http://other.local/">'
            b'<a href="child">c</a>'
        )
        [link] = HANDLER.handle(body, "http://fixture.local/dir/page").links
        assert link.normalized_url == "http://fixture.local/dir/child"

    def test_fragment_only_href_resolves_to_the_page_without_the_fragment(self):
        body = b'<a href="#section">jump</a>'
        [link] = HANDLER.handle(body, "http://fixture.local/page").links
        assert link.normalized_url == "http://fixture.local/page"

    def test_raw_url_keeps_the_original_href(self):
        body = b'<a href="/b?x=1">b</a>'
        [link] = HANDLER.handle(body, "http://fixture.local/page").links
        assert link.raw_url == "/b?x=1"
        assert link.normalized_url == "http://fixture.local/b?x=1"

    def test_href_link_is_not_an_asset(self):
        [link] = HANDLER.handle(b'<a href="/a">a</a>', "http://fixture.local/").links
        assert link.is_asset is False

    def test_asset_src_urls_are_extracted_as_links(self):
        body = (
            b"<html><body>"
            b'<img src="/logo.png">'
            b'<video src="/clip.mp4"></video>'
            b'<source src="/clip.webm">'
            b'<embed src="/doc.pdf">'
            b"</body></html>"
        )
        result = HANDLER.handle(body, "http://fixture.local/page")
        urls = {link.normalized_url for link in result.links}
        assert urls == {
            "http://fixture.local/logo.png",
            "http://fixture.local/clip.mp4",
            "http://fixture.local/clip.webm",
            "http://fixture.local/doc.pdf",
        }
        assert all(link.is_asset for link in result.links)
        assert all(link.anchor_text is None for link in result.links)

    def test_asset_without_src_is_skipped(self):
        assert HANDLER.handle(b"<html><body><img></body></html>", "http://fixture.local/").links == []

    def test_offhost_asset_src_is_still_extracted(self):
        # Host restriction is worker.py's call (is_asset bypasses it there) --
        # the handler extracts every asset src regardless of host.
        [link] = HANDLER.handle(
            b'<img src="http://cdn.local/logo.png">', "http://fixture.local/"
        ).links
        assert link.normalized_url == "http://cdn.local/logo.png"
        assert link.is_asset is True


class TestMetadata:
    def test_title_and_link_count(self):
        body = (
            b"<html><head><title>  Example Page  </title></head>"
            b'<body><a href="/a">a</a><img src="/b.png"></body></html>'
        )
        result = HANDLER.handle(body, "http://fixture.local/")
        assert result.metadata == {"title": "Example Page", "link_count": 2}

    def test_missing_title_is_none(self):
        body = b"<html><body>no title here</body></html>"
        result = HANDLER.handle(body, "http://fixture.local/")
        assert result.metadata["title"] is None

    def test_empty_title_is_none(self):
        body = b"<html><head><title></title></head><body></body></html>"
        result = HANDLER.handle(body, "http://fixture.local/")
        assert result.metadata["title"] is None

    def test_page_with_no_links_has_zero_link_count(self):
        result = HANDLER.handle(b"<html><body>no links</body></html>", "http://fixture.local/")
        assert result.metadata["link_count"] == 0
