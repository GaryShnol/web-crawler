"""handlers/webp.py: WEBP sniffed by the RIFF container's own WEBP fourCC,
dimensions and size read via the shared raster_metadata. Pure, no fixtures.
"""

from fake_api.payloads import tiny_gif, tiny_pdf, tiny_png, tiny_webp

from crawler.handlers.webp import WebpHandler

HANDLER = WebpHandler()


class TestSniff:
    def test_webp_matches(self):
        assert HANDLER.sniff(tiny_webp())

    def test_png_does_not_match(self):
        assert not HANDLER.sniff(tiny_png())

    def test_gif_does_not_match(self):
        # Also a RIFF-free format, but this is really guarding against the
        # RIFF *container* alone (shared with AVI/WAV) being mistaken for
        # the fourCC check.
        assert not HANDLER.sniff(tiny_gif())

    def test_pdf_does_not_match(self):
        assert not HANDLER.sniff(tiny_pdf())

    def test_other_riff_container_does_not_match(self):
        # RIFF present, but not WEBP at the fourCC offset -- e.g. a WAV file.
        assert not HANDLER.sniff(b"RIFF\x00\x00\x00\x00WAVEfmt ")

    def test_html_mislabeled_as_webp_does_not_match(self):
        assert not HANDLER.sniff(b"<html>not a webp</html>")


class TestHandle:
    def test_reads_dimensions_and_file_size(self):
        body = tiny_webp()
        result = HANDLER.handle(body, "http://fixture.local/hero.webp")
        assert result.metadata == {"width": 4, "height": 5, "file_size": len(body)}
        assert result.links == []
