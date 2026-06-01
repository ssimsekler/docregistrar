"""PDF text extraction using pdfplumber, with pypdf fallback.

Emits per-page progress via the optional `progress_cb` so callers (e.g.
JobManager) can show 'reading page X of N' to the user. The callback may
raise UserSkippedError to abort cleanly.
"""
from __future__ import annotations

from pathlib import Path

from . import ExtractionResult, ProgressCB, UserSkippedError


def _emit(cb: ProgressCB, cur: int, total: int) -> None:
    if cb is None:
        return
    try:
        cb(cur, total, "page")
    except UserSkippedError:
        raise
    except Exception:
        # Never let a buggy progress callback break extraction.
        pass


def extract_pdf(path: Path, progress_cb: ProgressCB = None) -> ExtractionResult:
    text_parts: list[str] = []
    page_count: int | None = None

    # Try pdfplumber first (better tables/columns)
    try:
        import pdfplumber

        with pdfplumber.open(str(path)) as pdf:
            page_count = len(pdf.pages)
            _emit(progress_cb, 0, page_count)
            for i, page in enumerate(pdf.pages):
                try:
                    t = page.extract_text() or ""
                except Exception:
                    t = ""
                if t.strip():
                    text_parts.append(f"--- Page {i + 1} ---\n{t}")
                _emit(progress_cb, i + 1, page_count)
        text = "\n\n".join(text_parts).strip()
        if text:
            return ExtractionResult(text=text, page_count=page_count)
    except UserSkippedError:
        raise
    except Exception:
        pass

    # Fallback: pypdf
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        page_count = len(reader.pages)
        _emit(progress_cb, 0, page_count)
        for i, page in enumerate(reader.pages):
            try:
                t = page.extract_text() or ""
            except Exception:
                t = ""
            if t.strip():
                text_parts.append(f"--- Page {i + 1} ---\n{t}")
            _emit(progress_cb, i + 1, page_count)
        text = "\n\n".join(text_parts).strip()
        return ExtractionResult(
            text=text,
            page_count=page_count,
            extraction_error="" if text else "no_text_extracted",
        )
    except UserSkippedError:
        raise
    except Exception as e:
        return ExtractionResult(
            text="",
            page_count=page_count,
            extraction_error=f"{type(e).__name__}: {e}",
        )
