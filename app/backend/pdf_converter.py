"""Local MarkItDown PDF conversion; Markdown is the reading text source."""

from __future__ import annotations

from io import BytesIO
from typing import Callable

DEFAULT_MAX_MARKDOWN_CHARS = 2_000_000
DEFAULT_DRYRUN = True


def convert_pdf_markdown(pdf_bytes: bytes, max_chars: int = DEFAULT_MAX_MARKDOWN_CHARS,
                         dryrun: bool = DEFAULT_DRYRUN) -> str:
    """Convert PDF bytes without files, plugins, credentials, or model calls.

    MarkItDown supplies text and Markdown table structure. The reader keeps
    original PDF coordinates separately because Markdown has no crop geometry.
    ``dryrun`` is accepted for reusable validation; both modes remain in memory.
    """
    from markitdown import StreamInfo
    from markitdown.converters import PdfConverter

    if not isinstance(pdf_bytes, (bytes, bytearray, memoryview)) or not pdf_bytes:
        raise ValueError("PDF 内容为空。")
    # The input type is already known. Calling the official PDF converter
    # directly avoids creating Magika's format-identification model.
    result = PdfConverter().convert(
        BytesIO(bytes(pdf_bytes)),
        stream_info=StreamInfo(extension=".pdf", mimetype="application/pdf"),
    )
    return str(result.text_content or "").strip()[:max(0, int(max_chars))]


def convert_pdf_pages(pdf_bytes: bytes, page_count: int,
                      page_loader: Callable[[int], bytes],
                      dryrun: bool = DEFAULT_DRYRUN) -> tuple[str, list[str]]:
    """Keep MarkItDown's paragraph order and real PDF page boundaries.

    The plain-prose converter retains PDFMiner page separators. Its table/form
    path does not, so convert individual in-memory pages in that case instead
    of guessing page numbers from text. ``page_loader`` takes a zero-based page.
    """
    markdown = convert_pdf_markdown(pdf_bytes, dryrun=dryrun)
    pages = markdown.split("\f")
    while len(pages) > page_count and not pages[-1].strip():
        pages.pop()
    if len(pages) != page_count:
        pages = [convert_pdf_markdown(page_loader(index), dryrun=dryrun)
                 for index in range(page_count)]
        markdown = "\n\n\f\n\n".join(pages)
    return markdown, [page.strip("\n\r\f ") for page in pages]
