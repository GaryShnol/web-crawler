"""Shared dimension/size extraction for every raster image handler. Each of
image.py/jpeg.py/gif.py/webp.py owns its own magic-byte sniff -- the one
thing that has to differ per format -- and hands the matched body here, so a
fifth raster format is a sniff plus this call, not a second copy of
Pillow's Image.open dance.
"""

import io

from PIL import Image


def raster_metadata(body: bytes) -> dict[str, object]:
    with Image.open(io.BytesIO(body)) as image:
        width, height = image.size
    return {"width": width, "height": height, "file_size": len(body)}
