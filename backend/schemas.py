"""Pydantic schemas: LLM output, file records, API DTOs."""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field


# -------- LLM-extracted fields (the model fills these) --------

class NamedEntities(BaseModel):
    persons: list[str] = Field(default_factory=list)
    organizations: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    dates: list[str] = Field(default_factory=list)
    products_technologies: list[str] = Field(default_factory=list)


class KVPair(BaseModel):
    key: str = ""
    value: str = ""


MAX_CUSTOM_PROPERTIES = 50

# Maximum number of past errors to retain in extraction_json.error_history.
MAX_ERROR_HISTORY = 5

# Maximum length (chars) of any single 'error' string we persist on the row.
MAX_ERROR_TEXT_CHARS = 4000


class ErrorEntry(BaseModel):
    """One historical error attempt for a file."""
    at: str = ""           # ISO datetime when the error occurred
    stage: str = ""        # which stage failed (e.g. 'extract_text', 'llm_extract')
    message: str = ""      # short, human-readable error description


class LLMExtraction(BaseModel):
    title: str = ""
    description: str = ""                   # up to 250 chars - one-line gist of the file
    summary: str = ""                       # up to 2500 chars
    document_date: str = ""                 # ISO YYYY-MM-DD if known, else YYYY-MM, else YYYY, else ""
    last_update_date: str = ""              # same format as above
    document_type: str = ""                 # presentation, white paper, spreadsheet, report, ...
    language: str = ""                      # ISO code or English name
    authors: list[str] = Field(default_factory=list)
    version: str = ""
    confidentiality: str = ""               # Public / Internal / Confidential / Strictly Confidential / Unknown
    named_entities: NamedEntities = Field(default_factory=NamedEntities)
    key_concepts: list[str] = Field(default_factory=list)
    key_phrases: list[str] = Field(default_factory=list)   # top 10
    tags: list[str] = Field(default_factory=list)
    geographic_scope: str = ""
    industry_domain: str = ""
    quality_score: float = 0.0              # 0..1, model self-rated.
                                             # For map-reduce extractions: this == quality_score_min
                                             # (kept as the legacy field so existing UI/Excel keeps working).
    quality_score_min: float = 0.0          # Min across chunks (single-shot: same as quality_score).
    quality_score_avg: float = 0.0          # Avg across chunks (single-shot: same as quality_score).
    repository: str = ""                    # DEPRECATED in JSON: kept for backwards compat;
                                            # real source of truth is the `repository` column.
    source_url_1: str = ""                  # user-assigned URL references (not extracted by LLM)
    source_url_2: str = ""
    source_url_3: str = ""
    custom_properties: list[KVPair] = Field(default_factory=list)  # user-defined K/V pairs
    error_history: list[ErrorEntry] = Field(default_factory=list)  # last N error attempts
    # Optional non-fatal warning about an extraction that completed with
    # status='done' but with a known quality concern (e.g.
    # "low_chunk_yield: 50% of 60 chunks produced output"). Surfaced by
    # the worker through the activity log and into the file row's
    # `error` column so it shows up in the Excel and grid even though
    # the row is not in error state.
    extraction_warning: str = ""


# Set of LLMExtraction fields that count as "generated attributes" (i.e.
# things the LLM produces). Editing any of these flips manually_edited=1.
# User-managed fields (NOT in this set) do NOT flip the flag:
#   - repository, source_url_1/2/3, custom_properties
GENERATED_ATTRIBUTE_FIELDS: set[str] = {
    "title", "description", "summary",
    "document_date", "last_update_date",
    "document_type", "language",
    "authors", "version", "confidentiality",
    "key_concepts", "key_phrases", "tags",
    "geographic_scope", "industry_domain",
    "quality_score",
    # named-entity edit aliases used by FileEditRequest
    "persons", "organizations", "locations",
    "mentioned_dates", "products_technologies",
}


# -------- File record (one per file) --------

FileStatus = Literal["pending", "processing", "done", "error", "skipped"]


class FileRecord(BaseModel):
    id: str = ""                            # UUID4-equivalent (32 hex chars), DB primary key
    relative_path: str
    relative_folder_path: str = ""          # folder path relative to the repository root ("" for files at the root)
    file_name: str
    extension: str
    file_size: int
    sha256: str
    repository: str = ""                    # repository this file belongs to (promoted out of extraction_json)
    page_count: Optional[int] = None
    os_created: Optional[str] = None        # ISO datetime
    os_modified: Optional[str] = None
    status: FileStatus = "pending"
    error: str = ""
    error_count: int = 0                    # consecutive automatic-retry failures (cleared on success / re-eval)
    last_error_at: Optional[str] = None     # ISO datetime of the most recent error
    indexing_started_at: Optional[str] = None   # ISO datetime with TZ when processing began
    indexing_completed_at: Optional[str] = None  # ISO datetime with TZ when processing finished
    extraction: Optional[LLMExtraction] = None
    used_thinking: bool = False
    manually_edited: bool = False           # True if user has edited any GENERATED attribute; protects from auto re-eval
    is_duplicate: bool = False              # True iff another file in the registry has the same SHA-256
    duplicate_group: str = ""               # SHA-256 string shared by all duplicates of this content


# -------- Repository (master data) --------

class Repository(BaseModel):
    name: str
    path: str = ""                          # absolute folder path; REQUIRED for new repos / scanning / opening
    description: str = ""
    created_at: str                         # ISO datetime, immutable
    file_count: int = 0                     # how many files reference this repo


class RepositoryCreateRequest(BaseModel):
    name: str
    path: str                               # required & non-empty
    description: str = ""


class RepositoryUpdateRequest(BaseModel):
    path: Optional[str] = None              # if provided, must be non-empty
    description: Optional[str] = None


class RepositoryRenameRequest(BaseModel):
    new_name: str


# -------- API DTOs --------

class StartRequest(BaseModel):
    """Start a scan for the given repository. The scan root is the
    repository's configured path."""
    repository: str


class SetRepositoryRequest(BaseModel):
    repository: str = ""


class ReevaluateRequest(BaseModel):
    relative_paths: list[str]
    use_thinking: bool = False
    force: bool = False                    # if True, overwrite manually-edited rows


class FileEditRequest(BaseModel):
    """All fields are optional; only provided fields are updated."""
    # Status / housekeeping
    status: Optional[FileStatus] = None
    error: Optional[str] = None
    # User-managed
    repository: Optional[str] = None
    source_url_1: Optional[str] = None
    source_url_2: Optional[str] = None
    source_url_3: Optional[str] = None
    # LLM-style fields (any may be edited)
    title: Optional[str] = None
    description: Optional[str] = None
    summary: Optional[str] = None
    document_date: Optional[str] = None
    last_update_date: Optional[str] = None
    document_type: Optional[str] = None
    language: Optional[str] = None
    authors: Optional[list[str]] = None
    version: Optional[str] = None
    confidentiality: Optional[str] = None
    geographic_scope: Optional[str] = None
    industry_domain: Optional[str] = None
    key_concepts: Optional[list[str]] = None
    key_phrases: Optional[list[str]] = None
    tags: Optional[list[str]] = None
    quality_score: Optional[float] = None
    persons: Optional[list[str]] = None
    organizations: Optional[list[str]] = None
    locations: Optional[list[str]] = None
    mentioned_dates: Optional[list[str]] = None
    products_technologies: Optional[list[str]] = None
    custom_properties: Optional[list[KVPair]] = None


class BulkEditRequest(BaseModel):
    relative_paths: list[str]
    repository: Optional[str] = None
    status: Optional[FileStatus] = None


class SkipDupSiblingsRequest(BaseModel):
    """Mark every other file sharing the same SHA-256 as the given files
    as 'skipped'. The given files themselves are NOT modified."""
    relative_paths: list[str]


class DeleteFilesRequest(BaseModel):
    """Permanently remove the given rows from the registry. Does NOT touch
    the file on disk; if the folder is rescanned, the file is added back
    as a fresh 'pending' entry."""
    relative_paths: list[str]


# -------- Event log --------

class EventLogEntry(BaseModel):
    """One persisted log line.

    `category` separates user-driven events (`user`) from worker-driven
    events (`worker`). `level` is informational; we record everything as
    'info' today but the column is there so we can graduate noisy errors
    later without a migration.
    """
    id: int
    ts: str                                 # ISO datetime UTC
    level: str = "info"
    category: str                           # 'user' | 'worker' | 'system'
    message: str


class EventLogClearRequest(BaseModel):
    """`before` is a YYYY-MM-DD date string interpreted in the server's
    local time. Everything strictly older than the start of that day is
    purged. The default supplied by the UI is today − 7 days."""
    before: str


class FileStep(BaseModel):
    name: str                              # "extract_text" | "llm_extract" | "save"
    started_at: str
    finished_at: Optional[str] = None
    duration_ms: Optional[int] = None
    detail: str = ""


class CurrentFileProgress(BaseModel):
    relative_path: str = ""
    started_at: Optional[str] = None
    elapsed_ms: int = 0
    percent: int = 0                       # 0..100, coarse step-based
    steps: list[FileStep] = Field(default_factory=list)
    current_step: str = ""
    last_detail: str = ""
    # Fine-grained sub-progress for the current step. The frontend can
    # render a sub-progress bar like "Reading slide 14 / 23" or
    # "Processing chunk 4 / 12" without parsing strings.
    sub_unit: str = ""                     # e.g. "page", "slide", "sheet", "paragraph", "chunk"
    sub_current: int = 0                   # 0-based or 1-based; just a counter
    sub_total: int = 0                     # 0 means "unknown / not applicable"


class ProgressSnapshot(BaseModel):
    state: Literal["idle", "scanning", "running", "paused", "stopping", "error"] = "idle"
    target_folder: str = ""                 # the folder currently being scanned/processed (== repo path)
    repository: str = ""                    # the active repository name
    total: int = 0
    done: int = 0
    error: int = 0
    skipped: int = 0
    pending: int = 0
    processing: int = 0
    current_file: str = ""
    started_at: Optional[str] = None
    last_message: str = ""
    current_file_progress: Optional[CurrentFileProgress] = None


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")