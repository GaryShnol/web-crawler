"""Small real payloads for the fake API's asset routes — genuine bytes, not stubs."""

import io

from PIL import Image
from pypdf import PdfWriter


def tiny_png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (1, 1), color=(200, 30, 30)).save(buf, format="PNG")
    return buf.getvalue()


def tiny_jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (2, 3), color=(30, 200, 30)).save(buf, format="JPEG")
    return buf.getvalue()


def tiny_gif() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (3, 4), color=(30, 30, 200)).save(buf, format="GIF")
    return buf.getvalue()


def tiny_webp() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (4, 5), color=(200, 200, 30)).save(buf, format="WEBP")
    return buf.getvalue()


def tiny_pdf() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def tiny_video_no_duration() -> bytes:
    """A bare ISO-BMFF ftyp box: recognizable as MP4 by magic bytes, but with
    no moov box, so nothing can read a duration out of it. That's deliberate —
    it's the fixture for the null-duration-with-reason path, not a bug.
    """
    return (16).to_bytes(4, "big") + b"ftypisom" + (0x200).to_bytes(4, "big")
