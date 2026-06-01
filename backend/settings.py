"""Runtime-mutable settings layer.

Effective config = AppConfig defaults  ⊕  YAML config (config.yaml)  ⊕  DB overrides.

The DB overrides live in the `meta` table under the key `settings_overrides_json`
as a single JSON blob. We use dotted keys ("llm.request_timeout_seconds") so the
client side and the override store both see one flat namespace.

Server settings (host/port) are *read-only* at runtime — changing them needs an
app restart — but they are still surfaced by `effective_config()` so the UI can
display the current value.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from pydantic import ValidationError

from .config import AppConfig, load_config
from .db import Database

log = logging.getLogger("docregistrar.settings")

_OVERRIDE_META_KEY = "settings_overrides_json"

# Keys whose change requires a process restart to take effect.
RESTART_REQUIRED_KEYS: set[str] = {
    "server.host",
    "server.port",
}

# Keys the UI can edit. Anything not in this set is rejected by `set_setting`.
EDITABLE_KEYS: set[str] = {
    # LLM
    "llm.base_url",
    "llm.api_key",
    "llm.model",
    "llm.request_timeout_seconds",
    "llm.temperature",
    "llm.top_p",
    "llm.top_k",
    "llm.presence_penalty",
    "llm.thinking_default",
    "llm.thinking_on_low_quality",
    "llm.low_quality_threshold",
    "llm.max_output_tokens",
    "llm.per_file_timeout_seconds",
    # Processing
    "processing.max_error_retries",
    "excel_write_every_n_files",
    # Text extraction
    "extract.head_chars",
    "extract.middle_chars",
    "extract.tail_chars",
    "extract.max_file_size_bytes",
    "extract.per_file_timeout_seconds",
    "extract.per_page_timeout_seconds",
    # Map-reduce strategy for large documents
    "extract.mapreduce.enabled",
    "extract.mapreduce.threshold_chars",
    "extract.mapreduce.chunk_chars",
    "extract.mapreduce.chunk_overlap_chars",
    "extract.mapreduce.max_chunks",
    "extract.mapreduce.reduce_with_llm",
    "extract.mapreduce.per_chunk_max_output_tokens",
    # Scanner
    "include_extensions",
    "ignore_dir_names",
    # Storage
    "registry_xlsx",
    # Server (restart required, but readable)
    "server.host",
    "server.port",
}

# Keys that affect the live LLM client. When any of these change, the worker
# rebuilds its `LMClient` at the next loop iteration.
LLM_AFFECTING_KEYS: set[str] = {
    "llm.base_url",
    "llm.api_key",
    "llm.request_timeout_seconds",
}


def _get_dotted(obj: Any, key: str) -> Any:
    """Walk `obj` using dotted attribute / dict access. Returns None on miss."""
    cur = obj
    for part in key.split("."):
        if cur is None:
            return None
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            cur = getattr(cur, part, None)
    return cur


def _set_dotted(d: dict, key: str, value: Any) -> None:
    """Set `key` (dotted) inside the nested dict `d`, creating sub-dicts."""
    parts = key.split(".")
    cur = d
    for p in parts[:-1]:
        nxt = cur.get(p)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[p] = nxt
        cur = nxt
    cur[parts[-1]] = value


class SettingsService:
    """Manages effective config + DB-backed overrides.

    Thread-safe: each method reads/writes the meta blob in a single SQLite
    transaction (the Database class itself uses an RLock).
    """

    def __init__(self, db: Database):
        self._db = db
        # Read base (defaults + YAML) once at construction; this is the same
        # object the rest of the app would have used before the settings layer.
        self._yaml_config: AppConfig = load_config()

    # ---------- public API ----------

    def overrides(self) -> dict[str, Any]:
        """Return the current dotted-key overrides dict (may be empty)."""
        raw = self._db.get_meta(_OVERRIDE_META_KEY, "")
        if not raw:
            return {}
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except Exception:
            log.warning("Could not parse settings_overrides_json: %r", raw)
            return {}

    def effective_config(self) -> AppConfig:
        """Return defaults ⊕ YAML ⊕ DB overrides as a validated AppConfig."""
        merged = self._yaml_config.model_dump()
        for key, value in self.overrides().items():
            _set_dotted(merged, key, value)
        try:
            return AppConfig(**merged)
        except ValidationError as e:
            # If the persisted overrides are now invalid (e.g. an old key for a
            # field that no longer exists), drop them defensively and log loudly.
            log.error(
                "Persisted settings overrides failed validation; ignoring overrides. "
                "Error: %s. Overrides were: %r", e, self.overrides(),
            )
            return self._yaml_config

    def defaults_config(self) -> AppConfig:
        """Return the defaults-only config (no YAML, no DB overrides).
        Used by the UI to show 'reset to default' targets.
        """
        return AppConfig()

    def yaml_config(self) -> AppConfig:
        """Return the defaults ⊕ YAML config (no DB overrides)."""
        return self._yaml_config

    def overridden_keys(self) -> list[str]:
        return sorted(self.overrides().keys())

    def set_setting(self, key: str, value: Any) -> AppConfig:
        """Validate + persist a single dotted-key override.

        Raises ValueError on unknown / read-only / validation failure.
        Returns the new effective config on success.
        """
        if key not in EDITABLE_KEYS:
            raise ValueError(f"Unknown setting: {key!r}")
        if key in RESTART_REQUIRED_KEYS:
            raise ValueError(
                f"{key!r} can only be changed by editing config.yaml and restarting "
                f"the application."
            )

        current = self.overrides()
        # Tentatively apply.
        candidate = dict(current)
        candidate[key] = value
        merged = self._yaml_config.model_dump()
        for k, v in candidate.items():
            _set_dotted(merged, k, v)
        try:
            new_cfg = AppConfig(**merged)
        except ValidationError as e:
            raise ValueError(f"Invalid value for {key!r}: {e}")

        # Persist.
        self._db.set_meta(_OVERRIDE_META_KEY, json.dumps(candidate, ensure_ascii=False))
        log.info("Setting changed: %s = %r", key, value)
        return new_cfg

    def reset_setting(self, key: str) -> AppConfig:
        """Remove a single override; the field reverts to YAML/default."""
        if key not in EDITABLE_KEYS:
            raise ValueError(f"Unknown setting: {key!r}")
        current = self.overrides()
        if key in current:
            del current[key]
            self._db.set_meta(_OVERRIDE_META_KEY, json.dumps(current, ensure_ascii=False))
            log.info("Setting reset to default: %s", key)
        return self.effective_config()

    def reset_all(self) -> AppConfig:
        """Remove every override at once."""
        self._db.set_meta(_OVERRIDE_META_KEY, "")
        log.info("All setting overrides cleared.")
        return self.effective_config()

    # ---------- helpers used by API/UI layer ----------

    def to_dict_view(self) -> dict[str, Any]:
        """Build the response payload for `GET /api/settings`.

        Returns a dict with:
          - current: effective config as a dotted-key map for the editable keys.
          - defaults: same shape, the defaults-only values.
          - yaml: same shape, the defaults-⊕-YAML values (ignores DB overrides).
          - overridden_keys: list of keys that have a DB override.
          - restart_required_keys: list of keys whose change needs a restart.
          - editable_keys: list of all keys the UI may attempt to set.
        """
        eff = self.effective_config()
        defaults = self.defaults_config()
        yaml_cfg = self.yaml_config()

        current_map: dict[str, Any] = {}
        defaults_map: dict[str, Any] = {}
        yaml_map: dict[str, Any] = {}
        for key in sorted(EDITABLE_KEYS):
            current_map[key] = _get_dotted(eff, key)
            defaults_map[key] = _get_dotted(defaults, key)
            yaml_map[key] = _get_dotted(yaml_cfg, key)

        return {
            "current": current_map,
            "defaults": defaults_map,
            "yaml": yaml_map,
            "overridden_keys": self.overridden_keys(),
            "restart_required_keys": sorted(RESTART_REQUIRED_KEYS),
            "editable_keys": sorted(EDITABLE_KEYS),
        }