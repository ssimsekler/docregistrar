"""Load and validate the YAML config file."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


class VisionConfig(BaseModel):
    """Settings for sending image files to a vision-capable LLM.

    When `enabled` is true, image files (.png, .jpg, .heic, ...) are sent
    to the LLM as a multi-modal message: a base64-encoded image plus a
    short text hint (filename, folder, EXIF) when `include_text_hints`
    is true. When `enabled` is false, only the text hint is sent (legacy
    behaviour).
    """
    # Master switch.
    enabled: bool = True
    # Optional override of the LLM model name JUST for image documents.
    # Empty string means "reuse llm.model". Useful when you run a fast
    # text-only model for documents and a separate vision model for
    # images (LM Studio can host both at once).
    model: str = ""
    # Longest side, in pixels. Larger images are downscaled before
    # encoding to keep payload size bounded.
    max_image_dim: int = 1568
    # Cap on the encoded payload (bytes). If still over after the
    # quality ladder, dimensions are clamped further.
    max_bytes: int = 4 * 1024 * 1024
    # Initial JPEG quality. Re-encode walks down to lower qualities if
    # max_bytes is exceeded.
    jpeg_quality: int = 85
    # Whether to also include filename / folder / EXIF as a text hint
    # alongside the image. Strongly recommended; EXIF DateTimeOriginal
    # in particular carries information the model can't see in the
    # pixels.
    include_text_hints: bool = True
    # OpenAI vision "detail" hint: "auto", "low", or "high". Servers
    # that don't honor it ignore it harmlessly.
    detail: str = "auto"
    # If true and the vision call fails with an HTTP 4xx (e.g. the
    # configured model doesn't actually accept images), retry once with
    # text-only content so the file at least gets some metadata.
    fallback_to_text_on_error: bool = True


class LLMConfig(BaseModel):
    base_url: str = "http://localhost:1234/v1"
    api_key: str = "lm-studio"
    model: str = "qwen/qwen3.5-9b"
    request_timeout_seconds: int = 600
    temperature: float = 0.1
    top_p: float = 0.95
    top_k: int = 20
    presence_penalty: float = 1.5
    thinking_default: bool = False
    thinking_on_low_quality: bool = True
    low_quality_threshold: float = 0.6
    max_output_tokens: int = 4096
    # Total wall-clock time budget (seconds) for ALL LLM activity on a
    # single file (chunk calls + reduce, including retries). Different
    # from `request_timeout_seconds` (which is per HTTP request). When
    # exceeded, the file is marked 'error' with 'llm_total_timeout'.
    # Default is intentionally generous (~16.7 hours) so this acts as
    # a safety net rather than an active limit; tighten it from the
    # Settings dialog if you want a hard cap per file.
    per_file_timeout_seconds: int = 60000
    # Vision (multi-modal) settings for image files. See VisionConfig.
    vision: VisionConfig = Field(default_factory=VisionConfig)


class MapReduceConfig(BaseModel):
    """Map-reduce LLM strategy for large documents.

    Small docs (<= threshold_chars) go through a single LLM call (the
    legacy fast path). Larger docs are split into chunks; each chunk is
    extracted individually (the "map" phase), then results are merged
    deterministically and an optional final LLM call ("reduce") produces
    the consolidated narrative fields (title/description/summary/etc.).
    """
    enabled: bool = True
    # Documents with extracted text <= this length go through the
    # single-shot fast path. Above this they are chunked.
    threshold_chars: int = 10000
    # Target size of each chunk (in characters).
    chunk_chars: int = 10000
    # Overlap between consecutive chunks to avoid losing context across
    # boundaries.
    chunk_overlap_chars: int = 500
    # Hard ceiling on the number of chunks; if exceeded, chunks are
    # sampled evenly across the document.
    max_chunks: int = 200
    # If True, run a final LLM reduce step to produce the narrative
    # fields. If False, narrative fields are merged deterministically
    # (less coherent but faster and zero hallucination risk).
    reduce_with_llm: bool = True
    # Smaller output cap for chunk extractions (faster, less likely to
    # truncate, since each chunk only needs partial fields).
    per_chunk_max_output_tokens: int = 1500


class ExtractConfig(BaseModel):
    head_chars: int = 12000
    middle_chars: int = 4000
    tail_chars: int = 4000
    max_file_size_bytes: int = 0
    # Total time budget (seconds) for extracting text from a single
    # file. If exceeded, the file is marked 'error' with
    # 'extraction_timeout' and the worker moves on. 0 = disabled.
    per_file_timeout_seconds: int = 300
    # Per-page time budget (seconds) for PDF extraction. Pages that
    # exceed this are skipped with a marker note. 0 = disabled.
    per_page_timeout_seconds: int = 20
    # Map-reduce strategy for large documents. See MapReduceConfig.
    mapreduce: MapReduceConfig = Field(default_factory=MapReduceConfig)


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8000


class ProcessingConfig(BaseModel):
    # How many automatic retries an 'error' file gets before the worker stops
    # picking it up on its own. Manual Re-evaluate always works and resets the
    # counter. 0 = never auto-retry an error file (it must be re-evaluated).
    # Within the error queue, files with the lowest error_count are tried first
    # (and the oldest last_error_at within that group), so transient flaps don't
    # exhaust their attempts before the cluster gets re-tried.
    max_error_retries: int = 5
    # If True, prevent the OS from sleeping while the worker is running.
    # On Windows this calls SetThreadExecutionState with ES_CONTINUOUS |
    # ES_SYSTEM_REQUIRED so the system stays awake (display can still turn
    # off). The lock is released as soon as the worker finishes or idles.
    # On non-Windows platforms this is a no-op. Without this, a laptop that
    # sleeps mid-extraction typically kills the in-flight file with
    # llm_transport_error / extraction_timeout when it wakes (the OS pauses
    # the process while time.monotonic continues).
    keep_awake_while_running: bool = True
    # How often the worker logs an `[hb]` heartbeat line during long LLM
    # calls (visible in the Activity log at "verbose" verbosity). The
    # cfp.last_detail UI ticker still updates every 5s regardless of this
    # value; this only controls how often a heartbeat shows up in the
    # persistent log. 0 disables heartbeat log lines (the UI ticker still
    # ticks). Default 120 (= every 2 minutes).
    heartbeat_log_interval_seconds: int = 120


class AppConfig(BaseModel):
    target_folder: str = ""
    registry_xlsx: str = ""
    llm: LLMConfig = Field(default_factory=LLMConfig)
    extract: ExtractConfig = Field(default_factory=ExtractConfig)
    processing: ProcessingConfig = Field(default_factory=ProcessingConfig)
    include_extensions: list[str] = Field(
        default_factory=lambda: [
            ".pdf", ".docx", ".pptx", ".xlsx",
            ".doc", ".ppt", ".xls",
            ".txt", ".md", ".rtf",
            ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff",
        ]
    )
    ignore_dir_names: list[str] = Field(
        default_factory=lambda: [
            ".git", ".venv", "node_modules",
            "$RECYCLE.BIN", "System Volume Information", "__MACOSX", ".obsidian",
        ]
    )
    excel_write_every_n_files: int = 10
    server: ServerConfig = Field(default_factory=ServerConfig)


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_config(path: Path | None = None) -> AppConfig:
    """Load config.yaml if present, else config.example.yaml, else defaults."""
    if path is None:
        cfg_path = PROJECT_ROOT / "config.yaml"
        if not cfg_path.exists():
            cfg_path = PROJECT_ROOT / "config.example.yaml"
    else:
        cfg_path = path

    if cfg_path.exists():
        with cfg_path.open("r", encoding="utf-8") as f:
            data: dict[str, Any] = yaml.safe_load(f) or {}
    else:
        data = {}

    return AppConfig(**data)


def normalize_extensions(exts: list[str]) -> set[str]:
    return {e.lower() if e.startswith(".") else f".{e.lower()}" for e in exts}


# Extensions we consider "documents" (text-bearing files we run through the
# LLM extraction pipeline). Macro-enabled Office formats are included; they
# are documents, the host app prompts before running macros.
DOCUMENT_EXTENSIONS: set[str] = {
    ".pdf",
    ".docx", ".doc", ".docm",
    ".pptx", ".ppt", ".pptm",
    ".xlsx", ".xls", ".xlsm", ".xlsb",
    ".txt", ".md", ".rtf", ".csv",
    ".odt", ".ods", ".odp",
}

# Extensions we treat as images (the LLM still gets filename/path hints).
IMAGE_EXTENSIONS: set[str] = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp",
    ".tif", ".tiff", ".webp", ".heic",
}

# The set of extensions allowed to be processed. Anything else is auto-
# skipped (status='skipped', error='not_a_document_or_image: <ext>').
ALLOWED_DOC_OR_IMAGE_EXTENSIONS: set[str] = DOCUMENT_EXTENSIONS | IMAGE_EXTENSIONS
