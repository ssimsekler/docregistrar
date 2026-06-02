"""Job manager: scans a repository's folder, runs LLM extraction per file,
supports pause/resume/stop and re-evaluation of selected files.

Runs in a background thread. The FastAPI app communicates with it via
thread-safe controls. Progress events are pushed to all connected
WebSocket clients via an asyncio.Queue + a small bridge.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from .config import (
    ALLOWED_DOC_OR_IMAGE_EXTENSIONS,
    AppConfig,
    normalize_extensions,
)
from .db import Database
from .excel_writer import write_registry
from .extractors import (
    UserSkippedError,
    extract_any,
    truncate_head_middle_tail,
)
from .keep_awake import KeepAwake
from .llm import (
    LLMCancelled,
    LLMError,
    LLMHTTPError,
    LLMInvalidJSONError,
    LLMSchemaError,
    LLMTransportError,
    LMClient,
)
from .schemas import (
    CurrentFileProgress,
    FileStep,
    ProgressSnapshot,
    now_iso,
)

log = logging.getLogger("docregistrar.jobmanager")


@dataclass
class _CurrentFile:
    relative_path: str = ""
    started_at_iso: str = ""
    started_monotonic: float = 0.0
    percent: int = 0
    current_step: str = ""
    last_detail: str = ""
    steps: list[dict] = field(default_factory=list)  # list of FileStep dicts
    # Fine-grained sub-progress for the current step (e.g. page 12/47,
    # slide 3/23, chunk 4/12). Always kept fresh by the worker; the
    # broadcast is throttled separately.
    sub_unit: str = ""
    sub_current: int = 0
    sub_total: int = 0


@dataclass
class _State:
    state: str = "idle"   # idle / scanning / running / paused / stopping / error
    target_folder: str = ""           # equals the active repository's path while running
    repository: str = ""              # active repository name
    registry_xlsx: str = ""
    current_file: str = ""
    started_at: Optional[str] = None
    last_message: str = ""
    # Re-evaluation overrides keyed by relative_path
    thinking_overrides: dict[str, bool] = field(default_factory=dict)
    # Per-file fine-grained progress
    current_file_progress: Optional[_CurrentFile] = None
    # Set of relative_paths the user asked to skip while they are/were
    # being processed.
    skip_signals: set[str] = field(default_factory=set)
    # If True, the worker should skip the scan phase and go straight to
    # processing pending files (used by re-evaluation auto-start).
    skip_scan: bool = False


class JobManager:
    def __init__(self, cfg: AppConfig, db: Database, data_dir: Path,
                 settings: Optional["SettingsService"] = None):
        self.cfg = cfg
        self.db = db
        self.data_dir = data_dir
        # Optional settings service. When set, the worker reloads `self.cfg`
        # at the top of every _process_loop iteration and rebuilds the LLM
        # client when LLM-affecting keys changed.
        self._settings = settings
        # Tracks the LLM-relevant fingerprint of the current LMClient so we
        # know when to recreate it.
        self._llm_fingerprint: tuple = ()

        self._lock = threading.RLock()
        self._state = _State()

        self._pause_event = threading.Event()
        self._pause_event.set()         # set = NOT paused; cleared = paused
        self._stop_event = threading.Event()
        self._wakeup = threading.Event()  # wake worker when new files queued

        self._thread: Optional[threading.Thread] = None

        # Reference to the live LM Studio client so Stop can close its socket
        # to interrupt an in-flight LLM call.
        self._llm_client: Optional[LMClient] = None

        # OS-sleep suppressor. Acquired in _run() while the worker thread
        # is alive (if cfg.processing.keep_awake_while_running is True),
        # released when the thread exits.
        self._keep_awake = KeepAwake()

        # Bridge for WebSocket: list of asyncio.Queue, populated from any thread.
        self._listeners: list[asyncio.Queue] = []
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    # --------------- public API (called from FastAPI handlers) ---------------

    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def add_listener(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        # Each listener gets a default `subscribed_repository = None` (= all repos).
        # The frontend can override this via the WebSocket `subscribe` message.
        q.subscribed_repository = None  # type: ignore[attr-defined]
        self._listeners.append(q)
        return q

    def remove_listener(self, q: asyncio.Queue) -> None:
        try:
            self._listeners.remove(q)
        except ValueError:
            pass

    def set_listener_repository(self, q: asyncio.Queue, repository: Optional[str]) -> None:
        """Update which repository's stats this listener wants. Pass None
        to subscribe to global stats."""
        try:
            q.subscribed_repository = repository  # type: ignore[attr-defined]
        except Exception:
            pass

    def snapshot(self, repository: Optional[str] = None) -> ProgressSnapshot:
        """Return a progress snapshot.

        `repository` semantics:
          - None  : global counts (legacy behavior)
          - ""    : files with no repository assigned
          - "X"   : counts for that repository

        While a run is in progress, the active run's repository takes
        precedence over `repository` so all listeners see what's actually
        being processed.
        """
        with self._lock:
            # Active run: force the snapshot to the run's repo so listeners
            # always see the work happening, regardless of their subscription.
            scope = repository
            if self._state.state in ("running", "scanning", "paused", "stopping") \
                    and self._state.repository:
                scope = self._state.repository
            counts = self.db.counts_by_status(scope)
            total = self.db.total_count(scope)
            cfp_obj = None
            cfp = self._state.current_file_progress
            if cfp is not None:
                elapsed_ms = int((time.monotonic() - cfp.started_monotonic) * 1000) if cfp.started_monotonic else 0
                cfp_obj = CurrentFileProgress(
                    relative_path=cfp.relative_path,
                    started_at=cfp.started_at_iso or None,
                    elapsed_ms=elapsed_ms,
                    percent=cfp.percent,
                    steps=[FileStep(**s) for s in cfp.steps],
                    current_step=cfp.current_step,
                    last_detail=cfp.last_detail,
                    sub_unit=cfp.sub_unit,
                    sub_current=cfp.sub_current,
                    sub_total=cfp.sub_total,
                )
            return ProgressSnapshot(
                state=self._state.state,                     # type: ignore[arg-type]
                target_folder=self._state.target_folder,
                repository=self._state.repository,
                total=total,
                done=counts.get("done", 0),
                error=counts.get("error", 0),
                skipped=counts.get("skipped", 0),
                pending=counts.get("pending", 0),
                processing=counts.get("processing", 0),
                current_file=self._state.current_file,
                started_at=self._state.started_at,
                last_message=self._state.last_message,
                current_file_progress=cfp_obj,
            )

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def broadcast_progress(self) -> None:
        """Public hook so REST handlers can push fresh stats to clients
        after a mutation that changes counts."""
        self._broadcast_progress()

    def start(self, repository: str) -> None:
        """Start a scan + processing run for the given repository.

        The repository must exist and have a non-empty path configured.
        """
        repository = (repository or "").strip()
        if not repository:
            self._set_state("error", "Repository is required to start.")
            return

        repo = self.db.get_repository(repository)
        if repo is None:
            self._set_state("error", f"Repository not found: {repository!r}")
            return
        if not repo.path:
            self._set_state(
                "error",
                f"Repository {repository!r} has no path configured. "
                "Edit the repository (Browse repos) and set its Path first.",
            )
            return

        target = Path(repo.path).expanduser().resolve()
        if not target.exists() or not target.is_dir():
            self._set_state("error", f"Repository folder does not exist: {target}")
            return

        # If we were paused, just resume.
        if self.is_running() and self._state.state == "paused":
            self.resume()
            return

        if self.is_running() and self._state.state not in ("paused",):
            self._set_message("Already running.")
            return

        with self._lock:
            self._state.target_folder = str(target)
            self._state.repository = repository
            self._state.registry_xlsx = self.cfg.registry_xlsx or str(target / "registry.xlsx")
            self._state.started_at = now_iso()
            self._state.thinking_overrides.clear()
            self._state.skip_scan = False

        # Recover any "processing" rows from a prior crash
        n = self.db.reset_in_progress_to_pending()
        if n:
            log.info("Recovered %d 'processing' rows back to 'pending'.", n)

        self._stop_event.clear()
        self._pause_event.set()
        self._thread = threading.Thread(
            target=self._run, name="docregistrar-worker", daemon=True
        )
        self._thread.start()
        self._set_state("scanning", f"Started scan of repository {repository!r}")

    def pause(self) -> None:
        if not self.is_running():
            return
        self._pause_event.clear()
        self._set_state("paused", "Paused")

    def resume(self) -> None:
        if not self.is_running():
            return
        self._pause_event.set()
        self._set_state("running", "Resumed")
        self._wakeup.set()

    def stop(self) -> None:
        if not self.is_running():
            with self._lock:
                if self._state.state != "idle":
                    self._state.state = "idle"
                    self._state.current_file = ""
                    self._state.current_file_progress = None
                    self._state.last_message = "Idle."
            self._set_state("idle", "Already idle")
            return
        self._set_state("stopping", "Stopping...")
        self._stop_event.set()
        self._pause_event.set()  # unblock if paused
        self._wakeup.set()
        client = self._llm_client
        if client is not None:
            try:
                client.cancel()
            except Exception:
                pass

    def shutdown_blocking(self, timeout: float = 5.0) -> None:
        self.stop()
        t = self._thread
        if t is not None:
            t.join(timeout=timeout)

    def reevaluate(self, relative_paths: list[str], use_thinking: bool = False,
                   force: bool = False) -> tuple[int, int, int]:
        """Reset the given files to 'pending' and ensure the worker is
        running so they get processed. If the worker is idle, kick off a
        no-scan run that goes straight into processing.

        Returns (n_reset, n_skipped_due_to_manual_edit, n_skipped_status_skipped).
        """
        n_reset, n_skipped, n_skipped_status = self.db.reset_to_pending(
            relative_paths, force=force,
        )
        if use_thinking:
            with self._lock:
                for p in relative_paths:
                    self._state.thinking_overrides[p] = True
        msg = f"Queued {n_reset} file(s) for re-evaluation (thinking={use_thinking})"
        if n_skipped:
            msg += f"; skipped {n_skipped} manually-edited file(s) (use Force to override)"
        if n_skipped_status:
            msg += (f"; refused {n_skipped_status} file(s) in 'skipped' status "
                    f"(set their status to 'pending' first)")
        self._set_message(msg + ".")
        self._wakeup.set()

        # If the worker is idle and we actually queued at least one file,
        # auto-start a no-scan processing run so the user doesn't have to
        # press Start manually.
        if n_reset > 0 and not self.is_running():
            self._start_eval_only_run()
        return n_reset, n_skipped, n_skipped_status

    def _start_eval_only_run(self) -> None:
        """Start the worker thread in skip-scan mode.

        Used by re-evaluation when the worker is idle. We don't scan any
        folder; we just go straight to _process_loop. Each file's path
        for actual reading off disk is resolved through its repository
        master record (see _process_one).
        """
        with self._lock:
            self._state.skip_scan = True
            self._state.started_at = now_iso()
            self._state.thinking_overrides = self._state.thinking_overrides

        # Recover any "processing" rows from a prior crash
        n = self.db.reset_in_progress_to_pending()
        if n:
            log.info("Recovered %d 'processing' rows back to 'pending'.", n)

        self._stop_event.clear()
        self._pause_event.set()
        self._thread = threading.Thread(
            target=self._run, name="docregistrar-worker-eval", daemon=True
        )
        self._thread.start()
        self._set_state("running", "Re-evaluation: processing queued files...")

    def signal_skip(self, relative_path: str) -> None:
        if not relative_path:
            return
        is_current = False
        with self._lock:
            self._state.skip_signals.add(relative_path)
            is_current = (self._state.current_file == relative_path)
        if is_current:
            client = self._llm_client
            if client is not None:
                try:
                    client.cancel()
                except Exception:
                    pass

    def _consume_skip_signal(self, relative_path: str) -> bool:
        with self._lock:
            if relative_path in self._state.skip_signals:
                self._state.skip_signals.discard(relative_path)
                return True
            return False

    # --------------- worker thread ---------------

    def _run(self) -> None:
        # Acquire OS-sleep suppressor (Windows) so the laptop won't go to
        # sleep mid-extraction. Released in the finally block so normal
        # sleep policy is restored when the worker idles or stops.
        keep_awake_acquired = False
        try:
            if self.cfg.processing.keep_awake_while_running:
                keep_awake_acquired = self._keep_awake.acquire()
                if not keep_awake_acquired and self._keep_awake.supported:
                    log.warning(
                        "keep_awake_while_running is enabled but the OS "
                        "sleep suppressor could not be acquired; the laptop "
                        "may still sleep mid-extraction."
                    )
                # Surface the keep-awake outcome as a [user]-style line so
                # it shows up in the persistent activity log AND the WS
                # broadcast. Useful when the user wonders why the worker
                # appears to freeze for minutes at a time on a laptop
                # (Modern Standby suspends background processes despite
                # the classic ES_SYSTEM_REQUIRED hint).
                status = self._keep_awake.last_status
                if status:
                    self._set_message(f"[startup] {status}")
            else:
                self._set_message(
                    "[startup] keep_awake_while_running=false; system may "
                    "sleep mid-extraction (heartbeat will detect resumption)"
                )
        except Exception:
            log.exception("Keep-awake acquire failed; continuing without it.")

        try:
            # One-shot rolling cleanup: drop event_log entries older than
            # 30 days at the start of every run. This is a safety net so
            # the table can't grow without bound; the user-driven "clear
            # logs older than X" purge (POST /api/events/clear) is the
            # primary mechanism.
            try:
                cutoff_dt = datetime.now(tz=timezone.utc) - timedelta(days=30)
                cutoff_iso = cutoff_dt.isoformat(timespec="seconds")
                purged = self.db.delete_events_before(cutoff_iso)
                if purged > 0:
                    self._set_message(
                        f"[event_log] purged {purged} entries older than 30 days"
                    )
            except Exception:
                log.exception("event_log 30-day rolling purge failed")

            with self._lock:
                skip_scan = self._state.skip_scan
                self._state.skip_scan = False  # one-shot
            if not skip_scan:
                self._scan_target_folder()
                self._set_state("running", "Scan complete; processing files...")
            else:
                self._set_state("running", "Processing queued files...")
            self._process_loop()
        except Exception as e:
            log.exception("Worker crashed")
            self._set_state("error", f"Worker crashed: {e}")
        finally:
            self._regenerate_excel()
            with self._lock:
                if self._state.state not in ("error",):
                    self._state.state = "idle"
                    self._state.current_file = ""
                    self._state.current_file_progress = None
            self._broadcast_progress()
            try:
                self._keep_awake.release()
            except Exception:
                log.exception("Keep-awake release failed.")
            log.info("Worker thread exiting.")

    def _scan_target_folder(self) -> None:
        target = Path(self._state.target_folder)
        repository = self._state.repository
        include = normalize_extensions(self.cfg.include_extensions)
        ignore_dirs = {n.lower() for n in self.cfg.ignore_dir_names}
        max_size = self.cfg.extract.max_file_size_bytes

        present: set[str] = set()
        n_seen = 0
        n_added = 0
        n_unchanged = 0

        self._set_state("scanning", f"Scanning {target} ...")
        for root, dirs, files in _walk(target, ignore_dirs):
            if self._stop_event.is_set():
                return
            for fname in files:
                p = root / fname
                ext = p.suffix.lower()
                if ext not in include:
                    continue
                try:
                    st = p.stat()
                except OSError:
                    continue
                if max_size and st.st_size > max_size:
                    continue

                rel = str(p.relative_to(target)).replace("\\", "/")
                present.add(rel)

                try:
                    sha = _sha256_file(p)
                except OSError as e:
                    log.warning("Cannot hash %s: %s", p, e)
                    continue

                created = datetime.fromtimestamp(st.st_ctime).isoformat(timespec="seconds")
                modified = datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds")

                rel_parent = str(p.parent.relative_to(target)).replace("\\", "/")
                if rel_parent in (".", ""):
                    relative_folder_path = ""
                else:
                    relative_folder_path = rel_parent

                status = self.db.upsert_file_basic(
                    relative_path=rel,
                    file_name=fname,
                    extension=ext,
                    file_size=int(st.st_size),
                    sha256=sha,
                    os_created=created,
                    os_modified=modified,
                    relative_folder_path=relative_folder_path,
                    repository=repository,
                )

                n_seen += 1
                if status == "pending":
                    n_added += 1
                else:
                    n_unchanged += 1

                # Auto-skip files that aren't documents or images.
                if ext not in ALLOWED_DOC_OR_IMAGE_EXTENSIONS and status == "pending":
                    self.db.mark_status(
                        rel,
                        "skipped",
                        f"not_a_document_or_image: {ext or '(no extension)'}",
                        repository=repository,
                    )

                if n_seen % 50 == 0:
                    self._set_message(
                        f"Scanned {n_seen} files (queued {n_added}, unchanged {n_unchanged})..."
                    )

        # Remove DB rows for files no longer on disk WITHIN this repo only
        removed = self.db.delete_files_not_in_repo(repository, present) if repository else 0

        n_groups = self.db.recompute_duplicates()

        self._set_message(
            f"Scan done. {n_seen} files seen, {n_added} queued, {n_unchanged} unchanged"
            + (f", {removed} removed from registry" if removed else "")
            + (f", {n_groups} duplicate group(s) detected." if n_groups else ".")
        )

    def _refresh_config_from_settings(self) -> bool:
        """Reload `self.cfg` from the SettingsService (if attached).

        Returns True if any LLM-affecting key changed since last refresh,
        signaling that the LMClient should be rebuilt.
        """
        if self._settings is None:
            return False
        try:
            new_cfg = self._settings.effective_config()
        except Exception as e:
            log.warning("Could not refresh effective config: %s", e)
            return False
        # Build a fingerprint of LLM-affecting fields.
        fp = (
            new_cfg.llm.base_url,
            new_cfg.llm.api_key,
            int(new_cfg.llm.request_timeout_seconds),
        )
        changed = (fp != self._llm_fingerprint)
        self.cfg = new_cfg
        self._llm_fingerprint = fp
        return changed

    def _apply_extractor_env(self) -> None:
        """Push extractor-related config values to env vars that the
        per-page extractors read (kept loose-coupled so we don't have to
        thread cfg through every extractor signature)."""
        try:
            os.environ["DOCREGISTRAR_PER_PAGE_TIMEOUT_S"] = str(
                int(self.cfg.extract.per_page_timeout_seconds)
            )
        except Exception:
            pass

    def _process_loop(self) -> None:
        # Pull fresh effective config before constructing the first client.
        self._refresh_config_from_settings()
        self._apply_extractor_env()
        cfg = self.cfg
        client = LMClient(cfg.llm, cfg.extract.mapreduce)
        self._llm_client = client
        # Initial fingerprint matches what we just built.
        self._llm_fingerprint = (
            cfg.llm.base_url, cfg.llm.api_key, int(cfg.llm.request_timeout_seconds)
        )
        try:
            processed_since_xlsx = 0
            while not self._stop_event.is_set():
                if not self._pause_event.is_set():
                    self._set_state("paused", "Paused")
                    self._pause_event.wait()
                    if self._stop_event.is_set():
                        break
                    self._set_state("running", "Resumed")

                # Refresh effective config at the top of each loop. If LLM-
                # affecting settings changed, swap in a fresh client.
                if self._refresh_config_from_settings():
                    log.info("LLM-affecting settings changed; rebuilding LM client.")
                    try:
                        client.close()
                    except Exception:
                        pass
                    cfg = self.cfg
                    client = LMClient(cfg.llm, cfg.extract.mapreduce)
                    self._llm_client = client
                else:
                    cfg = self.cfg
                # Refresh extractor env vars in case extract config changed.
                self._apply_extractor_env()

                rec = self.db.next_pending(
                    max_error_retries=cfg.processing.max_error_retries,
                )
                if rec is None:
                    self._regenerate_excel()
                    self._set_state("idle", "Idle — no pending work; worker stopped.")
                    return

                self.db.mark_status(
                    rec.relative_path, "processing",
                    repository=rec.repository,
                )
                with self._lock:
                    self._state.current_file = rec.relative_path
                self._broadcast_progress()

                if client._cancelled:  # noqa: SLF001
                    log.info("LLM client was cancelled; recreating.")
                    try:
                        client.close()
                    except Exception:
                        pass
                    client = LMClient(cfg.llm, cfg.extract.mapreduce)
                    self._llm_client = client

                ok = self._process_one(client, rec)
                processed_since_xlsx += 1

                if self._stop_event.is_set():
                    self.db.mark_status(rec.relative_path, "pending", "")
                    self._set_message(f"Requeued {rec.relative_path} (stopped during processing)")
                    break

                if ok and processed_since_xlsx >= cfg.excel_write_every_n_files:
                    self._regenerate_excel()
                    processed_since_xlsx = 0

            self._regenerate_excel()
        finally:
            try:
                client.close()
            except Exception:
                pass
            self._llm_client = None

    def _resolve_file_for_processing(self, rec) -> Optional[Path]:
        """Resolve the absolute Path on disk for a file the worker is
        about to process. Uses the file's repository master record.

        Returns None if the file's repo is missing or has no path.
        """
        repo_path = self.db.repo_path_for_file(rec.relative_path)
        if not repo_path:
            return None
        rfp = (rec.relative_folder_path or "").replace("\\", "/")
        base = Path(repo_path)
        if rfp:
            return base / rfp / rec.file_name
        return base / rec.file_name

    def _process_one(self, client: LMClient, rec) -> bool:
        cfg = self.cfg

        # Initialize per-file progress state
        self._begin_file(rec.relative_path)

        # Auto-skip files that aren't documents or images.
        if rec.extension not in ALLOWED_DOC_OR_IMAGE_EXTENSIONS:
            reason = f"not_a_document_or_image: {rec.extension or '(no extension)'}"
            self._begin_step("skip_non_doc", percent=100, detail=reason)
            self.db.mark_status(
                rec.relative_path, "skipped", reason,
                repository=rec.repository,
            )
            self._set_message(f"Skipped (not a document or image): {rec.relative_path}")
            self._end_step("skip_non_doc", percent=100, detail="skipped")
            self._end_file()
            return True

        # Resolve the file's actual location via its repository's path
        path = self._resolve_file_for_processing(rec)
        if path is None:
            reason = (
                "resolve_path_failed: file has no repository assigned, "
                "or its repository has no path configured."
            )
            self._begin_step("resolve_path", percent=5, detail=reason)
            self.db.mark_status(
                rec.relative_path, "error", reason,
                repository=rec.repository, stage="resolve_path",
            )
            self._set_message(f"Path resolution failed: {rec.relative_path}")
            self._end_step("resolve_path", percent=10, detail="failed")
            self._end_file()
            return False
        if not path.exists():
            reason = f"file_not_found: {path}"
            self._begin_step("resolve_path", percent=5, detail=reason)
            self.db.mark_status(
                rec.relative_path, "error", reason,
                repository=rec.repository, stage="resolve_path",
            )
            self._set_message(f"Path resolution failed: {rec.relative_path}")
            self._end_step("resolve_path", percent=10, detail="missing")
            self._end_file()
            return False

        # Reuse extraction from a byte-identical sibling that's already done.
        sibling = self.db.find_done_sibling(rec.sha256, rec.id)
        if sibling is not None:
            default_repo = rec.repository or self._state.repository or ""
            self._begin_step(
                "dup_reuse",
                percent=50,
                detail=f"Reusing extraction from duplicate: {sibling.relative_path}",
            )
            ok_copy = self.db.copy_extraction_from_sibling(
                rec.id,
                sibling.id,
                default_repository=default_repo,
            )
            if ok_copy:
                self._end_step(
                    "dup_reuse",
                    percent=100,
                    detail=f"Copied from {sibling.relative_path}",
                )
                self._set_message(
                    f"Done (reused from duplicate): {rec.relative_path} "
                    f"<= {sibling.relative_path}"
                )
                self._end_file()
                return True
            self._end_step("dup_reuse", percent=50, detail="copy failed; falling back to LLM")

        try:
            self._begin_step("extract_text", percent=5,
                             detail=f"Reading {rec.extension} file ({_fmt_size(rec.file_size)})...")

            # Per-page progress callback. We ALWAYS update the in-memory
            # sub-progress fields (so /api/progress and the next natural
            # broadcast see the latest values), but THROTTLE the
            # WebSocket broadcasts (via _set_detail) to avoid flooding
            # listeners on a 500-page PDF.
            last_emit = [0.0]
            EMIT_THROTTLE_S = 0.5

            def _on_extract_progress(cur: int, total: int, unit: str) -> None:
                if self._consume_skip_signal(rec.relative_path):
                    raise UserSkippedError("user skip")
                # Always-fresh in-memory state.
                pct = 5 + int(25 * cur / total) if total else 5
                with self._lock:
                    cfp = self._state.current_file_progress
                    if cfp is not None:
                        cfp.sub_unit = unit or ""
                        cfp.sub_current = int(cur)
                        cfp.sub_total = int(total)
                        if total:
                            cfp.last_detail = (
                                f"Reading {unit}: {cur}/{total} "
                                f"({100 * cur // total}%)"
                            )
                        else:
                            cfp.last_detail = f"Reading {unit}: {cur}"
                        if pct > cfp.percent:
                            cfp.percent = pct
                # Throttled broadcast.
                now = time.monotonic()
                is_boundary = (cur == total) or (cur == 0)
                if not is_boundary and (now - last_emit[0]) < EMIT_THROTTLE_S:
                    return
                last_emit[0] = now
                self._broadcast_progress()

            # Per-file extraction timeout: run extract_any in a worker
            # thread and abort if it exceeds the configured budget. This
            # protects the worker from corrupt files that hang forever.
            per_file_timeout = float(cfg.extract.per_file_timeout_seconds or 0)
            try:
                if per_file_timeout > 0:
                    with ThreadPoolExecutor(max_workers=1) as _ex:
                        _fut = _ex.submit(
                            extract_any, path, rec.extension,
                            progress_cb=_on_extract_progress,
                        )
                        try:
                            res = _fut.result(timeout=per_file_timeout)
                        except FuturesTimeout:
                            # Mark as error and bail. The background
                            # extraction thread will keep running until
                            # the underlying parser returns; that's
                            # unfortunate but unavoidable without C-level
                            # signals (and we're on Windows).
                            detail = (
                                f"extraction_timeout: extract_any did not "
                                f"complete within {int(per_file_timeout)}s "
                                f"(file_size={_fmt_size(rec.file_size)})"
                            )
                            self._end_step("extract_text", percent=10,
                                           detail=f"FAILED: {detail}")
                            self.db.mark_status(
                                rec.relative_path, "error", detail,
                                repository=rec.repository,
                                stage="extract_text",
                            )
                            self._set_message(
                                f"Extraction timed out: {rec.relative_path}"
                            )
                            self._end_file()
                            return False
                else:
                    res = extract_any(path, rec.extension,
                                      progress_cb=_on_extract_progress)
            except UserSkippedError:
                self._end_step("extract_text", percent=10, detail="skipped by user (mid-extraction)")
                self.db.mark_status(
                    rec.relative_path, "skipped",
                    "Skipped by user during processing.",
                    repository=rec.repository,
                )
                self._set_message(
                    f"Skipped (user request) during extract_text: {rec.relative_path}"
                )
                self._end_file()
                return True

            if self._consume_skip_signal(rec.relative_path):
                self._end_step("extract_text", percent=10, detail="skipped by user")
                self.db.mark_status(
                    rec.relative_path, "skipped",
                    "Skipped by user during processing.",
                    repository=rec.repository,
                )
                self._set_message(f"Skipped (user request) during extract_text: {rec.relative_path}")
                self._end_file()
                return True

            if res.extraction_error and not res.text:
                detail = (
                    f"text_extraction_failed [{rec.extension}]: {res.extraction_error} "
                    f"(file_size={_fmt_size(rec.file_size)})"
                )
                self._end_step("extract_text", percent=10, detail=f"FAILED: {res.extraction_error}")
                self.db.mark_status(
                    rec.relative_path, "error", detail,
                    repository=rec.repository, stage="extract_text",
                )
                self._set_message(f"Extraction failed: {rec.relative_path} ({res.extraction_error})")
                self._end_file()
                return False

            text_len = len(res.text or "")
            page_info = f", pages={res.page_count}" if res.page_count is not None else ""
            self._end_step("extract_text", percent=30,
                           detail=f"Extracted {text_len:,} chars{page_info}")

            # Clear sub-progress between steps (prevents stale "Reading
            # slide: 23/23" from showing during the LLM step).
            with self._lock:
                cfp_clr = self._state.current_file_progress
                if cfp_clr is not None:
                    cfp_clr.sub_unit = ""
                    cfp_clr.sub_current = 0
                    cfp_clr.sub_total = 0

            # Map-reduce decision: if the extracted text is large enough
            # to trigger chunked extraction, send the FULL text so every
            # part of the document is actually processed by the LLM.
            # Otherwise stick with the head+middle+tail sample for speed.
            mr_cfg = cfg.extract.mapreduce
            use_mapreduce_for_this_file = (
                bool(mr_cfg.enabled)
                and text_len > int(mr_cfg.threshold_chars)
            )
            # Compute the actual text we'll send + (for map-reduce) the
            # chunk count so we can SHOW the strategy decision before the
            # first LLM call, not after.
            planned_chunk_count = 0
            if use_mapreduce_for_this_file:
                text = res.text or ""
                # Pure function, no side effects; LMClient will compute the
                # same split internally when it runs.
                from .llm import split_text_into_chunks
                planned_chunk_count = len(split_text_into_chunks(text, mr_cfg))
                strategy_detail = (
                    f"map-reduce, {planned_chunk_count} chunk(s) "
                    f"({text_len:,} chars > {int(mr_cfg.threshold_chars):,} threshold)"
                )
            else:
                text = truncate_head_middle_tail(
                    res.text or "",
                    cfg.extract.head_chars,
                    cfg.extract.middle_chars,
                    cfg.extract.tail_chars,
                )
                sent_len = len(text)
                if sent_len < text_len:
                    strategy_detail = (
                        f"single-shot, {sent_len:,} chars sent "
                        f"(head+middle+tail of {text_len:,}; "
                        f"<= {int(mr_cfg.threshold_chars):,} threshold)"
                    )
                else:
                    strategy_detail = (
                        f"single-shot, {sent_len:,} chars sent "
                        f"(<= {int(mr_cfg.threshold_chars):,} threshold)"
                    )

            override = self._consume_thinking_override(rec.relative_path)
            effective_thinking = (
                cfg.llm.thinking_default if override is None else override
            )
            thinking_label = "on" if effective_thinking else "off"
            self._begin_step(
                "llm_extract",
                percent=35,
                detail=f"{strategy_detail}, thinking={thinking_label}",
            )

            # Chunk-progress callback: only meaningful for the map-reduce
            # path. Updates the same sub_* fields so the UI shows
            # "Processing chunk 4 / 12" with a sub-progress bar. Also
            # honors pause/skip between chunks. ALWAYS broadcasts on
            # boundary (cur=0 or cur=total) so the chunk count appears
            # immediately even before the first chunk completes.
            llm_last_emit = [0.0]
            LLM_EMIT_THROTTLE_S = 0.5

            # Track when each chunk / the reduce step actually started so
            # the heartbeat can show per-chunk elapsed time even between
            # chunk-completion events. Mutable dict so the closures (both
            # the chunk callback and the heartbeat thread) can share it.
            chunk_state: dict[str, Optional[float]] = {
                "chunk_started_at": None,   # monotonic when current chunk began
                "reduce_started_at": None,  # monotonic when reduce step began
            }

            def _on_llm_progress(unit: str, cur: int, total: int) -> None:
                if self._consume_skip_signal(rec.relative_path):
                    raise LLMCancelled("user skip during llm_extract")
                # Pause between chunks if requested.
                if not self._pause_event.is_set() and not self._stop_event.is_set():
                    self._set_state("paused", "Paused (between chunks)")
                    self._pause_event.wait()
                    if self._stop_event.is_set():
                        raise LLMCancelled("stopped during pause")
                    self._set_state("running", "Resumed")
                if self._stop_event.is_set():
                    raise LLMCancelled("stopped between chunks")

                # Track per-chunk / per-reduce start timestamps so the
                # heartbeat can render "(chunk elapsed: Ms)" between
                # chunk-completion events.
                #
                # Semantics from the LLM client:
                #   cb("chunk", 0, total)   -> map phase about to start;
                #                              chunk 1 begins now
                #   cb("chunk", i, total) i>0 -> chunk i just FINISHED;
                #                                if i < total, chunk i+1
                #                                begins now
                #   cb("reduce", 0, 1)      -> reduce step starting now
                #   cb("reduce", 1, 1)      -> reduce finished
                now_mono = time.monotonic()
                # Compute per-chunk elapsed BEFORE we mutate chunk_state,
                # so the "chunk i done in Ms" log line below reports the
                # actual time the chunk took, not 0ms.
                prev_chunk_started_at = chunk_state.get("chunk_started_at")
                prev_reduce_started_at = chunk_state.get("reduce_started_at")
                if unit == "chunk":
                    if cur == 0:
                        # First call, before any chunk runs.
                        chunk_state["chunk_started_at"] = now_mono
                    elif cur < total:
                        # Chunk `cur` just finished; chunk `cur+1` starts.
                        chunk_state["chunk_started_at"] = now_mono
                    else:
                        # cur == total: all chunks done.
                        chunk_state["chunk_started_at"] = None
                elif unit == "reduce":
                    if cur == 0:
                        chunk_state["reduce_started_at"] = now_mono
                    else:
                        chunk_state["reduce_started_at"] = None

                # Verbose-only log lines for each chunk / reduce boundary.
                # These start with two spaces + "[" so the frontend filter
                # keeps them at "verbose" verbosity only.
                try:
                    if unit == "chunk":
                        if cur == 0 and total > 0:
                            self._set_message(
                                f"  [llm_extract] starting chunk 1/{total}"
                            )
                        elif 0 < cur < total:
                            ms = (
                                int((now_mono - prev_chunk_started_at) * 1000)
                                if prev_chunk_started_at is not None else None
                            )
                            ms_part = f" in {ms} ms" if ms is not None else ""
                            self._set_message(
                                f"  [llm_extract] chunk {cur}/{total} done{ms_part} "
                                f"(next: chunk {cur + 1})"
                            )
                        elif cur == total and total > 0:
                            ms = (
                                int((now_mono - prev_chunk_started_at) * 1000)
                                if prev_chunk_started_at is not None else None
                            )
                            ms_part = f" in {ms} ms" if ms is not None else ""
                            self._set_message(
                                f"  [llm_extract] chunk {cur}/{total} done{ms_part}"
                            )
                            self._set_message(
                                f"  [llm_extract] all {total} chunks done; "
                                f"entering reduce phase"
                            )
                    elif unit == "reduce":
                        if cur == 0:
                            self._set_message(
                                f"  [llm_extract] reduce starting "
                                f"(consolidating {planned_chunk_count} chunks)"
                            )
                        elif cur == total:
                            ms = (
                                int((now_mono - prev_reduce_started_at) * 1000)
                                if prev_reduce_started_at is not None else None
                            )
                            ms_part = f" in {ms} ms" if ms is not None else ""
                            self._set_message(
                                f"  [llm_extract] reduce done{ms_part}"
                            )
                except Exception:
                    # Never let progress-log emission take down the worker.
                    log.exception("llm_extract progress log emission failed")

                # Always-fresh in-memory state.
                with self._lock:
                    cfp = self._state.current_file_progress
                    if cfp is not None:
                        cfp.sub_unit = unit or ""
                        cfp.sub_current = int(cur)
                        cfp.sub_total = int(total)
                        if total and cur:
                            if unit == "reduce":
                                cfp.last_detail = (
                                    f"Reducing: {cur}/{total} "
                                    f"({100 * cur // max(total, 1)}%)"
                                )
                            elif unit == "chunk":
                                cfp.last_detail = (
                                    f"Processing chunk {cur}/{total} "
                                    f"({100 * cur // max(total, 1)}%)"
                                )
                            else:
                                cfp.last_detail = (
                                    f"Processing {unit}: {cur}/{total} "
                                    f"({100 * cur // max(total, 1)}%)"
                                )
                        elif total:
                            if unit == "reduce":
                                cfp.last_detail = f"Reducing: starting..."
                            elif unit == "chunk":
                                cfp.last_detail = (
                                    f"Processing chunk 1/{total} (starting)"
                                )
                            else:
                                cfp.last_detail = f"Processing {unit}: 0/{total} (starting)"
                        # Bump per-file percent within the llm_extract
                        # band (35 -> 90).
                        if total:
                            pct = 35 + int(55 * cur / total)
                            if pct > cfp.percent:
                                cfp.percent = pct
                # Boundary updates ALWAYS broadcast immediately. Other
                # ticks are throttled.
                now = time.monotonic()
                is_boundary = (cur == 0) or (cur == total)
                if not is_boundary and (now - llm_last_emit[0]) < LLM_EMIT_THROTTLE_S:
                    return
                llm_last_emit[0] = now
                self._broadcast_progress()

            # Heartbeat thread: ticks every ~5s while client.extract() is
            # running. Two purposes:
            #   1. Visibility: composes a unified "last_detail" line with
            #      wall-clock elapsed time + chunk-aware context (e.g.
            #      "Processing chunk 1/7 (chunk elapsed: 142s, total LLM:
            #      142s)") so the user always sees the worker is alive,
            #      even between chunk-completion events. Single-shot
            #      mode shows just "Calling LLM (single-shot)... elapsed:
            #      Ns".
            #   2. Total-time deadline: if the elapsed time exceeds
            #      cfg.llm.per_file_timeout_seconds, the heartbeat
            #      cancels the LLM client (propagates as LLMCancelled /
            #      LLMTransportError into the extract() call).
            #
            # Note: this heartbeat NEVER advances cfp.percent. The chunk
            # callback advances percent on real signals (chunk completes
            # or reduce completes); a wall-clock-driven creep would be
            # misleading (it would race to ~89% in 3 minutes even while
            # chunk 1 of N is still running).
            llm_started = time.monotonic()
            llm_total_budget_s = float(cfg.llm.per_file_timeout_seconds or 0)
            llm_deadline = (
                llm_started + llm_total_budget_s
                if llm_total_budget_s > 0 else None
            )
            heartbeat_stop = threading.Event()
            heartbeat_state = {
                "deadline_hit": False,
                "deadline_at": llm_deadline,
            }
            HEARTBEAT_INTERVAL_S = 5.0
            # Separate cadence for the verbose "[hb]" log line. The 5s
            # interval above drives the cfp.last_detail UI ticker; this
            # one drives a persistent log line that survives reloads.
            # Read the interval ONCE; settings changes mid-file take
            # effect on the next file.
            try:
                hb_log_interval_s = float(
                    cfg.processing.heartbeat_log_interval_seconds or 0
                )
                if 0 < hb_log_interval_s < 5:
                    # Below 5s would compete with the UI ticker without
                    # adding signal; clamp to the floor mentioned in the
                    # setting help.
                    hb_log_interval_s = 5.0
            except Exception:
                hb_log_interval_s = 120.0

            def _fmt_secs(s: float) -> str:
                s = max(0, int(s))
                if s < 60:
                    return f"{s}s"
                m, sec = divmod(s, 60)
                if m < 60:
                    return f"{m}m {sec}s"
                h, m = divmod(m, 60)
                return f"{h}h {m}m {sec}s"

            def _heartbeat_run() -> None:
                started = llm_started
                deadline = llm_deadline
                mode = (
                    "map-reduce"
                    if use_mapreduce_for_this_file
                    else "single-shot"
                )
                last_log_emit_mono = started  # never emit before the first interval elapses
                # Wall-clock sibling of `last_log_emit_mono`. Used to detect
                # OS suspension: when the laptop sleeps, time.monotonic()
                # is paused but datetime.now() keeps advancing. A wall-
                # clock delta significantly larger than the monotonic delta
                # over a single tick means the process was suspended for
                # ~delta-monotonic seconds.
                last_tick_wall = datetime.now()
                last_tick_mono = started
                # Threshold above which we treat a missing tick as a real
                # suspension (vs minor scheduler jitter).
                SUSPENSION_THRESHOLD_S = 30.0
                while not heartbeat_stop.is_set():
                    # Wait up to HEARTBEAT_INTERVAL_S; wakes early on stop.
                    if heartbeat_stop.wait(timeout=HEARTBEAT_INTERVAL_S):
                        return
                    now_mono = time.monotonic()
                    elapsed = now_mono - started

                    # OS-suspension detection. Compare wall-clock and
                    # monotonic deltas since the previous tick. If the
                    # wall-clock delta is materially larger than the
                    # monotonic delta, the process was suspended (Modern
                    # Standby, lid close, etc.). Emit a single log line
                    # so the user can tell "it stalled because I closed
                    # the laptop", not "the LLM got stuck".
                    now_wall = datetime.now()
                    try:
                        wall_delta = (now_wall - last_tick_wall).total_seconds()
                    except Exception:
                        wall_delta = HEARTBEAT_INTERVAL_S
                    mono_delta = now_mono - last_tick_mono
                    suspended_s = wall_delta - mono_delta
                    if suspended_s >= SUSPENSION_THRESHOLD_S:
                        try:
                            self._set_message(
                                f"  [hb] system was suspended for "
                                f"~{_fmt_secs(suspended_s)} "
                                f"(resumed at {now_wall.strftime('%H:%M:%S')}); "
                                f"LLM call still in flight"
                            )
                        except Exception:
                            log.exception("suspension log emission failed")
                    last_tick_wall = now_wall
                    last_tick_mono = now_mono
                    # Deadline check first so we surface the cause clearly.
                    if deadline is not None and now_mono > deadline:
                        log.warning(
                            "LLM total-time budget exceeded for %s "
                            "(%.0fs > %.0fs); cancelling.",
                            rec.relative_path,
                            elapsed,
                            llm_total_budget_s,
                        )
                        heartbeat_state["deadline_hit"] = True
                        try:
                            client.cancel()
                        except Exception:
                            pass
                        # Stop ticking; the cancel will propagate as an
                        # exception out of client.extract().
                        return

                    # Compose a chunk/reduce-aware detail line. We do NOT
                    # touch cfp.percent; that's owned by the chunk
                    # callback which advances it on real completions.
                    chunk_started_at = chunk_state.get("chunk_started_at")
                    reduce_started_at = chunk_state.get("reduce_started_at")
                    with self._lock:
                        cfp = self._state.current_file_progress
                        if cfp is None:
                            return
                        sub_unit = cfp.sub_unit or ""
                        sub_current = int(cfp.sub_current)
                        sub_total = int(cfp.sub_total)

                        if sub_unit == "chunk" and sub_total > 0 \
                                and chunk_started_at is not None:
                            chunk_elapsed = now_mono - chunk_started_at
                            # The chunk currently in flight is sub_current+1
                            # (the chunk callback only advances sub_current
                            # AFTER a chunk completes). Cap at sub_total in
                            # case of a race.
                            in_flight = min(sub_current + 1, sub_total)
                            cfp.last_detail = (
                                f"Processing chunk {in_flight}/{sub_total} "
                                f"(chunk elapsed: {_fmt_secs(chunk_elapsed)}, "
                                f"total LLM: {_fmt_secs(elapsed)})"
                            )
                        elif sub_unit == "reduce" \
                                and reduce_started_at is not None:
                            reduce_elapsed = now_mono - reduce_started_at
                            cfp.last_detail = (
                                f"Reducing... elapsed: "
                                f"{_fmt_secs(reduce_elapsed)} "
                                f"(total LLM: {_fmt_secs(elapsed)})"
                            )
                        else:
                            # Single-shot, or map-reduce before the very
                            # first chunk callback has fired.
                            budget_suffix = ""
                            if llm_total_budget_s > 0 and llm_total_budget_s < 1e9:
                                budget_suffix = (
                                    f" (budget: {_fmt_secs(llm_total_budget_s)})"
                                )
                            cfp.last_detail = (
                                f"Calling LLM ({mode})... elapsed: "
                                f"{_fmt_secs(elapsed)}{budget_suffix}"
                            )
                        # Capture values for the throttled [hb] log line
                        # (computed below, OUTSIDE the lock).
                        hb_sub_unit = sub_unit
                        hb_sub_total = sub_total
                        hb_in_flight = (
                            min(sub_current + 1, sub_total)
                            if sub_total > 0 else 0
                        )
                        hb_chunk_elapsed_ms = (
                            int((now_mono - chunk_started_at) * 1000)
                            if chunk_started_at is not None else None
                        )
                        hb_reduce_elapsed_ms = (
                            int((now_mono - reduce_started_at) * 1000)
                            if reduce_started_at is not None else None
                        )
                    self._broadcast_progress()

                    # Throttled persistent "[hb]" log line. Disabled when
                    # interval == 0. Starts with two spaces + "[" so the
                    # frontend filter keeps it at "verbose" verbosity.
                    if hb_log_interval_s > 0 and \
                            (now_mono - last_log_emit_mono) >= hb_log_interval_s:
                        last_log_emit_mono = now_mono
                        try:
                            total_ms = int(elapsed * 1000)
                            if hb_sub_unit == "chunk" and hb_sub_total > 0 \
                                    and hb_chunk_elapsed_ms is not None:
                                hb_msg = (
                                    f"  [hb] LLM alive — chunk "
                                    f"{hb_in_flight}/{hb_sub_total} "
                                    f"(chunk {hb_chunk_elapsed_ms} ms / "
                                    f"total {total_ms} ms)"
                                )
                            elif hb_sub_unit == "reduce" \
                                    and hb_reduce_elapsed_ms is not None:
                                hb_msg = (
                                    f"  [hb] LLM alive — reduce "
                                    f"({hb_reduce_elapsed_ms} ms / "
                                    f"total {total_ms} ms)"
                                )
                            else:
                                hb_msg = (
                                    f"  [hb] LLM alive — {mode} "
                                    f"(total {total_ms} ms)"
                                )
                            self._set_message(hb_msg)
                        except Exception:
                            log.exception("heartbeat log emission failed")

            heartbeat_thread = threading.Thread(
                target=_heartbeat_run,
                name=f"docregistrar-llm-heartbeat-{rec.id or rec.relative_path}",
                daemon=True,
            )
            heartbeat_thread.start()

            try:
                try:
                    extraction, used_thinking = client.extract(
                        text=text,
                        file_name=rec.file_name,
                        relative_path=rec.relative_path,
                        use_thinking=override,
                        progress_cb=_on_llm_progress,
                    )
                except LLMCancelled:
                    if heartbeat_state["deadline_hit"]:
                        # Total-time budget exceeded: this is an error.
                        elapsed_s = int(time.monotonic() - llm_started)
                        detail = (
                            f"llm_total_timeout: LLM activity exceeded "
                            f"{int(llm_total_budget_s)}s budget "
                            f"(elapsed {elapsed_s}s, "
                            f"strategy: {strategy_detail})"
                        )
                        self._fail_step(detail=detail)
                        self.db.mark_status(
                            rec.relative_path, "error", detail,
                            repository=rec.repository, stage="llm_extract",
                        )
                        self._set_message(
                            f"LLM total-time budget exceeded on "
                            f"{rec.relative_path}: {elapsed_s}s"
                        )
                        self._end_file()
                        return False
                    # Otherwise the user asked to skip mid-call.
                    self._end_step("llm_extract", percent=50, detail="cancelled (user skip)")
                    self.db.mark_status(
                        rec.relative_path, "skipped",
                        "Skipped by user during processing.",
                        repository=rec.repository,
                    )
                    self._set_message(
                        f"Skipped (user request) during llm_extract: {rec.relative_path}"
                    )
                    self._end_file()
                    return True
                except LLMError as e:
                    if heartbeat_state["deadline_hit"]:
                        # Deadline cancellation can also surface as a
                        # transport error (httpx client closed mid-call).
                        elapsed_s = int(time.monotonic() - llm_started)
                        detail = (
                            f"llm_total_timeout: LLM activity exceeded "
                            f"{int(llm_total_budget_s)}s budget "
                            f"(elapsed {elapsed_s}s, "
                            f"strategy: {strategy_detail}; "
                            f"underlying: {type(e).__name__}: {e})"
                        )
                        self._fail_step(detail=detail)
                        self.db.mark_status(
                            rec.relative_path, "error", detail,
                            repository=rec.repository, stage="llm_extract",
                        )
                        self._set_message(
                            f"LLM total-time budget exceeded on "
                            f"{rec.relative_path}: {elapsed_s}s"
                        )
                        self._end_file()
                        return False
                    if self._consume_skip_signal(rec.relative_path):
                        self._end_step("llm_extract", percent=50, detail="skipped by user (cancelled LLM)")
                        self.db.mark_status(
                            rec.relative_path, "skipped",
                            "Skipped by user during processing.",
                            repository=rec.repository,
                        )
                        self._set_message(
                            f"Skipped (user request) during llm_extract: {rec.relative_path}"
                        )
                        self._end_file()
                        return True
                    raise
            finally:
                heartbeat_stop.set()
                # We don't join() here: the thread is daemon and will
                # exit on its own; joining would just delay our return
                # by up to HEARTBEAT_INTERVAL_S in the worst case.

            if self._consume_skip_signal(rec.relative_path):
                self._end_step("llm_extract", percent=90, detail="skipped by user after LLM returned")
                self.db.mark_status(
                    rec.relative_path, "skipped",
                    "Skipped by user during processing.",
                    repository=rec.repository,
                )
                self._set_message(
                    f"Skipped (user request) after llm_extract: {rec.relative_path}"
                )
                self._end_file()
                return True

            ne = extraction.named_entities
            verbose = (
                f"title={'yes' if extraction.title else 'no'}, "
                f"summary={len(extraction.summary)} chars, "
                f"date={extraction.document_date or '-'}, "
                f"type={extraction.document_type or '-'}, "
                f"lang={extraction.language or '-'}, "
                f"conf={extraction.confidentiality or '-'}, "
                f"authors={len(extraction.authors)}, "
                f"persons={len(ne.persons)}, orgs={len(ne.organizations)}, "
                f"locs={len(ne.locations)}, products={len(ne.products_technologies)}, "
                f"keyphrases={len(extraction.key_phrases)}, "
                f"q={extraction.quality_score:.2f}, "
                f"thinking={'on' if used_thinking else 'off'}"
            )
            self._end_step("llm_extract", percent=90, detail=verbose)

            self._begin_step("save", percent=95, detail="Saving to local DB...")
            default_repo = self._state.repository or ""
            self.db.save_extraction(
                relative_path=rec.relative_path,
                page_count=res.page_count,
                extraction=extraction,
                used_thinking=used_thinking,
                default_repository=default_repo,
            )
            self._end_step("save", percent=100, detail="Saved")

            self._set_message(
                f"Done: {rec.relative_path} "
                f"(q={extraction.quality_score:.2f}, "
                f"thinking={'on' if used_thinking else 'off'})"
            )
            self._end_file()
            return True

        except LLMHTTPError as e:
            detail = (
                f"llm_http_{e.status_code}: {e.body_snippet} "
                f"(model={e.model!r}, url={e.base_url!r})"
            )
            self._fail_step(detail=detail)
            self.db.mark_status(
                rec.relative_path, "error", detail,
                repository=rec.repository, stage="llm_extract",
            )
            self._set_message(f"LLM HTTP error on {rec.relative_path}: {e}")
            self._end_file()
            return False
        except LLMInvalidJSONError as e:
            detail = f"llm_invalid_json: {e.raw_snippet} (model={cfg.llm.model!r})"
            self._fail_step(detail=detail)
            self.db.mark_status(
                rec.relative_path, "error", detail,
                repository=rec.repository, stage="llm_extract",
            )
            self._set_message(f"LLM returned invalid JSON: {rec.relative_path}")
            self._end_file()
            return False
        except LLMSchemaError as e:
            detail = f"llm_schema_validation_failed: {e.details}"
            self._fail_step(detail=detail)
            self.db.mark_status(
                rec.relative_path, "error", detail,
                repository=rec.repository, stage="llm_extract",
            )
            self._set_message(f"LLM schema validation failed: {rec.relative_path}")
            self._end_file()
            return False
        except LLMTransportError as e:
            detail = (
                f"llm_transport_error: {e} "
                f"(url={cfg.llm.base_url!r}, timeout={cfg.llm.request_timeout_seconds}s)"
            )
            self._fail_step(detail=detail)
            self.db.mark_status(
                rec.relative_path, "error", detail,
                repository=rec.repository, stage="llm_extract",
            )
            self._set_message(f"LLM transport error on {rec.relative_path}: {e}")
            self._end_file()
            return False
        except LLMError as e:
            # Catch-all for any other LLM-related failure.
            detail = f"llm_error: {type(e).__name__}: {e}"
            self._fail_step(detail=detail)
            self.db.mark_status(
                rec.relative_path, "error", detail,
                repository=rec.repository, stage="llm_extract",
            )
            self._set_message(f"LLM error on {rec.relative_path}: {e}")
            self._end_file()
            return False
        except Exception as e:
            log.exception("Unexpected error processing %s", rec.relative_path)
            current_step = (
                self._state.current_file_progress.current_step
                if self._state.current_file_progress else ""
            )
            detail = (
                f"unexpected_error: {type(e).__name__}: {e}"
                + (f" | step={current_step}" if current_step else "")
            )
            self._fail_step(detail=f"{type(e).__name__}: {e}")
            self.db.mark_status(
                rec.relative_path, "error", detail,
                repository=rec.repository, stage=current_step or "unknown",
            )
            self._set_message(f"Error on {rec.relative_path}: {e}")
            self._end_file()
            return False

    def _consume_thinking_override(self, relative_path: str) -> Optional[bool]:
        with self._lock:
            return self._state.thinking_overrides.pop(relative_path, None)

    # ---------- per-file step bookkeeping ----------

    def _begin_file(self, relative_path: str) -> None:
        with self._lock:
            self._state.current_file_progress = _CurrentFile(
                relative_path=relative_path,
                started_at_iso=now_iso(),
                started_monotonic=time.monotonic(),
                percent=0,
                current_step="",
                last_detail="",
                steps=[],
            )
        self._set_message(f"Begin: {relative_path}")

    def _end_file(self) -> None:
        with self._lock:
            self._state.current_file_progress = None
        self._broadcast_progress()

    def _begin_step(self, name: str, *, percent: int, detail: str = "") -> None:
        with self._lock:
            cfp = self._state.current_file_progress
            if cfp is None:
                return
            step = {
                "name": name,
                "started_at": now_iso(),
                "finished_at": None,
                "duration_ms": None,
                "detail": detail,
            }
            cfp.steps.append(step)
            cfp.current_step = name
            cfp.percent = max(cfp.percent, percent)
            cfp.last_detail = detail
        self._set_message(f"  [{name}] {detail}" if detail else f"  [{name}]")

    def _end_step(self, name: str, *, percent: int, detail: str = "") -> None:
        with self._lock:
            cfp = self._state.current_file_progress
            if cfp is None:
                return
            for s in reversed(cfp.steps):
                if s["name"] == name and s["finished_at"] is None:
                    s["finished_at"] = now_iso()
                    try:
                        t0 = datetime.fromisoformat(s["started_at"])
                        t1 = datetime.fromisoformat(s["finished_at"])
                        s["duration_ms"] = int((t1 - t0).total_seconds() * 1000)
                    except Exception:
                        s["duration_ms"] = None
                    if detail:
                        s["detail"] = (s.get("detail") + " | " + detail) if s.get("detail") else detail
                    break
            cfp.percent = max(cfp.percent, percent)
            cfp.last_detail = detail or cfp.last_detail
            cfp.current_step = ""
        self._set_message(f"  [{name} done in {self._last_step_ms(name)} ms] {detail}".rstrip())

    def _fail_step(self, *, detail: str) -> None:
        with self._lock:
            cfp = self._state.current_file_progress
            if cfp is None:
                return
            for s in reversed(cfp.steps):
                if s["finished_at"] is None:
                    s["finished_at"] = now_iso()
                    try:
                        t0 = datetime.fromisoformat(s["started_at"])
                        t1 = datetime.fromisoformat(s["finished_at"])
                        s["duration_ms"] = int((t1 - t0).total_seconds() * 1000)
                    except Exception:
                        s["duration_ms"] = None
                    s["detail"] = (s.get("detail") + " | FAILED: " + detail) if s.get("detail") else f"FAILED: {detail}"
                    break
            cfp.last_detail = f"FAILED: {detail}"

    def _last_step_ms(self, name: str) -> str:
        cfp = self._state.current_file_progress
        if cfp is None:
            return "?"
        for s in reversed(cfp.steps):
            if s["name"] == name and s.get("duration_ms") is not None:
                return str(s["duration_ms"])
        return "?"

    def _set_detail(self, detail: str) -> None:
        with self._lock:
            cfp = self._state.current_file_progress
            if cfp is not None:
                cfp.last_detail = detail
        self._set_message(f"  {detail}")

    # --------------- Excel ---------------

    def _regenerate_excel(self) -> None:
        if not self._state.registry_xlsx:
            return
        records = self.db.list_all_for_excel()
        out = Path(self._state.registry_xlsx)
        repo_paths = {r.name: r.path for r in self.db.list_repositories()}
        ok = write_registry(records, out, repo_paths)
        if ok:
            self._set_message(f"Registry updated: {out}")
        else:
            self._set_message(f"Registry write deferred (target busy?): {out}")
        self._broadcast_progress()

    # --------------- state helpers + broadcast ---------------

    def _set_state(self, state: str, message: str = "") -> None:
        with self._lock:
            self._state.state = state
            if message:
                self._state.last_message = message
        log.info("STATE %s | %s", state, message)
        self._broadcast_progress()

    def _set_message(self, message: str) -> None:
        with self._lock:
            self._state.last_message = message
        log.info("MSG %s", message)
        # Persist to event_log (best-effort; never raises). Anything that
        # comes through _set_message — worker step lines, [user] action
        # logs from main.py, heartbeats — gets captured here.
        try:
            category = "user" if (message or "").startswith("[user] ") else "worker"
            self.db.append_event("info", category, message)
        except Exception:
            # _set_message must never take down the worker. The DB layer
            # already swallows + logs, so this is just defense-in-depth.
            pass
        self._broadcast_progress()

    def _broadcast_progress(self) -> None:
        """Push a progress event to every WebSocket listener, scoped to that
        listener's subscribed repository (set via `set_listener_repository`).

        While a run is in progress, snapshot() forces the scope to the active
        run's repo so all listeners see what's actually being processed.
        """
        if self._loop is None:
            return
        # Compute the global snapshot once; reuse it for listeners that didn't
        # subscribe to a specific repo.
        global_snap = self.snapshot().model_dump()
        # Cache scoped snapshots by repository so we don't re-query the DB
        # once per listener with the same subscription.
        scoped_cache: dict[Optional[str], dict] = {None: global_snap}
        for q in list(self._listeners):
            sub = getattr(q, "subscribed_repository", None)
            if sub not in scoped_cache:
                try:
                    scoped_cache[sub] = self.snapshot(repository=sub).model_dump()
                except Exception:
                    scoped_cache[sub] = global_snap
            payload = {"type": "progress", "data": scoped_cache[sub]}
            try:
                self._loop.call_soon_threadsafe(_safe_put, q, payload)
            except RuntimeError:
                pass


# ---------- module-level helpers ----------

def _safe_put(q: asyncio.Queue, item) -> None:
    try:
        q.put_nowait(item)
    except asyncio.QueueFull:
        try:
            q.get_nowait()
        except Exception:
            pass
        try:
            q.put_nowait(item)
        except Exception:
            pass


def _walk(root: Path, ignore_dir_names_lower: set[str]):
    """os.walk-like generator that yields (Path, dirs, files) and prunes ignored dirs."""
    import os

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d.lower() not in ignore_dir_names_lower]
        yield Path(dirpath), dirnames, filenames


def _sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _fmt_size(n: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    x = float(n)
    i = 0
    while x >= 1024 and i < len(units) - 1:
        x /= 1024
        i += 1
    return f"{x:.1f} {units[i]}" if i > 0 else f"{int(x)} {units[i]}"
