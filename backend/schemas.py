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


class LLMExtraction(BaseModel):
    title: str = ""
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
    quality_score: float = 0.0              # 0..1, model self-rated


# -------- File record (one per file) --------

FileStatus = Literal["pending", "processing", "done", "error", "skipped"]


class FileRecord(BaseModel):
    relative_path: str
    file_name: str
    extension: str
    file_size: int
    sha256: str
    page_count: Optional[int] = None
    os_created: Optional[str] = None        # ISO datetime
    os_modified: Optional[str] = None
    status: FileStatus = "pending"
    error: str = ""
    indexed_at: Optional[str] = None
    extraction: Optional[LLMExtraction] = None
    used_thinking: bool = False


# -------- API DTOs --------

class StartRequest(BaseModel):
    target_folder: str
    registry_xlsx: str = ""


class ReevaluateRequest(BaseModel):
    relative_paths: list[str]
    use_thinking: bool = False


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


class ProgressSnapshot(BaseModel):
    state: Literal["idle", "scanning", "running", "paused", "stopping", "error"] = "idle"
    target_folder: str = ""
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