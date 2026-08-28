"""Image: WEBP bodies, sniffed by the RIFF container's own `WEBP` fourCC at
offset 8 -- RIFF alone is shared by AVI/WAV/etc, so the fourCC is what
actually identifies this as WEBP, never the declared Content-Type or the
url. See image.py for the shared shape.
"""

from ._raster import raster_metadata
from .base import Handler, HandlerResult, register


@register
class WebpHandler(Handler):
    kind = "image"
    directory = "images"
    extension = "webp"
    content_type = "image/webp"

    def sniff(self, body: bytes) -> bool:
        return body[:4] == b"RIFF" and body[8:12] == b"WEBP"

    def handle(self, body: bytes, url: str) -> HandlerResult:
        return HandlerResult(metadata=raster_metadata(body), links=[])
