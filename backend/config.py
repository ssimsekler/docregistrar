"""Load and validate the YAML config file."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


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
    max_output_tokens: int = 2048


class ExtractConfig(BaseModel):
    head_chars: int = 12000
    middle_chars: int = 4000
    tail_chars: int = 4000
    max_file_size_bytes: int = 0


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8000


class AppConfig(BaseModel):
    target_folder: str = ""
    registry_xlsx: str = ""
    llm: LLMConfig = Field(default_factory=LLMConfig)
    extract: ExtractConfig = Field(default_factory=ExtractConfig)
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