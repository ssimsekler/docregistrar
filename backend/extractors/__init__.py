"""Per-file-type text + metadata extractors.

Each extractor returns an ExtractionResult with:
  - text: concatenated plain text (caller will apply head/middle/tail truncation)
  - page_count: int or None

Extractors that loop over pages/slides/sheets accept an optional
`progress_cb(current, total, unit)` callback to surface live progress
to the caller (e.g. JobManager). The callback may raise
`UserSkippedError` to abort extraction cleanly.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

# A progress callback. Called with (current, total, unit_label).
# `unit_label` is e.g. "page", "slide", "sheet". The callback may raise
# UserSkippedError to abort the extractor mid-loop.
ProgressCB = Optional[Callable[[int, int, str], None]]


class UserSkippedError(Exception):
    """Raised by a progress callback to abort extraction cleanly because
    the user requested a skip."""
    pass


@dataclass
class ExtractionResult:
    text: str = ""
    page_count: Optional[int] = None
    extraction_error: str = ""


def extract_any(path: Path, ext: str,
                progress_cb: ProgressCB = None) -> ExtractionResult:
    """Dispatch to the right extractor by file extension (lowercase, with dot).

    Re-raises UserSkippedError so the caller can distinguish a user-initiated
    skip from a genuine extraction error.
    """
    ext = ext.lower()
    try:
        if ext == ".pdf":
            from .pdf import extract_pdf
            return extract_pdf(path, progress_cb=progress_cb)
        if ext == ".docx":
            from .docx_ext import extract_docx
            return extract_docx(path, progress_cb=progress_cb)
        if ext == ".pptx":
            from .pptx_ext import extract_pptx
            return extract_pptx(path, progress_cb=progress_cb)
        if ext == ".xlsx":
            from .xlsx_ext import extract_xlsx
            return extract_xlsx(path, progress_cb=progress_cb)
        if ext in {".txt", ".md", ".rtf"}:
            from .text import extract_text
            res = extract_text(path)
            if progress_cb is not None:
                try:
                    progress_cb(1, 1, "file")
                except UserSkippedError:
                    raise
                except Exception:
                    pass
            return res
        if ext in {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff"}:
            from .image import extract_image
            res = extract_image(path)
            if progress_cb is not None:
                try:
                    progress_cb(1, 1, "image")
                except UserSkippedError:
                    raise
                except Exception:
                    pass
            return res
        if ext in {".doc", ".ppt", ".xls"}:
            # Legacy formats: filename only (no parser without paid add-ons).
            return ExtractionResult(
                text=f"[Legacy {ext} format — no text extraction available. Filename: {path.name}]",
                page_count=None,
                extraction_error="legacy_format_unsupported",
            )
    except UserSkippedError:
        raise
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