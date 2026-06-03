"""Image extraction.

Two responsibilities:

1. Build a small text blob (filename, folder, EXIF) so a *text-only* LLM has
   at least filename + EXIF to work from.
2. Optionally produce an `ExtractedImage` payload (raw bytes + MIME +
   dimensions) so a *vision-capable* LLM can be sent the image itself.

The image bytes go through a deterministic pipeline:
  * decode with Pillow (HEIC supported if `pillow-heif` is installed),
  * downscale so the longest side is no greater than `max_image_dim`,
  * encode to JPEG (or PNG when alpha must be preserved),
  * if still over `max_bytes`, iteratively reduce JPEG quality and clamp
    dimensions until it fits.

Every meaningful step emits a log line so failures (corrupt files, missing
HEIC support, oversized payloads) show up clearly in the activity log.
"""
from __future__ import annotations

import io
import logging
from pathlib import Path
from typing import Optional, Tuple

from . import ExtractionResult, ExtractedImage

log = logging.getLogger("docregistrar.extractors.image")


# Defaults — JobManager / LMClient pass real values from VisionConfig at
# call time, but we fall back to these when extract_image() is called
# without explicit knobs (e.g. unit tests).
DEFAULT_MAX_DIM = 1568
DEFAULT_MAX_BYTES = 4 * 1024 * 1024
DEFAULT_JPEG_QUALITY = 85


# Try to register HEIC support exactly once. We do this at import time so
# the warning shows up in the log right when the worker starts, not on the
# first HEIC encountered. Failure is non-fatal — HEIC files just fall back
# to text-only.
_HEIC_OK = False
try:
    import pillow_heif  # type: ignore
    pillow_heif.register_heif_opener()
    _HEIC_OK = True
    log.info("HEIC/HEIF support: enabled (pillow-heif).")
except Exception as _e:  # pragma: no cover - import-time
    log.info(
        "HEIC/HEIF support: disabled (pillow-heif not installed: %s). "
        "HEIC images will fall back to text-only metadata.",
        _e,
    )


def extract_image(
    path: Path,
    *,
    build_image_payload: bool = True,
    max_image_dim: int = DEFAULT_MAX_DIM,
    max_bytes: int = DEFAULT_MAX_BYTES,
    jpeg_quality: int = DEFAULT_JPEG_QUALITY,
) -> ExtractionResult:
    """Extract text hints (always) and optionally an image payload.

    `build_image_payload=False` reproduces the legacy behaviour (text only).
    Failures to build the image payload are logged and degrade gracefully —
    the text hints are still returned.
    """
    parts: list[str] = [
        f"Image file: {path.name}",
        f"Folder: {path.parent.name}",
    ]
    image_payload: Optional[ExtractedImage] = None
    extraction_error = ""

    try:
        from PIL import Image, ExifTags  # type: ignore

        try:
            img = Image.open(str(path))
        except Exception as e:
            # Distinguish HEIC-without-plugin from generic decode failure
            # because the fix is different (install pillow-heif vs. file
            # is corrupt).
            ext = path.suffix.lower()
            if ext == ".heic" and not _HEIC_OK:
                msg = (
                    f"HEIC support unavailable (pillow-heif not installed); "
                    f"falling back to text-only for {path.name}."
                )
                log.warning(msg)
                parts.append(f"[image read warning: {msg}]")
            else:
                log.warning(
                    "Image open failed for %s: %s: %s; falling back to "
                    "text-only hints.",
                    path, type(e).__name__, e,
                )
                parts.append(
                    f"[image read error: {type(e).__name__}: {e}]"
                )
            text = "\n".join(parts)
            return ExtractionResult(text=text, page_count=1)

        with img:
            # Some formats (e.g. animated GIF) need an explicit copy after
            # open() before we mess with them; Pillow lazy-loads.
            log.info(
                "Image open: %s format=%s mode=%s size=%dx%d",
                path.name, img.format, img.mode, img.size[0], img.size[1],
            )
            parts.append(f"Format: {img.format}")
            parts.append(f"Size: {img.size[0]}x{img.size[1]}")
            parts.append(f"Mode: {img.mode}")

            # Pull a few EXIF fields the LLM can actually use.
            exif_present = False
            try:
                exif = img.getexif()
                if exif:
                    exif_present = True
                    tag_map = {
                        ExifTags.TAGS.get(k, str(k)): v
                        for k, v in exif.items()
                    }
                    interesting = [
                        "DateTime", "DateTimeOriginal",
                        "Artist", "Copyright", "ImageDescription",
                        "Software", "Make", "Model",
                    ]
                    for k in interesting:
                        if k in tag_map and tag_map[k]:
                            parts.append(f"EXIF {k}: {tag_map[k]}")
            except Exception as e:
                log.warning(
                    "EXIF read failed for %s: %s: %s (continuing).",
                    path.name, type(e).__name__, e,
                )

            # Build the image payload AFTER we've harvested EXIF, since the
            # encode step closes/replaces the source image.
            if build_image_payload:
                try:
                    image_payload = _build_image_payload(
                        img,
                        max_dim=max_image_dim,
                        max_bytes=max_bytes,
                        jpeg_quality=jpeg_quality,
                        log_prefix=path.name,
                    )
                    if image_payload is not None:
                        log.info(
                            "Image payload ready: %s mime=%s dims=%dx%d "
                            "bytes=%d (exif=%s)",
                            path.name,
                            image_payload.mime,
                            image_payload.width,
                            image_payload.height,
                            len(image_payload.data),
                            "yes" if exif_present else "no",
                        )
                except Exception as e:
                    log.warning(
                        "Image payload build failed for %s: %s: %s; "
                        "falling back to text-only.",
                        path.name, type(e).__name__, e,
                    )
                    extraction_error = (
                        f"image_payload_build_failed: "
                        f"{type(e).__name__}: {e}"
                    )

    except Exception as e:
        # Catch-all so a Pillow ImportError or similar doesn't abort the
        # whole worker. The job manager only fails the file when
        # extraction_error is set AND text is empty.
        log.warning(
            "Unexpected image-extractor failure for %s: %s: %s",
            path, type(e).__name__, e,
        )
        parts.append(f"[image read error: {type(e).__name__}: {e}]")

    text = "\n".join(parts)
    return ExtractionResult(
        text=text,
        page_count=1,
        extraction_error=extraction_error,
        image=image_payload,
    )


def _build_image_payload(
    img,
    *,
    max_dim: int,
    max_bytes: int,
    jpeg_quality: int,
    log_prefix: str,
) -> Optional[ExtractedImage]:
    """Downscale + re-encode `img` to fit within (max_dim, max_bytes).

    Returns None if Pillow cannot produce any encoded output we trust.
    """
    from PIL import Image  # type: ignore

    # Normalize mode. Vision models work best with sRGB; the only reason
    # to keep RGBA is to preserve transparency, in which case we emit PNG.
    has_alpha = img.mode in ("RGBA", "LA") or (
        img.mode == "P" and "transparency" in img.info
    )
    if has_alpha:
        work = img.convert("RGBA")
        target_format = "PNG"
        mime = "image/png"
    else:
        # Convert to RGB so JPEG encoding is straightforward (drops e.g.
        # CMYK / palette modes that would otherwise upset the encoder).
        work = img.convert("RGB")
        target_format = "JPEG"
        mime = "image/jpeg"

    orig_w, orig_h = work.size

    # First pass: downscale if longest side > max_dim.
    new_w, new_h = _fit_dims(orig_w, orig_h, max_dim)
    if (new_w, new_h) != (orig_w, orig_h):
        log.info(
            "Image downscale: %s %dx%d -> %dx%d (max_dim=%d)",
            log_prefix, orig_w, orig_h, new_w, new_h, max_dim,
        )
        work = work.resize((new_w, new_h), Image.LANCZOS)

    # Encode and check size. If too big, walk down the quality ladder.
    quality_ladder = (jpeg_quality, 75, 60, 50, 40)
    data: bytes = b""
    quality_used = quality_ladder[0]
    for q in quality_ladder:
        data = _encode(work, target_format, q)
        quality_used = q
        log.info(
            "Image re-encode: %s format=%s quality=%d bytes=%d",
            log_prefix, target_format, q, len(data),
        )
        if len(data) <= max_bytes:
            break

    # Still over budget? Clamp dimensions further (halve longest side
    # repeatedly) until we fit or the image becomes unusable.
    cur_w, cur_h = work.size
    safety = 0
    while len(data) > max_bytes and max(cur_w, cur_h) > 256 and safety < 6:
        safety += 1
        cur_w, cur_h = max(1, cur_w // 2), max(1, cur_h // 2)
        log.warning(
            "Image still over max_bytes (%d > %d); shrinking %s to %dx%d",
            len(data), max_bytes, log_prefix, cur_w, cur_h,
        )
        work = work.resize((cur_w, cur_h), Image.LANCZOS)
        data = _encode(work, target_format, quality_used)

    if len(data) > max_bytes:
        log.warning(
            "Image too large to fit max_bytes=%d for %s; final attempt: "
            "dims=%dx%d quality=%d bytes=%d (sending anyway).",
            max_bytes, log_prefix, cur_w, cur_h, quality_used, len(data),
        )

    final_w, final_h = work.size
    return ExtractedImage(
        data=data,
        mime=mime,
        width=int(final_w),
        height=int(final_h),
    )


def _fit_dims(w: int, h: int, max_dim: int) -> Tuple[int, int]:
    """Return (w, h) scaled so that max(w, h) <= max_dim, preserving ratio."""
    longest = max(w, h)
    if longest <= max_dim or longest <= 0:
        return w, h
    scale = max_dim / float(longest)
    return max(1, int(round(w * scale))), max(1, int(round(h * scale)))


def _encode(img, target_format: str, quality: int) -> bytes:
    """Encode `img` to bytes in `target_format`. `quality` is honored for
    JPEG only; PNG ignores it (PNG is lossless)."""
    buf = io.BytesIO()
    if target_format == "JPEG":
        # `optimize=True` shaves a few %; `progressive=True` is friendlier
        # to multi-modal servers that stream-decode.
        img.save(buf, format="JPEG", quality=int(quality),
                 optimize=True, progressive=True)
    else:
        img.save(buf, format=target_format, optimize=True)
    return buf.getvalue()
