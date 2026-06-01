"""PDF text extraction using pdfplumber, with pypdf fallback.

Emits per-page progress via the optional `progress_cb` so callers (e.g.
JobManager) can show 'reading page X of N' to the user. The callback may
raise UserSkippedError to abort cleanly.

Each page extraction is run with a per-page timeout (default 20s) so a
single corrupt/complex page can't hang the whole worker. The timeout is
controlled by the DOCREGISTRAR_PER_PAGE_TIMEOUT_S env var (set by the
job manager from extract.per_page_timeout_seconds); 0 = disabled.
"""
from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from pathlib import Path

from . import ExtractionResult, ProgressCB, UserSkippedError

log = logging.getLogger("docregistrar.extractors.pdf")


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


def _per_page_timeout_s() -> float:
    raw = os.environ.get("DOCREGISTRAR_PER_PAGE_TIMEOUT_S", "")
    try:
        v = float(raw) if raw else 0.0
    except ValueError:
        v = 0.0
    return max(0.0, v)


def _extract_page_text_safe(extract_fn, timeout_s: float) -> tuple[str, str]:
    """Run extract_fn() with an optional timeout. Returns (text, marker)
    where marker is "" on success, "(timeout)" if extraction timed out,
    or "(error)" on any other exception.
    """
    if timeout_s <= 0:
        try:
            t = extract_fn() or ""
        except Exception:
            return "", "(error)"
        return t, ""
    # Use a single-shot thread pool so we can enforce a wall-clock timeout.
    with ThreadPoolExecutor(max_workers=1) as ex:
        fut = ex.submit(extract_fn)
        try:
            t = fut.result(timeout=timeout_s) or ""
            return t, ""
        except FuturesTimeout:
            log.warning("PDF page extraction timed out after %.1fs", timeout_s)
            # The thread will keep running until pdfplumber/pypdf returns;
            # we return immediately and let it complete in the background.
            return "", "(timeout)"
        except Exception:
            return "", "(error)"


def extract_pdf(path: Path, progress_cb: ProgressCB = None) -> ExtractionResult:
    text_parts: list[str] = []
    page_count: int | None = None
    timeout_s = _per_page_timeout_s()

    # Try pdfplumber first (better tables/columns)
    try:
        import pdfplumber

        with pdfplumber.open(str(path)) as pdf:
            page_count = len(pdf.pages)
            _emit(progress_cb, 0, page_count)
            for i, page in enumerate(pdf.pages):
                t, marker = _extract_page_text_safe(page.extract_text, timeout_s)
                if t.strip():
                    text_parts.append(f"--- Page {i + 1} ---\n{t}")
                elif marker:
                    text_parts.append(f"--- Page {i + 1} {marker} ---")
                _emit(progress_cb, i + 1, page_count)
        text = "\n\n".join(text_parts).strip()
        if text:
            return ExtractionResult(text=text, page_count=page_count)
    except UserSkippedError:
        raise
    except Exception as e:
        log.info("pdfplumber failed for %s: %s; falling back to pypdf", path.name, e)

    # Fallback: pypdf
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        page_count = len(reader.pages)
        _emit(progress_cb, 0, page_count)
        text_parts = []  # reset; pdfplumber pass produced nothing useful
        for i, page in enumerate(reader.pages):
            t, marker = _extract_page_text_safe(page.extract_text, timeout_s)
            if t.strip():
                text_parts.append(f"--- Page {i + 1} ---\n{t}")
            elif marker:
                text_parts.append(f"--- Page {i + 1} {marker} ---")
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
