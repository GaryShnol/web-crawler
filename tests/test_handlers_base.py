"""handlers/base.py: resolve()'s own dispatch -- content-type hint, sniff
fallback, and no-match -- exercised directly against the real registry
rather than only indirectly through worker.py integration tests. PNG and
JPEG are two registered handlers over the same top-level type, so this is
also the concrete proof the hint lookup doesn't just happen to work when
there's only one image handler around (CODE_REVIEW.md C7).
"""

from fake_api.payloads import tiny_jpeg, tiny_pdf, tiny_png

from crawler.handlers import base
from crawler.handlers.image import ImageHandler
from crawler.handlers.jpeg import JpegHandler


class TestResolve:
    def test_correct_content_type_hint_matches_immediately(self):
        handler = base.resolve("image/png", tiny_png())
        assert isinstance(handler, ImageHandler)

    def test_second_registered_handler_for_the_same_family_is_reachable_too(self):
        handler = base.resolve("image/jpeg", tiny_jpeg())
        assert isinstance(handler, JpegHandler)

    def test_hint_with_parameters_is_still_matched(self):
        handler = base.resolve("image/png; charset=binary", tiny_png())
        assert isinstance(handler, ImageHandler)

    def test_wrong_hint_falls_back_to_sniffing_every_handler(self):
        # Declared png, actually jpeg bytes -- the hint gets a first try,
        # fails sniff(), and the fallback loop finds the real match.
        handler = base.resolve("image/png", tiny_jpeg())
        assert isinstance(handler, JpegHandler)

    def test_no_hint_falls_back_to_sniffing_every_handler(self):
        handler = base.resolve(None, tiny_pdf())
        assert handler is not None
        assert handler.kind == "pdf"

    def test_no_handler_matches_returns_none(self):
        assert base.resolve("application/octet-stream", b"not any known format") is None

    def test_matched_handlers_own_content_type_is_the_canonical_string(self):
        # A hint carrying parameters must never leak into what a matched
        # handler reports back -- contents.content_type always the bare
        # literal declared on the class.
        handler = base.resolve("image/png; charset=binary", tiny_png())
        assert handler.content_type == "image/png"
