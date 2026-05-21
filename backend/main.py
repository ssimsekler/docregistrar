"""FastAPI app: serves UI + REST + WebSocket."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import PROJECT_ROOT, load_config
from .db import Database
from .excel_writer import build_registry_bytes
from .jobmanager import JobManager
from .schemas import (
    BulkEditRequest,
    DeleteFilesRequest,
    FileEditRequest,
    ReevaluateRequest,
    SetRepositoryRequest,
    SkipDupSiblingsRequest,
    StartRequest,
)

# Extensions blocked by POST /api/open-file. Everything else is allowed,
# including macro-enabled Office files (.xlsm, .docm, .pptm, .xlsb), which
# are documents (the host app prompts before running macros).
BLOCKED_OPEN_EXTENSIONS = {
    ".exe", ".bat", ".cmd", ".com", ".msi", ".scr",
    ".ps1", ".vbs", ".vbe", ".js", ".jse",
    ".wsf", ".wsh", ".pif", ".lnk",
}


class OpenFileRequest(BaseModel):
    relative_path: str

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("docregistrar.main")

cfg = load_config()
DATA_DIR = PROJECT_ROOT / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

db = Database(DATA_DIR / "state.db")
job = JobManager(cfg, db, DATA_DIR)

app = FastAPI(title="docregistrar", version="0.2.0")

FRONTEND_DIR = PROJECT_ROOT / "frontend"


@app.on_event("startup")
async def _on_startup() -> None:
    job.attach_loop(asyncio.get_running_loop())
    log.info("App started. UI at http://%s:%s/", cfg.server.host, cfg.server.port)


@app.on_event("shutdown")
async def _on_shutdown() -> None:
    """Graceful shutdown for Ctrl+C from run.bat.

    Closes the in-flight LLM HTTP socket and joins the worker thread so
    uvicorn can exit promptly.
    """
    log.info("Shutdown requested - stopping worker and closing LLM client...")
    try:
        job.shutdown_blocking(timeout=5.0)
    except Exception as e:
        log.warning("Error during shutdown: %s", e)
    log.info("Shutdown complete.")


# -------- API --------

@app.get("/api/config")
def api_config():
    return {
        "server": cfg.server.model_dump(),
        "llm_model": cfg.llm.model,
        "llm_base_url": cfg.llm.base_url,
        "include_extensions": cfg.include_extensions,
        "default_target_folder": cfg.target_folder,
    }


@app.post("/api/pick-folder")
def api_pick_folder():
    """Open a native OS folder picker on the machine running the server."""
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception as e:
        raise HTTPException(500, f"tkinter not available: {e}")

    root = tk.Tk()
    try:
        root.withdraw()
        root.attributes("-topmost", True)
        try:
            root.update_idletasks()
        except Exception:
            pass
        path = filedialog.askdirectory(
            parent=root,
            title="Select folder to scan",
            mustexist=True,
        )
    finally:
        try:
            root.destroy()
        except Exception:
            pass

    return {"path": path or ""}


@app.get("/api/progress")
def api_progress():
    return job.snapshot().model_dump()


@app.post("/api/start")
def api_start(req: StartRequest):
    if not req.target_folder.strip():
        raise HTTPException(400, "target_folder is required")
    job.start(
        req.target_folder,
        req.registry_xlsx,
        default_repository=req.default_repository,
    )
    return job.snapshot().model_dump()


@app.post("/api/pause")
def api_pause():
    job.pause()
    return job.snapshot().model_dump()


@app.post("/api/resume")
def api_resume():
    job.resume()
    return job.snapshot().model_dump()


@app.post("/api/stop")
def api_stop():
    job.stop()
    return job.snapshot().model_dump()


@app.post("/api/repository")
def api_set_repository(req: SetRepositoryRequest):
    """Update the default repository assigned to files processed from now on."""
    job.set_default_repository(req.repository)
    return {"repository": req.repository}


@app.get("/api/repositories")
def api_list_repositories():
    """List all distinct, non-empty Repository values currently in use across
    the registry, with usage counts. Powers the "Browse repositories"
    picker in the UI.
    """
    return {"items": db.list_repositories()}


@app.post("/api/reevaluate")
def api_reevaluate(req: ReevaluateRequest):
    if not req.relative_paths:
        raise HTTPException(400, "relative_paths is required")
    n_reset, n_skipped, n_skipped_status = job.reevaluate(
        req.relative_paths,
        use_thinking=req.use_thinking,
        force=req.force,
    )
    # Item 5: re-eval changes status counts (rows flip back to 'pending');
    # push fresh stats to clients so the header refreshes immediately.
    job.broadcast_progress()
    return {
        "reset": n_reset,
        "skipped_manual": n_skipped,
        # Files refused because they're in 'skipped' status. Caller should
        # set their status to 'pending' first to re-evaluate them.
        "skipped_status": n_skipped_status,
        "use_thinking": req.use_thinking,
        "force": req.force,
    }


@app.post("/api/file/edit")
def api_file_edit(relative_path: str, body: FileEditRequest):
    """Edit one row's fields. Only fields the caller provides are changed."""
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(400, "no fields to update")
    ok = db.update_fields(relative_path, fields)
    if not ok:
        raise HTTPException(404, f"Not found: {relative_path}")
    # If user re-queued the row, kick the worker
    if fields.get("status") == "pending":
        job._wakeup.set()  # noqa: SLF001 (intentional)
    # Item 7: if the user just flipped this row to 'skipped' AND it's the
    # file currently being processed, signal the worker so it aborts cleanly.
    if fields.get("status") == "skipped":
        job.signal_skip(relative_path)
    # Item 5: any field edit may change the visible counts (e.g. status
    # changes, repository changes). Push fresh stats to all WS clients.
    job.broadcast_progress()
    rec = db.get_file(relative_path)
    return rec.model_dump() if rec else {"ok": True}


@app.post("/api/files/bulk-edit")
def api_files_bulk_edit(body: BulkEditRequest):
    if not body.relative_paths:
        raise HTTPException(400, "relative_paths is required")
    if body.repository is None and body.status is None:
        raise HTTPException(400, "provide repository and/or status")
    n = db.bulk_set(
        body.relative_paths,
        repository=body.repository,
        status=body.status,
    )
    if body.status == "pending":
        job._wakeup.set()  # noqa: SLF001
    # Item 7: same as single edit — propagate skip signals to the worker.
    if body.status == "skipped":
        for p in body.relative_paths:
            job.signal_skip(p)
    # Item 5: bulk edits commonly change status counts; push a fresh snapshot.
    job.broadcast_progress()
    return {"updated": n}


@app.get("/api/files")
def api_files(
    status: str | None = None,
    search: str = "",
    limit: int = 1000,
    offset: int = 0,
    sha256: str | None = None,
    duplicates_only: bool = False,
    # Item 6: filter the grid by Repository.
    #   - parameter omitted entirely  -> no repository filter (all rows)
    #   - parameter present but empty -> rows whose repository is empty
    #   - parameter present, non-empty -> exact-match repository filter
    repository: str | None = None,
):
    records = db.list_files(
        status=status if status else None,
        limit=max(1, min(5000, limit)),
        offset=max(0, offset),
        search=search,
        sha256=sha256 if sha256 else None,
        duplicates_only=duplicates_only,
        repository=repository,
    )
    out = []
    for r in records:
        d = r.model_dump()
        e = d.pop("extraction", None) or {}
        ne = (e.get("named_entities") or {}) if e else {}
        d["title"] = e.get("title", "")
        d["description"] = e.get("description", "")
        d["document_type"] = e.get("document_type", "")
        d["language"] = e.get("language", "")
        d["confidentiality"] = e.get("confidentiality", "")
        d["quality_score"] = e.get("quality_score", "")
        d["document_date"] = e.get("document_date", "")
        d["authors"] = e.get("authors", [])
        d["tags"] = e.get("tags", [])
        d["geographic_scope"] = e.get("geographic_scope", "")
        d["industry_domain"] = e.get("industry_domain", "")
        d["repository"] = e.get("repository", "")
        d["products_technologies"] = ne.get("products_technologies", [])
        d["custom_properties"] = e.get("custom_properties", [])
        out.append(d)
    return {"items": out, "count": len(out)}


@app.get("/api/file")
def api_file(relative_path: str):
    rec = db.get_file(relative_path)
    if rec is None:
        raise HTTPException(404, f"Not found: {relative_path}")
    return rec.model_dump()


@app.get("/api/file/dup-siblings")
def api_file_dup_siblings(relative_path: str):
    """Return all OTHER files with the same SHA-256 as this one (not this one)."""
    sibs = db.list_dup_siblings(relative_path)
    return {
        "relative_path": relative_path,
        "siblings": [s.model_dump() for s in sibs],
        "count": len(sibs),
    }


@app.post("/api/files/skip-dup-siblings")
def api_skip_dup_siblings(body: SkipDupSiblingsRequest):
    """For every file in `relative_paths`, mark every OTHER file sharing the
    same SHA-256 as 'skipped'. The given files themselves are NOT modified.
    """
    if not body.relative_paths:
        raise HTTPException(400, "relative_paths is required")
    n = db.skip_dup_siblings_of(body.relative_paths)
    # Item 5: rows have flipped to 'skipped' so the header counts changed.
    job.broadcast_progress()
    return {"updated": n}


@app.post("/api/files/delete")
def api_files_delete(body: DeleteFilesRequest):
    """Permanently remove the given files from the registry.

    The actual file on disk is NOT deleted. If the containing folder is
    later rescanned, each file will be re-discovered and added back as a
    fresh 'pending' entry.

    If any of the targeted files is currently being processed by the
    worker, we send a skip signal first so the worker stops touching it
    and doesn't try to write back a row that's about to be deleted.
    """
    if not body.relative_paths:
        raise HTTPException(400, "relative_paths is required")
    # If any of them is the currently-processing file, signal the worker to
    # abort that file cleanly (it'll be requeued/skipped). Then we delete.
    for p in body.relative_paths:
        if p:
            job.signal_skip(p)
    n = db.delete_files(body.relative_paths)
    # Item 5: counts changed -> push fresh stats.
    job.broadcast_progress()
    return {"deleted": n}


@app.get("/api/registry.xlsx")
def api_registry_download():
    """Download the current registry as an Excel file. Snapshot at click time."""
    records = db.list_all_for_excel()
    data = build_registry_bytes(records)
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    filename = f"docregistrar_{ts}.xlsx"
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _resolve_file_path(rel: str) -> Path:
    """Resolve the absolute Path on disk for a registry row, even when the
    JobManager is idle (items 1, 2, 3).

    Strategy:
      1. Look up the row in the DB. If it has a non-empty `full_path`, use it.
      2. Otherwise try `full_folder_path` + file_name.
      3. As a last resort, fall back to the active `target_folder` + rel.
    Raises HTTPException on any failure.
    """
    rec = db.get_file(rel)
    if rec is None:
        raise HTTPException(404, f"File not found in registry: {rel}")

    candidate: Optional[Path] = None
    if rec.full_path:
        candidate = Path(rec.full_path)
    elif rec.full_folder_path and rec.file_name:
        candidate = Path(rec.full_folder_path) / rec.file_name

    if candidate is None:
        target_folder = job.snapshot().target_folder
        if not target_folder:
            raise HTTPException(
                400,
                "No stored full_path for this file and no active target "
                "folder. Re-scan the folder so paths get recorded.",
            )
        candidate = Path(target_folder) / rel

    try:
        return candidate.resolve()
    except OSError:
        return candidate


@app.post("/api/open-file")
def api_open_file(req: OpenFileRequest):
    """Open a file from the registry in the OS default app.

    Item 1 + 3: works even when the JobManager is idle, because we look up
    the file's stored absolute path from the DB instead of requiring an
    active target_folder.

    Safety:
      - Extensions in BLOCKED_OPEN_EXTENSIONS (executables / scripts) are
        refused with HTTP 403.
      - Macro-enabled Office files (.xlsm, .docm, .pptm, .xlsb) are allowed:
        they are documents, and the host app shows its own macro warning.
    """
    rel = (req.relative_path or "").strip()
    if not rel:
        raise HTTPException(400, "relative_path is required")

    full = _resolve_file_path(rel)

    if not full.exists() or not full.is_file():
        raise HTTPException(404, f"File not found on disk: {full}")

    ext = full.suffix.lower()
    if ext in BLOCKED_OPEN_EXTENSIONS:
        raise HTTPException(
            403,
            f"Refusing to open '{ext}' files for safety. "
            f"Blocked: {', '.join(sorted(BLOCKED_OPEN_EXTENSIONS))}",
        )

    try:
        if sys.platform.startswith("win"):
            os.startfile(str(full))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            import subprocess
            subprocess.Popen(["open", str(full)])
        else:
            import subprocess
            subprocess.Popen(["xdg-open", str(full)])
    except Exception as e:
        raise HTTPException(500, f"Failed to open file: {e}")

    return {"ok": True, "opened": rel, "full_path": str(full)}


@app.post("/api/open-file-location")
def api_open_file_location(req: OpenFileRequest):
    """Open the OS file explorer at the file's parent folder, with the
    file selected/highlighted when the platform supports it.

    Item 2 + 3: works even when the JobManager is idle, by using the
    stored full_path / full_folder_path from the DB.
    """
    rel = (req.relative_path or "").strip()
    if not rel:
        raise HTTPException(400, "relative_path is required")

    rec = db.get_file(rel)
    if rec is None:
        raise HTTPException(404, f"File not found in registry: {rel}")

    # Prefer the explicit folder column; fall back to the file's parent.
    full = _resolve_file_path(rel)
    if rec.full_folder_path:
        try:
            parent = Path(rec.full_folder_path).resolve()
        except OSError:
            parent = Path(rec.full_folder_path)
    else:
        parent = full.parent

    if not parent.exists():
        # As a last resort, also try the file's parent (might differ if
        # the stored full_folder_path is stale).
        if full.parent.exists():
            parent = full.parent
        else:
            raise HTTPException(404, f"Folder not found: {parent}")

    try:
        if sys.platform.startswith("win"):
            if full.exists():
                # Open Explorer with the file pre-selected and its folder
                # showing (item 2). The /select switch DOES open Explorer
                # at that folder; we just need to make sure we pass the
                # full file path, not just the folder.
                import subprocess
                # NOTE: ", " between /select and the path is intentional —
                # explorer.exe is finicky about syntax. Both forms work,
                # but using a single argument is safest.
                subprocess.Popen(f'explorer /select,"{str(full)}"')
            else:
                # File is gone; just open the parent folder.
                os.startfile(str(parent))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            import subprocess
            if full.exists():
                subprocess.Popen(["open", "-R", str(full)])
            else:
                subprocess.Popen(["open", str(parent)])
        else:
            import subprocess
            subprocess.Popen(["xdg-open", str(parent)])
    except Exception as e:
        raise HTTPException(500, f"Failed to open file location: {e}")

    return {
        "ok": True,
        "revealed": rel,
        "folder": str(parent),
        "full_path": str(full),
    }


# -------- WebSocket --------

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    q = job.add_listener()
    try:
        await ws.send_text(json.dumps({"type": "progress", "data": job.snapshot().model_dump()}))
    except Exception:
        pass
    try:
        while True:
            msg = await q.get()
            await ws.send_text(json.dumps(msg))
    except WebSocketDisconnect:
        pass
    except asyncio.CancelledError:
        # Server shutting down. Don't log a noisy traceback for this.
        log.info("WebSocket cancelled (server shutdown).")
    except Exception as e:
        log.warning("WebSocket error: %s", e)
    finally:
        job.remove_listener(q)
        try:
            await ws.close()
        except Exception:
            pass


# -------- Static UI --------

if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

    @app.get("/")
    def root_index():
        return FileResponse(FRONTEND_DIR / "index.html")
else:
    @app.get("/")
    def root_index_missing():
        return JSONResponse({"error": "frontend not found"}, status_code=500)