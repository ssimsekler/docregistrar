"""Per-file-type text + metadata extractors.

Each extractor returns an ExtractionResult with:
  - text: concatenated plain text (caller will apply head/middle/tail truncation)
  - page_count: int or None
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class ExtractionResult:
    text: str = ""
    page_count: Optional[int] = None
    extraction_error: str = ""


def extract_any(path: Path, ext: str) -> ExtractionResult:
    """Dispatch to the right extractor by file extension (lowercase, with dot)."""
    ext = ext.lower()
    try:
        if ext == ".pdf":
            from .pdf import extract_pdf
            return extract_pdf(path)
        if ext == ".docx":
            from .docx_ext import extract_docx
            return extract_docx(path)
        if ext == ".pptx":
            from .pptx_ext import extract_pptx
            return extract_pptx(path)
        if ext == ".xlsx":
            from .xlsx_ext import extract_xlsx
            return extract_xlsx(path)
        if ext in {".txt", ".md", ".rtf"}:
            from .text import extract_text
            return extract_text(path)
        if ext in {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff"}:
            from .image import extract_image
            return extract_image(path)
        if ext in {".doc", ".ppt", ".xls"}:
            # Legacy formats: filename only (no parser without paid add-ons).
            return ExtractionResult(
                text=f"[Legacy {ext} format — no text extraction available. Filename: {path.name}]",
                page_count=None,
                extraction_error="legacy_format_unsupported",
            )
    except Exception as e:
        return ExtractionResult(text="", page_count=None, extraction_error=f"{type(e).__name__}: {e}")
    return ExtractionResult(text="", page_count=None, extraction_error="unsupported_extension")


def truncate_head_middle_tail(text: str, head: int, middle: int, tail: int) -> str:
    """Take head + middle + tail of the text, separated by markers."""
    n = len(text)
    if n <= head + middle + tail:
        return text
    head_part = text[:head]
    mid_start = (n - middle) // 2
    mid_part = text[mid_start: mid_start + middle]
    tail_part = text[-tail:]
    return (
        head_part
        + "\n\n[... omitted middle section, sampling from center ...]\n\n"
        + mid_part
        + "\n\n[... omitted, sampling from end ...]\n\n"
        + tail_part
    )