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

# Optional / version-tolerant imports for the WebSocket disconnect
# exception types. Different stacks raise different concrete classes on
# a normal client disconnect; we want to swallow them all silently so
# the server console stays quiet.
try:
    from starlette.websockets import (
        WebSocketDisconnect as _StarletteWSDisconnect,
    )
except Exception:  # pragma: no cover - starlette is always present
    _StarletteWSDisconnect = WebSocketDisconnect  # type: ignore[assignment]

try:
    from websockets.exceptions import ConnectionClosedError as _WSClosedError
    from websockets.exceptions import ConnectionClosedOK as _WSClosedOK
except Exception:  # pragma: no cover
    _WSClosedError = Exception  # type: ignore[assignment]
    _WSClosedOK = Exception  # type: ignore[assignment]

try:
    # Newer Starlette / httpx-style
    from starlette.requests import ClientDisconnect as _ClientDisconnect
except Exception:  # pragma: no cover
    _ClientDisconnect = Exception  # type: ignore[assignment]

# Tuple used by ws_endpoint to recognise "client just went away" as a
# non-error condition.
_WS_DISCONNECT_EXCEPTIONS = (
    WebSocketDisconnect,
    _StarletteWSDisconnect,
    _WSClosedError,
    _WSClosedOK,
    _ClientDisconnect,
    ConnectionResetError,
    ConnectionAbortedError,
)

from .config import PROJECT_ROOT, load_config
from .db import Database
from .excel_writer import build_registry_bytes
from .jobmanager import JobManager
from .schemas import (
    BulkEditRequest,
    DeleteFilesRequest,
    EventLogClearRequest,
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


def _log_user(message: str) -> None:
    """Emit a user-action audit line.

    Routed through `JobManager._set_message` so it lands in the activity
    log AND in the persistent `event_log` table (best-effort). The
    `[user] ` prefix has ONE leading space (not two), so the frontend
    verbosity filter — which hides lines starting with two spaces at
    quiet/normal level — still surfaces these everywhere.
    """
    try:
        if not message:
            return
        # Direct call into the worker's _set_message: it does the DB
        # append and the WS broadcast for us. Using the leading "[user] "
        # makes the line easy to grep AND keeps it visible at all
        # verbosities.
        job._set_message(f"[user] {message}")  # noqa: SLF001
    except Exception:
        # Audit logging must never raise into a request handler.
        log.exception("_log_user failed")


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
def api_progress(repository: str | None = None):
    """Return a progress snapshot. If `repository` is provided, the counts
    are scoped to that repository (use empty string to scope to files with
    no repository assigned). During an active run, the active run's
    repository takes precedence."""
    return job.snapshot(repository=repository).model_dump()


@app.post("/api/start")
def api_start(req: StartRequest):
    if not (req.repository or "").strip():
        raise HTTPException(400, "repository is required")
    repo = req.repository.strip()
    _log_user(f"start repository={repo!r}")
    job.start(repo)
    return job.snapshot().model_dump()


@app.post("/api/pause")
def api_pause():
    _log_user("pause")
    job.pause()
    return job.snapshot().model_dump()


@app.post("/api/resume")
def api_resume():
    _log_user("resume")
    job.resume()
    return job.snapshot().model_dump()


@app.post("/api/stop")
def api_stop():
    _log_user("stop")
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
    _log_user(
        f"repository create name={rec.name!r} path={rec.path!r}"
    )
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
    fields_changed = []
    if body.path is not None:
        fields_changed.append(f"path={body.path!r}")
    if body.description is not None:
        fields_changed.append("description=<updated>")
    _log_user(
        f"repository update name={name!r} "
        + (", ".join(fields_changed) if fields_changed else "(no fields)")
    )
    job.broadcast_progress()
    return rec.model_dump()


@app.post("/api/repositories/{name}/rename")
def api_rename_repository(name: str, body: RepositoryRenameRequest):
    try:
        rec = db.rename_repository(name, body.new_name)
    except ValueError as e:
        raise HTTPException(400, str(e))
    _log_user(f"repository rename {name!r} -> {body.new_name!r}")
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
    _log_user(
        f"repository delete name={name!r} files_cleared={n}"
    )
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
    # Render the value compactly for the audit line. Long lists get
    # summarised so we don't dump 50 entries into the log.
    try:
        if isinstance(body.value, list):
            value_str = f"[{len(body.value)} item(s)]"
        else:
            value_str = repr(body.value)
            if len(value_str) > 80:
                value_str = value_str[:77] + "..."
    except Exception:
        value_str = "<unprintable>"
    _log_user(f"settings set {body.key}={value_str}")
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
    _log_user(f"settings reset {key}")
    job.broadcast_progress()
    return settings.to_dict_view()


@app.post("/api/settings/reset-all")
def api_settings_reset_all():
    """Drop all overrides at once."""
    settings.reset_all()
    _log_user("settings reset-all")
    job.broadcast_progress()
    return settings.to_dict_view()


@app.post("/api/reevaluate")
def api_reevaluate(req: ReevaluateRequest):
    if not req.relative_paths:
        raise HTTPException(400, "relative_paths is required")
    _log_user(
        f"reevaluate n={len(req.relative_paths)} "
        f"thinking={req.use_thinking} force={req.force}"
    )
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
    _log_user(
        f"file edit path={relative_path!r} fields=[{', '.join(sorted(fields.keys()))}]"
    )
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
    parts = [f"n={len(body.relative_paths)}"]
    if body.repository is not None:
        parts.append(f"repository={body.repository!r}")
    if body.status is not None:
        parts.append(f"status={body.status!r}")
    _log_user(f"files bulk-edit {' '.join(parts)} updated={n}")
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
    _log_user(
        f"files skip-dup-siblings n={len(body.relative_paths)} updated={n}"
    )
    job.broadcast_progress()
    return {"updated": n}


@app.post("/api/file/skip-current")
def api_file_skip_current(req: OpenFileRequest):
    """Signal the worker to skip the file currently being processed.
    The worker will mark it as 'skipped' once it reaches the next checkpoint
    (extractor page boundary, or after the in-flight LLM request is cancelled).
    """
    rel = (req.relative_path or "").strip()
    if not rel:
        raise HTTPException(400, "relative_path is required")
    _log_user(f"file skip-current path={rel!r}")
    job.signal_skip(rel)
    job.broadcast_progress()
    return {"ok": True, "skipping": rel}


@app.post("/api/files/delete")
def api_files_delete(body: DeleteFilesRequest):
    """Permanently remove the given files from the registry."""
    if not body.relative_paths:
        raise HTTPException(400, "relative_paths is required")
    for p in body.relative_paths:
        if p:
            job.signal_skip(p)
    n = db.delete_files(body.relative_paths)
    _log_user(
        f"files delete n={len(body.relative_paths)} deleted={n}"
    )
    job.broadcast_progress()
    return {"deleted": n}


# -------- Event log (persistent activity log) --------

@app.get("/api/events")
def api_events_list(limit: int = 1000, since: str | None = None):
    """Return recent event_log rows, newest-first.

    `limit` is capped at 10000 by the DB layer. `since` filters to rows
    with `ts > since` (ISO datetime string).
    """
    items = db.list_events(
        limit=max(1, min(10000, int(limit or 1000))),
        since_iso=since if since else None,
    )
    return {"items": items, "count": len(items)}


@app.post("/api/events/clear")
def api_events_clear(body: EventLogClearRequest):
    """Delete all events strictly older than the START of the local-day
    given by `before` (YYYY-MM-DD). Returns the deleted row count.
    """
    raw = (body.before or "").strip()
    if not raw:
        raise HTTPException(400, "before (YYYY-MM-DD) is required")
    try:
        # Parse as a local-naive date. Anything before its midnight in
        # *local* time is considered older. We compare against the UTC
        # ISO string we store in event_log, so we convert local-midnight
        # to UTC first.
        local_midnight = datetime.strptime(raw, "%Y-%m-%d")
        # astimezone(None) treats naive datetimes as local time and
        # produces an aware UTC datetime via .astimezone(timezone.utc).
        from datetime import timezone as _tz
        local_midnight_aware = local_midnight.astimezone()  # adds local tz
        cutoff_utc = local_midnight_aware.astimezone(_tz.utc)
        cutoff_iso = cutoff_utc.isoformat(timespec="seconds")
    except ValueError:
        raise HTTPException(400, f"invalid date {raw!r}; expected YYYY-MM-DD")
    n = db.delete_events_before(cutoff_iso)
    _log_user(f"events clear before={raw} deleted={n}")
    return {"deleted": n, "before": raw, "cutoff_iso": cutoff_iso}


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

    # Coroutine that receives client messages (subscriptions) concurrently
    # with the outbound progress stream. Both loops swallow client-
    # disconnect exceptions silently: those are completely normal (e.g.
    # browser tab closed, laptop slept). Any other unexpected exception
    # is logged but never re-raised, so we never see the noisy
    # "Task exception was never retrieved" tracebacks the asyncio
    # runtime prints when a daemon task dies with an unhandled error.
    async def _recv_loop() -> None:
        try:
            while True:
                try:
                    msg_text = await ws.receive_text()
                except _WS_DISCONNECT_EXCEPTIONS:
                    return
                try:
                    msg = json.loads(msg_text)
                except Exception:
                    continue
                if isinstance(msg, dict) and msg.get("type") == "subscribe":
                    # `repository` may be None / omitted (= all repos)
                    # or a string.
                    repo = msg.get("repository")
                    if repo is None or isinstance(repo, str):
                        job.set_listener_repository(q, repo)
                        _log_user(
                            f"ws subscribe repository="
                            + (repr(repo) if repo is not None else "None")
                        )
                        # Send an immediate scoped snapshot so the UI
                        # updates fast.
                        try:
                            snap = job.snapshot(repository=repo).model_dump()
                            await ws.send_text(
                                json.dumps({"type": "progress", "data": snap})
                            )
                        except _WS_DISCONNECT_EXCEPTIONS:
                            return
                        except Exception:
                            log.exception(
                                "ws _recv_loop: snapshot send failed"
                            )
        except _WS_DISCONNECT_EXCEPTIONS:
            return
        except asyncio.CancelledError:
            # Normal cancellation when the peer task finishes first.
            return
        except Exception:
            # Last-resort safety net: log, never re-raise.
            log.exception("ws _recv_loop: unexpected")
            return

    async def _send_loop() -> None:
        try:
            while True:
                payload = await q.get()
                try:
                    await ws.send_text(json.dumps(payload))
                except _WS_DISCONNECT_EXCEPTIONS:
                    return
        except _WS_DISCONNECT_EXCEPTIONS:
            return
        except asyncio.CancelledError:
            return
        except Exception:
            log.exception("ws _send_loop: unexpected")
            return

    recv_task = asyncio.create_task(_recv_loop())
    send_task = asyncio.create_task(_send_loop())
    try:
        done, pending = await asyncio.wait(
            {recv_task, send_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for t in pending:
            t.cancel()
    except _WS_DISCONNECT_EXCEPTIONS:
        pass
    except asyncio.CancelledError:
        log.info("WebSocket cancelled (server shutdown).")
    except Exception as e:
        log.warning("WebSocket error: %s", e)
    finally:
        for t in (recv_task, send_task):
            if not t.done():
                t.cancel()
        # Drain task results so asyncio doesn't print
        # "Task exception was never retrieved" warnings if a task
        # raised after we already returned. Both loops trap
        # everything internally now, but this is the belt-and-braces
        # contract: no orphan exceptions, ever.
        try:
            await asyncio.gather(recv_task, send_task, return_exceptions=True)
        except Exception:
            pass
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
