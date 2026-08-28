"""handlers/jpeg.py: JPEG sniffed by its own SOI marker, dimensions and size
read via the shared raster_metadata. Pure, no fixtures.
"""

from fake_api.payloads import tiny_gif, tiny_jpeg, tiny_pdf, tiny_png

from crawler.handlers.jpeg import JpegHandler

HANDLER = JpegHandler()


class TestSniff:
    def test_jpeg_matches(self):
        assert HANDLER.sniff(tiny_jpeg())

    def test_png_does_not_match(self):
        assert not HANDLER.sniff(tiny_png())

    def test_gif_does_not_match(self):
        assert not HANDLER.sniff(tiny_gif())

    def test_pdf_does_not_match(self):
        assert not HANDLER.sniff(tiny_pdf())

    def test_html_mislabeled_as_jpeg_does_not_match(self):
        assert not HANDLER.sniff(b"<html>not a jpeg</html>")


class TestHandle:
    def test_reads_dimensions_and_file_size(self):
        body = tiny_jpeg()
        result = HANDLER.handle(body, "http://fixture.local/photo.jpg")
        assert result.metadata == {"width": 2, "height": 3, "file_size": len(body)}
        assert result.links == []
