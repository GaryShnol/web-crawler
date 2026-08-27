"""Image: PNG bodies, sniffed by the format's own magic bytes -- never the
declared Content-Type or the url. Dimensions come from Pillow. Only PNG is
registered here; another raster format is a natural follow-up, its own
@register'd file, the same shape as this one -- this covers what the fixture
site actually serves.
"""

import io

from PIL import Image

from .base import Handler, HandlerResult, register

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


@register("image/png")
class ImageHandler(Handler):
    kind = "image"
    directory = "images"
    extension = "png"

    def sniff(self, body: bytes) -> bool:
        return body.startswith(_PNG_MAGIC)

    def handle(self, body: bytes, url: str) -> HandlerResult:
        with Image.open(io.BytesIO(body)) as image:
            width, height = image.size
        metadata = {"width": width, "height": height, "file_size": len(body)}
        return HandlerResult(metadata=metadata, links=[])
