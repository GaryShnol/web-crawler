"""Image: PNG bodies, sniffed by the format's own magic bytes -- never the
declared Content-Type or the url. jpeg.py/gif.py/webp.py are the other
raster formats, same shape, sharing _raster.raster_metadata for the
Pillow-backed dimension/size extraction.
"""

from ._raster import raster_metadata
from .base import Handler, HandlerResult, register

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


@register
class ImageHandler(Handler):
    kind = "image"
    directory = "images"
    extension = "png"
    content_type = "image/png"

    def sniff(self, body: bytes) -> bool:
        return body.startswith(_PNG_MAGIC)

    def handle(self, body: bytes, url: str) -> HandlerResult:
        return HandlerResult(metadata=raster_metadata(body), links=[])
