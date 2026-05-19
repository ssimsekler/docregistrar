"""SQLite-backed storage for file records and job state.

The DB is the canonical source of truth; registry.xlsx is a derived view
regenerated at checkpoints.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

from .schemas import FileRecord, KVPair, LLMExtraction, MAX_CUSTOM_PROPERTIES, NamedEntities, now_iso

SCHEMA_VERSION = 1

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
CREATE INDEX IF NOT EXISTS idx_files_dup ON files(is_duplicate);
"""

# Columns added in later schema versions; we ensure they exist via ALTER TABLE
# at startup (safe to repeat — we check first).
_LATER_COLUMNS = [
    ("manually_edited", "INTEGER NOT NULL DEFAULT 0"),
    ("is_duplicate", "INTEGER NOT NULL DEFAULT 0"),
    ("duplicate_group", "TEXT NOT NULL DEFAULT ''"),
]


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
            cur = conn.execute("SELECT value FROM meta WHERE key='schema_version'")
            row = cur.fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO meta(key,value) VALUES('schema_version', ?)",
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
    ) -> str:
        """Insert a new pending row, or update size/sha/dates if file changed.

        Returns the new status of the row ('pending' if (re)queued, otherwise unchanged).
        """
        with self.conn() as c:
            row = c.execute(
                "SELECT sha256, status FROM files WHERE relative_path=?",
                (relative_path,),
            ).fetchone()
            if row is None:
                c.execute(
                    """INSERT INTO files(relative_path, file_name, extension, file_size,
                                         sha256, os_created, os_modified, status)
                       VALUES(?,?,?,?,?,?,?, 'pending')""",
                    (relative_path, file_name, extension, file_size,
                     sha256, os_created, os_modified),
                )
                return "pending"

            if row["sha256"] != sha256:
                # File changed → reset to pending
                c.execute(
                    """UPDATE files SET file_size=?, sha256=?, os_created=?, os_modified=?,
                                        status='pending', error='', extraction_json=NULL,
                                        indexed_at=NULL, used_thinking=0
                       WHERE relative_path=?""",
                    (file_size, sha256, os_created, os_modified, relative_path),
                )
                return "pending"

            # Unchanged
            return row["status"]

    def mark_status(self, relative_path: str, status: str, error: str = "") -> None:
        with self.conn() as c:
            c.execute(
                "UPDATE files SET status=?, error=? WHERE relative_path=?",
                (status, error, relative_path),
            )

    def reset_to_pending(self, relative_paths: Iterable[str], *, force: bool = False) -> tuple[int, int]:
        """Set rows back to 'pending' so they get re-processed.

        If `force=False`, rows with manually_edited=1 are SKIPPED.
        Returns (n_reset, n_skipped_due_to_manual_edit).
        """
        n_reset = 0
        n_skipped = 0
        with self.conn() as c:
            for p in relative_paths:
                row = c.execute(
                    "SELECT manually_edited FROM files WHERE relative_path=?", (p,)
                ).fetchone()
                if row is None:
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
        return n_reset, n_skipped

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

        with self.conn() as c:
            c.execute(
                """UPDATE files
                      SET page_count=?, extraction_json=?, status='done', error='',
                          indexed_at=?, used_thinking=?
                    WHERE relative_path=?""",
                (
                    page_count,
                    extraction.model_dump_json(),
                    now_iso(),
                    1 if used_thinking else 0,
                    relative_path,
                ),
            )

    def update_fields(self, relative_path: str, fields: dict) -> bool:
        """Edit a single row's fields. `fields` maps from the schema's
        FileEditRequest fields. Marks row as manually_edited=1.

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

            sets = ["extraction_json=?", "manually_edited=1"]
            params: list[Any] = [ext_json]
            if new_status is not None:
                sets.append("status=?")
                params.append(new_status)
                # If user moves a manually-edited row back to pending, that's
                # an explicit re-queue; clear the manual flag so the worker
                # doesn't immediately skip it.
                if new_status == "pending":
                    sets[-2] = "manually_edited=0"
                    sets.append("extraction_json=NULL")
                    sets.append("indexed_at=NULL")
                    sets.append("used_thinking=0")
                    sets.append("error=''")
                    params.pop(0)  # remove ext_json arg
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

        Sets manually_edited=1 on each affected row.
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
                    c.execute(
                        "UPDATE files SET extraction_json=?, manually_edited=1 WHERE relative_path=?",
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
        with self.conn() as c:
            row = c.execute(
                "SELECT * FROM files WHERE status='pending' "
                "ORDER BY relative_path LIMIT 1"
            ).fetchone()
            return _row_to_record(row) if row else None

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
    ) -> list[FileRecord]:
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
        """One-pass authoritative recompute of is_duplicate / duplicate_group
        for every row. Returns the number of duplicate groups (size >= 2).
        """
        with self.conn() as c:
            c.execute("""
                UPDATE files
                   SET is_duplicate = 0,
                       duplicate_group = ''
            """)
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
        with the same SHA-256 as 'skipped'. The selected files themselves
        are NOT touched. Returns number of rows updated.
        """
        with self.conn() as c:
            shas = set()
            for p in relative_paths:
                row = c.execute(
                    "SELECT sha256 FROM files WHERE relative_path=?", (p,)
                ).fetchone()
                if row and row["sha256"]:
                    shas.add(row["sha256"])
            if not shas:
                return 0
            placeholders = ",".join("?" for _ in relative_paths)
            sha_placeholders = ",".join("?" for _ in shas)
            params: list[Any] = []
            params.extend(shas)
            params.extend(relative_paths)
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
        extraction=extraction,
        used_thinking=bool(row["used_thinking"]),
        manually_edited=manually_edited,
        is_duplicate=is_duplicate,
        duplicate_group=duplicate_group,
    )
