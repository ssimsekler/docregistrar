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
    RepositoryCreateRequest,
    RepositoryRenameRequest,
    RepositoryUpdateRequest,
    SetRepositoryRequest,
    SkipDupSiblingsRequest,
    StartRequest,
)
from .settings import SettingsService

# Extensions blocked by POST /api/open-file.
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
settings = SettingsService(db)
# Effective config = defaults ⊕ YAML ⊕ DB overrides. Apply once at boot.
cfg = settings.effective_config()
job = JobManager(cfg, db, DATA_DIR, settings=settings)

app = FastAPI(title="docregistrar", version="0.3.0")

FRONTEND_DIR = PROJECT_ROOT / "frontend"


@app.on_event("startup")
async def _on_startup() -> None:
    job.attach_loop(asyncio.get_running_loop())
    log.info("App started. UI at http://%s:%s/", cfg.server.host, cfg.server.port)


@app.on_event("shutdown")
async def _on_shutdown() -> None:
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
            title="Select folder",
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
    if not (req.repository or "").strip():
        raise HTTPException(400, "repository is required")
    job.start(req.repository.strip())
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


# -------- Repository (master data) endpoints --------

@app.get("/api/repositories")
def api_list_repositories():
    """List all repositories (with file counts) sorted alphabetically."""
    repos = db.list_repositories()
    return {"items": [r.model_dump() for r in repos]}


@app.get("/api/repositories/{name}")
def api_get_repository(name: str):
    rec = db.get_repository(name)
    if rec is None:
        raise HTTPException(404, f"Repository not found: {name!r}")
    return rec.model_dump()


@app.post("/api/repositories")
def api_create_repository(body: RepositoryCreateRequest):
    try:
        rec = db.create_repository(body.name, body.path, body.description or "")
    except ValueError as e:
        raise HTTPException(400, str(e))
    job.broadcast_progress()
    return rec.model_dump()


@app.patch("/api/repositories/{name}")
def api_update_repository(name: str, body: RepositoryUpdateRequest):
    try:
        rec = db.update_repository(
            name,
            path=body.path,
            description=body.description,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    job.broadcast_progress()
    return rec.model_dump()


@app.post("/api/repositories/{name}/rename")
def api_rename_repository(name: str, body: RepositoryRenameRequest):
    try:
        rec = db.rename_repository(name, body.new_name)
    except ValueError as e:
        raise HTTPException(400, str(e))
    job.broadcast_progress()
    return rec.model_dump()


@app.delete("/api/repositories/{name}")
def api_delete_repository(name: str):
    """Delete a repository AND clear its assignment from any referencing files.
    Returns the number of files affected.
    """
    try:
        n = db.delete_repository(name, clear_files=True)
    except ValueError as e:
        raise HTTPException(404, str(e))
    job.broadcast_progress()
    return {"deleted": True, "files_cleared": n}


# -------- Settings (runtime config overrides) --------

class SettingsUpdateRequest(BaseModel):
    key: str
    value: object  # any JSON-compatible value; SettingsService validates


@app.get("/api/settings")
def api_settings_get():
    """Return the merged settings view (defaults + YAML + DB overrides) plus
    metadata the UI needs to render the Settings modal."""
    return settings.to_dict_view()


@app.put("/api/settings")
def api_settings_set(body: SettingsUpdateRequest):
    """Persist a single dotted-key override. Validates by trying to construct
    AppConfig with the merged dict; returns 400 on validation failure.
    """
    try:
        settings.set_setting(body.key, body.value)
    except ValueError as e:
        raise HTTPException(400, str(e))
    # Push fresh stats so the UI reacts; the worker will pick up the change
    # at the top of its next loop iteration.
    job.broadcast_progress()
    return settings.to_dict_view()


@app.delete("/api/settings/{key:path}")
def api_settings_reset_one(key: str):
    """Drop the override for `key`. The field reverts to YAML/default."""
    try:
        settings.reset_setting(key)
    except ValueError as e:
        raise HTTPException(400, str(e))
    job.broadcast_progress()
    return settings.to_dict_view()


@app.post("/api/settings/reset-all")
def api_settings_reset_all():
    """Drop all overrides at once."""
    settings.reset_all()
    job.broadcast_progress()
    return settings.to_dict_view()


@app.post("/api/reevaluate")
def api_reevaluate(req: ReevaluateRequest):
    if not req.relative_paths:
        raise HTTPException(400, "relative_paths is required")
    n_reset, n_skipped, n_skipped_status = job.reevaluate(
        req.relative_paths,
        use_thinking=req.use_thinking,
        force=req.force,
    )
    job.broadcast_progress()
    return {
        "reset": n_reset,
        "skipped_manual": n_skipped,
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
    if fields.get("status") == "pending":
        job._wakeup.set()  # noqa: SLF001
    if fields.get("status") == "skipped":
        job.signal_skip(relative_path)
    job.broadcast_progress()
    rec = db.get_file(relative_path)
    return _decorate_file_response(rec)


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
    if body.status == "skipped":
        for p in body.relative_paths:
            job.signal_skip(p)
    job.broadcast_progress()
    return {"updated": n}


def _decorate_file_response(rec) -> dict:
    """Return a record dict augmented with convenience fields used by the UI:
       - repository_path: configured path of the file's repository (read-only)
       - flattened extraction-derived fields used by the grid
    """
    if rec is None:
        return {}
    d = rec.model_dump() if hasattr(rec, "model_dump") else dict(rec)
    repo_name = d.get("repository", "") or ""
    repo_path = ""
    if repo_name:
        rec_repo = db.get_repository(repo_name)
        if rec_repo is not None:
            repo_path = rec_repo.path or ""
    d["repository"] = repo_name
    d["repository_path"] = repo_path
    return d


@app.get("/api/files")
def api_files(
    status: str | None = None,
    search: str = "",
    limit: int = 1000,
    offset: int = 0,
    sha256: str | None = None,
    duplicates_only: bool = False,
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
    # Pre-fetch repo paths into a lookup to avoid N queries
    repo_map: dict[str, str] = {}
    for r in records:
        repo_name = r.repository or ""
        if repo_name and repo_name not in repo_map:
            rec_repo = db.get_repository(repo_name)
            repo_map[repo_name] = rec_repo.path if rec_repo else ""

    out = []
    for r in records:
        d = r.model_dump()
        e = d.pop("extraction", None) or {}
        ne = (e.get("named_entities") or {}) if e else {}
        repo_name = r.repository or ""
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
        d["repository"] = repo_name
        d["repository_path"] = repo_map.get(repo_name, "")
        d["products_technologies"] = ne.get("products_technologies", [])
        d["custom_properties"] = e.get("custom_properties", [])
        out.append(d)
    return {"items": out, "count": len(out)}


@app.get("/api/file")
def api_file(relative_path: str):
    rec = db.get_file(relative_path)
    if rec is None:
        raise HTTPException(404, f"Not found: {relative_path}")
    return _decorate_file_response(rec)


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
    if not body.relative_paths:
        raise HTTPException(400, "relative_paths is required")
    n = db.skip_dup_siblings_of(body.relative_paths)
    job.broadcast_progress()
    return {"updated": n}


@app.post("/api/files/delete")
def api_files_delete(body: DeleteFilesRequest):
    """Permanently remove the given files from the registry."""
    if not body.relative_paths:
        raise HTTPException(400, "relative_paths is required")
    for p in body.relative_paths:
        if p:
            job.signal_skip(p)
    n = db.delete_files(body.relative_paths)
    job.broadcast_progress()
    return {"deleted": n}


@app.get("/api/registry.xlsx")
def api_registry_download():
    """Download the current registry as an Excel file."""
    records = db.list_all_for_excel()
    repos = {r.name: r.path for r in db.list_repositories()}
    data = build_registry_bytes(records, repos)
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    filename = f"docregistrar_{ts}.xlsx"
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _resolve_file_path(rel: str) -> Path:
    """Resolve the absolute Path on disk for a registry row using the
    file's repository's configured path + relative_folder_path + file_name.
    """
    rec = db.get_file(rel)
    if rec is None:
        raise HTTPException(404, f"File not found in registry: {rel}")

    repo_name = (rec.repository or "").strip()
    if not repo_name:
        raise HTTPException(
            400,
            "This file has no repository assigned. Assign a repository "
            "(via Edit or bulk-edit) so its full path can be resolved.",
        )
    repo = db.get_repository(repo_name)
    if repo is None:
        raise HTTPException(
            400,
            f"Repository {repo_name!r} no longer exists. Re-create it (with a path) "
            "or assign this file to a different repository.",
        )
    if not repo.path:
        raise HTTPException(
            400,
            f"Repository {repo_name!r} has no path configured. Open 'Browse repos' "
            "and set its Path.",
        )
    base = Path(repo.path)
    rfp = (rec.relative_folder_path or "").replace("\\", "/")
    candidate = base / rfp / rec.file_name if rfp else base / rec.file_name
    try:
        return candidate.resolve()
    except OSError:
        return candidate


@app.post("/api/open-file")
def api_open_file(req: OpenFileRequest):
    """Open a file from the registry in the OS default app.

    Path is resolved as: repository.path / relative_folder_path / file_name.
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
    """Open the OS file explorer at the file's parent folder."""
    rel = (req.relative_path or "").strip()
    if not rel:
        raise HTTPException(400, "relative_path is required")

    full = _resolve_file_path(rel)
    parent = full.parent

    if not parent.exists():
        raise HTTPException(404, f"Folder not found: {parent}")

    try:
        if sys.platform.startswith("win"):
            if full.exists():
                import subprocess
                subprocess.Popen(f'explorer /select,"{str(full)}"')
            else:
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
