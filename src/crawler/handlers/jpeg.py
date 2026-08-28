"""Image: JPEG bodies, sniffed by the format's own SOI marker -- never the
declared Content-Type or the url. See image.py for the shared shape.
"""

from ._raster import raster_metadata
from .base import Handler, HandlerResult, register

_JPEG_MAGIC = b"\xff\xd8\xff"


@register
class JpegHandler(Handler):
    kind = "image"
    directory = "images"
    extension = "jpg"
    content_type = "image/jpeg"

    def sniff(self, body: bytes) -> bool:
        return body.startswith(_JPEG_MAGIC)

    def handle(self, body: bytes, url: str) -> HandlerResult:
        return HandlerResult(metadata=raster_metadata(body), links=[])
