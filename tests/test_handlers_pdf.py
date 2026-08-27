"""handlers/pdf.py: sniffed by the `%PDF-` header, page count and title read
with pypdf. Pure — no I/O, so these run with no fixtures at all.
"""

from fake_api.payloads import tiny_pdf, tiny_png

from crawler.handlers.pdf import PdfHandler

HANDLER = PdfHandler()


class TestSniff:
    def test_pdf_matches(self):
        assert HANDLER.sniff(tiny_pdf())

    def test_png_does_not_match(self):
        assert not HANDLER.sniff(tiny_png())

    def test_html_mislabeled_as_pdf_does_not_match(self):
        assert not HANDLER.sniff(b"<html>not a pdf</html>")


class TestHandle:
    def test_reads_page_count_and_title(self):
        # tiny_pdf() is a single blank page with no /Title set.
        result = HANDLER.handle(tiny_pdf(), "http://fixture.local/doc.pdf")
        assert result.metadata == {"page_count": 1, "title": None}
        assert result.links == []
