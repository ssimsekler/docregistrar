const { useEffect, useMemo, useRef, useState, useCallback } = React;

// ---------- helpers ----------
const fmtBytes = (n) => {
  if (n == null) return "";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0, x = Number(n);
  while (x >= 1024 && i < u.length - 1) { x /= 1024; i++; }
  return `${x.toFixed(x < 10 && i > 0 ? 1 : 0)} ${u[i]}`;
};
const fmtMs = (ms) => {
  if (ms == null) return "";
  if (ms < 1000) return `${ms} ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(1)} s`;
  const m = Math.floor(ms / 60000), s = Math.floor((ms % 60000) / 1000);
  return `${m}m ${s}s`;
};

async function api(method, path, body) {
  const r = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!r.ok) {
    const text = await r.text().catch(() => "");
    throw new Error(`${r.status} ${r.statusText}: ${text}`);
  }
  return r.json();
}

// Field configs for the editor
const STRING_FIELDS = [
  ["title", "Title"],
  ["document_type", "Document type"],
  ["document_date", "Document date"],
  ["last_update_date", "Last update date"],
  ["language", "Language"],
  ["version", "Version"],
  ["confidentiality", "Confidentiality"],
  ["geographic_scope", "Geographic scope"],
  ["industry_domain", "Industry domain"],
  ["repository", "Repository"],
];
const LIST_FIELDS = [
  ["authors", "Authors"],
  ["key_concepts", "Key concepts"],
  ["key_phrases", "Key phrases (top 10)"],
  ["tags", "Tags"],
  ["persons", "Persons"],
  ["organizations", "Organizations"],
  ["locations", "Locations"],
  ["mentioned_dates", "Mentioned dates"],
  ["products_technologies", "Products / technologies"],
];

const STATUS_OPTIONS = ["pending", "done", "error", "skipped"];

// ---------- small components ----------
function PropRow({ label, value }) {
  if (value === undefined || value === null || value === "" ||
      (Array.isArray(value) && value.length === 0)) {
    return (
      <div className="prop-row">
        <div className="prop-label">{label}</div>
        <div className="prop-value muted">—</div>
      </div>
    );
  }
  const v = Array.isArray(value) ? value.join(", ") : String(value);
  return (
    <div className="prop-row">
      <div className="prop-label">{label}</div>
      <div className="prop-value">{v}</div>
    </div>
  );
}

function EditField({ label, value, onChange, multiline = false }) {
  return (
    <div className="prop-row">
      <div className="prop-label">{label}</div>
      <div className="prop-value">
        {multiline ? (
          <textarea value={value ?? ""} onChange={e => onChange(e.target.value)}
                    rows={6} style={{ width: "100%" }} />
        ) : (
          <input type="text" value={value ?? ""} onChange={e => onChange(e.target.value)}
                 style={{ width: "100%" }} />
        )}
      </div>
    </div>
  );
}

function listToString(arr) {
  return Array.isArray(arr) ? arr.join("; ") : (arr || "");
}
function stringToList(s) {
  return (s || "").split(";").map(x => x.trim()).filter(Boolean);
}

const MAX_CUSTOM_PROPERTIES = 50;

function FileDetailPanel({ relativePath, currentProgress, onClose, onReevaluate, onEdited }) {
  const [rec, setRec] = useState(null);
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(false);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState({});
  // Custom properties are edited independently of the main "Edit" mode
  const [kv, setKv] = useState([]);                 // [{key, value}]
  const [kvSavingHint, setKvSavingHint] = useState("");

  const isCurrent = currentProgress && currentProgress.relative_path === relativePath;

  const load = useCallback(async () => {
    if (!relativePath) return;
    setLoading(true); setErr("");
    try {
      const r = await api("GET", `/api/file?relative_path=${encodeURIComponent(relativePath)}`);
      setRec(r);
      // Initialize the draft from the loaded record
      const e = r.extraction || {};
      const ne = e.named_entities || {};
      setDraft({
        title: e.title || "",
        summary: e.summary || "",
        document_date: e.document_date || "",
        last_update_date: e.last_update_date || "",
        document_type: e.document_type || "",
        language: e.language || "",
        version: e.version || "",
        confidentiality: e.confidentiality || "",
        geographic_scope: e.geographic_scope || "",
        industry_domain: e.industry_domain || "",
        repository: e.repository || "",
        quality_score: e.quality_score ?? "",
        authors: listToString(e.authors),
        key_concepts: listToString(e.key_concepts),
        key_phrases: listToString(e.key_phrases),
        tags: listToString(e.tags),
        persons: listToString(ne.persons),
        organizations: listToString(ne.organizations),
        locations: listToString(ne.locations),
        mentioned_dates: listToString(ne.dates),
        products_technologies: listToString(ne.products_technologies),
        status: r.status,
      });
      // Custom properties (independent edit)
      const cp = Array.isArray(e.custom_properties) ? e.custom_properties : [];
      setKv(cp.map(p => ({ key: p.key || "", value: p.value || "" })));
      setKvSavingHint("");
    } catch (e) { setErr(e.message); setRec(null); }
    finally { setLoading(false); }
  }, [relativePath]);

  const addKv = () => {
    setKv(prev => prev.length >= MAX_CUSTOM_PROPERTIES ? prev : [...prev, { key: "", value: "" }]);
  };
  const updateKv = (i, field, val) => {
    setKv(prev => prev.map((p, idx) => idx === i ? { ...p, [field]: val } : p));
  };
  const removeKv = (i) => {
    setKv(prev => prev.filter((_, idx) => idx !== i));
  };
  const saveKv = async () => {
    // Trim and drop fully-empty rows
    const cleaned = kv
      .map(p => ({ key: (p.key || "").trim(), value: (p.value || "").trim() }))
      .filter(p => p.key || p.value)
      .slice(0, MAX_CUSTOM_PROPERTIES);
    try {
      await api("POST", `/api/file/edit?relative_path=${encodeURIComponent(relativePath)}`,
                { custom_properties: cleaned });
      setKvSavingHint(`Saved ${cleaned.length} custom propert${cleaned.length === 1 ? "y" : "ies"}.`);
      await load();
      onEdited && onEdited();
    } catch (e) { alert(e.message); }
  };

  useEffect(() => { load(); }, [load]);

  // Auto-refresh while file is being processed (only when not editing)
  useEffect(() => {
    if (!isCurrent || editing) return;
    const id = setInterval(load, 1500);
    return () => clearInterval(id);
  }, [isCurrent, editing, load]);

  const setField = (k, v) => setDraft(prev => ({ ...prev, [k]: v }));

  const saveEdits = async () => {
    // Build payload: only fields that changed from rec
    const e = rec.extraction || {};
    const ne = e.named_entities || {};
    const payload = {};
    const cmp = (cur, next) => (cur ?? "") !== (next ?? "");
    const cmpList = (cur, nextStr) => {
      const a = (cur || []).join("; ");
      return a !== (nextStr || "");
    };

    for (const [k] of STRING_FIELDS) {
      const cur = e[k] ?? "";
      if (cmp(cur, draft[k])) payload[k] = draft[k];
    }
    if (cmp(e.quality_score ?? "", draft.quality_score ?? "")) {
      const q = parseFloat(draft.quality_score);
      if (!Number.isNaN(q)) payload.quality_score = q;
    }
    if (cmpList(e.authors, draft.authors))         payload.authors = stringToList(draft.authors);
    if (cmpList(e.key_concepts, draft.key_concepts))   payload.key_concepts = stringToList(draft.key_concepts);
    if (cmpList(e.key_phrases, draft.key_phrases))     payload.key_phrases = stringToList(draft.key_phrases);
    if (cmpList(e.tags, draft.tags))               payload.tags = stringToList(draft.tags);
    if (cmpList(ne.persons, draft.persons))             payload.persons = stringToList(draft.persons);
    if (cmpList(ne.organizations, draft.organizations)) payload.organizations = stringToList(draft.organizations);
    if (cmpList(ne.locations, draft.locations))         payload.locations = stringToList(draft.locations);
    if (cmpList(ne.dates, draft.mentioned_dates))       payload.mentioned_dates = stringToList(draft.mentioned_dates);
    if (cmpList(ne.products_technologies, draft.products_technologies)) payload.products_technologies = stringToList(draft.products_technologies);

    // Summary
    if (cmp(e.summary ?? "", draft.summary)) payload.summary = draft.summary;

    // Status (always send if changed)
    if (draft.status && draft.status !== rec.status) payload.status = draft.status;

    if (Object.keys(payload).length === 0) {
      setEditing(false);
      return;
    }

    try {
      await api("POST", `/api/file/edit?relative_path=${encodeURIComponent(relativePath)}`, payload);
      setEditing(false);
      await load();
      onEdited && onEdited();
    } catch (e) { alert(e.message); }
  };

  if (!relativePath) return null;

  const e = rec?.extraction || {};
  const ne = e.named_entities || {};

  return (
    <div className="detail-panel">
      <div className="detail-head">
        <div className="detail-title">
          <div className="detail-name">📄 {rec?.file_name || relativePath.split("/").pop()}</div>
          <div className="detail-path muted">{relativePath}</div>
        </div>
        <div className="detail-actions">
          {!editing && <button onClick={() => setEditing(true)}
                               title="Enter edit mode for the LLM-produced fields, status, and named entities">✎ Edit</button>}
          {editing && <button className="primary" onClick={saveEdits}
                              title="Save edits and exit edit mode">💾 Save</button>}
          {editing && <button onClick={() => { setEditing(false); load(); }}
                              title="Discard edits and reload the saved values">Cancel</button>}
          {!editing && <button onClick={() => onReevaluate(relativePath, false)}
                               title="Queue this file for re-evaluation by the LLM (skipped if manually edited)">↻ Re-eval</button>}
          {!editing && <button onClick={() => onReevaluate(relativePath, true)}
                               title="Re-evaluate using the model's slower 'thinking' mode for higher quality">↻ + 🧠</button>}
          <button onClick={load}
                  title="Reload this file's data from the server (useful after external changes)">🔄</button>
          <button onClick={onClose} title="Close this details panel and show the activity log">✕</button>
        </div>
      </div>

      {loading && !rec && <div className="detail-body muted">Loading...</div>}
      {err && <div className="detail-body error-text">Error: {err}</div>}

      {rec && (
        <div className="detail-body">
          {/* Live processing block */}
          {isCurrent && currentProgress && !editing && (
            <div className="card live">
              <div className="card-title">
                ⚙️ Processing — {currentProgress.percent}% ({fmtMs(currentProgress.elapsed_ms)} elapsed)
              </div>
              <div className="mini-progress"><div style={{width: `${currentProgress.percent}%`}} /></div>
              <div className="step-list">
                {currentProgress.steps.map((s, i) => (
                  <div key={i} className={`step-row ${s.finished_at ? "done" : "active"}`}>
                    <div className="step-name">{s.name}</div>
                    <div className="step-time">{s.duration_ms != null ? fmtMs(s.duration_ms) : "..."}</div>
                    <div className="step-detail muted">{s.detail}</div>
                  </div>
                ))}
                {currentProgress.steps.length === 0 && <div className="muted">Starting...</div>}
              </div>
            </div>
          )}

          {/* File-system / computed (read-only) */}
          <div className="card">
            <div className="card-title">File (read-only)</div>
            {editing ? (
              <div className="prop-row">
                <div className="prop-label">Status</div>
                <div className="prop-value">
                  <select value={draft.status || rec.status}
                          onChange={e => setField("status", e.target.value)}>
                    {STATUS_OPTIONS.map(s => <option key={s} value={s}>{s}</option>)}
                  </select>
                  {draft.status === "pending" && rec.status !== "pending" && (
                    <span className="muted" style={{ marginLeft: 8 }}>
                      (will re-process; clears extracted data)
                    </span>
                  )}
                </div>
              </div>
            ) : (
              <PropRow label="Status" value={rec.status} />
            )}
            <PropRow label="Error" value={rec.error} />
            <PropRow label="Size" value={fmtBytes(rec.file_size)} />
            <PropRow label="Pages / slides" value={rec.page_count} />
            <PropRow label="Extension" value={rec.extension} />
            <PropRow label="SHA-256" value={rec.sha256} />
            <PropRow label="OS created" value={rec.os_created} />
            <PropRow label="OS modified" value={rec.os_modified} />
            <PropRow label="Indexed at" value={rec.indexed_at} />
            <PropRow label="Used thinking" value={rec.used_thinking ? "Yes" : "No"} />
            <PropRow label="Manually edited" value={rec.manually_edited ? "Yes" : "No"} />
          </div>

          {/* Extracted properties */}
          <div className="card">
            <div className="card-title">Extracted properties {editing && "(editable)"}</div>
            {STRING_FIELDS.map(([k, label]) => (
              editing
                ? <EditField key={k} label={label} value={draft[k]} onChange={v => setField(k, v)} />
                : <PropRow key={k} label={label} value={e[k]} />
            ))}
            {editing
              ? <EditField label="Quality score (0-1)" value={draft.quality_score}
                           onChange={v => setField("quality_score", v)} />
              : <PropRow label="Quality score"
                         value={e.quality_score != null ? Number(e.quality_score).toFixed(2) : ""} />
            }
          </div>

          <div className="card">
            <div className="card-title">Summary {editing && "(editable)"}</div>
            {editing
              ? <EditField label="" value={draft.summary} onChange={v => setField("summary", v)} multiline />
              : <div className="summary-text">{e.summary || <span className="muted">—</span>}</div>
            }
          </div>

          {/* Lists (named entities + topics) */}
          <div className="card">
            <div className="card-title">Lists {editing && "(use ; to separate items)"}</div>
            {LIST_FIELDS.map(([k, label]) => {
              if (editing) {
                return <EditField key={k} label={label} value={draft[k]} onChange={v => setField(k, v)} />;
              }
              if (k === "mentioned_dates") {
                return <PropRow key={k} label={label} value={ne.dates} />;
              }
              if (k === "persons" || k === "organizations" || k === "locations" ||
                  k === "products_technologies") {
                return <PropRow key={k} label={label} value={ne[k]} />;
              }
              return <PropRow key={k} label={label} value={e[k]} />;
            })}
          </div>

          {/* Custom properties (independent of Edit mode) */}
          <div className="card">
            <div className="card-title" title="User-defined key/value pairs. Saved per file. Included in the Excel export.">
              Custom properties ({kv.length}/{MAX_CUSTOM_PROPERTIES})
            </div>
            <div className="kv-list">
              {kv.length === 0 && (
                <div className="muted" style={{ fontSize: "12px" }}>
                  No custom properties yet. Click <b>+ Add</b> to create one.
                </div>
              )}
              {kv.map((p, i) => (
                <div className="kv-row" key={i}>
                  <input
                    className="kv-key"
                    type="text"
                    placeholder="key"
                    value={p.key}
                    onChange={ev => updateKv(i, "key", ev.target.value)}
                    title="Property key (free text)"
                  />
                  <input
                    type="text"
                    placeholder="value"
                    value={p.value}
                    onChange={ev => updateKv(i, "value", ev.target.value)}
                    title="Property value (free text)"
                  />
                  <button className="kv-remove" onClick={() => removeKv(i)}
                          title="Remove this key/value pair">🗑</button>
                </div>
              ))}
            </div>
            <div className="kv-actions">
              <button onClick={addKv}
                      disabled={kv.length >= MAX_CUSTOM_PROPERTIES}
                      title={kv.length >= MAX_CUSTOM_PROPERTIES
                        ? `Limit reached (${MAX_CUSTOM_PROPERTIES})`
                        : "Add a new custom property row"}>
                ➕ Add property
              </button>
              <button className="primary" onClick={saveKv}
                      title="Save custom properties to this file (writes to local DB and to registry.xlsx on next checkpoint)">
                💾 Save custom
              </button>
              {kvSavingHint && <span className="kv-status">{kvSavingHint}</span>}
              <span className="kv-status muted" style={{ marginLeft: "auto" }}>
                Excel format: <code>k1: v1 | k2: v2 | …</code>
              </span>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

// ---------- main app ----------
function App() {
  const [config, setConfig] = useState(null);
  const [progress, setProgress] = useState({
    state: "idle", target_folder: "",
    total: 0, done: 0, error: 0, skipped: 0, pending: 0, processing: 0,
    current_file: "", last_message: "", started_at: null,
    current_file_progress: null,
  });
  const [folder, setFolder] = useState("");
  const [repository, setRepository] = useState("");
  const [files, setFiles] = useState([]);
  const [statusFilter, setStatusFilter] = useState("");
  const [search, setSearch] = useState("");
  const [selected, setSelected] = useState(new Set());
  const [logLines, setLogLines] = useState([]);
  const [useThinking, setUseThinking] = useState(false);
  const [verbosity, setVerbosity] = useState("normal");
  const [openedPath, setOpenedPath] = useState("");
  const [tick, setTick] = useState(0);
  // Bulk-edit input states
  const [bulkRepo, setBulkRepo] = useState("");
  const [bulkStatus, setBulkStatus] = useState("");
  // Right-panel width (resizable, persisted)
  const [rightWidth, setRightWidth] = useState(() => {
    const saved = parseInt(localStorage.getItem("docregistrar.rightWidth") || "", 10);
    if (!Number.isNaN(saved) && saved >= 280 && saved <= 1600) return saved;
    return 460;
  });
  const [dragging, setDragging] = useState(false);
  const wsRef = useRef(null);
  const verbosityRef = useRef(verbosity);
  useEffect(() => { verbosityRef.current = verbosity; }, [verbosity]);

  // Splitter drag handlers (mouse on whole window so pointer can leave splitter)
  useEffect(() => {
    if (!dragging) return;
    const onMove = (e) => {
      const min = 280;
      const max = Math.max(min + 100, window.innerWidth - 320);
      const w = Math.min(max, Math.max(min, window.innerWidth - e.clientX));
      setRightWidth(w);
    };
    const onUp = () => {
      setDragging(false);
      try { localStorage.setItem("docregistrar.rightWidth", String(rightWidth)); } catch {}
    };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
    return () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
  }, [dragging, rightWidth]);

  useEffect(() => {
    const id = setInterval(() => setTick(t => t + 1), 1000);
    return () => clearInterval(id);
  }, []);

  useEffect(() => {
    api("GET", "/api/config").then(c => {
      setConfig(c);
      if (c.default_target_folder) setFolder(c.default_target_folder);
    }).catch(console.error);

    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(`${proto}//${location.host}/ws`);
    wsRef.current = ws;
    ws.onmessage = (ev) => {
      try {
        const m = JSON.parse(ev.data);
        if (m.type === "progress") {
          setProgress(m.data);
          const msg = m.data.last_message;
          if (msg) {
            const v = verbosityRef.current;
            const isStepMsg = msg.startsWith("  [") || msg.startsWith("  ");
            const isBegin = msg.startsWith("Begin: ");
            let allow = true;
            if (v === "quiet") allow = !isStepMsg && !isBegin;
            else if (v === "normal") allow = !isStepMsg;
            if (allow) {
              setLogLines(prev => {
                const last = prev[prev.length - 1];
                if (last && last.text === msg) return prev;
                const next = [...prev, { ts: new Date().toLocaleTimeString(), text: msg }];
                return next.slice(-1000);
              });
            }
          }
        }
      } catch {}
    };
    ws.onclose = () => console.log("WS closed");
    return () => { try { ws.close(); } catch {} };
  }, []);

  useEffect(() => {
    if (progress.target_folder && !folder) setFolder(progress.target_folder);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [progress.target_folder]);

  const refreshFiles = useCallback(async () => {
    try {
      const r = await api("GET", `/api/files?status=${encodeURIComponent(statusFilter)}&search=${encodeURIComponent(search)}&limit=2000`);
      setFiles(r.items);
    } catch (e) { console.error(e); }
  }, [statusFilter, search]);

  useEffect(() => {
    refreshFiles();
    const id = setInterval(refreshFiles, 3000);
    return () => clearInterval(id);
  }, [refreshFiles]);

  // Actions
  const onStart = async () => {
    if (!folder.trim()) { alert("Enter a folder path."); return; }
    try {
      await api("POST", "/api/start", {
        target_folder: folder.trim(),
        default_repository: repository.trim(),
      });
    } catch (e) { alert(e.message); }
  };
  const onBrowse = async () => {
    try {
      const r = await api("POST", "/api/pick-folder");
      if (r.path) setFolder(r.path);
    } catch (e) { alert(e.message); }
  };
  const onPause  = () => api("POST", "/api/pause").catch(e => alert(e.message));
  const onResume = () => api("POST", "/api/resume").catch(e => alert(e.message));
  const onStop   = () => api("POST", "/api/stop").catch(e => alert(e.message));
  const onUpdateRepo = () => api("POST", "/api/repository", { repository: repository.trim() }).catch(e => alert(e.message));
  const onDownload = () => { window.location.href = "/api/registry.xlsx"; };

  const reevaluatePaths = async (paths, withThinking, force = false) => {
    try {
      const r = await api("POST", "/api/reevaluate", {
        relative_paths: paths,
        use_thinking: withThinking,
        force,
      });
      return r;
    } catch (e) { alert(e.message); return null; }
  };
  const onReevaluateBatch = async () => {
    if (selected.size === 0) { alert("Select at least one file."); return; }
    const r = await reevaluatePaths(Array.from(selected), useThinking, false);
    if (r) {
      let msg = `Queued ${r.reset} file(s) for re-evaluation${useThinking ? " (thinking mode)" : ""}.`;
      if (r.skipped_manual) {
        msg += `\n${r.skipped_manual} manually-edited file(s) were SKIPPED. Use the "Force" button to override.`;
      }
      alert(msg);
      setSelected(new Set());
      refreshFiles();
    }
  };
  const onReevaluateBatchForce = async () => {
    if (selected.size === 0) { alert("Select at least one file."); return; }
    if (!confirm("Force re-evaluate WILL discard manual edits on the selected files. Continue?")) return;
    const r = await reevaluatePaths(Array.from(selected), useThinking, true);
    if (r) {
      alert(`Queued ${r.reset} file(s) for re-evaluation (force).`);
      setSelected(new Set());
      refreshFiles();
    }
  };
  const onReevaluateOne = async (path, withThinking) => {
    const r = await reevaluatePaths([path], withThinking, false);
    if (r) {
      if (r.skipped_manual) {
        if (confirm("This file is manually edited and was skipped. Force re-evaluate (discards edits)?")) {
          await reevaluatePaths([path], withThinking, true);
        }
      }
      refreshFiles();
    }
  };

  const onBulkApply = async () => {
    if (selected.size === 0) { alert("Select at least one file."); return; }
    const payload = { relative_paths: Array.from(selected) };
    if (bulkRepo !== "") payload.repository = bulkRepo;
    if (bulkStatus !== "") payload.status = bulkStatus;
    if (payload.repository === undefined && payload.status === undefined) {
      alert("Provide a repository value and/or pick a status.");
      return;
    }
    try {
      const r = await api("POST", "/api/files/bulk-edit", payload);
      alert(`Bulk-edited ${r.updated} file(s).`);
      setBulkRepo("");
      setBulkStatus("");
      setSelected(new Set());
      refreshFiles();
    } catch (e) { alert(e.message); }
  };

  const toggleSel = (rp) => {
    const s = new Set(selected);
    if (s.has(rp)) s.delete(rp); else s.add(rp);
    setSelected(s);
  };
  const toggleSelAll = (checked) => {
    if (!checked) { setSelected(new Set()); return; }
    setSelected(new Set(files.map(f => f.relative_path)));
  };

  const runElapsed = useMemo(() => {
    if (!progress.started_at) return "";
    const t0 = new Date(progress.started_at).getTime();
    return fmtMs(Date.now() - t0);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [progress.started_at, tick]);

  const pct = progress.total > 0 ? Math.round(((progress.done + progress.error) / progress.total) * 100) : 0;
  const running = ["scanning", "running", "paused", "stopping"].includes(progress.state);
  const paused = progress.state === "paused";
  const cfp = progress.current_file_progress;

  return (
    <div className="app">
      <div className="header">
        <h1>📚 docregistrar — local document indexer</h1>
        <div className="row">
          <input
            type="text"
            placeholder="Target folder (e.g. C:/Users/me/Documents/MyDocs)"
            value={folder}
            onChange={e => setFolder(e.target.value)}
            disabled={running && !paused}
            style={{ flex: 2 }}
            title="Absolute path to the folder to scan. Use forward slashes on Windows."
          />
          <button onClick={onBrowse} disabled={running && !paused} title="Open folder picker">
            📂 Browse
          </button>
          <input
            type="text"
            placeholder="Repository (e.g. SharePoint/Team-X)"
            value={repository}
            onChange={e => setRepository(e.target.value)}
            style={{ flex: 1, minWidth: 220 }}
            title="Default Repository value tagged on every file processed from now on. Use 'Apply repo' to update mid-run."
          />
          <button onClick={onUpdateRepo} title="Apply repository to files processed from now on">
            Apply repo
          </button>
          {!running && <button className="primary" onClick={onStart}
                               title="Scan the target folder and start processing pending files">▶ Start</button>}
          {running && !paused && <button onClick={onPause}
                                          title="Pause processing between files (resume later from the same point)">⏸ Pause</button>}
          {paused && <button className="primary" onClick={onResume}
                              title="Resume processing">▶ Resume</button>}
          {running && <button className="danger" onClick={onStop}
                              title="Stop the worker. The currently-processing file (if any) is requeued as pending. Returns to idle within ~1s.">⏹ Stop</button>}
          <button onClick={onDownload} title="Download current registry as Excel">
            ⬇ Download .xlsx
          </button>
        </div>

        <div className="progress-bar"><div style={{ width: `${pct}%` }}></div></div>

        <div className="status-line">
          <span className={`pill state-${progress.state}`}>{progress.state.toUpperCase()}</span>
          <span>📁 {progress.target_folder || "(no folder)"}</span>
          <span>📊 total: <b>{progress.total}</b></span>
          <span>✅ done: <b>{progress.done}</b></span>
          <span>⏳ pending: <b>{progress.pending}</b></span>
          <span>⚙️ processing: <b>{progress.processing}</b></span>
          <span>❌ errors: <b>{progress.error}</b></span>
          <span>📈 {pct}%</span>
          {runElapsed && <span>⏱ elapsed: <b>{runElapsed}</b></span>}
          {cfp && cfp.relative_path && (
            <span>▶ {cfp.relative_path} ({cfp.percent}% / {fmtMs(cfp.elapsed_ms)})</span>
          )}
          {!cfp && progress.current_file && <span>▶ {progress.current_file}</span>}
        </div>
      </div>

      <div className="main"
           style={{ "--right-width": `${rightWidth}px` }}>
        <div style={{ display: "flex", flexDirection: "column", minHeight: 0 }}>
          <div className="toolbar">
            <label>Status:</label>
            <select value={statusFilter} onChange={e => setStatusFilter(e.target.value)}
                    title="Filter the table by file status">
              <option value="">all</option>
              <option value="pending">pending</option>
              <option value="processing">processing</option>
              <option value="done">done</option>
              <option value="error">error</option>
              <option value="skipped">skipped</option>
            </select>
            <input
              type="text"
              placeholder="search filename or path..."
              value={search}
              onChange={e => setSearch(e.target.value)}
              title="Filter the table by a substring of the filename or relative path"
            />
            <button onClick={refreshFiles} title="Reload the file list from the server">↻ Refresh</button>
            <div className="spacer"></div>
            <label>Verbosity:</label>
            <select value={verbosity} onChange={e => setVerbosity(e.target.value)}
                    title="How chatty the activity log on the right should be">
              <option value="quiet">quiet</option>
              <option value="normal">normal</option>
              <option value="verbose">verbose</option>
            </select>
            <label title="When checked, re-evaluations use the model's slower 'thinking' mode for higher quality">
              <input type="checkbox" className="checkbox"
                     checked={useThinking}
                     onChange={e => setUseThinking(e.target.checked)} />
              {" "}thinking mode
            </label>
            <button className="primary" onClick={onReevaluateBatch} disabled={selected.size === 0}
                    title="Re-evaluate the selected files (manually-edited rows are skipped)">
              ↻ Re-evaluate {selected.size > 0 ? `(${selected.size})` : ""}
            </button>
            <button onClick={onReevaluateBatchForce} disabled={selected.size === 0}
                    title="Re-evaluate the selected files, INCLUDING manually-edited ones (their edits will be lost)">
              ↻ Force
            </button>
          </div>

          {/* Bulk-edit toolbar (visible when selection > 0) */}
          {selected.size > 0 && (
            <div className="toolbar" style={{ background: "var(--panel)" }}>
              <span className="muted">Bulk edit ({selected.size}):</span>
              <input
                type="text"
                placeholder="set Repository to..."
                value={bulkRepo}
                onChange={e => setBulkRepo(e.target.value)}
                style={{ minWidth: 200 }}
              />
              <label>set Status:</label>
              <select value={bulkStatus} onChange={e => setBulkStatus(e.target.value)}>
                <option value="">(no change)</option>
                {STATUS_OPTIONS.map(s => <option key={s} value={s}>{s}</option>)}
              </select>
              <button className="primary" onClick={onBulkApply}>Apply</button>
              <div className="spacer"></div>
              <button onClick={() => setSelected(new Set())}>Clear selection</button>
            </div>
          )}

          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th style={{ width: 24 }}>
                    <input type="checkbox" className="checkbox"
                      checked={selected.size > 0 && selected.size === files.length}
                      onChange={e => toggleSelAll(e.target.checked)} />
                  </th>
                  <th>Status</th>
                  <th>Q</th>
                  <th>File</th>
                  <th>Path</th>
                  <th>Repository</th>
                  <th>Type</th>
                  <th>Title</th>
                  <th>Date</th>
                  <th>Conf.</th>
                  <th>Size</th>
                  <th>Pages</th>
                  <th>Edit</th>
                  <th>Error</th>
                </tr>
              </thead>
              <tbody>
                {files.map(f => (
                  <tr key={f.relative_path}
                      className={`${selected.has(f.relative_path) ? "selected" : ""} ${openedPath === f.relative_path ? "opened" : ""}`}
                      onClick={() => setOpenedPath(f.relative_path)}>
                    <td onClick={e => e.stopPropagation()}>
                      <input type="checkbox" className="checkbox"
                        checked={selected.has(f.relative_path)}
                        onChange={() => toggleSel(f.relative_path)} />
                    </td>
                    <td className={`status-cell status-${f.status}`}>{f.status}</td>
                    <td>{f.quality_score !== "" && f.quality_score != null ? Number(f.quality_score).toFixed(2) : ""}</td>
                    <td className="truncate" title={f.file_name}>{f.file_name}</td>
                    <td className="truncate" title={f.relative_path}>{f.relative_path}</td>
                    <td className="truncate" title={f.repository}>{f.repository || ""}</td>
                    <td>{f.document_type || f.extension}</td>
                    <td className="truncate" title={f.title}>{f.title}</td>
                    <td>{f.document_date}</td>
                    <td>{f.confidentiality}</td>
                    <td>{fmtBytes(f.file_size)}</td>
                    <td>{f.page_count ?? ""}</td>
                    <td>{f.manually_edited ? "✎" : ""}</td>
                    <td className="error-text truncate" title={f.error}>{f.error}</td>
                  </tr>
                ))}
                {files.length === 0 && (
                  <tr><td colSpan={14} style={{ padding: 20, textAlign: "center", color: "var(--muted)" }}>
                    No files. Click Start with a folder path to scan.
                  </td></tr>
                )}
              </tbody>
            </table>
          </div>
        </div>

        <div className={`splitter ${dragging ? "dragging" : ""}`}
             title="Drag to resize the side panel"
             onMouseDown={() => setDragging(true)} />

        {openedPath ? (
          <FileDetailPanel
            relativePath={openedPath}
            currentProgress={cfp}
            onClose={() => setOpenedPath("")}
            onReevaluate={onReevaluateOne}
            onEdited={refreshFiles}
          />
        ) : (
          <div className="right-panel">
            <h2>Activity log</h2>
            <div className="log">
              {logLines.map((l, i) => (
                <div className="line" key={i}>
                  <span className="ts">{l.ts}</span>{l.text}
                </div>
              ))}
              {logLines.length === 0 && <div className="line">(no events yet)</div>}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

ReactDOM.createRoot(document.getElementById("root")).render(<App />);
