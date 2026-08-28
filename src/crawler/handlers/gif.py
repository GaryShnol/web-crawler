"""Image: GIF bodies, sniffed by the format's own header (`GIF87a`/`GIF89a`)
-- never the declared Content-Type or the url. See image.py for the shared
shape.
"""

from ._raster import raster_metadata
from .base import Handler, HandlerResult, register

_GIF_MAGICS = (b"GIF87a", b"GIF89a")


@register
class GifHandler(Handler):
    kind = "image"
    directory = "images"
    extension = "gif"
    content_type = "image/gif"

    def sniff(self, body: bytes) -> bool:
        return body.startswith(_GIF_MAGICS)

    def handle(self, body: bytes, url: str) -> HandlerResult:
        return HandlerResult(metadata=raster_metadata(body), links=[])
