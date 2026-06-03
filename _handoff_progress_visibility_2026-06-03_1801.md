# Handoff: progress visibility, log fidelity, and summary quality fixes

> Created: **2026-06-03 18:01 (Asia/Dubai, UTC+4:00)**
> Project: `c:/Users/I768387/OneDrive - SAP SE/Documents/99. Workareas/dev/docregistrar`
> Author of plan: Cline (planning session)
> Audience: future implementer (Cline in Act mode, or a human dev)
>
> Read this top-to-bottom; the "Implementation order" section at the end says exactly what to do first, second, third.

---

## 0. TL;DR

We diagnosed three independent issues during a 12-hour run on a single 600-slide `.pptx`:

1. **`run.bat` log noise**: a WebSocket disconnect produced two unhelpful `asyncio Task exception was never retrieved` tracebacks. Harmless but loud.
2. **Activity Log pane drift**: the in-browser panel diverged badly from reality. Multiple causes — verbosity filter hides step lines, 1000-line cap, replay-on-mount-only (no replay on WS reconnect), heartbeat lines crowd everything else.
3. **Summary quality regression on chunked files**: the LLM's `summary` field for a big map-reduce file contained literal text like *"This chunk contains slides 247–259…"*, exposing the chunked extraction strategy to end users. The reduce step is supposed to fix this but failed silently and the deterministic fallback was used.

Plus, from the previous session (`_handoff_progress_visibility_2026-06-02_1531.md`-style follow-up that we never wrote): the LLM kept returning `(empty response)` for ~30 chunks, costing ~12 hours of wall-clock time. Mitigation needs to land too.

This handoff specifies what to change, where, and why.

---

## 1. Problem statements with evidence

### 1.1 `run.bat` console noise: WebSocket disconnect tracebacks

```
2026-06-03 01:58:55,372 asyncio [ERROR] Task exception was never retrieved
future: <Task finished name='Task-3349' coro=<ws_endpoint.<locals>._send_loop() ...
... websockets.exceptions.ConnectionClosedError: sent 1011 (internal error) keepalive ping timeout; no close frame received
... starlette.websockets.WebSocketDisconnect: (<CloseCode.ABNORMAL_CLOSURE: 1006>, '')
```

Two daemon coroutines `_send_loop` / `_recv_loop` in `backend/main.py` (around line 721 / 753) raise `WebSocketDisconnect` / `ConnectionClosedError` / `ClientDisconnected` on a normal client disconnect. They aren't `gather()`'d nor have their exceptions consumed, so asyncio prints the full traceback at ERROR level. The worker is unaffected, but it spams the console.

### 1.2 Activity Log panel drifts from reality

Concrete symptom (user-reported):
- 1:46 AM: last `[hb] LLM alive — chunk 30/60` line visible.
- 12:40 PM: chunk-30 completion + Registry updated + `[user] ws subscribe`.
- 5:02 PM: `[user] start repository=…` for the next run.
- The 4½-hour gap between 12:40 PM and 5:02 PM has zero lines, even though the run.bat console shows hundreds (heartbeats, chunk-completion, `[WARNING] retry failed`).

Root causes (in `frontend/app.jsx`):

| # | Mechanism | Code reference |
|---|---|---|
| A | "normal" verbosity hides step lines | `if (v === "normal") allow = !isStepMsg;` — any line starting with two spaces + `[…]` is dropped. Heartbeats, chunk-completes, extract_text/llm_extract/save are all step lines. |
| B | Hard 1000-line cap on the in-memory state | `setLogLines(prev => [...lines, ...prev].slice(-1000));` — appears in two places (WS handler and replay-on-mount handler). For a 12-hour run this overflows even at "normal" verbosity. |
| C | Replay only runs on mount | The `useEffect` that calls `GET /api/events?limit=1000` runs once. After a WS drop + reconnect, missed lines are not back-filled. |
| D | Verbosity is not persisted | Resets to "normal" on every reload; users keep "losing" the verbose stream. |
| E | Heartbeats crowd the buffer | One file produced ~430 `[hb]` lines. Even at "verbose" they push useful lines out of the 1000-line cap. |

Secondary contributing factor: the WebSocket itself dropped at 01:58:55,372 due to a keepalive ping timeout (laptop slept / Modern Standby), and our reconnect logic doesn't trigger a re-fetch of the persisted log.

### 1.3 Summary references the chunking strategy

Quoted summary from `Cloud ERP/Cloud ERP Private/20260213_Business-Scope-2025-SAP-Cloud-ERP-Private.pptx`:

> "This is a comprehensive presentation deck for SAP S/4HANA Cloud Private Edition 2025 Business Scope…"
>
> "**This chunk contains slides 247-259** of a presentation on SAP S/4HANA Service capabilities. Topics include…"
>
> "**This chunk contains slides** discussing SAP S/4HANA Compatibility Packs usage rights…"

Two separate problems are visible here:

(a) **The LLM is leaking the word "chunk" into per-chunk summaries.** Our `CHUNK_SYSTEM_PROMPT` tells the model "you are seeing ONE CHUNK" but never explicitly tells it *not to use the word "chunk" in the output*. The model dutifully starts each chunk-summary with "This chunk contains…".

(b) **The deterministic merge in `merge_partials_deterministic()` literally concatenates the per-chunk `summary` fields** (capped at 2500 chars total). When the reduce-LLM step fails (which it did for this file — `Reduce step failed for …; keeping deterministic merge`), what ends up in the final `summary` is exactly that concatenation. The `quality_score=0.95` is misleading because it's the legacy field from the deterministic path, not a real signal.

So even with a perfect reduce step, per-chunk text mentioning "chunk" / "slides 247–259" / "This chunk contains" leaks into the final summary on every reduce-failure (and we already saw the reduce frequently fails on big files, see §1.4).

### 1.4 The 12-hour empty-response storm (already documented in the previous answer, included here for completeness)

For chunks 31–60 of the 600-slide deck, every LLM call returned HTTP 200 with empty content. Each chunk took ~700s of wall-clock time. Diagnosis:

- LM Studio's response carries `content == ""` (or only `<think>…</think>` that we strip).
- With `max_output_tokens=4096` and a chunk of dense product-feature listings, the model burns the whole budget on hidden reasoning tokens and never emits the `{`.
- The `_extract_chunk_once` retry uses the same `max_tokens` and the same chunk text — same failure mode.

This needs to be addressed for the *content* problem in §1.3 to even become observable on a fresh run.

---

## 2. Fix plan

I'll group fixes by file. Implementer can land them in any order within each group; ordering across groups is in §3.

### 2.1 Backend — Python

#### 2.1.1 `backend/main.py` — quiet the WS disconnect tracebacks

Locate the helper coroutines `_send_loop` and `_recv_loop` inside `ws_endpoint` (~lines 721 / 753).

**Change:** wrap the entire body of each in:

```python
try:
    ...existing body...
except (WebSocketDisconnect, ClientDisconnected):
    return
except ConnectionClosedError:
    return
except Exception:
    log.exception("ws %s_loop: unexpected", "send" or "recv")
    return
```

Also: when both tasks are spawned, store them and at the end do `await asyncio.gather(send_task, recv_task, return_exceptions=True)` so any leftover exception is consumed (no more "Task exception was never retrieved").

Acceptance: a normal browser-tab close produces ONE INFO line like `ws closed cleanly (code=1006)` instead of two ERROR tracebacks.

#### 2.1.2 `backend/llm.py` — empty-response retry with bigger budget

Cause: chunk text + `/no_think` directive sometimes produces `content == ""` because the model spends max_tokens on hidden tokens.

In `_chat()` add a new typed exception:

```python
class LLMEmptyResponseError(LLMError):
    """LM returned HTTP 200 with empty `content`. Treated as a retry signal,
    not as 'invalid JSON', so callers can re-issue with larger max_tokens."""
```

Detection: after parsing `r.json()`, if `data["choices"][0]["message"]["content"]` is empty/whitespace OR if after `_strip_thinking()` the content is empty, raise `LLMEmptyResponseError(model=...)` instead of returning the empty string.

In `_extract_chunk_once()`:
1. If first attempt raises `LLMEmptyResponseError`, retry with `max_tokens = min(int(max_tokens * 1.5), self.cfg.max_output_tokens)` AND prepend a stronger directive line at the top of the user message: `"Respond with the JSON object DIRECTLY. Do not produce reasoning, do not produce <think> blocks, do not preface or postface."` Re-injection of `/no_think` already exists.
2. If second attempt also raises `LLMEmptyResponseError`, log a WARNING and return an empty `LLMExtraction()` (current behaviour for `LLMInvalidJSONError`).

Same pattern applies to `_extract_single_shot()` and `_reduce_narrative()`.

Acceptance: at least the `(empty response)` retry burns less wall-clock and has a fighting chance.

#### 2.1.3 `backend/llm.py` — better quality reporting on chunk failures

In `_extract_mapreduce()` after building `partials`, compute:

```python
chunk_yield = sum(1 for p in partials if p.quality_score > 0) / max(1, len(partials))
```

If `chunk_yield < 0.5`, set `merged.quality_score_min = 0.0` and a new optional field `merged.extraction_warning = f"low_chunk_yield: {chunk_yield:.0%} of {len(partials)} chunks produced output"` (add this to `LLMExtraction` schema in `backend/schemas.py`). Surface it through the worker so the file's row gets `error_text` set even if status is `done`. This makes the misleading `q=0.95` go away.

#### 2.1.4 `backend/llm.py` — fix the "this chunk contains" leak

Two changes:

(a) In `CHUNK_SYSTEM_PROMPT`, append an explicit ban:

```
NEVER use the word "chunk" in any output field. NEVER reference the
slicing strategy. The fields "title", "description", "summary" must read
as if you were describing the WHOLE document from the small portion you
can see — never as if you were summarizing a slice of it. If you cannot
infer global properties (title, date, document_type), leave them empty
rather than describing the slice.
```

(b) In `merge_partials_deterministic()`, **stop concatenating per-chunk summaries** verbatim. Replace the current "Summary fallback: concatenate top partial summaries up to 2500 chars" block with a much more conservative fallback:

```python
# When the LLM reduce step fails or is disabled, the deterministic
# fallback for `summary` should NOT splice together per-chunk text
# (which leaks the chunking strategy and reads like a slide-by-slide
# walkthrough). Pick the single best partial summary instead, prefer
# the longest non-empty one whose text does not match the
# /chunk|slide \d+|this section|this part/ heuristic. Truncate to
# 2500 chars.
import re as _re
_LEAK_RE = _re.compile(
    r"\b(this chunk|this slice|this section|this part|chunk \d+|slides? \d+[-–]\d+)\b",
    _re.IGNORECASE,
)
candidates = [
    s for s in (p.summary.strip() for p in partials)
    if s and not _LEAK_RE.search(s)
]
if candidates:
    summary = max(candidates, key=len)[:2500]
else:
    # All partials leaked the strategy; safer to return empty than
    # to publish chunk-y prose. The reduce step (when it works) or a
    # downstream re-evaluation will fix it.
    summary = ""
```

(c) Same scrubbing logic should apply to `description` (which has the same leak risk, just shorter).

(d) **Sanity-clean every per-chunk `summary` server-side after parsing.** In `LMClient._parse()`, run a small post-processing step that replaces obvious leaks ("This chunk", "this slice", "Slides 247-259 of") with neutral phrasing or strips the offending leading sentence. This way the reduce step's input is also clean.

Acceptance: a future run of the same `.pptx` either produces a clean reduce output or, if reduce fails, a single-chunk fallback that doesn't mention chunks/slice/slide-ranges.

#### 2.1.5 `backend/llm.py` / `backend/config.py` — saner defaults

Add to `config.example.yaml` and the `MapReduceConfig` defaults:

```yaml
extract:
  mapreduce:
    chunk_chars: 6000              # was 10000 — finishes more reliably
    per_chunk_max_output_tokens: 2048   # was 1500
llm:
  max_output_tokens: 6144          # was 4096
  per_file_timeout_seconds: 3600   # was 60000 — was effectively disabled
```

The `per_file_timeout_seconds` default change is the one that would have killed the 12-hour run at 1 hour with `llm_total_timeout`. Surface this in the Settings dialog (it's already an editable key).

#### 2.1.6 `backend/schemas.py` — add `extraction_warning`

Add an optional field `extraction_warning: str = ""` to `LLMExtraction` so §2.1.3 can report `low_chunk_yield`. The DB row's `error` column should be populated from this when status is `done`-but-warned.

### 2.2 Frontend — `frontend/app.jsx`

Multiple small changes; all in the same file.

#### 2.2.1 Replay on WS reconnect (covers the 4½-hour gap)

Track the timestamp of the most recently rendered line. When `ws.onopen` fires after a previous `onclose`, do:

```js
const lastTs = logLinesRef.current.length
    ? logLinesRef.current[logLinesRef.current.length - 1].ts || ""
    : "";
const url = lastTs
    ? `/api/events?limit=1000&since=${encodeURIComponent(lastTs)}`
    : `/api/events?limit=1000`;
api("GET", url).then(...).catch(...);
```

The backend's `list_events(since_iso=...)` already supports this. De-dupe by `(ts, message)` against the in-memory tail before appending.

#### 2.2.2 Lift the in-memory cap

Replace the magic `1000` in both `slice(-1000)` calls with a constant `LOG_BUFFER_MAX = 5000`. Add a small visual indicator at the top of the panel: `"showing last N of M; scroll up to load older from the persistent log"`. (Loading older is a stretch goal; the bumped cap alone covers normal usage.)

#### 2.2.3 Persist verbosity in localStorage

```js
const [verbosity, setVerbosity] = useState(() =>
    localStorage.getItem("docregistrar.verbosity") || "normal"
);
useEffect(() => {
    localStorage.setItem("docregistrar.verbosity", verbosity);
}, [verbosity]);
```

#### 2.2.4 Smarter heartbeat handling

Two options. Pick (b) for now; (a) is a future enhancement.

(a) **Rolling line for heartbeats**: track the last `[hb] LLM alive — chunk i/N …` line and replace-in-place rather than appending a new line each tick. This requires a small refactor (we currently append; we'd need to detect heartbeats and update the tail line if it's also a heartbeat for the same chunk).

(b) **Dedicated "heartbeats" toggle in the toolbar** independent of the verbosity dropdown. Default off — heartbeats only show when you tick the box. The verbosity filter logic becomes:

```js
const isHeartbeat = msg.startsWith("  [hb]");
const isStepMsg = (msg.startsWith("  [") || msg.startsWith("  ")) && !isHeartbeat;
let allow = true;
if (v === "quiet")        allow = !isStepMsg && !isBegin && !isHeartbeat;
else if (v === "normal")  allow = !isStepMsg && (showHeartbeats || !isHeartbeat);
else /* verbose */         allow = showHeartbeats || !isHeartbeat;
```

Persist `showHeartbeats` in localStorage too.

Acceptance: a 12-hour run no longer floods the panel with hundreds of `[hb]` lines while still letting the user opt in when debugging.

### 2.3 Backend small touch — `backend/jobmanager.py`

When the deterministic merge sets `extraction_warning` (from §2.1.3), surface it via the activity log: emit one `_set_message(f"  [llm_extract] WARNING: {extraction.extraction_warning}")` so it lands in `event_log` and the UI panel.

Also, persist the warning into the file row's `error` column even when status moves to `done`. (Today `error` is only set when status is `error`. Two options: (i) add a separate `warning` column to `files` in a v6 migration, or (ii) prefix the existing `error` column with `WARN: ` and tolerate that string in `done` rows. Option (i) is cleaner; option (ii) is faster to ship.)

---

## 3. Implementation order

Tier 1 — ship first (highest value / lowest risk):

1. **§2.1.1** — quiet WS tracebacks. 5-line patch. No behaviour change.
2. **§2.2.1** — replay on WS reconnect with `?since=`. Recovers the 4½-hour gap immediately, no backend changes needed.
3. **§2.2.2** — bump the 1000-line cap to 5000.
4. **§2.2.3** — persist verbosity in localStorage.
5. **§2.2.4(b)** — heartbeats toggle.

Tier 2 — content quality (do together):

6. **§2.1.4(a)** — chunk-prompt addendum.
7. **§2.1.4(b/c)** — leak-aware deterministic merge + scrubbing.
8. **§2.1.4(d)** — `_parse()` post-processing scrub.
9. **§2.1.5** — saner defaults (`chunk_chars`, `max_output_tokens`, `per_file_timeout_seconds`).

Tier 3 — robustness:

10. **§2.1.2** — `LLMEmptyResponseError` + bigger-budget retry.
11. **§2.1.3** + **§2.1.6** + **§2.3** — `extraction_warning` end-to-end.

Tier 4 — nice-to-have:

12. **§2.2.4(a)** — rolling heartbeat line (optional).

---

## 4. Acceptance tests (manual, since we don't have automated tests)

After landing Tier 1:
- Reload the browser tab during a run; observe the panel back-fills the lines from `event_log`.
- Force-close the laptop lid for 5 minutes mid-run; on resume, the panel should recover the missed lines.
- 12-hour run: panel stays responsive (5000-line cap holds).
- Verbosity dropdown choice survives a reload.
- run.bat console: no more `Task exception was never retrieved` tracebacks on a normal browser-tab close.

After landing Tier 2:
- Re-evaluate `Cloud ERP/Cloud ERP Private/20260213_Business-Scope-2025-SAP-Cloud-ERP-Private.pptx`. The `summary` field must NOT contain "this chunk", "this section", "slides X-Y of", "this part of the document", or any other phrase referencing the chunking strategy. (The reduce step should produce a clean global summary; the deterministic fallback should pick a single non-leaky candidate.)
- Inspect a few smaller files; their summaries must read identically to before (no regression).

After landing Tier 3:
- Force a `(empty response)` storm by setting `llm.max_output_tokens=512` and re-evaluating any large file. Observe the new `LLMEmptyResponseError` triggers a bigger-budget retry, and on second failure the chunk yields an empty partial. After the run, the file's row shows a `low_chunk_yield: NN%` warning and `quality_score=0.0`.
- Run with `llm.per_file_timeout_seconds=600` on the same large file. After 10 minutes the file must be marked `error: llm_total_timeout`, not allowed to grind for 12 hours.

---

## 5. Code-pointer cheat sheet

For the implementer:

| Section | File | Approx. line / function |
|---|---|---|
| §2.1.1 | `backend/main.py` | `ws_endpoint` → `_send_loop` (~753), `_recv_loop` (~721) |
| §2.1.2 | `backend/llm.py` | `_chat()` ~line 950; `_extract_chunk_once()` ~line 776 |
| §2.1.3 | `backend/llm.py` | `_extract_mapreduce()` ~line 700 |
| §2.1.4(a) | `backend/llm.py` | `CHUNK_SYSTEM_PROMPT` ~line 105 |
| §2.1.4(b/c) | `backend/llm.py` | `merge_partials_deterministic()` ~line 459 |
| §2.1.4(d) | `backend/llm.py` | `LMClient._parse()` ~line 1014 |
| §2.1.5 | `backend/config.py`, `config.example.yaml` | `MapReduceConfig`, `LLMConfig` |
| §2.1.6 | `backend/schemas.py` | `LLMExtraction` |
| §2.2.* | `frontend/app.jsx` | App component (`logLines`, `verbosity`, replay `useEffect`) |
| §2.3 | `backend/jobmanager.py` | `_process_one()` after `client.extract(...)` returns |

---

## 6. Notes / non-goals

- **Don't** re-architect the activity log into a virtualised list. The 5000-line cap with auto-scroll is plenty for a workstation app; tackle virtualisation only if a user complains.
- **Don't** change the reduce-step prompt yet. The leak fix is at the chunk level (so the reduce input is clean) and at the deterministic-fallback level (so a failed reduce doesn't leak). The reduce prompt itself already says "do not introduce new facts"; we don't want to over-constrain it.
- **Don't** persist the heartbeats toggle as part of `config.yaml`. It's a UI preference; localStorage is the right scope.
- The OneDrive-related path quirks (spaces in `OneDrive - SAP SE`) are unrelated; no fix needed.
- The `pillow-heif` install is already in `requirements.txt`; if it failed silently for the user we already log a clear line at startup.

---

## 7. Quick reference: examples of "leak" strings the scrubber must catch

For the regex / scrubber in §2.1.4, here are the actual leaks observed in the wild:

- `This chunk contains slides 247-259 of a presentation on …`
- `This chunk contains slides discussing SAP S/4HANA Compatibility Packs …`
- `Slides 474-484 covering Master Data Governance features …`
- `Slide content listing SAP Finance Fiori Apps for various roles …`
- `This part of the document describes …`
- `This section covers …` (already common in docs themselves; **do NOT scrub this one** unless it co-occurs with a slide range — risk of false positive)

Suggested final regex (chosen to avoid over-scrubbing legitimate prose):

```python
_LEAK_RE = re.compile(
    r"\b(?:"
    r"this\s+chunk"
    r"|this\s+slice"
    r"|chunk\s+\d+(?:\s*of\s*\d+)?"
    r"|slides?\s+\d+\s*[-–]\s*\d+"
    r"|slide\s+\d+\s*content"
    r")\b",
    re.IGNORECASE,
)
```

---

## 8. Cross-references

- Vision-support changes from earlier this session (image documents → multi-modal LLM call) are unrelated and already merged. They appear in the same `backend/llm.py` and `backend/jobmanager.py` files; the implementer should not undo them when applying this plan.
- The previous handoff (`_handoff_progress_visibility_2026-06-02_1531.md`) covered the "files-list / progress-bar visibility" round of changes. This handoff is the next iteration on the same broader theme but addresses a strictly different surface area (logs / summaries / robustness).

---

End of handoff.

