# docregistrar

A local AI agent that walks a folder tree, extracts text from Office/PDF/text files, and asks a small language model running in **LM Studio** to fill in a rich set of metadata fields per document. Results are kept in a SQLite database and (re)published as a formatted Excel file (`registry.xlsx`) at the root of the scanned folder.

A small web UI on `http://localhost:8000` lets you start, pause, resume, stop, monitor progress, browse results, and re-evaluate selected files (optionally with the model's "thinking" mode for higher quality).

Everything runs **fully locally**. No paid add-ons. No data leaves your machine.

---

## What it extracts per file

**File-system / computed**
- File name
- Relative path (relative to the scanned folder)
- File size (bytes) and SHA-256 checksum
- Extension (technical type: pdf/docx/xlsx/pptx/…)
- Page or slide count
- OS created / modified timestamps

**Asked from the LLM**
- Title
- Summary (≤ 2500 characters)
- Document date (YYYY-MM-DD / YYYY-MM / YYYY)
- Last update date
- Document type (presentation, white paper, report, spreadsheet, …)
- Language
- Authors
- Version number
- **Confidentiality level** (inferred from headers/footers/content; one of `Public` / `Internal` / `Confidential` / `Strictly Confidential` / `Unknown`)
- **Named entities**: persons, organizations, locations, mentioned dates, **products & technologies** (e.g., "SAP BTP", "Kubernetes")
- Referenced key concepts
- Top 10 key phrases
- Tags / keywords
- Geographic scope / region
- Industry / business domain
- **Self-rated quality score** (0.0–1.0)

---

## Hardware requirements & recommended model

This project is designed for a Windows laptop with around 16 GB RAM and a 6 GB-VRAM laptop GPU. The recommended model is:

- **`qwen/qwen3.5-9b`** (publisher `lmstudio-community`, GGUF Q4_K_M)
  - Strong English instruction following + tool-use training (good for strict JSON)
  - Supports `enable_thinking` toggle — we use it OFF by default for speed and turn it ON automatically when the model's self-rated quality score is below the threshold
  - Will run partly on GPU, partly on CPU on a 6 GB GPU

You can swap the model in `config.yaml` (see "Configuration" below).

---

## Prerequisites

1. **Python 3.12 or 3.14** with pip available.
   Verify: `py --version` and `py -m pip --version`.
2. **LM Studio** installed (https://lmstudio.ai).
3. The recommended model downloaded **inside LM Studio**:
   - LM Studio → **Discover** → search `qwen/qwen3.5-9b` (publisher `lmstudio-community`)
   - Pick the recommended quant for your hardware (typically **Q4_K_M**)

---

## One-time LM Studio setup

1. Open LM Studio → **Developer** (or **Local Server**) tab.
2. Load the model `qwen/qwen3.5-9b`.
3. Set the **Context Length** to `8192` (you can raise to `16384` later if memory allows).
4. Set the **GPU offload** slider to ~`28` layers and adjust if you see out-of-memory errors.
5. Make sure the server is **OpenAI-compatible** and listening on `http://localhost:1234`.
6. Click **Start Server**.
7. Verify in a browser: `http://localhost:1234/v1/models` should return JSON.

> If you change the model, also update `llm.model` in `config.yaml`.

---

## Run the agent

In a terminal at the project root:

```bat
run.bat
```

That script will:
1. Create `.venv` (Python virtual environment) on first run.
2. Install all Python dependencies on first run.
3. Start the FastAPI server at `http://127.0.0.1:8000/`.

Open `http://127.0.0.1:8000/` in your browser.

In the UI:

1. Paste the **target folder** (e.g., `C:/Users/me/Documents/MyDocs`). Forward slashes work nicely on Windows.
2. Click **Start**.
3. Watch the progress bar, the file table, and the activity log.
4. Use **Pause** / **Resume** any time. **Stop** ends the worker; the next **Start** will pick up exactly where it left off.
5. To re-evaluate specific files: tick their checkboxes, optionally tick **thinking mode** for higher quality, then click **Re-evaluate**.
6. The Excel file is regenerated at `<target_folder>/registry.xlsx` every 10 completed files and at the end.

> Tip: keep `registry.xlsx` **closed** in Excel.exe while the agent runs. If it's open, the agent will keep a `.tmp` next to it and retry on the next checkpoint — no data is lost.

---

## How resuming works

- A SQLite database `data/state.db` is the source of truth.
- Every file's SHA-256 is computed during scan. Files with an unchanged hash are **skipped** automatically (their previously-extracted metadata stays).
- If a file's content changes, its row is reset to `pending` and re-processed.
- If the agent (or your laptop) crashes mid-file, that file is reset from `processing` → `pending` on next start.
- The Excel file is a derived view, regenerated atomically (`.tmp` → rename) from the SQLite store.

---

## Configuration

The first run uses `config.example.yaml`. To customize, **copy it to `config.yaml`** and edit. `config.yaml` is gitignored.

Key settings:

```yaml
target_folder: ""                # leave blank to set from the UI on each run

llm:
  model: "qwen/qwen3.5-9b"
  base_url: "http://localhost:1234/v1"
  temperature: 0.1
  thinking_default: false
  thinking_on_low_quality: true  # auto-rerun with thinking ON when quality < threshold
  low_quality_threshold: 0.6

extract:
  head_chars: 12000              # how much of the doc text to send to the LLM
  middle_chars: 4000
  tail_chars: 4000
  max_file_size_bytes: 0         # 0 = no skip; set e.g. 524288000 (500 MB) to skip huge files

excel_write_every_n_files: 10
```

---

## Project layout

```
docregistrar/
├── backend/
│   ├── main.py            # FastAPI app + WebSocket
│   ├── jobmanager.py      # scan, queue, pause/resume, worker thread
│   ├── db.py              # SQLite (canonical state)
│   ├── llm.py             # LM Studio client + JSON-only prompt + retry
│   ├── excel_writer.py    # registry.xlsx (atomic write)
│   ├── extractors/        # pdf, docx, pptx, xlsx, text, image
│   ├── schemas.py         # Pydantic models
│   └── config.py          # YAML config loader
├── frontend/
│   ├── index.html         # served by FastAPI
│   ├── app.jsx            # React via CDN, no build step
│   └── style.css
├── data/                  # state.db (gitignored)
├── requirements.txt
├── config.example.yaml
├── run.bat
└── README.md
```

---

## Troubleshooting

- **`py -m ensurepip --upgrade`** — run this if `pip` is missing on your Python install.
- **LM Studio "model not found"** — make sure the model identifier in `config.yaml` (`llm.model`) matches the one shown in LM Studio's local server page (e.g., `qwen/qwen3.5-9b`).
- **Out-of-memory in LM Studio** — lower the GPU offload by 4 layers, or shrink the context to `4096`.
- **Slow throughput** — try a smaller model (e.g., `phi-3.5-mini-instruct` Q4_K_M) by changing `llm.model` in `config.yaml`.
- **`registry.xlsx` not updating** — close it in Excel.exe (the agent will pick it up on the next checkpoint).
- **Re-running on same folder** — files with identical SHA-256 are skipped automatically. Use the UI's **Re-evaluate** button to force selected files.
- **Process stuck "scanning"** — the very first scan computes SHA-256 of every file; for 35 GB this can take several minutes.
- **The worker appears to freeze for several minutes on a laptop** — most modern laptops use **Modern Standby (S0)** instead of classic S3 sleep, and Windows can suspend background processes when the user is idle. The agent already requests `ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_AWAYMODE_REQUIRED` (covers both classic and Modern Standby), and on resume it emits a `[hb] system was suspended for ~Xm Ys (resumed at HH:MM:SS)` line in the activity log so you can tell at a glance whether the worker really froze or the OS just paused it.
  - To check whether your machine uses Modern Standby, run `powercfg /a` in PowerShell. If the output mentions "Standby (S0 Low Power Idle)" you're on Modern Standby.
  - If the worker keeps getting suspended despite our hint, Group Policy or your power plan is overriding it. Workarounds: keep the laptop **plugged in with the lid open**; set **Settings → System → Power → Sleep → "Plugged in"** to **Never**; or run the agent on a desktop / always-on machine.
  - The startup log line `[startup] keep-awake acquired ...; power model: Modern Standby (S0)` confirms which model your OS reports.

---

## Privacy

All extraction happens on your machine. The only network calls the agent makes are to `http://localhost:1234` (your LM Studio server). No telemetry, no cloud calls, no third-party APIs.

---

## v0.2 — what's new

- **Folder picker (📂 Browse)** opens a native Windows folder dialog from the header.
- **Repository field** — added to the schema, the registry, and the file-detail panel. Set the default in the header text box; it's recorded for every file processed from then on. Bulk-edit changes it for selected files.
- **Edit mode** in the file detail panel — click any file row, then ✎ Edit to change the LLM-produced fields manually. File-system fields stay read-only. Status can be moved back to `pending` to re-queue a file. Manually edited rows are shown with ✎ in the table and are protected from automatic re-evaluation; use the **Force** button or per-file confirmation to override.
- **Bulk edit** for selected files — appears as a second toolbar row when any rows are checked. Lets you set the **Repository** and/or the **Status** for many files at once.
- **Download Excel any time** — the **⬇ Download .xlsx** button in the header generates a fresh Excel from the current state and downloads it via your browser, even while processing is running.
- **Fast Stop / Ctrl+C** — Stop now closes the in-flight LM Studio HTTP socket so the worker exits within ~1 s instead of waiting for the LLM. Ctrl+C in `run.bat` triggers the same shutdown via FastAPI's lifespan hook.
- **Worker stays alive while idle** — once you Start, the worker keeps running even when there are no pending files, so you can re-evaluate, edit-to-pending, or scan again without restarting. Only **Stop** ends it.
- **Verbosity dropdown** (quiet / normal / verbose) controls how chatty the activity log is.
- **Per-file step timings + sub-progress** in the detail panel: extract_text → llm_extract → save, each with a duration and a one-line detail.
- **Total elapsed time** ticks in the header from when you click Start.
