"""handlers/image.py: PNG sniffed by its own magic bytes, dimensions and
size read with Pillow. Pure — no I/O, so these run with no fixtures at all.
"""

from fake_api.payloads import tiny_pdf, tiny_png

from crawler.handlers.image import ImageHandler

HANDLER = ImageHandler()


class TestSniff:
    def test_png_matches(self):
        assert HANDLER.sniff(tiny_png())

    def test_pdf_does_not_match(self):
        assert not HANDLER.sniff(tiny_pdf())

    def test_html_mislabeled_as_png_does_not_match(self):
        assert not HANDLER.sniff(b"<html>not a png</html>")


class TestHandle:
    def test_reads_dimensions_and_file_size(self):
        body = tiny_png()
        result = HANDLER.handle(body, "http://fixture.local/hero.png")
        assert result.metadata == {"width": 1, "height": 1, "file_size": len(body)}
        assert result.links == []
