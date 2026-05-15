"""FastAPI app: serves UI + REST + WebSocket."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .config import PROJECT_ROOT, load_config
from .db import Database
from .excel_writer import build_registry_bytes
from .jobmanager import JobManager
from .schemas import (
    BulkEditRequest,
    FileEditRequest,
    ReevaluateRequest,
    SetRepositoryRequest,
    StartRequest,
)

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


@app.post("/api/reevaluate")
def api_reevaluate(req: ReevaluateRequest):
    if not req.relative_paths:
        raise HTTPException(400, "relative_paths is required")
    n_reset, n_skipped = job.reevaluate(
        req.relative_paths,
        use_thinking=req.use_thinking,
        force=req.force,
    )
    return {
        "reset": n_reset,
        "skipped_manual": n_skipped,
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
    return {"updated": n}


@app.get("/api/files")
def api_files(
    status: str | None = None,
    search: str = "",
    limit: int = 1000,
    offset: int = 0,
):
    records = db.list_files(
        status=status if status else None,
        limit=max(1, min(5000, limit)),
        offset=max(0, offset),
        search=search,
    )
    out = []
    for r in records:
        d = r.model_dump()
        e = d.pop("extraction", None) or {}
        ne = (e.get("named_entities") or {}) if e else {}
        d["title"] = e.get("title", "")
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
        out.append(d)
    return {"items": out, "count": len(out)}


@app.get("/api/file")
def api_file(relative_path: str):
    rec = db.get_file(relative_path)
    if rec is None:
        raise HTTPException(404, f"Not found: {relative_path}")
    return rec.model_dump()


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