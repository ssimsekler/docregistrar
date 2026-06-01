# docregistrar — handoff for "progress visibility & timeouts" task

Goal: improve UX visibility into the LLM step (single-shot heartbeat,
strategy decision, chunk progress for map-reduce), lower the map-reduce
threshold default, and add an end-to-end LLM timeout.

## User-confirmed parameters

- `extract.mapreduce.threshold_chars` default = **10000**
- `llm.per_file_timeout_seconds` default = **60000** (NEW key, ~16.7 hours)
- "elapsed: …" style heartbeat is fine
- Heartbeat tick interval: **5 s**
- On deadline overrun: mark file as `error` with `llm_total_timeout`
- Skip "force map-reduce" toggle (no benefit at 10k threshold)

## Items to implement

1. **Lower threshold default**
   `extract.mapreduce.threshold_chars: 25000 → 10000`
   in `backend/config.py` (`MapReduceConfig.threshold_chars`) and
   `config.example.yaml`.

2. **New LLM total-time budget**
   Add `llm.per_file_timeout_seconds: int = 60000` to `LLMConfig`
   (`backend/config.py`). Add to `EDITABLE_KEYS` in `backend/settings.py`.
   Add to the LLM group in `SETTINGS_GROUPS` in `frontend/app.jsx` plus a
   `SETTINGS_HELP` entry.

3. **Strategy decision visible BEFORE the LLM call**
   In `_process_one` (jobmanager.py), right before `_begin_step("llm_extract", …)`,
   compute the strategy and put it in the step's `detail`:
   - map-reduce: `"map-reduce, N chunks (78,400 chars > 10,000 threshold), thinking=off"`
   - single-shot: `"single-shot (9,200 chars ≤ 10,000 threshold), thinking=off"`
   Use `split_text_into_chunks(text, mr_cfg)` to compute the actual chunk count
   to display (call once, reuse — note: client will call it again internally;
   that's fine, it's a pure function with no side effects).

4. **Single-shot heartbeat + slow percent crawl**
   Inside `_process_one`, before invoking `client.extract(...)`, start a
   small background `threading.Thread` (daemon) that:
   - Wakes every 5 s.
   - Updates `cfp.last_detail` to
     `"Calling LLM ({mode_label})... elapsed: {N}s"`.
   - Slowly increments `cfp.percent` within the 35..89 band, e.g.
     `pct = min(89, 35 + int(54 * (1 - exp(-elapsed / 90))))` (asymptote
     toward 89, half-life ~60s).
   - Calls `self._broadcast_progress()` so the UI gets the tick.
   - Stops when a `threading.Event` is set (we set it in `finally` after
     `client.extract` returns/raises).
   - Does the deadline check (item 5).

5. **Per-file LLM deadline**
   - Capture `deadline = time.monotonic() + cfg.llm.per_file_timeout_seconds`
     just before calling `client.extract`.
   - The heartbeat thread, on each tick, checks `time.monotonic() > deadline`.
     If exceeded:
     - Log a warning with `relative_path`.
     - Call `client.cancel()` (closes the underlying httpx client; in-flight
       chat raises and is caught as `LLMCancelled` or `LLMTransportError`).
     - Set a flag (`self._llm_deadline_hit = True` or pass-by-closure).
     - Stop ticking.
   - In the `LLMCancelled` / `LLMTransportError` exception handler in
     `_process_one`: if the deadline flag is set, branch to a NEW error
     path that marks the file `error` with detail `llm_total_timeout: ...`.
     Otherwise existing behavior (skip/error) is preserved.
   - **Map-reduce** also benefits from this: if a chunk loop runs past
     the deadline, the heartbeat cancels mid-chunk just the same. No
     separate per-chunk check needed if we already cancel the client.
   - Default 60000s makes this effectively a safety net, not an active
     limit; user-configurable down to e.g. 60s.

6. **Initial chunk broadcast bypasses throttle**
   In `_on_llm_progress` callback in `_process_one`:
   the existing code already special-cases `cur == 0 || cur == total`
   as boundary, but verify the boundary case ALWAYS broadcasts immediately,
   even if 0 ms have elapsed. (One-line check.)

7. **Activity log: include chosen strategy in `Begin: <path>` message**
   The `_begin_file` already emits `Begin: <path>` once. Right after it,
   emit `_set_message(f"  Strategy planned: {strategy_label}")`
   when we know it (after extraction). Optional but cheap.

## Files to touch

- `backend/config.py`
- `backend/settings.py`         (EDITABLE_KEYS)
- `backend/jobmanager.py`       (most of the work)
- `config.example.yaml`
- `frontend/app.jsx`            (SETTINGS_GROUPS + SETTINGS_HELP)

## Validation

```powershell
python -m py_compile backend/jobmanager.py backend/config.py backend/settings.py backend/llm.py
python -c "from backend.main import app" 2>&1 | Out-Null; echo "exit=$LASTEXITCODE"
```

Manual test:
- Restart the app.
- Pick a 23-slide pptx (~23k chars). Expect:
  a) `extract_text` step finishes with the existing "Extracted N chars"
     summary.
  b) `llm_extract` step's detail starts with
     `"map-reduce, 3 chunks (23,225 chars > 10,000 threshold), thinking=off"`.
  c) Sub-progress ticks `chunk 1/3`, `chunk 2/3`, etc.
- Pick a tiny .txt (<10k chars). Expect:
  a) `llm_extract` detail: `"single-shot (5,000 chars ≤ 10,000 threshold), thinking=off"`.
  b) The status detail ticks every ~5s: `"Calling LLM (single-shot)... elapsed: 5s"`,
     then 10s, then 15s, etc.
- In Settings, set `llm.per_file_timeout_seconds = 30`, save. Re-evaluate any
  file whose LLM call takes >30s. Expect:
  a) After ~30s, the heartbeat aborts the call.
  b) The file goes to `error` with detail `llm_total_timeout: ...`.

## Notes / pitfalls

- The heartbeat thread MUST be daemon=True and MUST be stopped in a
  `finally` block (use a `threading.Event` for clean shutdown).
- `client.cancel()` closes the httpx client, so the next file iteration
  must detect `client._cancelled` and rebuild it (this code path already
  exists in `_process_loop`).
- `cfp.percent` MUST never exceed the band of the current step. After
  llm_extract finishes successfully we already call
  `_end_step("llm_extract", percent=90, …)`, which clamps appropriately.