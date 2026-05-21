"""Job manager: scans the target folder, runs LLM extraction per file,
supports pause/resume/stop and re-evaluation of selected files.

Runs in a background thread (the LLM call is synchronous httpx). The FastAPI
app communicates with it via thread-safe controls. Progress events are pushed
to all connected WebSocket clients via an asyncio.Queue + a small bridge.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from .config import (
    ALLOWED_DOC_OR_IMAGE_EXTENSIONS,
    AppConfig,
    normalize_extensions,
)
from .db import Database
from .excel_writer import write_registry
from .extractors import extract_any, truncate_head_middle_tail
from .llm import LMClient, LLMError
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


@dataclass
class _State:
    state: str = "idle"   # idle / scanning / running / paused / stopping / error
    target_folder: str = ""
    registry_xlsx: str = ""
    current_file: str = ""
    started_at: Optional[str] = None
    last_message: str = ""
    # Default repository assigned to files that get processed during this run.
    default_repository: str = ""
    # Re-evaluation overrides keyed by relative_path
    thinking_overrides: dict[str, bool] = field(default_factory=dict)
    # Per-file fine-grained progress
    current_file_progress: Optional[_CurrentFile] = None
    # Set of relative_paths the user asked to skip while they are/were
    # being processed. The worker checks this set between major steps and
    # cleanly aborts the current file (item 7).
    skip_signals: set[str] = field(default_factory=set)


class JobManager:
    def __init__(self, cfg: AppConfig, db: Database, data_dir: Path):
        self.cfg = cfg
        self.db = db
        self.data_dir = data_dir

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

        # Bridge for WebSocket: list of asyncio.Queue, populated from any thread.
        self._listeners: list[asyncio.Queue] = []
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    # --------------- public API (called from FastAPI handlers) ---------------

    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def add_listener(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._listeners.append(q)
        return q

    def remove_listener(self, q: asyncio.Queue) -> None:
        try:
            self._listeners.remove(q)
        except ValueError:
            pass

    def snapshot(self) -> ProgressSnapshot:
        with self._lock:
            counts = self.db.counts_by_status()
            total = self.db.total_count()
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
                )
            return ProgressSnapshot(
                state=self._state.state,                     # type: ignore[arg-type]
                target_folder=self._state.target_folder,
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
        after a mutation that changes counts (item 5)."""
        self._broadcast_progress()

    def start(self, target_folder: str, registry_xlsx: str = "",
              default_repository: str = "") -> None:
        if self.is_running() and self._state.state not in ("paused",):
            # Just update the default repository if running
            with self._lock:
                self._state.default_repository = default_repository or self._state.default_repository
            self._set_message("Already running; default repository updated.")
            return

        target = Path(target_folder).expanduser().resolve()
        if not target.exists() or not target.is_dir():
            self._set_state("error", f"Target folder does not exist: {target}")
            return

        # If we were paused, just resume.
        if self.is_running() and self._state.state == "paused":
            with self._lock:
                self._state.default_repository = default_repository or self._state.default_repository
            self.resume()
            return

        with self._lock:
            self._state.target_folder = str(target)
            self._state.registry_xlsx = (
                registry_xlsx
                or self.cfg.registry_xlsx
                or str(target / "registry.xlsx")
            )
            self._state.started_at = now_iso()
            self._state.default_repository = default_repository or ""
            self._state.thinking_overrides.clear()

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
        self._set_state("scanning", "Started")

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
        # Item 4: even if the worker thread is no longer alive, force the
        # state to 'idle' so the UI doesn't get stuck on 'stopping' or
        # 'running' forever.
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
        # Forcibly close the in-flight LLM HTTP socket so the worker
        # exits its httpx.post() within milliseconds instead of waiting
        # for the LLM to finish.
        client = self._llm_client
        if client is not None:
            try:
                client.cancel()
            except Exception:
                pass

    def shutdown_blocking(self, timeout: float = 5.0) -> None:
        """Used by FastAPI shutdown hook (Ctrl+C). Like stop() but
        also joins the worker thread so uvicorn can exit cleanly.
        """
        self.stop()
        t = self._thread
        if t is not None:
            t.join(timeout=timeout)

    def reevaluate(self, relative_paths: list[str], use_thinking: bool = False,
                   force: bool = False) -> tuple[int, int, int]:
        """Reset the given files to 'pending' (will be picked up on next scan loop).

        Returns (n_reset, n_skipped_due_to_manual_edit, n_skipped_status_skipped).
        - Manually-edited rows are skipped unless `force=True`.
        - Rows currently in 'skipped' status are ALWAYS refused (item 5);
          their `error` field is set to a hint instructing the user to
          flip the status to 'pending' first.
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
        return n_reset, n_skipped, n_skipped_status

    def set_default_repository(self, repository: str) -> None:
        with self._lock:
            self._state.default_repository = repository or ""
        self._set_message(f"Default repository set to: {repository!r}")

    def signal_skip(self, relative_path: str) -> None:
        """Tell the worker the user wants this file skipped.

        If the file is the one currently being processed, also cancels the
        in-flight LLM HTTP call so the worker can react within ~1 second
        instead of waiting for the LLM to finish (item 7).
        """
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
        """Check & remove a skip signal for `relative_path`. Returns True if
        the user asked to skip this file."""
        with self._lock:
            if relative_path in self._state.skip_signals:
                self._state.skip_signals.discard(relative_path)
                return True
            return False

    # --------------- worker thread ---------------

    def _run(self) -> None:
        try:
            self._scan_target_folder()
            self._set_state("running", "Scan complete; processing files...")
            self._process_loop()
        except Exception as e:
            log.exception("Worker crashed")
            self._set_state("error", f"Worker crashed: {e}")
        finally:
            # Final Excel write
            self._regenerate_excel()
            with self._lock:
                if self._state.state not in ("error",):
                    self._state.state = "idle"
                    self._state.current_file = ""
                    self._state.current_file_progress = None
            self._broadcast_progress()
            log.info("Worker thread exiting.")

    def _scan_target_folder(self) -> None:
        target = Path(self._state.target_folder)
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

                # Item 3: store both the file's full absolute path and its
                # parent folder's full + relative paths so the UI can open
                # files even after the worker is idle (i.e. when no active
                # target_folder is set on the JobManager).
                try:
                    full_path = str(p.resolve())
                    full_folder_path = str(p.resolve().parent)
                except OSError:
                    full_path = str(p)
                    full_folder_path = str(p.parent)
                # parent folder path relative to the scanned target ("" if
                # the file is at the root of the scanned folder).
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
                    full_path=full_path,
                    full_folder_path=full_folder_path,
                    relative_folder_path=relative_folder_path,
                )
                n_seen += 1
                if status == "pending":
                    n_added += 1
                else:
                    n_unchanged += 1

                # Item 6: auto-skip files that aren't documents or images.
                # Only flip rows that are in 'pending' (don't override an
                # already-done/error/skipped/processing decision).
                if ext not in ALLOWED_DOC_OR_IMAGE_EXTENSIONS and status == "pending":
                    self.db.mark_status(
                        rel,
                        "skipped",
                        f"not_a_document_or_image: {ext or '(no extension)'}",
                    )

                if n_seen % 50 == 0:
                    self._set_message(
                        f"Scanned {n_seen} files (queued {n_added}, unchanged {n_unchanged})..."
                    )

        # Remove DB rows for files no longer on disk
        removed = self.db.delete_files_not_in(present)

        # Recompute duplicate flags authoritatively for the whole registry
        n_groups = self.db.recompute_duplicates()

        self._set_message(
            f"Scan done. {n_seen} files seen, {n_added} queued, {n_unchanged} unchanged"
            + (f", {removed} removed from registry" if removed else "")
            + (f", {n_groups} duplicate group(s) detected." if n_groups else ".")
        )

    def _process_loop(self) -> None:
        cfg = self.cfg
        client = LMClient(cfg.llm)
        self._llm_client = client
        try:
            processed_since_xlsx = 0
            while not self._stop_event.is_set():
                # Wait while paused
                if not self._pause_event.is_set():
                    self._set_state("paused", "Paused")
                    self._pause_event.wait()
                    if self._stop_event.is_set():
                        break
                    self._set_state("running", "Resumed")

                rec = self.db.next_pending()
                if rec is None:
                    # Item 4: no more pending work -> auto-stop the worker
                    # so the UI returns to a clean idle state. The user
                    # can press Start again to scan / resume processing.
                    self._regenerate_excel()
                    self._set_state("idle", "Idle — no pending work; worker stopped.")
                    return

                self.db.mark_status(rec.relative_path, "processing")
                with self._lock:
                    self._state.current_file = rec.relative_path
                self._broadcast_progress()

                # If we got cancelled mid-flight, the LLM client is now dead.
                # Re-create it so the next iteration can talk to LM Studio.
                if client._cancelled:  # noqa: SLF001 (intentional)
                    log.info("LLM client was cancelled; recreating.")
                    try:
                        client.close()
                    except Exception:
                        pass
                    client = LMClient(cfg.llm)
                    self._llm_client = client

                ok = self._process_one(client, rec)
                processed_since_xlsx += 1

                # If Stop was pressed during this file, requeue it
                if self._stop_event.is_set():
                    self.db.mark_status(rec.relative_path, "pending", "")
                    self._set_message(f"Requeued {rec.relative_path} (stopped during processing)")
                    break

                if ok and processed_since_xlsx >= cfg.excel_write_every_n_files:
                    self._regenerate_excel()
                    processed_since_xlsx = 0

            # Drain
            self._regenerate_excel()
        finally:
            try:
                client.close()
            except Exception:
                pass
            self._llm_client = None

    def _process_one(self, client: LMClient, rec) -> bool:
        cfg = self.cfg
        target = Path(self._state.target_folder)
        path = target / rec.relative_path

        # Initialize per-file progress state
        self._begin_file(rec.relative_path)

        # ---- Item 6 (defense in depth): never process a non-doc/non-image
        # file even if it slipped past the scan-time check. ----
        if rec.extension not in ALLOWED_DOC_OR_IMAGE_EXTENSIONS:
            reason = f"not_a_document_or_image: {rec.extension or '(no extension)'}"
            self._begin_step("skip_non_doc", percent=100, detail=reason)
            self.db.mark_status(rec.relative_path, "skipped", reason)
            self._set_message(f"Skipped (not a document or image): {rec.relative_path}")
            self._end_step("skip_non_doc", percent=100, detail="skipped")
            self._end_file()
            return True  # success in the sense that the worker should move on

        # ---- Item 1: if a byte-identical sibling is already 'done', reuse
        # its extraction instead of running the LLM. ----
        sibling = self.db.find_done_sibling(rec.sha256, rec.relative_path)
        if sibling is not None:
            with self._lock:
                default_repo = self._state.default_repository
            self._begin_step(
                "dup_reuse",
                percent=50,
                detail=f"Reusing extraction from duplicate: {sibling.relative_path}",
            )
            ok_copy = self.db.copy_extraction_from_sibling(
                rec.relative_path,
                sibling.relative_path,
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
            # If copy failed for any reason, fall through to normal extraction
            self._end_step("dup_reuse", percent=50, detail="copy failed; falling back to LLM")

        try:
            # ---- Step 1: extract text from the file ----
            self._begin_step("extract_text", percent=5,
                             detail=f"Reading {rec.extension} file ({_fmt_size(rec.file_size)})...")
            res = extract_any(path, rec.extension)

            # Item 7: user may have flipped this file to 'skipped' mid-flight
            if self._consume_skip_signal(rec.relative_path):
                self._end_step("extract_text", percent=10, detail="skipped by user")
                self.db.mark_status(
                    rec.relative_path, "skipped",
                    "Skipped by user during processing.",
                )
                self._set_message(f"Skipped (user request) during extract_text: {rec.relative_path}")
                self._end_file()
                return True

            if res.extraction_error and not res.text:
                self._end_step("extract_text", percent=10, detail=f"FAILED: {res.extraction_error}")
                self.db.mark_status(rec.relative_path, "error", f"extraction: {res.extraction_error}")
                self._set_message(f"Extraction failed: {rec.relative_path} ({res.extraction_error})")
                self._end_file()
                return False

            text_len = len(res.text or "")
            page_info = f", pages={res.page_count}" if res.page_count is not None else ""
            self._end_step("extract_text", percent=30,
                           detail=f"Extracted {text_len:,} chars{page_info}")

            text = truncate_head_middle_tail(
                res.text or "",
                cfg.extract.head_chars,
                cfg.extract.middle_chars,
                cfg.extract.tail_chars,
            )
            sent_len = len(text)
            if sent_len < text_len:
                self._set_detail(f"Truncated to {sent_len:,} chars (head/middle/tail) before sending to LLM")

            # ---- Step 2: LLM extraction ----
            override = self._consume_thinking_override(rec.relative_path)
            # Resolve the actual boolean we'll send to the client so the log
            # message reflects what's really happening (item 4).
            effective_thinking = (
                cfg.llm.thinking_default if override is None else override
            )
            thinking_label = "on" if effective_thinking else "off"
            self._begin_step("llm_extract", percent=35,
                             detail=f"Calling LLM (thinking={thinking_label})...")

            try:
                extraction, used_thinking = client.extract(
                    text=text,
                    file_name=rec.file_name,
                    relative_path=rec.relative_path,
                    use_thinking=override,
                )
            except LLMError as e:
                # Item 7: if the LLM was cancelled because the user asked to
                # skip this file, treat it as a clean skip rather than an error.
                if self._consume_skip_signal(rec.relative_path):
                    self._end_step("llm_extract", percent=50, detail="skipped by user (cancelled LLM)")
                    self.db.mark_status(
                        rec.relative_path, "skipped",
                        "Skipped by user during processing.",
                    )
                    self._set_message(
                        f"Skipped (user request) during llm_extract: {rec.relative_path}"
                    )
                    self._end_file()
                    return True
                raise

            # Item 7: user flipped to 'skipped' between the LLM call returning
            # and us writing to the DB. Honor it: do NOT save_extraction.
            if self._consume_skip_signal(rec.relative_path):
                self._end_step("llm_extract", percent=90, detail="skipped by user after LLM returned")
                self.db.mark_status(
                    rec.relative_path, "skipped",
                    "Skipped by user during processing.",
                )
                self._set_message(
                    f"Skipped (user request) after llm_extract: {rec.relative_path}"
                )
                self._end_file()
                return True

            # Build a verbose summary of what the model produced
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

            # ---- Step 3: persist ----
            self._begin_step("save", percent=95, detail="Saving to local DB...")
            with self._lock:
                default_repo = self._state.default_repository
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

        except LLMError as e:
            self._fail_step(detail=f"LLM error: {e}")
            self.db.mark_status(rec.relative_path, "error", f"llm: {e}")
            self._set_message(f"LLM error on {rec.relative_path}: {e}")
            self._end_file()
            return False
        except Exception as e:
            log.exception("Unexpected error processing %s", rec.relative_path)
            self._fail_step(detail=f"{type(e).__name__}: {e}")
            self.db.mark_status(rec.relative_path, "error", f"{type(e).__name__}: {e}")
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
                    # Compute duration from started_at
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
        ok = write_registry(records, out)
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
        self._broadcast_progress()

    def _broadcast_progress(self) -> None:
        if self._loop is None:
            return
        snap = self.snapshot().model_dump()
        payload = {"type": "progress", "data": snap}
        for q in list(self._listeners):
            try:
                self._loop.call_soon_threadsafe(_safe_put, q, payload)
            except RuntimeError:
                pass


# ---------- module-level helpers ----------

def _safe_put(q: asyncio.Queue, item) -> None:
    try:
        q.put_nowait(item)
    except asyncio.QueueFull:
        # Drop oldest, push newest
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
        # Prune in place
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
