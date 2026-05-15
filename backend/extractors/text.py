"""Plain-text / Markdown / RTF extraction."""
from __future__ import annotations

from pathlib import Path

from . import ExtractionResult


def _strip_rtf(rtf: str) -> str:
    """Very small RTF-to-text stripper (good enough for summaries).

    Removes control words, groups, and binary blobs.
    """
    import re

    # Remove RTF groups like {\*\generator ...}
    rtf = re.sub(r"\\\*\\[a-zA-Z]+[^{}]*", "", rtf)
    # Remove control words like \par, \b, \fs24, etc.
    rtf = re.sub(r"\\[a-zA-Z]+-?\d* ?", " ", rtf)
    # Remove control symbols
    rtf = re.sub(r"\\[^a-zA-Z]", "", rtf)
    # Remove braces
    rtf = rtf.replace("{", "").replace("}", "")
    # Collapse whitespace
    rtf = re.sub(r"\s+", " ", rtf).strip()
    return rtf


def extract_text(path: Path) -> ExtractionResult:
    suffix = path.suffix.lower()
    raw_bytes = path.read_bytes()

    # Try common encodings
    text = ""
    for enc in ("utf-8", "utf-8-sig", "utf-16", "cp1252", "latin-1"):
        try:
            text = raw_bytes.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if not text:
        return ExtractionResult(text="", page_count=None, extraction_error="undecodable")

    if suffix == ".rtf":
        text = _strip_rtf(text)

    return ExtractionResult(text=text, page_count=None)