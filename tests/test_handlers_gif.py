"""handlers/gif.py: GIF sniffed by its own GIF87a/GIF89a header, dimensions
and size read via the shared raster_metadata. Pure, no fixtures.
"""

from fake_api.payloads import tiny_gif, tiny_jpeg, tiny_pdf, tiny_png

from crawler.handlers.gif import GifHandler

HANDLER = GifHandler()


class TestSniff:
    def test_gif_matches(self):
        assert HANDLER.sniff(tiny_gif())

    def test_png_does_not_match(self):
        assert not HANDLER.sniff(tiny_png())

    def test_jpeg_does_not_match(self):
        assert not HANDLER.sniff(tiny_jpeg())

    def test_pdf_does_not_match(self):
        assert not HANDLER.sniff(tiny_pdf())

    def test_html_mislabeled_as_gif_does_not_match(self):
        assert not HANDLER.sniff(b"<html>not a gif</html>")


class TestHandle:
    def test_reads_dimensions_and_file_size(self):
        body = tiny_gif()
        result = HANDLER.handle(body, "http://fixture.local/anim.gif")
        assert result.metadata == {"width": 3, "height": 4, "file_size": len(body)}
        assert result.links == []
