"""PDF text extraction using pdfplumber, with pypdf fallback."""
from __future__ import annotations

from pathlib import Path

from . import ExtractionResult


def extract_pdf(path: Path) -> ExtractionResult:
    text_parts: list[str] = []
    page_count: int | None = None

    # Try pdfplumber first (better tables/columns)
    try:
        import pdfplumber

        with pdfplumber.open(str(path)) as pdf:
            page_count = len(pdf.pages)
            for i, page in enumerate(pdf.pages):
                try:
                    t = page.extract_text() or ""
                except Exception:
                    t = ""
                if t.strip():
                    text_parts.append(f"--- Page {i + 1} ---\n{t}")
        text = "\n\n".join(text_parts).strip()
        if text:
            return ExtractionResult(text=text, page_count=page_count)
    except Exception:
        pass

    # Fallback: pypdf
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        page_count = len(reader.pages)
        for i, page in enumerate(reader.pages):
            try:
                t = page.extract_text() or ""
            except Exception:
                t = ""
            if t.strip():
                text_parts.append(f"--- Page {i + 1} ---\n{t}")
        text = "\n\n".join(text_parts).strip()
        return ExtractionResult(
            text=text,
            page_count=page_count,
            extraction_error="" if text else "no_text_extracted",
        )
    except Exception as e:
        return ExtractionResult(
            text="",
            page_count=page_count,
            extraction_error=f"{type(e).__name__}: {e}",
        )