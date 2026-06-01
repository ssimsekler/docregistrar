# docregistrar — handoff for "fix heartbeat percent-crawl & chunk-elapsed visibility"

Created: 2026-06-02 03:01 (Asia/Dubai)
Supersedes (in spirit) the older `_handoff_progress_visibility.md`. Both files
remain in the repo for traceability.

## Context

After the previous batch (commit `9a54e89` on `main`) shipped map-reduce + the
LLM heartbeat thread, two real bugs surfaced in usage:

1. **Per-file percent races to ~88% in ~3 minutes** even when only chunk 1 of
   N is in flight. The user reported: "after 11 minutes the file is at 88%
   and chunk progress still says 0/7 (starting)".
2. **No live `elapsed: Ns` ticker**: in map-reduce mode the heartbeat refuses
   to update `last_detail` because of an overly-clever guard.

## Root cause

In `backend/jobmanager.py`, `_heartbeat_run` does TWO things every 5 s:

a) Updates `cfp.last_detail` to `"Calling LLM ({mode})... elapsed: {N}s"` —
   but only `if cfp.sub_total <= 0`. In map-reduce mode the LLM client emits
   `progress_cb("chunk", 0, total)` immediately, which sets `sub_total = 7`,
   so this condition is false and the heartbeat never writes a tick.

b) Asymptotically creeps `cfp.percent` toward 89 with half-life 60s:
   ```python
   creep = 1.0 - math.exp(-elapsed / 60.0)
   pct = min(89, 35 + int(54 * creep))
   if pct > cfp.percent:
       cfp.percent = pct
   ```
   At 660 s elapsed (11 min), `creep ≈ 1`, `pct = 88`. So the bar hits 88%
   while still on chunk 1, which is misleading because the truthful percent
   should track chunk completions.

## Fixes to apply

### Fix 1 — Heartbeat must NOT advance `cfp.percent`

Drop the percent-crawl entirely. The chunk callback `_on_llm_progress`
already advances percent on real signals (`pct = 35 + int(55 * cur / total)`),
which is honest. The heartbeat only updates `last_detail`.

### Fix 2 — Heartbeat ALWAYS writes `last_detail`

Drop the `if cfp.sub_total <= 0:` gate. The heartbeat composes a single
unified message that combines wall-clock elapsed time with whatever
chunk/reduce state is current. The chunk callback is allowed to overwrite
it on actual events.

### Fix 3 — Track per-chunk start time

Closure-captured monotonic timestamps in `_process_one` (via dicts, since
inner functions can mutate dict items but not rebind ints):

- `chunk_state = {"started_at": None}` (also `reduce_state`)
- In `_on_llm_progress(unit, cur, total)`:
  - If `unit == "chunk"`:
    - If `cur == 0` (very first call) → `chunk_state["started_at"] = time.monotonic()`.
    - If `cur > 0` and `cur < total` → chunk `cur` just finished, chunk
      `cur+1` just started → `chunk_state["started_at"] = time.monotonic()`.
    - If `cur == total` → all chunks done → leave or clear.
  - If `unit == "reduce"` and `cur == 0` → `reduce_state["started_at"] = time.monotonic()`.
  - If `unit == "reduce"` and `cur == 1` → reduce finished; clear.

The heartbeat reads these and renders:

- `unit == "chunk"`, mid-flight:
  `Processing chunk {sub_current+1}/{sub_total} (chunk elapsed: Ms, total LLM: Ns)`
- `unit == "reduce"`:
  `Reducing... elapsed: Ms (total LLM: Ns)`
- single-shot (no chunk/reduce signal yet):
  `Calling LLM (single-shot)... elapsed: Ns`

Format Ns/Ms human-friendly with the existing `_fmt_size`-style helper
(implement a small `_fmt_secs` if needed: `"42s"`, `"1m 12s"`, `"3m 45s"`).

### Fix 4 — UI label branches on `sub_unit`

In `frontend/app.jsx`, two places (header status line + detail-panel
sub-progress label):

```js
const subPrefix = (() => {
  if (!cfp.sub_unit) return "";
  if (cfp.sub_unit === "chunk") return "Processing chunk";
  if (cfp.sub_unit === "reduce") return "Reducing";
  // page / slide / sheet / paragraph etc.
  return `Reading ${cfp.sub_unit}`;
})();
```

Then render `${subPrefix}: ${sub_current}/${sub_total} (XX%)`. (For "chunk"
the natural English would be "Processing chunk N/M" without a colon
between "chunk" and "N", so consider rendering it as `${subPrefix} ${sub_current}/${sub_total}` instead — small style call.)

## Files to touch

- `backend/jobmanager.py` (Fixes 1, 2, 3) — primary edit.
- `frontend/app.jsx` (Fix 4) — label branch in two places.

## Validation

```powershell
python -m py_compile backend/jobmanager.py backend/config.py backend/settings.py backend/llm.py
python -c "from backend.main import app" 2>&1 | Out-Null; echo "exit=$LASTEXITCODE"
```

Manual: restart, re-evaluate a 100-slide pptx (~70k chars). Expect:

- Per-file percent stays at 35% until chunk 1 actually finishes.
- After chunk 1: percent jumps to ~43%. After chunk 7: ~89%. Then reduce.
- Live status ticks every 5s with chunk-aware elapsed:
  `Processing chunk 1/7 (chunk elapsed: 142s, total LLM: 142s)` →
  after chunk 1 finishes:
  `Processing chunk 2/7 (chunk elapsed: 3s, total LLM: 145s)` etc.

## Pitfalls / things to watch

- The chunk callback uses the existing throttle (0.5s) for non-boundary
  events; the heartbeat operates independently every 5s. They share the
  `cfp.last_detail` field. The heartbeat overwriting the callback's
  message is fine — they're saying compatible things.
- After Fix 1, the per-file percent will *appear less responsive* during a
  long single-shot LLM call (it'll sit at 35% the whole time). That's
  honest — the wall-clock `elapsed: Ns` text proves it's alive. If we
  later decide a *small* visual creep is OK, gate it to single-shot
  mode only and cap at e.g. 50%.

## Commit message after fix

```
Fix heartbeat percent-crawl + chunk-elapsed visibility

- Heartbeat no longer fakes per-file percent (was racing to ~88% in
  ~3 min via 1 - exp(-elapsed/60) curve; misleading users into
  thinking the file was nearly done while still on chunk 1). Per-file
  percent now only advances on concrete signals (chunk completion,
  step transitions).
- Heartbeat always writes last_detail (dropped the sub_total<=0 gate);
  in map-reduce mode it now shows
    "Processing chunk X/N (chunk elapsed: Ms, total LLM: Ns)"
  using chunk_started_at, even between chunk-completion events. Same
  for reduce ("Reducing... elapsed: Ms").
- _on_llm_progress now records chunk_started_at / reduce_started_at on
  each boundary so the heartbeat can render per-chunk elapsed time.
- UI: status line + detail panel sub-progress label branches on
  sub_unit: "Reading <unit>" for page/slide/sheet/paragraph,
  "Processing chunk" for chunk, "Reducing" for reduce.
- Adds _handoff_progress_visibility_2026-06-02_0301.md.