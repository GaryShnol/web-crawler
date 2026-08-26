"""handlers/html.py: sniffing by magic bytes (never the declared header or
the url), and link extraction with resolution against the page's own url.
Pure — no I/O, so these run with no fixtures at all.
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

        assert result.metadata is None
        urls = {link.normalized_url for link in result.links}
        assert urls == {"http://fixture.local/a", "http://fixture.local/b"}

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
