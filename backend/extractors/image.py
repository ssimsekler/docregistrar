"""Image 'extraction'.

We don't OCR (per project decision). We surface filename + EXIF metadata
so the LLM can at least make a best-effort guess at title/date from the
file name and EXIF DateTimeOriginal.
"""
from __future__ import annotations

from pathlib import Path

from . import ExtractionResult


def extract_image(path: Path) -> ExtractionResult:
    parts: list[str] = [
        f"Image file: {path.name}",
        f"Folder: {path.parent.name}",
    ]
    try:
        from PIL import Image, ExifTags  # type: ignore

        with Image.open(str(path)) as img:
            parts.append(f"Format: {img.format}")
            parts.append(f"Size: {img.size[0]}x{img.size[1]}")
            parts.append(f"Mode: {img.mode}")
            try:
                exif = img.getexif()
                if exif:
                    tag_map = {ExifTags.TAGS.get(k, str(k)): v for k, v in exif.items()}
                    interesting = [
                        "DateTime", "DateTimeOriginal", "Artist", "Copyright",
                        "ImageDescription", "Software", "Make", "Model",
                    ]
                    for k in interesting:
                        if k in tag_map and tag_map[k]:
                            parts.append(f"EXIF {k}: {tag_map[k]}")
            except Exception:
                pass
    except Exception as e:
        parts.append(f"[image read error: {type(e).__name__}: {e}]")

    text = "\n".join(parts)
    return ExtractionResult(text=text, page_count=1)