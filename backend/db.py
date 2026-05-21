"""SQLite-backed storage for file records, repository master data, and job state.

The DB is the canonical source of truth; registry.xlsx is a derived view
regenerated at checkpoints.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

from .schemas import (
    FileRecord,
    GENERATED_ATTRIBUTE_FIELDS,
    KVPair,
    LLMExtraction,
    MAX_CUSTOM_PROPERTIES,
    NamedEntities,
    Repository,
    now_iso,
)

log = logging.getLogger("docregistrar.db")

SCHEMA_VERSION = 2

DDL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS files (
    relative_path TEXT PRIMARY KEY,
    file_name TEXT NOT NULL,
    extension TEXT NOT NULL,
    file_size INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    page_count INTEGER,
    os_created TEXT,
    os_modified TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT NOT NULL DEFAULT '',
    indexed_at TEXT,
    used_thinking INTEGER NOT NULL DEFAULT 0,
    extraction_json TEXT,
    manually_edited INTEGER NOT NULL DEFAULT 0,
    is_duplicate INTEGER NOT NULL DEFAULT 0,
    duplicate_group TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_files_status ON files(status);
CREATE INDEX IF NOT EXISTS idx_files_sha ON files(sha256);

CREATE TABLE IF NOT EXISTS repositories (
    name TEXT PRIMARY KEY,
    path TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
"""

# Columns added in later schema versions; we ensure they exist via ALTER TABLE
# at startup (safe to repeat — we check first).
_LATER_COLUMNS = [
    ("manually_edited", "INTEGER NOT NULL DEFAULT 0"),
    ("is_duplicate", "INTEGER NOT NULL DEFAULT 0"),
    ("duplicate_group", "TEXT NOT NULL DEFAULT ''"),
    ("indexing_started_at", "TEXT"),
    ("indexing_completed_at", "TEXT"),
    ("relative_folder_path", "TEXT NOT NULL DEFAULT ''"),
]

# Columns we no longer use. We try to drop them with SQLite 3.35+'s
# ALTER TABLE DROP COLUMN. If unavailable / unsupported, we leave them.
_DEPRECATED_COLUMNS = ["full_path", "full_folder_path"]


class Database:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, isolation_level=None, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(DDL)
            # Apply additive column migrations for existing DBs
            existing_cols = {r["name"] for r in conn.execute("PRAGMA table_info(files)").fetchall()}
            for col_name, col_def in _LATER_COLUMNS:
                if col_name not in existing_cols:
                    conn.execute(f"ALTER TABLE files ADD COLUMN {col_name} {col_def}")

            # Try to drop deprecated columns (SQLite 3.35+).
            existing_cols = {r["name"] for r in conn.execute("PRAGMA table_info(files)").fetchall()}
            for col_name in _DEPRECATED_COLUMNS:
                if col_name in existing_cols:
                    try:
                        conn.execute(f"ALTER TABLE files DROP COLUMN {col_name}")
                        log.info("Dropped deprecated column files.%s", col_name)
                    except sqlite3.OperationalError as e:
                        log.warning(
                            "Could not drop deprecated column files.%s "
                            "(SQLite < 3.35?). Leaving as-is. %s",
                            col_name, e,
                        )

            # Create indexes that depend on migrated columns (safe to repeat)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_files_dup ON files(is_duplicate)")

            # Seed repositories table from existing extraction_json values
            # (one-time migration). Path/description are left empty so the
            # user must edit them before the repo can be used for scanning.
            existing_repos = {
                r["name"] for r in conn.execute("SELECT name FROM repositories").fetchall()
            }
            distinct_repos = conn.execute(
                """
                SELECT DISTINCT COALESCE(json_extract(extraction_json, '$.repository'), '') AS repo
                  FROM files
                 WHERE extraction_json IS NOT NULL
                   AND COALESCE(json_extract(extraction_json, '$.repository'), '') <> ''
                """
            ).fetchall()
            for r in distinct_repos:
                name = r["repo"]
                if name and name not in existing_repos:
                    conn.execute(
                        "INSERT INTO repositories(name, path, description, created_at) "
                        "VALUES(?,?,?,?)",
                        (name, "", "", now_iso()),
                    )
                    log.info("Seeded repository from existing data: %r (path empty)", name)

            cur = conn.execute("SELECT value FROM meta WHERE key='schema_version'")
            row = cur.fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO meta(key,value) VALUES('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
            else:
                conn.execute(
                    "UPDATE meta SET value=? WHERE key='schema_version'",
                    (str(SCHEMA_VERSION),),
                )

    @contextmanager
    def conn(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            c = self._connect()
            try:
                yield c
            finally:
                c.close()

    # ---------- meta ----------

    def set_meta(self, key: str, value: str) -> None:
        with self.conn() as c:
            c.execute(
                "INSERT INTO meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def get_meta(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self.conn() as c:
            row = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return row["value"] if row else default

    # ---------- repositories (master data) ----------

    def list_repositories(self) -> list[Repository]:
        """Return all repositories with file counts, sorted alphabetically."""
        with self.conn() as c:
            rows = c.execute(
                """
                SELECT r.name, r.path, r.description, r.created_at,
                       COALESCE((
                         SELECT COUNT(*) FROM files f
                          WHERE COALESCE(json_extract(f.extraction_json, '$.repository'), '') = r.name
                       ), 0) AS file_count
                  FROM repositories r
                 ORDER BY LOWER(r.name)
                """
            ).fetchall()
            return [
                Repository(
                    name=r["name"],
                    path=r["path"] or "",
                    description=r["description"] or "",
                    created_at=r["created_at"],
                    file_count=int(r["file_count"]),
                )
                for r in rows
            ]

    def get_repository(self, name: str) -> Optional[Repository]:
        if not name:
            return None
        with self.conn() as c:
            row = c.execute(
                "SELECT name, path, description, created_at FROM repositories WHERE name=?",
                (name,),
            ).fetchone()
            if row is None:
                return None
            cnt_row = c.execute(
                """SELECT COUNT(*) AS n FROM files
                    WHERE COALESCE(json_extract(extraction_json, '$.repository'), '') = ?""",
                (name,),
            ).fetchone()
            return Repository(
                name=row["name"],
                path=row["path"] or "",
                description=row["description"] or "",
                created_at=row["created_at"],
                file_count=int(cnt_row["n"]) if cnt_row else 0,
            )

    def create_repository(self, name: str, path: str, description: str = "") -> Repository:
        name = (name or "").strip()
        path = (path or "").strip()
        description = (description or "").strip()
        if not name:
            raise ValueError("Repository name is required.")
        if not path:
            raise ValueError("Repository path is required.")
        with self.conn() as c:
            existing = c.execute(
                "SELECT 1 FROM repositories WHERE name=?", (name,)
            ).fetchone()
            if existing:
                raise ValueError(f"Repository already exists: {name!r}")
            ts = now_iso()
            c.execute(
                "INSERT INTO repositories(name, path, description, created_at) VALUES(?,?,?,?)",
                (name, path, description, ts),
            )
        rec = self.get_repository(name)
        assert rec is not None
        return rec

    def update_repository(self, name: str, *, path: Optional[str] = None,
                          description: Optional[str] = None) -> Repository:
        with self.conn() as c:
            row = c.execute(
                "SELECT 1 FROM repositories WHERE name=?", (name,)
            ).fetchone()
            if row is None:
                raise ValueError(f"Repository not found: {name!r}")
            sets: list[str] = []
            params: list[Any] = []
            if path is not None:
                p = (path or "").strip()
                if not p:
                    raise ValueError("Repository path cannot be empty.")
                sets.append("path=?")
                params.append(p)
            if description is not None:
                sets.append("description=?")
                params.append((description or "").strip())
            if sets:
                params.append(name)
                c.execute(
                    f"UPDATE repositories SET {', '.join(sets)} WHERE name=?",
                    params,
                )
        rec = self.get_repository(name)
        assert rec is not None
        return rec

    def rename_repository(self, old_name: str, new_name: str) -> Repository:
        """Rename a repository and update all referencing files'
        extraction_json.repository to the new name.
        """
        new_name = (new_name or "").strip()
        if not new_name:
            raise ValueError("New repository name is required.")
        if old_name == new_name:
            rec = self.get_repository(old_name)
            if rec is None:
                raise ValueError(f"Repository not found: {old_name!r}")
            return rec
        with self.conn() as c:
            row = c.execute(
                "SELECT path, description, created_at FROM repositories WHERE name=?",
                (old_name,),
            ).fetchone()
            if row is None:
                raise ValueError(f"Repository not found: {old_name!r}")
            existing = c.execute(
                "SELECT 1 FROM repositories WHERE name=?", (new_name,)
            ).fetchone()
            if existing:
                raise ValueError(f"Target repository already exists: {new_name!r}")
            c.execute(
                "INSERT INTO repositories(name, path, description, created_at) VALUES(?,?,?,?)",
                (new_name, row["path"], row["description"], row["created_at"]),
            )
            # Update referencing files' extraction_json.repository
            files = c.execute(
                """SELECT relative_path, extraction_json FROM files
                    WHERE COALESCE(json_extract(extraction_json, '$.repository'), '') = ?""",
                (old_name,),
            ).fetchall()
            for f in files:
                try:
                    data = json.loads(f["extraction_json"]) if f["extraction_json"] else {}
                except Exception:
                    data = {}
                data["repository"] = new_name
                try:
                    ne_obj = NamedEntities(**(data.pop("named_entities", {}) or {}))
                    new_json = LLMExtraction(named_entities=ne_obj, **data).model_dump_json()
                except Exception:
                    new_json = json.dumps({**data, "repository": new_name}, ensure_ascii=False)
                c.execute(
                    "UPDATE files SET extraction_json=? WHERE relative_path=?",
                    (new_json, f["relative_path"]),
                )
            c.execute("DELETE FROM repositories WHERE name=?", (old_name,))
        rec = self.get_repository(new_name)
        assert rec is not None
        return rec

    def delete_repository(self, name: str, *, clear_files: bool = True) -> int:
        """Delete a repository. If clear_files=True, every file referencing
        this repo gets its extraction_json.repository cleared (set to "").
        Returns the number of files affected.
        """
        with self.conn() as c:
            row = c.execute("SELECT 1 FROM repositories WHERE name=?", (name,)).fetchone()
            if row is None:
                raise ValueError(f"Repository not found: {name!r}")
            n = 0
            if clear_files:
                files = c.execute(
                    """SELECT relative_path, extraction_json FROM files
                        WHERE COALESCE(json_extract(extraction_json, '$.repository'), '') = ?""",
                    (name,),
                ).fetchall()
                for f in files:
                    try:
                        data = json.loads(f["extraction_json"]) if f["extraction_json"] else {}
                    except Exception:
                        data = {}
                    data["repository"] = ""
                    try:
                        ne_obj = NamedEntities(**(data.pop("named_entities", {}) or {}))
                        new_json = LLMExtraction(named_entities=ne_obj, **data).model_dump_json()
                    except Exception:
                        new_json = json.dumps({**data, "repository": ""}, ensure_ascii=False)
                    c.execute(
                        "UPDATE files SET extraction_json=? WHERE relative_path=?",
                        (new_json, f["relative_path"]),
                    )
                    n += 1
            c.execute("DELETE FROM repositories WHERE name=?", (name,))
        return n

    def repo_path_for_file(self, relative_path: str) -> Optional[str]:
        """Return the configured path of the repository assigned to this file,
        or None if the file has no repo OR the repo has no path."""
        with self.conn() as c:
            row = c.execute(
                """SELECT r.path
                     FROM files f
                     LEFT JOIN repositories r
                       ON r.name = COALESCE(json_extract(f.extraction_json, '$.repository'), '')
                    WHERE f.relative_path=?""",
                (relative_path,),
            ).fetchone()
            if row is None:
                return None
            p = row["path"] or ""
            return p or None

    # ---------- files ----------

    def upsert_file_basic(
        self,
        *,
        relative_path: str,
        file_name: str,
        extension: str,
        file_size: int,
        sha256: str,
        os_created: Optional[str],
        os_modified: Optional[str],
        relative_folder_path: str = "",
    ) -> str:
        """Insert a new pending row, or update size/sha/dates if file changed.

        Returns the new status of the row ('pending' if (re)queued, otherwise unchanged).
        """
        with self.conn() as c:
            row = c.execute(
                "SELECT sha256, status, relative_folder_path FROM files WHERE relative_path=?",
                (relative_path,),
            ).fetchone()
            if row is None:
                c.execute(
                    "INSERT INTO files(relative_path, file_name, extension, file_size, "
                    "sha256, os_created, os_modified, status, relative_folder_path) "
                    "VALUES(?,?,?,?,?,?,?, 'pending', ?)",
                    (
                        relative_path, file_name, extension, file_size,
                        sha256, os_created, os_modified, relative_folder_path,
                    ),
                )
                return "pending"

            if row["sha256"] != sha256:
                # File changed → reset to pending
                c.execute(
                    "UPDATE files SET file_size=?, sha256=?, os_created=?, os_modified=?, "
                    "relative_folder_path=?, "
                    "status='pending', error='', extraction_json=NULL, "
                    "indexed_at=NULL, used_thinking=0 "
                    "WHERE relative_path=?",
                    (
                        file_size, sha256, os_created, os_modified,
                        relative_folder_path, relative_path,
                    ),
                )
                return "pending"

            # Unchanged content; refresh relative_folder_path if drifted.
            if (row["relative_folder_path"] or "") != relative_folder_path:
                c.execute(
                    "UPDATE files SET relative_folder_path=? WHERE relative_path=?",
                    (relative_folder_path, relative_path),
                )
            return row["status"]

    def assign_repository(self, relative_path: str, repository: str) -> None:
        """Set extraction_json.repository for a file WITHOUT touching
        manually_edited (used by the scanner to auto-assign the active repo
        on freshly ingested files).
        """
        with self.conn() as c:
            row = c.execute(
                "SELECT extraction_json FROM files WHERE relative_path=?", (relative_path,)
            ).fetchone()
            if row is None:
                return
            try:
                data = json.loads(row["extraction_json"]) if row["extraction_json"] else {}
            except Exception:
                data = {}
            if (data.get("repository") or "") == repository:
                return
            data["repository"] = repository
            try:
                ne_obj = NamedEntities(**(data.pop("named_entities", {}) or {}))
                new_json = LLMExtraction(named_entities=ne_obj, **data).model_dump_json()
            except Exception:
                new_json = json.dumps({**data, "repository": repository}, ensure_ascii=False)
            c.execute(
                "UPDATE files SET extraction_json=? WHERE relative_path=?",
                (new_json, relative_path),
            )

    def mark_status(self, relative_path: str, status: str, error: str = "") -> None:
        with self.conn() as c:
            if status == "processing":
                # Record when indexing started (with timezone)
                from datetime import datetime, timezone
                started = datetime.now(timezone.utc).isoformat(timespec="seconds")
                c.execute(
                    "UPDATE files SET status=?, error=?, indexing_started_at=? WHERE relative_path=?",
                    (status, error, started, relative_path),
                )
            else:
                c.execute(
                    "UPDATE files SET status=?, error=? WHERE relative_path=?",
                    (status, error, relative_path),
                )

    def reset_to_pending(self, relative_paths: Iterable[str], *, force: bool = False) -> tuple[int, int, int]:
        """Set rows back to 'pending' so they get re-processed.

        If `force=False`, rows with manually_edited=1 are SKIPPED.
        Rows whose current status is 'skipped' are ALWAYS refused (even with
        force=True). Their `error` field is set to a hint telling the user to
        flip the status to 'pending' explicitly first.

        Returns (n_reset, n_skipped_due_to_manual_edit, n_skipped_status_skipped).
        """
        n_reset = 0
        n_skipped = 0
        n_skipped_status = 0
        skip_msg = ("File is in 'skipped' status and cannot be re-evaluated. "
                    "Set its status to 'pending' first to re-evaluate.")
        with self.conn() as c:
            for p in relative_paths:
                row = c.execute(
                    "SELECT manually_edited, status FROM files WHERE relative_path=?", (p,)
                ).fetchone()
                if row is None:
                    continue
                if row["status"] == "skipped":
                    c.execute(
                        "UPDATE files SET error=? WHERE relative_path=?",
                        (skip_msg, p),
                    )
                    n_skipped_status += 1
                    continue
                if not force and row["manually_edited"]:
                    n_skipped += 1
                    continue
                cur = c.execute(
                    """UPDATE files SET status='pending', error='', extraction_json=NULL,
                                        indexed_at=NULL, used_thinking=0,
                                        manually_edited=0
                       WHERE relative_path=?""",
                    (p,),
                )
                n_reset += cur.rowcount
        return n_reset, n_skipped, n_skipped_status

    def reset_in_progress_to_pending(self) -> int:
        with self.conn() as c:
            cur = c.execute(
                "UPDATE files SET status='pending' WHERE status='processing'"
            )
            return cur.rowcount

    def save_extraction(
        self,
        *,
        relative_path: str,
        page_count: Optional[int],
        extraction: LLMExtraction,
        used_thinking: bool,
        default_repository: str = "",
    ) -> None:
        """Persist a fresh LLM extraction. If `default_repository` is set
        and the extraction's repository is empty, fill it in.

        Preserves any user-defined `custom_properties` from the previous
        extraction (the LLM never produces them).
        """
        if not extraction.repository and default_repository:
            extraction = extraction.model_copy(update={"repository": default_repository})

        # Preserve previously-saved custom_properties
        with self.conn() as c:
            row = c.execute(
                "SELECT extraction_json FROM files WHERE relative_path=?", (relative_path,)
            ).fetchone()
        prev_custom: list[KVPair] = []
        if row and row["extraction_json"]:
            try:
                prev = json.loads(row["extraction_json"])
                raw_cp = prev.get("custom_properties") or []
                if isinstance(raw_cp, list):
                    for item in raw_cp[:MAX_CUSTOM_PROPERTIES]:
                        if isinstance(item, dict):
                            prev_custom.append(
                                KVPair(key=str(item.get("key", "")),
                                       value=str(item.get("value", "")))
                            )
            except Exception:
                pass
        if prev_custom and not extraction.custom_properties:
            extraction = extraction.model_copy(update={"custom_properties": prev_custom})

        from datetime import datetime, timezone
        completed = datetime.now(timezone.utc).isoformat(timespec="seconds")

        with self.conn() as c:
            c.execute(
                """UPDATE files
                      SET page_count=?, extraction_json=?, status='done', error='',
                          indexed_at=?, used_thinking=?, indexing_completed_at=?
                    WHERE relative_path=?""",
                (
                    page_count,
                    extraction.model_dump_json(),
                    now_iso(),
                    1 if used_thinking else 0,
                    completed,
                    relative_path,
                ),
            )

    def update_fields(self, relative_path: str, fields: dict) -> bool:
        """Edit a single row's fields. `fields` maps from the schema's
        FileEditRequest fields.

        manually_edited is set to 1 ONLY if at least one GENERATED attribute
        is present in the payload (see GENERATED_ATTRIBUTE_FIELDS). Editing
        only user-managed fields (repository, source_url_*, custom_properties)
        does NOT flip the flag.

        Special handling:
          - 'status' / 'error' update the dedicated columns.
          - 'persons'/'organizations'/'locations'/'mentioned_dates'/
            'products_technologies' go INTO extraction_json.named_entities.
          - All other LLM-style fields go INTO extraction_json.
          - 'repository' goes INTO extraction_json.repository.
        Returns True if the row existed.
        """
        with self.conn() as c:
            row = c.execute(
                "SELECT * FROM files WHERE relative_path=?", (relative_path,)
            ).fetchone()
            if row is None:
                return False

            new_status = fields.pop("status", None)
            new_error = fields.pop("error", None)

            # Determine if any GENERATED attribute is being edited
            edits_generated = any(k in GENERATED_ATTRIBUTE_FIELDS for k in fields.keys())

            # Decode existing extraction_json (or build a fresh empty one)
            ext_data: dict = {}
            if row["extraction_json"]:
                try:
                    ext_data = json.loads(row["extraction_json"])
                except Exception:
                    ext_data = {}
            ne_data = ext_data.get("named_entities") or {}

            # Map named-entity-ish fields
            ne_keys = {
                "persons": "persons",
                "organizations": "organizations",
                "locations": "locations",
                "mentioned_dates": "dates",
                "products_technologies": "products_technologies",
            }
            for src, dst in ne_keys.items():
                if src in fields:
                    ne_data[dst] = fields.pop(src) or []

            # Custom properties: list of {key,value}; clamp size & coerce
            if "custom_properties" in fields:
                raw_cp = fields.pop("custom_properties") or []
                cleaned: list[dict] = []
                for item in raw_cp[:MAX_CUSTOM_PROPERTIES]:
                    if isinstance(item, dict):
                        k = str(item.get("key", "")).strip()
                        v = str(item.get("value", "")).strip()
                        if k or v:
                            cleaned.append({"key": k, "value": v})
                ext_data["custom_properties"] = cleaned

            # Everything else goes top-level in ext_data
            for k, v in list(fields.items()):
                ext_data[k] = v if v is not None else ""

            ext_data["named_entities"] = ne_data

            # Re-validate via Pydantic to ensure we keep a consistent shape
            try:
                ne_obj = NamedEntities(**ne_data)
                clean_top = {k: v for k, v in ext_data.items() if k != "named_entities"}
                extraction = LLMExtraction(named_entities=ne_obj, **clean_top)
                ext_json = extraction.model_dump_json()
            except Exception:
                ext_json = json.dumps(ext_data, ensure_ascii=False)

            sets = ["extraction_json=?"]
            params: list[Any] = [ext_json]
            if edits_generated:
                sets.append("manually_edited=1")
            if new_status is not None:
                sets.append("status=?")
                params.append(new_status)
                # If user moves a manually-edited row back to pending, that's
                # an explicit re-queue; clear the manual flag so the worker
                # doesn't immediately skip it.
                if new_status == "pending":
                    sets = ["manually_edited=0", "status=?", "extraction_json=NULL",
                            "indexed_at=NULL", "used_thinking=0", "error=''"]
                    params = [new_status]
            if new_error is not None:
                sets.append("error=?")
                params.append(new_error)

            sql = f"UPDATE files SET {', '.join(sets)} WHERE relative_path=?"
            params.append(relative_path)
            c.execute(sql, params)
            return True

    def bulk_set(self, relative_paths: Iterable[str], *,
                 repository: Optional[str] = None,
                 status: Optional[str] = None) -> int:
        """Apply a status and/or repository to many files at once.

        Repository-only edits do NOT set manually_edited=1.
        Status changes set manually_edited=1 (except status='pending', which
        clears the flag and re-queues).
        Returns number of rows affected.
        """
        n = 0
        with self.conn() as c:
            for p in relative_paths:
                row = c.execute(
                    "SELECT extraction_json FROM files WHERE relative_path=?", (p,)
                ).fetchone()
                if row is None:
                    continue

                if repository is not None:
                    ext_data: dict = {}
                    if row["extraction_json"]:
                        try:
                            ext_data = json.loads(row["extraction_json"])
                        except Exception:
                            ext_data = {}
                    ext_data["repository"] = repository
                    if "named_entities" not in ext_data:
                        ext_data["named_entities"] = {}
                    try:
                        ne_obj = NamedEntities(**ext_data.get("named_entities", {}))
                        clean_top = {k: v for k, v in ext_data.items() if k != "named_entities"}
                        ext_json = LLMExtraction(named_entities=ne_obj, **clean_top).model_dump_json()
                    except Exception:
                        ext_json = json.dumps(ext_data, ensure_ascii=False)
                    # Repository-only assignment: do NOT touch manually_edited.
                    c.execute(
                        "UPDATE files SET extraction_json=? WHERE relative_path=?",
                        (ext_json, p),
                    )

                if status is not None:
                    if status == "pending":
                        # Re-queue: clear extracted data and the manual flag
                        c.execute(
                            """UPDATE files SET status='pending', error='',
                                                extraction_json=NULL, indexed_at=NULL,
                                                used_thinking=0, manually_edited=0
                               WHERE relative_path=?""",
                            (p,),
                        )
                    else:
                        c.execute(
                            "UPDATE files SET status=?, manually_edited=1 WHERE relative_path=?",
                            (status, p),
                        )

                n += 1
        return n

    def next_pending(self) -> Optional[FileRecord]:
        """Return the next file the worker should process.

        Priority:
          1. status='pending' rows
          2. then status='error' rows
        """
        with self.conn() as c:
            row = c.execute(
                "SELECT * FROM files WHERE status='pending' "
                "ORDER BY relative_path LIMIT 1"
            ).fetchone()
            if row is not None:
                return _row_to_record(row)
            row = c.execute(
                "SELECT * FROM files WHERE status='error' "
                "ORDER BY relative_path LIMIT 1"
            ).fetchone()
            return _row_to_record(row) if row else None

    def find_done_sibling(self, sha256: str, exclude_relative_path: str) -> Optional[FileRecord]:
        """Return any other file with the same SHA-256 that's already 'done'."""
        if not sha256:
            return None
        with self.conn() as c:
            row = c.execute(
                "SELECT * FROM files WHERE sha256=? AND relative_path<>? "
                "AND status='done' AND extraction_json IS NOT NULL "
                "ORDER BY relative_path LIMIT 1",
                (sha256, exclude_relative_path),
            ).fetchone()
            return _row_to_record(row) if row else None

    def copy_extraction_from_sibling(
        self,
        target_relative_path: str,
        source_relative_path: str,
        *,
        default_repository: str = "",
    ) -> bool:
        """Copy `extraction_json` and `page_count` from one row to another."""
        from datetime import datetime, timezone
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")

        with self.conn() as c:
            src = c.execute(
                "SELECT extraction_json, page_count FROM files WHERE relative_path=?",
                (source_relative_path,),
            ).fetchone()
            if src is None or not src["extraction_json"]:
                return False

            ext_data: dict
            try:
                ext_data = json.loads(src["extraction_json"])
            except Exception:
                return False

            if default_repository and not (ext_data.get("repository") or ""):
                ext_data["repository"] = default_repository

            try:
                ne_obj = NamedEntities(**(ext_data.pop("named_entities", {}) or {}))
                extraction = LLMExtraction(named_entities=ne_obj, **ext_data)
                ext_json = extraction.model_dump_json()
            except Exception:
                ext_json = src["extraction_json"]

            cur = c.execute(
                """UPDATE files
                      SET extraction_json=?, page_count=?, status='done', error='',
                          indexed_at=?, indexing_started_at=?, indexing_completed_at=?,
                          used_thinking=0, manually_edited=0
                    WHERE relative_path=?""",
                (
                    ext_json,
                    src["page_count"],
                    now_iso(),
                    ts,
                    ts,
                    target_relative_path,
                ),
            )
            return cur.rowcount > 0

    def get_file(self, relative_path: str) -> Optional[FileRecord]:
        with self.conn() as c:
            row = c.execute(
                "SELECT * FROM files WHERE relative_path=?", (relative_path,)
            ).fetchone()
            return _row_to_record(row) if row else None

    def list_files(
        self,
        *,
        status: Optional[str] = None,
        limit: int = 5000,
        offset: int = 0,
        search: str = "",
        sha256: Optional[str] = None,
        duplicates_only: bool = False,
        repository: Optional[str] = None,
    ) -> list[FileRecord]:
        """List file rows.

        `repository` semantics:
          - `None` (default): no repository filter, return all rows.
          - `""`            : return only rows whose repository is empty
          - `"X"`           : return only rows whose extraction.repository == "X".
        """
        sql = "SELECT * FROM files"
        clauses = []
        params: list[Any] = []
        if status:
            clauses.append("status=?")
            params.append(status)
        if search:
            clauses.append("(relative_path LIKE ? OR file_name LIKE ?)")
            like = f"%{search}%"
            params.extend([like, like])
        if sha256:
            clauses.append("sha256=?")
            params.append(sha256)
        if duplicates_only:
            clauses.append("is_duplicate=1")
        if repository is not None:
            if repository == "":
                clauses.append(
                    "(extraction_json IS NULL "
                    "OR COALESCE(json_extract(extraction_json, '$.repository'), '') = '')"
                )
            else:
                clauses.append(
                    "COALESCE(json_extract(extraction_json, '$.repository'), '') = ?"
                )
                params.append(repository)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY relative_path LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        with self.conn() as c:
            rows = c.execute(sql, params).fetchall()
            return [_row_to_record(r) for r in rows]

    def list_dup_siblings(self, relative_path: str) -> list[FileRecord]:
        """Return all OTHER files that share the same SHA-256 as this one."""
        with self.conn() as c:
            cur = c.execute(
                "SELECT sha256 FROM files WHERE relative_path=?", (relative_path,)
            ).fetchone()
            if cur is None:
                return []
            sha = cur["sha256"]
            rows = c.execute(
                "SELECT * FROM files WHERE sha256=? AND relative_path<>? "
                "ORDER BY relative_path",
                (sha, relative_path),
            ).fetchall()
            return [_row_to_record(r) for r in rows]

    def recompute_duplicates(self) -> int:
        """One-pass authoritative recompute of is_duplicate / duplicate_group."""
        with self.conn() as c:
            c.execute("UPDATE files SET is_duplicate = 0, duplicate_group = ''")
            c.execute("""
                UPDATE files
                   SET is_duplicate = 1,
                       duplicate_group = sha256
                 WHERE sha256 IN (
                    SELECT sha256 FROM files
                    GROUP BY sha256
                    HAVING COUNT(*) >= 2
                 )
            """)
            row = c.execute("""
                SELECT COUNT(*) AS n FROM (
                    SELECT 1 FROM files
                    GROUP BY sha256
                    HAVING COUNT(*) >= 2
                )
            """).fetchone()
            return int(row["n"]) if row else 0

    def skip_dup_siblings_of(self, relative_paths: Iterable[str]) -> int:
        """For every SHA-256 in the given selection, mark every OTHER file
        with the same SHA-256 as 'skipped'."""
        with self.conn() as c:
            shas = set()
            rp_list = list(relative_paths)
            for p in rp_list:
                row = c.execute(
                    "SELECT sha256 FROM files WHERE relative_path=?", (p,)
                ).fetchone()
                if row and row["sha256"]:
                    shas.add(row["sha256"])
            if not shas:
                return 0
            placeholders = ",".join("?" for _ in rp_list)
            sha_placeholders = ",".join("?" for _ in shas)
            params: list[Any] = []
            params.extend(shas)
            params.extend(rp_list)
            cur = c.execute(
                f"""UPDATE files
                       SET status='skipped', manually_edited=1
                     WHERE sha256 IN ({sha_placeholders})
                       AND relative_path NOT IN ({placeholders})""",
                params,
            )
            return cur.rowcount

    def list_all_for_excel(self) -> list[FileRecord]:
        with self.conn() as c:
            rows = c.execute(
                "SELECT * FROM files ORDER BY relative_path"
            ).fetchall()
            return [_row_to_record(r) for r in rows]

    def counts_by_status(self) -> dict[str, int]:
        with self.conn() as c:
            rows = c.execute(
                "SELECT status, COUNT(*) AS n FROM files GROUP BY status"
            ).fetchall()
            return {r["status"]: r["n"] for r in rows}

    def total_count(self) -> int:
        with self.conn() as c:
            row = c.execute("SELECT COUNT(*) AS n FROM files").fetchone()
            return int(row["n"]) if row else 0

    def delete_files_not_in(self, present_paths: set[str]) -> int:
        """Remove DB rows for files no longer on disk."""
        if not present_paths:
            return 0
        with self.conn() as c:
            existing = {
                r["relative_path"]
                for r in c.execute("SELECT relative_path FROM files").fetchall()
            }
            stale = existing - present_paths
            n = 0
            for p in stale:
                c.execute("DELETE FROM files WHERE relative_path=?", (p,))
                n += 1
            return n

    def delete_files_not_in_repo(self, repository: str, present_paths: set[str]) -> int:
        """Like delete_files_not_in, but limited to files belonging to the
        given repository. Used by the scanner so it doesn't delete files
        from other repos. `present_paths` are relative_paths (relative to
        the repo root) that are still present on disk.
        """
        if not repository:
            return 0
        with self.conn() as c:
            existing = {
                r["relative_path"]
                for r in c.execute(
                    """SELECT relative_path FROM files
                        WHERE COALESCE(json_extract(extraction_json, '$.repository'), '') = ?""",
                    (repository,),
                ).fetchall()
            }
            stale = existing - present_paths
            n = 0
            for p in stale:
                c.execute("DELETE FROM files WHERE relative_path=?", (p,))
                n += 1
            return n

    def delete_files(self, relative_paths: Iterable[str]) -> int:
        """Permanently remove the given rows from the registry."""
        n = 0
        with self.conn() as c:
            for p in relative_paths:
                if not p:
                    continue
                cur = c.execute("DELETE FROM files WHERE relative_path=?", (p,))
                n += cur.rowcount
            if n > 0:
                c.execute("UPDATE files SET is_duplicate = 0, duplicate_group = ''")
                c.execute(
                    """UPDATE files
                          SET is_duplicate = 1,
                              duplicate_group = sha256
                        WHERE sha256 IN (
                          SELECT sha256 FROM files
                          GROUP BY sha256
                          HAVING COUNT(*) >= 2
                        )"""
                )
        return n


def _row_to_record(row: sqlite3.Row) -> FileRecord:
    extraction: Optional[LLMExtraction] = None
    if row["extraction_json"]:
        try:
            data = json.loads(row["extraction_json"])
            ne = NamedEntities(**data.pop("named_entities", {}))
            extraction = LLMExtraction(named_entities=ne, **data)
        except Exception:
            extraction = None

    def _opt(name: str, default: Any = None) -> Any:
        try:
            return row[name]
        except (KeyError, IndexError):
            return default

    manually_edited = bool(_opt("manually_edited", 0))
    is_duplicate = bool(_opt("is_duplicate", 0))
    duplicate_group = _opt("duplicate_group", "") or ""

    return FileRecord(
        relative_path=row["relative_path"],
        relative_folder_path=_opt("relative_folder_path", "") or "",
        file_name=row["file_name"],
        extension=row["extension"],
        file_size=int(row["file_size"]),
        sha256=row["sha256"],
        page_count=row["page_count"],
        os_created=row["os_created"],
        os_modified=row["os_modified"],
        status=row["status"],
        error=row["error"] or "",
        indexed_at=row["indexed_at"],
        indexing_started_at=_opt("indexing_started_at"),
        indexing_completed_at=_opt("indexing_completed_at"),
        extraction=extraction,
        used_thinking=bool(row["used_thinking"]),
        manually_edited=manually_edited,
        is_duplicate=is_duplicate,
        duplicate_group=duplicate_group,
    )
