"""PDF: sniffed by the `%PDF-` header every PDF starts with, page count and
title read with pypdf.
"""

import io

from pypdf import PdfReader

from .base import Handler, HandlerResult, register

_PDF_MAGIC = b"%PDF-"


@register
class PdfHandler(Handler):
    kind = "pdf"
    directory = "pdfs"
    extension = "pdf"
    content_type = "application/pdf"

    def sniff(self, body: bytes) -> bool:
        return body.startswith(_PDF_MAGIC)

    def handle(self, body: bytes, url: str) -> HandlerResult:
        reader = PdfReader(io.BytesIO(body))
        title = reader.metadata.title if reader.metadata else None
        metadata = {"page_count": len(reader.pages), "title": title}
        return HandlerResult(metadata=metadata, links=[])
