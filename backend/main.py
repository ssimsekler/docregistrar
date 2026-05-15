"""FastAPI app: serves UI + REST + WebSocket."""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import PROJECT_ROOT, load_config
from .db import Database
from .jobmanager import JobManager
from .schemas import ReevaluateRequest, StartRequest

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

app = FastAPI(title="docregistrar", version="0.1.0")

FRONTEND_DIR = PROJECT_ROOT / "frontend"


@app.on_event("startup")
async def _on_startup() -> None:
    job.attach_loop(asyncio.get_running_loop())
    log.info("App started. UI at http://%s:%s/", cfg.server.host, cfg.server.port)


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
    """Open a native OS folder picker on the machine running the server.

    Works because the server runs locally on the user's own machine.
    """
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception as e:
        raise HTTPException(500, f"tkinter not available: {e}")

    # Run the dialog in a fresh hidden Tk root, then destroy it.
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
    job.start(req.target_folder, req.registry_xlsx)
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


@app.post("/api/reevaluate")
def api_reevaluate(req: ReevaluateRequest):
    if not req.relative_paths:
        raise HTTPException(400, "relative_paths is required")
    n = job.reevaluate(req.relative_paths, use_thinking=req.use_thinking)
    return {"reset": n, "use_thinking": req.use_thinking}


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
        # Flatten extraction for the table
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
        d["products_technologies"] = ne.get("products_technologies", [])
        out.append(d)
    return {"items": out, "count": len(out)}


@app.get("/api/file")
def api_file(relative_path: str):
    rec = db.get_file(relative_path)
    if rec is None:
        raise HTTPException(404, f"Not found: {relative_path}")
    return rec.model_dump()


# -------- WebSocket --------

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    q = job.add_listener()
    # Send initial snapshot
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
    except Exception as e:
        log.warning("WebSocket error: %s", e)
    finally:
        job.remove_listener(q)


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