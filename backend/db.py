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

from .schemas import FileRecord, LLMExtraction, NamedEntities, now_iso

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
    extraction_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_files_status ON files(status);
CREATE INDEX IF NOT EXISTS idx_files_sha ON files(sha256);
"""


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

    def reset_to_pending(self, relative_paths: Iterable[str]) -> int:
        n = 0
        with self.conn() as c:
            for p in relative_paths:
                cur = c.execute(
                    """UPDATE files SET status='pending', error='', extraction_json=NULL,
                                        indexed_at=NULL, used_thinking=0
                       WHERE relative_path=?""",
                    (p,),
                )
                n += cur.rowcount
        return n

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
    ) -> None:
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
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY relative_path LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        with self.conn() as c:
            rows = c.execute(sql, params).fetchall()
            return [_row_to_record(r) for r in rows]

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
    )