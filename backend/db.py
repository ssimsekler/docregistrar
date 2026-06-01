"""SQLite-backed storage for file records, repository master data, and job state.

Schema v3 (current): files keyed by `id` (UUID4-equivalent, 32 hex chars),
with UNIQUE(repository, relative_path). Repository is now a column (was
previously inside extraction_json). Adds error_count + last_error_at for
automatic-retry capping.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

from .schemas import (
    FileRecord,
    GENERATED_ATTRIBUTE_FIELDS,
    KVPair,
    LLMExtraction,
    MAX_CUSTOM_PROPERTIES,
    MAX_ERROR_HISTORY,
    MAX_ERROR_TEXT_CHARS,
    NamedEntities,
    Repository,
    now_iso,
)

log = logging.getLogger("docregistrar.db")

SCHEMA_VERSION = 3

_FILES_COLUMNS_SQL = """
    id              TEXT PRIMARY KEY,
    repository      TEXT NOT NULL DEFAULT '',
    relative_path   TEXT NOT NULL,
    file_name       TEXT NOT NULL,
    extension       TEXT NOT NULL,
    file_size       INTEGER NOT NULL,
    sha256          TEXT NOT NULL,
    relative_folder_path TEXT NOT NULL DEFAULT '',
    page_count      INTEGER,
    os_created      TEXT,
    os_modified     TEXT,
    status          TEXT NOT NULL DEFAULT 'pending',
    error           TEXT NOT NULL DEFAULT '',
    error_count     INTEGER NOT NULL DEFAULT 0,
    last_error_at   TEXT,
    indexed_at      TEXT,
    indexing_started_at   TEXT,
    indexing_completed_at TEXT,
    used_thinking   INTEGER NOT NULL DEFAULT 0,
    extraction_json TEXT,
    manually_edited INTEGER NOT NULL DEFAULT 0,
    is_duplicate    INTEGER NOT NULL DEFAULT 0,
    duplicate_group TEXT NOT NULL DEFAULT '',
    UNIQUE (repository, relative_path)
"""

DDL_FRESH = f"""
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS files ({_FILES_COLUMNS_SQL});
CREATE INDEX IF NOT EXISTS idx_files_status     ON files(status);
CREATE INDEX IF NOT EXISTS idx_files_sha        ON files(sha256);
CREATE INDEX IF NOT EXISTS idx_files_dup        ON files(is_duplicate);
CREATE INDEX IF NOT EXISTS idx_files_repo       ON files(repository);
CREATE INDEX IF NOT EXISTS idx_files_repo_path  ON files(repository, relative_path);
CREATE TABLE IF NOT EXISTS repositories (
    name TEXT PRIMARY KEY,
    path TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
"""


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _trim_error(msg: str) -> str:
    if not msg:
        return ""
    if len(msg) <= MAX_ERROR_TEXT_CHARS:
        return msg
    return msg[: MAX_ERROR_TEXT_CHARS - 14] + " ...[truncated]"


def _serialize_extraction_data(data: dict) -> str:
    """Validate `data` as an LLMExtraction and return JSON. Strips the
    deprecated `repository` key (now a column). Falls back to plain dumps
    on validation error so we never lose user data."""
    data = dict(data) if data else {}
    data.pop("repository", None)
    try:
        ne = NamedEntities(**(data.pop("named_entities", {}) or {}))
        return LLMExtraction(named_entities=ne, **data).model_dump_json()
    except Exception:
        try:
            return json.dumps(data, ensure_ascii=False)
        except Exception:
            return "{}"


def _ext_data_from_row(extraction_json: Optional[str]) -> dict:
    if not extraction_json:
        return {}
    try:
        return json.loads(extraction_json)
    except Exception:
        return {}


def _append_error_history(ext_data: dict, *, stage: str, message: str) -> dict:
    history = ext_data.get("error_history") or []
    if not isinstance(history, list):
        history = []
    history.append({"at": _utc_iso(), "stage": stage or "", "message": _trim_error(message)})
    ext_data["error_history"] = history[-MAX_ERROR_HISTORY:]
    return ext_data


def _strip_repo_from_extraction_json_text(raw: Optional[str]) -> Optional[str]:
    """Pop `repository` from a serialized extraction_json blob (used by migration)."""
    if not raw:
        return raw
    try:
        data = json.loads(raw)
    except Exception:
        return raw
    if isinstance(data, dict) and "repository" in data:
        data.pop("repository", None)
        try:
            return json.dumps(data, ensure_ascii=False)
        except Exception:
            return raw
    return raw


_FILES_INSERT_COLS = (
    "id, repository, relative_path, file_name, extension, file_size, sha256, "
    "relative_folder_path, page_count, os_created, os_modified, status, error, "
    "error_count, last_error_at, indexed_at, indexing_started_at, indexing_completed_at, "
    "used_thinking, extraction_json, manually_edited, is_duplicate, duplicate_group"
)
_FILES_INSERT_PLACEHOLDERS = ",".join("?" * 23)


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

    @contextmanager
    def conn(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            c = self._connect()
            try:
                yield c
            finally:
                c.close()

    # ---------- init / migration ----------

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
            row = conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
            current = int(row["value"]) if row and row["value"] and row["value"].isdigit() else 0

            files_exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='files'"
            ).fetchone() is not None

            if not files_exists:
                conn.executescript(DDL_FRESH)
            else:
                if current < 3:
                    log.info(
                        "Migrating files table to schema v3 (id PK, repository column, "
                        "error_count, UNIQUE(repository, relative_path)). "
                        "Back up state.db before re-running if you need a rollback."
                    )
                    self._migrate_to_v3(conn)
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS repositories (
                        name TEXT PRIMARY KEY,
                        path TEXT NOT NULL DEFAULT '',
                        description TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_files_status     ON files(status);
                    CREATE INDEX IF NOT EXISTS idx_files_sha        ON files(sha256);
                    CREATE INDEX IF NOT EXISTS idx_files_dup        ON files(is_duplicate);
                    CREATE INDEX IF NOT EXISTS idx_files_repo       ON files(repository);
                    CREATE INDEX IF NOT EXISTS idx_files_repo_path  ON files(repository, relative_path);
                    """
                )

            existing_repos = {r["name"] for r in conn.execute("SELECT name FROM repositories").fetchall()}
            for r in conn.execute(
                "SELECT DISTINCT repository FROM files WHERE repository <> ''"
            ).fetchall():
                name = r["repository"]
                if name and name not in existing_repos:
                    conn.execute(
                        "INSERT INTO repositories(name, path, description, created_at) VALUES(?,?,?,?)",
                        (name, "", "", now_iso()),
                    )
                    log.info("Seeded repository from existing data: %r (path empty)", name)

            conn.execute(
                "INSERT INTO meta(key,value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )

    def _migrate_to_v3(self, conn: sqlite3.Connection) -> None:
        conn.execute("DROP TABLE IF EXISTS files_new")
        conn.execute(f"CREATE TABLE files_new ({_FILES_COLUMNS_SQL})")

        existing_cols = {r["name"] for r in conn.execute("PRAGMA table_info(files)").fetchall()}

        def col(name: str, default_sql: str = "NULL") -> str:
            return name if name in existing_cols else default_sql

        rfp = col("relative_folder_path", "''")
        idx_started = col("indexing_started_at")
        idx_completed = col("indexing_completed_at")
        manually = col("manually_edited", "0")
        is_dup = col("is_duplicate", "0")
        dup_grp = col("duplicate_group", "''")

        select_sql = f"""
            SELECT
                lower(hex(randomblob(16))) AS id,
                COALESCE(json_extract(extraction_json, '$.repository'), '') AS repository,
                relative_path, file_name, extension, file_size, sha256,
                {rfp} AS relative_folder_path,
                page_count, os_created, os_modified, status, error,
                0 AS error_count, NULL AS last_error_at,
                indexed_at,
                {idx_started} AS indexing_started_at,
                {idx_completed} AS indexing_completed_at,
                used_thinking, extraction_json,
                {manually} AS manually_edited,
                {is_dup} AS is_duplicate,
                {dup_grp} AS duplicate_group
            FROM files
        """
        rows = conn.execute(select_sql).fetchall()

        # De-duplicate by (repository, relative_path) — pre-v3 had only
        # relative_path uniqueness. Keep the first occurrence for each pair.
        seen: set[tuple[str, str]] = set()
        for r in rows:
            key = (r["repository"] or "", r["relative_path"])
            if key in seen:
                log.warning(
                    "Migration: dropping duplicate (repository=%r, relative_path=%r) "
                    "discovered during v3 upgrade.", key[0], key[1],
                )
                continue
            seen.add(key)
            ext_json_clean = _strip_repo_from_extraction_json_text(r["extraction_json"])
            conn.execute(
                f"""INSERT INTO files_new ({_FILES_INSERT_COLS}) VALUES ({_FILES_INSERT_PLACEHOLDERS})""",
                (
                    r["id"], r["repository"], r["relative_path"], r["file_name"],
                    r["extension"], r["file_size"], r["sha256"],
                    r["relative_folder_path"] or "",
                    r["page_count"], r["os_created"], r["os_modified"],
                    r["status"], r["error"] or "",
                    0, None,
                    r["indexed_at"], r["indexing_started_at"], r["indexing_completed_at"],
                    int(r["used_thinking"] or 0),
                    ext_json_clean,
                    int(r["manually_edited"] or 0),
                    int(r["is_duplicate"] or 0),
                    r["duplicate_group"] or "",
                ),
            )

        conn.execute("DROP TABLE files")
        conn.execute("ALTER TABLE files_new RENAME TO files")

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

    # ---------- internal: id resolution ----------

    def _resolve_id(self, c: sqlite3.Connection, relative_path: str,
                    repository: Optional[str] = None) -> Optional[str]:
        """Resolve a single row's id by relative_path. If `repository` is None
        and the relative_path matches multiple rows, returns None and logs a
        warning. If `repository` is provided, the lookup is exact.
        """
        if not relative_path:
            return None
        if repository is not None:
            row = c.execute(
                "SELECT id FROM files WHERE repository=? AND relative_path=?",
                (repository, relative_path),
            ).fetchone()
            return row["id"] if row else None
        rows = c.execute(
            "SELECT id, repository FROM files WHERE relative_path=?",
            (relative_path,),
        ).fetchall()
        if not rows:
            return None
        if len(rows) > 1:
            log.warning(
                "Ambiguous relative_path %r matches %d rows across repositories %r; "
                "caller should pass repository explicitly.",
                relative_path, len(rows), [r["repository"] for r in rows],
            )
            return None
        return rows[0]["id"]

    # ---------- repositories (master data) ----------

    def list_repositories(self) -> list[Repository]:
        with self.conn() as c:
            rows = c.execute(
                """
                SELECT r.name, r.path, r.description, r.created_at,
                       COALESCE((
                         SELECT COUNT(*) FROM files f
                          WHERE f.repository = r.name
                       ), 0) AS file_count
                  FROM repositories r
                 ORDER BY LOWER(r.name)
                """
            ).fetchall()
            return [
                Repository(
                    name=r["name"], path=r["path"] or "",
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
                "SELECT COUNT(*) AS n FROM files WHERE repository=?",
                (name,),
            ).fetchone()
            return Repository(
                name=row["name"], path=row["path"] or "",
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
            if c.execute("SELECT 1 FROM repositories WHERE name=?", (name,)).fetchone():
                raise ValueError(f"Repository already exists: {name!r}")
            c.execute(
                "INSERT INTO repositories(name, path, description, created_at) VALUES(?,?,?,?)",
                (name, path, description, now_iso()),
            )
        rec = self.get_repository(name)
        assert rec is not None
        return rec

    def update_repository(self, name: str, *, path: Optional[str] = None,
                          description: Optional[str] = None) -> Repository:
        with self.conn() as c:
            if not c.execute("SELECT 1 FROM repositories WHERE name=?", (name,)).fetchone():
                raise ValueError(f"Repository not found: {name!r}")
            sets: list[str] = []
            params: list[Any] = []
            if path is not None:
                p = (path or "").strip()
                if not p:
                    raise ValueError("Repository path cannot be empty.")
                sets.append("path=?"); params.append(p)
            if description is not None:
                sets.append("description=?"); params.append((description or "").strip())
            if sets:
                params.append(name)
                c.execute(f"UPDATE repositories SET {', '.join(sets)} WHERE name=?", params)
        rec = self.get_repository(name)
        assert rec is not None
        return rec

    def rename_repository(self, old_name: str, new_name: str) -> Repository:
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
            if c.execute("SELECT 1 FROM repositories WHERE name=?", (new_name,)).fetchone():
                raise ValueError(f"Target repository already exists: {new_name!r}")
            c.execute(
                "INSERT INTO repositories(name, path, description, created_at) VALUES(?,?,?,?)",
                (new_name, row["path"], row["description"], row["created_at"]),
            )
            c.execute("UPDATE files SET repository=? WHERE repository=?", (new_name, old_name))
            c.execute("DELETE FROM repositories WHERE name=?", (old_name,))
        rec = self.get_repository(new_name)
        assert rec is not None
        return rec

    def delete_repository(self, name: str, *, clear_files: bool = True) -> int:
        with self.conn() as c:
            if not c.execute("SELECT 1 FROM repositories WHERE name=?", (name,)).fetchone():
                raise ValueError(f"Repository not found: {name!r}")
            n = 0
            if clear_files:
                cur = c.execute("UPDATE files SET repository='' WHERE repository=?", (name,))
                n = cur.rowcount or 0
            c.execute("DELETE FROM repositories WHERE name=?", (name,))
        return n

    def repo_path_for_file(self, relative_path: str,
                           repository: Optional[str] = None) -> Optional[str]:
        with self.conn() as c:
            if repository is not None:
                row = c.execute(
                    """SELECT r.path FROM files f
                         LEFT JOIN repositories r ON r.name = f.repository
                        WHERE f.repository=? AND f.relative_path=?""",
                    (repository, relative_path),
                ).fetchone()
            else:
                row = c.execute(
                    """SELECT r.path FROM files f
                         LEFT JOIN repositories r ON r.name = f.repository
                        WHERE f.relative_path=?""",
                    (relative_path,),
                ).fetchone()
            if row is None:
                return None
            return row["path"] or None

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
        repository: str = "",
    ) -> str:
        """Insert a new pending row, or update size/sha/dates if file changed.

        Scoped by `repository`: a row is uniquely identified by (repository,
        relative_path). Returns the new status of the row.
        """
        with self.conn() as c:
            row = c.execute(
                "SELECT id, sha256, status, relative_folder_path FROM files "
                "WHERE repository=? AND relative_path=?",
                (repository, relative_path),
            ).fetchone()
            if row is None:
                new_id_row = c.execute("SELECT lower(hex(randomblob(16))) AS id").fetchone()
                new_id = new_id_row["id"]
                c.execute(
                    f"INSERT INTO files ({_FILES_INSERT_COLS}) "
                    f"VALUES ({_FILES_INSERT_PLACEHOLDERS})",
                    (
                        new_id, repository, relative_path, file_name, extension,
                        file_size, sha256, relative_folder_path,
                        None, os_created, os_modified, "pending", "",
                        0, None, None, None, None,
                        0, None, 0, 0, "",
                    ),
                )
                return "pending"

            if row["sha256"] != sha256:
                c.execute(
                    """UPDATE files
                          SET file_size=?, sha256=?, os_created=?, os_modified=?,
                              relative_folder_path=?,
                              status='pending', error='', extraction_json=NULL,
                              indexed_at=NULL, used_thinking=0,
                              error_count=0, last_error_at=NULL
                        WHERE id=?""",
                    (file_size, sha256, os_created, os_modified,
                     relative_folder_path, row["id"]),
                )
                return "pending"

            if (row["relative_folder_path"] or "") != relative_folder_path:
                c.execute(
                    "UPDATE files SET relative_folder_path=? WHERE id=?",
                    (relative_folder_path, row["id"]),
                )
            return row["status"]

    def assign_repository(self, relative_path: str, repository: str,
                          *, current_repository: Optional[str] = None) -> None:
        """Set the repository column for a file WITHOUT touching manually_edited.
        If `current_repository` is provided, the lookup is exact by
        (current_repository, relative_path); otherwise it falls back to
        the unique relative_path match (skipped if ambiguous).
        """
        with self.conn() as c:
            file_id = self._resolve_id(c, relative_path, current_repository)
            if file_id is None:
                return
            c.execute("UPDATE files SET repository=? WHERE id=?", (repository, file_id))

    def mark_status(self, relative_path: str, status: str, error: str = "",
                    *, repository: Optional[str] = None,
                    stage: str = "") -> None:
        """Update a file's status. For status='error', this also bumps
        error_count, sets last_error_at, and appends to error_history in
        extraction_json. The error string is capped at MAX_ERROR_TEXT_CHARS.
        """
        with self.conn() as c:
            file_id = self._resolve_id(c, relative_path, repository)
            if file_id is None:
                return
            error_trimmed = _trim_error(error or "")
            if status == "processing":
                started = _utc_iso()
                c.execute(
                    "UPDATE files SET status=?, error=?, indexing_started_at=? WHERE id=?",
                    (status, error_trimmed, started, file_id),
                )
                return
            if status == "error":
                row = c.execute(
                    "SELECT extraction_json, error_count FROM files WHERE id=?",
                    (file_id,),
                ).fetchone()
                ext_data = _ext_data_from_row(row["extraction_json"] if row else None)
                _append_error_history(ext_data, stage=stage, message=error_trimmed)
                ext_json = _serialize_extraction_data(ext_data)
                new_count = int(row["error_count"] if row else 0) + 1
                c.execute(
                    """UPDATE files
                          SET status=?, error=?, error_count=?, last_error_at=?,
                              extraction_json=?
                        WHERE id=?""",
                    (status, error_trimmed, new_count, _utc_iso(), ext_json, file_id),
                )
                return
            c.execute(
                "UPDATE files SET status=?, error=? WHERE id=?",
                (status, error_trimmed, file_id),
            )

    def reset_to_pending(self, relative_paths: Iterable[str], *, force: bool = False,
                         repository: Optional[str] = None) -> tuple[int, int, int]:
        """Set rows back to 'pending' so they get re-processed. Clears
        error_count. If `force=False`, manually_edited rows are SKIPPED.
        Skipped-status rows are ALWAYS refused.

        Returns (n_reset, n_skipped_due_to_manual_edit, n_skipped_status_skipped).
        """
        n_reset = 0
        n_skipped = 0
        n_skipped_status = 0
        skip_msg = ("File is in 'skipped' status and cannot be re-evaluated. "
                    "Set its status to 'pending' first to re-evaluate.")
        with self.conn() as c:
            for p in relative_paths:
                file_id = self._resolve_id(c, p, repository)
                if file_id is None:
                    continue
                row = c.execute(
                    "SELECT manually_edited, status FROM files WHERE id=?",
                    (file_id,),
                ).fetchone()
                if row is None:
                    continue
                if row["status"] == "skipped":
                    c.execute(
                        "UPDATE files SET error=? WHERE id=?",
                        (skip_msg, file_id),
                    )
                    n_skipped_status += 1
                    continue
                if not force and row["manually_edited"]:
                    n_skipped += 1
                    continue
                cur = c.execute(
                    """UPDATE files
                          SET status='pending', error='', extraction_json=NULL,
                              indexed_at=NULL, used_thinking=0,
                              manually_edited=0,
                              error_count=0, last_error_at=NULL
                        WHERE id=?""",
                    (file_id,),
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
        repository: Optional[str] = None,
    ) -> None:
        """Persist a fresh LLM extraction and clear any error_count/last_error_at."""
        with self.conn() as c:
            file_id = self._resolve_id(c, relative_path, repository)
            if file_id is None:
                return
            row = c.execute(
                "SELECT extraction_json, repository FROM files WHERE id=?",
                (file_id,),
            ).fetchone()

            # Preserve previously-saved custom_properties.
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

            # Repository column: assign default_repository iff currently empty.
            current_repo = (row["repository"] if row else "") or ""
            new_repo = current_repo
            if default_repository and not current_repo:
                new_repo = default_repository

            completed = _utc_iso()
            c.execute(
                """UPDATE files
                      SET page_count=?, extraction_json=?, status='done', error='',
                          indexed_at=?, used_thinking=?, indexing_completed_at=?,
                          error_count=0, last_error_at=NULL,
                          repository=?
                    WHERE id=?""",
                (
                    page_count,
                    extraction.model_dump_json(),
                    now_iso(),
                    1 if used_thinking else 0,
                    completed,
                    new_repo,
                    file_id,
                ),
            )

    def update_fields(self, relative_path: str, fields: dict,
                      *, repository: Optional[str] = None) -> bool:
        """Edit a single row's fields. Only provided fields are updated.

        manually_edited is set to 1 ONLY if at least one GENERATED attribute
        is in the payload. Editing only user-managed fields (repository,
        source_url_*, custom_properties) does NOT flip the flag.

        Special handling:
          - 'status' / 'error' update dedicated columns.
          - 'persons'/'organizations'/'locations'/'mentioned_dates'/
            'products_technologies' go INTO extraction_json.named_entities.
          - 'repository' updates the dedicated column (NOT the JSON).
          - All other LLM-style fields go INTO extraction_json.

        Returns True if the row existed.
        """
        with self.conn() as c:
            file_id = self._resolve_id(c, relative_path, repository)
            if file_id is None:
                return False
            row = c.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
            if row is None:
                return False

            new_status = fields.pop("status", None)
            new_error = fields.pop("error", None)
            new_repository = fields.pop("repository", None)  # column, not JSON

            edits_generated = any(k in GENERATED_ATTRIBUTE_FIELDS for k in fields.keys())

            ext_data: dict = _ext_data_from_row(row["extraction_json"])
            ne_data = ext_data.get("named_entities") or {}

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

            for k, v in list(fields.items()):
                ext_data[k] = v if v is not None else ""

            ext_data["named_entities"] = ne_data
            ext_json = _serialize_extraction_data(ext_data)

            sets = ["extraction_json=?"]
            params: list[Any] = [ext_json]
            if edits_generated:
                sets.append("manually_edited=1")
            if new_repository is not None:
                sets.append("repository=?"); params.append(str(new_repository))
            if new_status is not None:
                sets.append("status=?"); params.append(new_status)
                if new_status == "pending":
                    # Re-queue: clear extracted data, manual flag, and error counters.
                    sets = [
                        "manually_edited=0", "status=?", "extraction_json=NULL",
                        "indexed_at=NULL", "used_thinking=0", "error=''",
                        "error_count=0", "last_error_at=NULL",
                    ]
                    params = [new_status]
                    if new_repository is not None:
                        sets.append("repository=?")
                        params.append(str(new_repository))
            if new_error is not None:
                sets.append("error=?"); params.append(_trim_error(str(new_error)))

            sql = f"UPDATE files SET {', '.join(sets)} WHERE id=?"
            params.append(file_id)
            c.execute(sql, params)
            return True

    def bulk_set(self, relative_paths: Iterable[str], *,
                 repository: Optional[str] = None,
                 status: Optional[str] = None,
                 source_repository: Optional[str] = None) -> int:
        """Apply a status and/or repository to many files at once.

        `source_repository`, when given, scopes the lookup to rows currently
        in that repository; otherwise rows are looked up by relative_path
        (skipped if ambiguous).
        Repository-only edits do NOT set manually_edited=1.
        Status changes set manually_edited=1 (except status='pending', which
        clears the flag and re-queues).
        """
        n = 0
        with self.conn() as c:
            for p in relative_paths:
                file_id = self._resolve_id(c, p, source_repository)
                if file_id is None:
                    continue

                if repository is not None:
                    c.execute(
                        "UPDATE files SET repository=? WHERE id=?",
                        (str(repository), file_id),
                    )

                if status is not None:
                    if status == "pending":
                        c.execute(
                            """UPDATE files
                                  SET status='pending', error='',
                                      extraction_json=NULL, indexed_at=NULL,
                                      used_thinking=0, manually_edited=0,
                                      error_count=0, last_error_at=NULL
                                WHERE id=?""",
                            (file_id,),
                        )
                    else:
                        c.execute(
                            "UPDATE files SET status=?, manually_edited=1 WHERE id=?",
                            (status, file_id),
                        )

                n += 1
        return n

    def next_pending(self, *, max_error_retries: int = 5,
                     repository: Optional[str] = None) -> Optional[FileRecord]:
        """Return the next file the worker should process.

        Priority:
          1. status='pending' rows (oldest path first)
          2. then status='error' rows whose error_count < max_error_retries,
             ordered by error_count ASC (least-failed first), then last_error_at
             ASC (oldest first within the same retry tier).

        If `repository` is given, the search is scoped to that repository.
        """
        with self.conn() as c:
            params: list[Any] = []
            repo_clause = ""
            if repository is not None:
                repo_clause = "AND repository=?"
                params.append(repository)

            row = c.execute(
                f"SELECT * FROM files WHERE status='pending' {repo_clause} "
                f"ORDER BY relative_path LIMIT 1",
                params,
            ).fetchone()
            if row is not None:
                return _row_to_record(row)

            err_params = list(params) + [int(max_error_retries)]
            row = c.execute(
                f"""SELECT * FROM files
                      WHERE status='error' {repo_clause}
                        AND error_count < ?
                      ORDER BY error_count ASC,
                               COALESCE(last_error_at, '') ASC,
                               relative_path
                      LIMIT 1""",
                err_params,
            ).fetchone()
            return _row_to_record(row) if row else None

    def find_done_sibling(self, sha256: str, exclude_id: str) -> Optional[FileRecord]:
        """Return any other file with the same SHA-256 that's already 'done'."""
        if not sha256:
            return None
        with self.conn() as c:
            row = c.execute(
                "SELECT * FROM files WHERE sha256=? AND id<>? "
                "AND status='done' AND extraction_json IS NOT NULL "
                "ORDER BY relative_path LIMIT 1",
                (sha256, exclude_id),
            ).fetchone()
            return _row_to_record(row) if row else None

    def copy_extraction_from_sibling(
        self,
        target_id: str,
        source_id: str,
        *,
        default_repository: str = "",
    ) -> bool:
        """Copy `extraction_json` and `page_count` from one row to another (by id)."""
        ts = _utc_iso()
        with self.conn() as c:
            src = c.execute(
                "SELECT extraction_json, page_count FROM files WHERE id=?",
                (source_id,),
            ).fetchone()
            if src is None or not src["extraction_json"]:
                return False

            try:
                ext_data = json.loads(src["extraction_json"])
            except Exception:
                return False

            ext_json = _serialize_extraction_data(ext_data)

            tgt_row = c.execute(
                "SELECT repository FROM files WHERE id=?", (target_id,)
            ).fetchone()
            current_repo = (tgt_row["repository"] if tgt_row else "") or ""
            new_repo = current_repo if current_repo else default_repository

            cur = c.execute(
                """UPDATE files
                      SET extraction_json=?, page_count=?, status='done', error='',
                          indexed_at=?, indexing_started_at=?, indexing_completed_at=?,
                          used_thinking=0, manually_edited=0,
                          error_count=0, last_error_at=NULL,
                          repository=?
                    WHERE id=?""",
                (ext_json, src["page_count"], now_iso(), ts, ts, new_repo, target_id),
            )
            return cur.rowcount > 0

    def get_file(self, relative_path: str,
                 repository: Optional[str] = None) -> Optional[FileRecord]:
        with self.conn() as c:
            file_id = self._resolve_id(c, relative_path, repository)
            if file_id is None:
                return None
            row = c.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
            return _row_to_record(row) if row else None

    def get_file_by_id(self, file_id: str) -> Optional[FileRecord]:
        if not file_id:
            return None
        with self.conn() as c:
            row = c.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
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
          - `""`            : return only rows whose repository is empty.
          - `"X"`           : return only rows whose repository == "X".
        """
        sql = "SELECT * FROM files"
        clauses = []
        params: list[Any] = []
        if status:
            clauses.append("status=?"); params.append(status)
        if search:
            clauses.append("(relative_path LIKE ? OR file_name LIKE ?)")
            like = f"%{search}%"
            params.extend([like, like])
        if sha256:
            clauses.append("sha256=?"); params.append(sha256)
        if duplicates_only:
            clauses.append("is_duplicate=1")
        if repository is not None:
            clauses.append("repository=?")
            params.append(repository)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY relative_path LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        with self.conn() as c:
            rows = c.execute(sql, params).fetchall()
            return [_row_to_record(r) for r in rows]

    def list_dup_siblings(self, relative_path: str,
                          repository: Optional[str] = None) -> list[FileRecord]:
        """Return all OTHER files that share the same SHA-256 as this one."""
        with self.conn() as c:
            file_id = self._resolve_id(c, relative_path, repository)
            if file_id is None:
                return []
            cur = c.execute("SELECT sha256 FROM files WHERE id=?", (file_id,)).fetchone()
            if cur is None:
                return []
            sha = cur["sha256"]
            rows = c.execute(
                "SELECT * FROM files WHERE sha256=? AND id<>? "
                "ORDER BY relative_path",
                (sha, file_id),
            ).fetchall()
            return [_row_to_record(r) for r in rows]

    def recompute_duplicates(self) -> int:
        """One-pass authoritative recompute of is_duplicate / duplicate_group."""
        with self.conn() as c:
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
            row = c.execute(
                """SELECT COUNT(*) AS n FROM (
                       SELECT 1 FROM files GROUP BY sha256 HAVING COUNT(*) >= 2
                   )"""
            ).fetchone()
            return int(row["n"]) if row else 0

    def skip_dup_siblings_of(self, relative_paths: Iterable[str],
                             repository: Optional[str] = None) -> int:
        """For every SHA-256 in the given selection, mark every OTHER file
        with the same SHA-256 as 'skipped'."""
        with self.conn() as c:
            shas: set[str] = set()
            ids: list[str] = []
            for p in relative_paths:
                file_id = self._resolve_id(c, p, repository)
                if file_id is None:
                    continue
                ids.append(file_id)
                row = c.execute("SELECT sha256 FROM files WHERE id=?", (file_id,)).fetchone()
                if row and row["sha256"]:
                    shas.add(row["sha256"])
            if not shas or not ids:
                return 0
            sha_placeholders = ",".join("?" for _ in shas)
            id_placeholders = ",".join("?" for _ in ids)
            params: list[Any] = []
            params.extend(shas)
            params.extend(ids)
            cur = c.execute(
                f"""UPDATE files
                       SET status='skipped', manually_edited=1
                     WHERE sha256 IN ({sha_placeholders})
                       AND id NOT IN ({id_placeholders})""",
                params,
            )
            return cur.rowcount

    def list_all_for_excel(self) -> list[FileRecord]:
        with self.conn() as c:
            rows = c.execute(
                "SELECT * FROM files ORDER BY repository, relative_path"
            ).fetchall()
            return [_row_to_record(r) for r in rows]

    def counts_by_status(self, repository: Optional[str] = None) -> dict[str, int]:
        """Counts grouped by status.

        `repository` semantics:
          - None : all repositories (legacy behavior).
          - ""   : only files with no repository assigned.
          - "X"  : only files in that repository.
        """
        with self.conn() as c:
            if repository is None:
                rows = c.execute(
                    "SELECT status, COUNT(*) AS n FROM files GROUP BY status"
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT status, COUNT(*) AS n FROM files "
                    "WHERE repository=? GROUP BY status",
                    (repository,),
                ).fetchall()
            return {r["status"]: r["n"] for r in rows}

    def total_count(self, repository: Optional[str] = None) -> int:
        """Total file count.

        `repository` semantics match `counts_by_status`.
        """
        with self.conn() as c:
            if repository is None:
                row = c.execute("SELECT COUNT(*) AS n FROM files").fetchone()
            else:
                row = c.execute(
                    "SELECT COUNT(*) AS n FROM files WHERE repository=?",
                    (repository,),
                ).fetchone()
            return int(row["n"]) if row else 0

    def delete_files_not_in(self, present_paths: set[str]) -> int:
        """Remove DB rows for files no longer on disk (across ALL repositories).
        Note: with repo-scoped scans, prefer `delete_files_not_in_repo`."""
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
                cur = c.execute("DELETE FROM files WHERE relative_path=?", (p,))
                n += cur.rowcount
            return n

    def delete_files_not_in_repo(self, repository: str, present_paths: set[str]) -> int:
        """Like delete_files_not_in, but limited to files in a given repository."""
        if not repository:
            return 0
        with self.conn() as c:
            existing = {
                r["relative_path"]
                for r in c.execute(
                    "SELECT relative_path FROM files WHERE repository=?",
                    (repository,),
                ).fetchall()
            }
            stale = existing - present_paths
            n = 0
            for p in stale:
                cur = c.execute(
                    "DELETE FROM files WHERE repository=? AND relative_path=?",
                    (repository, p),
                )
                n += cur.rowcount
            return n

    def delete_files(self, relative_paths: Iterable[str],
                     *, repository: Optional[str] = None) -> int:
        """Permanently remove the given rows from the registry."""
        n = 0
        with self.conn() as c:
            for p in relative_paths:
                if not p:
                    continue
                file_id = self._resolve_id(c, p, repository)
                if file_id is None:
                    continue
                cur = c.execute("DELETE FROM files WHERE id=?", (file_id,))
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
            data.pop("repository", None)  # column is the source of truth
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
        id=_opt("id", "") or "",
        relative_path=row["relative_path"],
        relative_folder_path=_opt("relative_folder_path", "") or "",
        file_name=row["file_name"],
        extension=row["extension"],
        file_size=int(row["file_size"]),
        sha256=row["sha256"],
        repository=_opt("repository", "") or "",
        page_count=row["page_count"],
        os_created=row["os_created"],
        os_modified=row["os_modified"],
        status=row["status"],
        error=row["error"] or "",
        error_count=int(_opt("error_count", 0) or 0),
        last_error_at=_opt("last_error_at"),
        indexed_at=row["indexed_at"],
        indexing_started_at=_opt("indexing_started_at"),
        indexing_completed_at=_opt("indexing_completed_at"),
        extraction=extraction,
        used_thinking=bool(row["used_thinking"]),
        manually_edited=manually_edited,
        is_duplicate=is_duplicate,
        duplicate_group=duplicate_group,
    )
