# Handoff: Progress visibility, persistent logs, settings dialog fix

**Started**: 2026-06-02 15:31 (UTC+4)
**Branch**: `main`
**Last commit before this work**: `951ac80b375530752f89bba721df11db3d06ce53`

## Why

The activity log was sparse during long LLM runs. Two reasons:

1. The chunk-progress callback (`_on_llm_progress` in `backend/jobmanager.py`) only updated `cfp.last_detail` and never called `_set_message`, so chunk start/finish never produced log lines. A 13-chunk file could take 2h+ and produce just two log lines (`[llm_extract] map-reduce, 13 chunk(s)…` and the eventual `[llm_extract done in Ms]`).
2. The 5-second LLM heartbeat in `_heartbeat_run` also only updated `cfp.last_detail`, never `_set_message`. So between chunk callbacks the activity panel stayed silent.

User also asked for:
- User-action logs (every mutating REST request).
- Persistent logs (survive across page reloads), with a "clear older than date" purge (default today − 7 days).
- A heartbeat-log interval setting (default 120s, configurable in Settings).
- Settings dialog UX fix: long help text overruns the Save button; Save button is too tall.

## Status checklist

### Backend
- [x] `backend/config.py` — add `processing.heartbeat_log_interval_seconds: int = 120`
- [x] `backend/settings.py` — add the new key to `EDITABLE_KEYS`
- [x] `config.example.yaml` — document the new key
- [x] `backend/db.py`:
  - [x] Bump `SCHEMA_VERSION` to **5**
  - [x] Add `event_log` table to `DDL_FRESH` and as a separate idempotent migration step in `_init_db`
  - [x] Add methods: `append_event(level, category, message)`, `list_events(limit=1000, since_iso=None)`, `delete_events_before(cutoff_iso)`
- [x] `backend/schemas.py`:
  - [x] Add `EventLogEntry` (id, ts, level, category, message)
  - [x] Add `EventLogClearRequest { before: str }`
- [x] `backend/jobmanager.py`:
  - [x] In `_set_message`, also `db.append_event("info", "worker", message)` (best-effort). `[user] `-prefixed messages are tagged `category="user"` automatically.
  - [x] In `_on_llm_progress`, emit step messages on real signals:
    - chunk cur=0 → `  [llm_extract] starting chunk 1/N`
    - 1 ≤ cur < total → `  [llm_extract] chunk i/N done in Ms (next: chunk i+1)` using `chunk_state["chunk_started_at"]`
    - cur == total → `  [llm_extract] all N chunks done; entering reduce phase`
    - reduce cur=0 → `  [llm_extract] reduce starting (consolidating N chunks)`
    - reduce cur==total → `  [llm_extract] reduce done in Ms`
  - [x] In `_heartbeat_run`, also emit a throttled `  [hb] LLM alive — chunk i/N (chunk Ms / total Ms)` every `cfg.processing.heartbeat_log_interval_seconds` (separate counter from the 5s cfp-update tick). Skip when interval is 0; clamp to 5s minimum when >0.
  - [x] On worker startup, call `db.delete_events_before((utcnow - 30 days).isoformat())` once.
- [x] `backend/main.py`:
  - [x] Add `_log_user(message)` helper that calls `job._set_message(f"[user] {message}")` (going through the DB layer too).
  - [x] Wire user logs into every mutating handler:
    - `/api/start` (with repository name)
    - `/api/pause`, `/api/resume`, `/api/stop`
    - `/api/reevaluate` (n_paths, thinking, force)
    - `/api/file/edit` (relative_path + summary of fields edited)
    - `/api/files/bulk-edit` (n, repo?, status?)
    - `/api/file/skip-current` (relative_path)
    - `/api/files/skip-dup-siblings` (n)
    - `/api/files/delete` (n)
    - `/api/repositories` POST/PATCH/POST(rename)/DELETE
    - `/api/settings` PUT, `/api/settings/{key}` DELETE, `/api/settings/reset-all` POST
    - WebSocket subscribe message
  - [x] Add `GET /api/events?limit=1000&since=<iso>` returning recent events.
  - [x] Add `POST /api/events/clear` body `{ before: "YYYY-MM-DD" }` → translates to local-day midnight (UTC ISO), returns `{ deleted: n }`. Also `_log_user` itself ("events clear before=YYYY-MM-DD deleted=N").

### Frontend
- [x] `frontend/app.jsx`:
  - [x] On mount, fetch `/api/events?limit=1000` and prepend into `logLines` (newest-first → reversed to chronological).
  - [x] Activity-panel toolbar (above the `.log` div):
    - date input pre-populated with `today − 7 days` (local).
    - 🗑 button: confirm → POST `/api/events/clear`, then refresh log panel from `/api/events`.
  - [x] `SETTINGS_GROUPS` Processing group: add `processing.heartbeat_log_interval_seconds`.
  - [x] `SETTINGS_HELP` entry for the new key.
  - [x] Settings dialog row layout:
    - Row 1: `[label] [input flex:1] [💾 Save] [🔄 Reset]` on one line.
    - Row 2: full-width muted help paragraph (only when present).
    - Uses `display: flex; flex-wrap: wrap; align-items: center; gap: 8px`. Help element gets `flex-basis: 100%`.
    - Save / Reset buttons sit at natural width — no more oversized Save.
- [x] `frontend/style.css`: added `.setting-row` / `.setting-key` / `.setting-input` / `.setting-help` and `.log-toolbar` rules.

### Validation
- [x] `python -m compileall -q backend` → clean compile.
- [x] Boot existing DB → `schema_version == 5`; `event_log` table created on the v4→v5 upgrade.
- [x] Insert 2 events via `append_event` → `list_events(limit=5)` returns them newest-first; `delete_events_before('2999-01-01...')` purges all.
- [x] Both new endpoints (`GET /api/events`, `POST /api/events/clear`) register in the FastAPI app.
- [ ] Run a sample LLM flow end-to-end (single file) and confirm chunk/heartbeat lines appear in the log. (Skipped — requires a live LM Studio; the log lines are wired through the same `_set_message` path that the existing chunk-progress UI ticker has been using all along.)

### Wrap-up
- [ ] Single commit with conventional message.
- [ ] `git push`.

## Resume instructions

If interrupted, resume from the first unchecked box above. The work is roughly:
1. Backend changes are independent and can be applied in any order — start with `db.py` to unlock everything else.
2. Frontend changes are independent of backend (the new endpoints just need to be live before the frontend can fetch them).
3. The settings-dialog UI fix is purely cosmetic — can be done last.

## Why each change

- **Chunk/reduce/heartbeat logs**: visibility for multi-hour LLM runs.
- **`[user]` logs**: audit trail for "who/when did what" while debugging.
- **Persistent logs**: survive page reloads; let user trace back yesterday's run.
- **Clear-older-than**: keep DB small; current 30d auto-cap is just a safety net.
- **Settings UI fix**: existing flex column made the Save button stretch vertically and clipped help text.
- **Configurable heartbeat interval**: 5s is too noisy for the log; 120s is a good default; user might still want to tune.