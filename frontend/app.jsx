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
  if (ms < 60000) return `${(ms/1000).toFixed(1)} s`;
  const m = Math.floor(ms/60000), s = Math.floor((ms%60000)/1000);
  return `${m}m ${s}s`;
};
const fmtArr = (a) => Array.isArray(a) ? a.join("; ") : (a || "");

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

function FileDetailPanel({ relativePath, currentProgress, onClose, onReevaluate }) {
  const [rec, setRec] = useState(null);
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(false);

  const isCurrent = currentProgress && currentProgress.relative_path === relativePath;

  const load = useCallback(async () => {
    if (!relativePath) return;
    setLoading(true); setErr("");
    try {
      const r = await api("GET", `/api/file?relative_path=${encodeURIComponent(relativePath)}`);
      setRec(r);
    } catch (e) { setErr(e.message); setRec(null); }
    finally { setLoading(false); }
  }, [relativePath]);

  useEffect(() => { load(); }, [load]);

  // Auto-refresh while file is being processed
  useEffect(() => {
    if (!isCurrent) return;
    const id = setInterval(load, 1500);
    return () => clearInterval(id);
  }, [isCurrent, load]);

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
          <button onClick={() => onReevaluate(relativePath, false)}>↻ Re-evaluate</button>
          <button onClick={() => onReevaluate(relativePath, true)} title="Re-run with thinking mode">↻ + 🧠</button>
          <button onClick={load}>🔄</button>
          <button onClick={onClose}>✕</button>
        </div>
      </div>

      {loading && !rec && <div className="detail-body muted">Loading...</div>}
      {err && <div className="detail-body error-text">Error: {err}</div>}

      {rec && (
        <div className="detail-body">
          {/* Live processing block */}
          {isCurrent && currentProgress && (
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

          {/* File-system / computed */}
          <div className="card">
            <div className="card-title">File</div>
            <PropRow label="Status" value={rec.status} />
            <PropRow label="Error" value={rec.error} />
            <PropRow label="Size" value={fmtBytes(rec.file_size)} />
            <PropRow label="Pages / slides" value={rec.page_count} />
            <PropRow label="Extension" value={rec.extension} />
            <PropRow label="SHA-256" value={rec.sha256} />
            <PropRow label="OS created" value={rec.os_created} />
            <PropRow label="OS modified" value={rec.os_modified} />
            <PropRow label="Indexed at" value={rec.indexed_at} />
            <PropRow label="Used thinking mode" value={rec.used_thinking ? "Yes" : "No"} />
          </div>

          {/* LLM-extracted */}
          <div className="card">
            <div className="card-title">Extracted properties</div>
            <PropRow label="Title" value={e.title} />
            <PropRow label="Document type" value={e.document_type} />
            <PropRow label="Document date" value={e.document_date} />
            <PropRow label="Last update date" value={e.last_update_date} />
            <PropRow label="Language" value={e.language} />
            <PropRow label="Version" value={e.version} />
            <PropRow label="Confidentiality" value={e.confidentiality} />
            <PropRow label="Authors" value={e.authors} />
            <PropRow label="Geographic scope" value={e.geographic_scope} />
            <PropRow label="Industry domain" value={e.industry_domain} />
            <PropRow label="Quality score" value={e.quality_score != null ? Number(e.quality_score).toFixed(2) : ""} />
          </div>

          <div className="card">
            <div className="card-title">Summary</div>
            <div className="summary-text">{e.summary || <span className="muted">—</span>}</div>
          </div>

          <div className="card">
            <div className="card-title">Named entities</div>
            <PropRow label="Persons" value={ne.persons} />
            <PropRow label="Organizations" value={ne.organizations} />
            <PropRow label="Locations" value={ne.locations} />
            <PropRow label="Mentioned dates" value={ne.dates} />
            <PropRow label="Products / technologies" value={ne.products_technologies} />
          </div>

          <div className="card">
            <div className="card-title">Topics</div>
            <PropRow label="Key concepts" value={e.key_concepts} />
            <PropRow label="Key phrases (top 10)" value={e.key_phrases} />
            <PropRow label="Tags" value={e.tags} />
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
  const [files, setFiles] = useState([]);
  const [statusFilter, setStatusFilter] = useState("");
  const [search, setSearch] = useState("");
  const [selected, setSelected] = useState(new Set());
  const [logLines, setLogLines] = useState([]);
  const [useThinking, setUseThinking] = useState(false);
  const [verbosity, setVerbosity] = useState("normal"); // quiet | normal | verbose
  const [openedPath, setOpenedPath] = useState("");
  const [tick, setTick] = useState(0);  // for elapsed-time ticker
  const wsRef = useRef(null);

  // Tick every second so elapsed times update live
  useEffect(() => {
    const id = setInterval(() => setTick(t => t + 1), 1000);
    return () => clearInterval(id);
  }, []);

  // Load initial config + start WS
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
            // Filter messages based on verbosity
            const isStepMsg = msg.startsWith("  [") || msg.startsWith("  ");
            const isBegin = msg.startsWith("Begin: ");
            const isDone = msg.startsWith("Done: ");
            let allow = true;
            if (verbosity === "quiet") {
              allow = !isStepMsg && !isBegin;
            } else if (verbosity === "normal") {
              allow = !isStepMsg;  // hide nested step lines but keep Begin/Done/Errors
            }  // verbose: allow all
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
      } catch (e) { /* ignore */ }
    };
    ws.onclose = () => console.log("WS closed");
    return () => { try { ws.close(); } catch {} };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [verbosity]);

  // Sync folder field when progress.target_folder changes (after Start)
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
    try { await api("POST", "/api/start", { target_folder: folder.trim() }); }
    catch (e) { alert(e.message); }
  };
  const onBrowse = async () => {
    try {
      const r = await api("POST", "/api/pick-folder");
      if (r.path) setFolder(r.path);
    } catch (e) { alert(e.message); }
  };
  const onPause = () => api("POST", "/api/pause").catch(e => alert(e.message));
  const onResume = () => api("POST", "/api/resume").catch(e => alert(e.message));
  const onStop = () => api("POST", "/api/stop").catch(e => alert(e.message));

  const reevaluatePaths = async (paths, withThinking) => {
    try {
      const r = await api("POST", "/api/reevaluate", {
        relative_paths: paths,
        use_thinking: withThinking,
      });
      return r.reset;
    } catch (e) { alert(e.message); return 0; }
  };
  const onReevaluateBatch = async () => {
    if (selected.size === 0) { alert("Select at least one file."); return; }
    const n = await reevaluatePaths(Array.from(selected), useThinking);
    alert(`Queued ${n} file(s) for re-evaluation${useThinking ? " (thinking mode)" : ""}.`);
    setSelected(new Set());
    refreshFiles();
  };
  const onReevaluateOne = async (path, withThinking) => {
    const n = await reevaluatePaths([path], withThinking);
    if (n) refreshFiles();
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

  // Total elapsed time of the whole run
  const runElapsed = useMemo(() => {
    if (!progress.started_at) return "";
    const t0 = new Date(progress.started_at).getTime();
    const ms = Date.now() - t0;
    return fmtMs(ms);
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
          />
          <button onClick={onBrowse} disabled={running && !paused} title="Open folder picker">
            📂 Browse
          </button>
          {!running && <button className="primary" onClick={onStart}>▶ Start</button>}
          {running && !paused && <button onClick={onPause}>⏸ Pause</button>}
          {paused && <button className="primary" onClick={onResume}>▶ Resume</button>}
          {running && <button className="danger" onClick={onStop}>⏹ Stop</button>}
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

      <div className={`main ${openedPath ? "with-detail" : ""}`}>
        <div style={{ display: "flex", flexDirection: "column", minHeight: 0 }}>
          <div className="toolbar">
            <label>Status:</label>
            <select value={statusFilter} onChange={e => setStatusFilter(e.target.value)}>
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
            />
            <button onClick={refreshFiles}>↻ Refresh</button>
            <div className="spacer"></div>
            <label>Verbosity:</label>
            <select value={verbosity} onChange={e => setVerbosity(e.target.value)}>
              <option value="quiet">quiet</option>
              <option value="normal">normal</option>
              <option value="verbose">verbose</option>
            </select>
            <label><input type="checkbox" className="checkbox"
                          checked={useThinking}
                          onChange={e => setUseThinking(e.target.checked)} />
              {" "}thinking mode</label>
            <button className="primary" onClick={onReevaluateBatch} disabled={selected.size === 0}>
              ↻ Re-evaluate {selected.size > 0 ? `(${selected.size})` : ""}
            </button>
          </div>

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
                  <th>Type</th>
                  <th>Title</th>
                  <th>Date</th>
                  <th>Conf.</th>
                  <th>Size</th>
                  <th>Pages</th>
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
                    <td>{f.document_type || f.extension}</td>
                    <td className="truncate" title={f.title}>{f.title}</td>
                    <td>{f.document_date}</td>
                    <td>{f.confidentiality}</td>
                    <td>{fmtBytes(f.file_size)}</td>
                    <td>{f.page_count ?? ""}</td>
                    <td className="error-text truncate" title={f.error}>{f.error}</td>
                  </tr>
                ))}
                {files.length === 0 && (
                  <tr><td colSpan={12} style={{ padding: 20, textAlign: "center", color: "var(--muted)" }}>
                    No files. Click Start with a folder path to scan.
                  </td></tr>
                )}
              </tbody>
            </table>
          </div>
        </div>

        {openedPath ? (
          <FileDetailPanel
            relativePath={openedPath}
            currentProgress={cfp}
            onClose={() => setOpenedPath("")}
            onReevaluate={onReevaluateOne}
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
