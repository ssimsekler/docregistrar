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
  ["source_url_1", "Source URL 1"],
  ["source_url_2", "Source URL 2"],
  ["source_url_3", "Source URL 3"],
];
const DESCRIPTION_MAX = 250;
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
const MAX_CUSTOM_PROPERTIES = 50;

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

// ---------- Repository management dialog ----------
// Full master-data CRUD UI: list, edit (path/description), rename, delete,
// and create new repositories. Picking a row (clicking the row body but
// NOT a button or input) selects that repository for the header.
function RepositoryManagerDialog({ open, onPick, onClose, currentSelection, onChanged }) {
  const [items, setItems] = useState([]);
  const [filter, setFilter] = useState("");
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");
  // Edit drafts keyed by repo name
  const [edits, setEdits] = useState({});  // { name: { path, description } }
  // New-repo form
  const [newName, setNewName] = useState("");
  const [newPath, setNewPath] = useState("");
  const [newDesc, setNewDesc] = useState("");

  const refresh = useCallback(async () => {
    setLoading(true); setErr("");
    try {
      const r = await api("GET", "/api/repositories");
      setItems(Array.isArray(r.items) ? r.items : []);
      setEdits({});
    } catch (e) { setErr(e.message); setItems([]); }
    finally { setLoading(false); }
  }, []);

  useEffect(() => {
    if (!open) return;
    refresh();
    setFilter(""); setNewName(""); setNewPath(""); setNewDesc("");
  }, [open, refresh]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;

  const f = filter.trim().toLowerCase();
  const filtered = !f ? items
    : items.filter(it => (it.name || "").toLowerCase().includes(f)
                      || (it.path || "").toLowerCase().includes(f)
                      || (it.description || "").toLowerCase().includes(f));

  const startEdit = (rec) => {
    setEdits(prev => ({
      ...prev,
      [rec.name]: { path: rec.path || "", description: rec.description || "" },
    }));
  };
  const cancelEdit = (name) => {
    setEdits(prev => {
      const c = { ...prev }; delete c[name]; return c;
    });
  };
  const updateEditField = (name, key, value) => {
    setEdits(prev => ({
      ...prev,
      [name]: { ...(prev[name] || {}), [key]: value },
    }));
  };
  const saveEdit = async (rec) => {
    const e = edits[rec.name];
    if (!e) return;
    if (!e.path || !e.path.trim()) {
      alert("Path is required.");
      return;
    }
    try {
      await api("PATCH", `/api/repositories/${encodeURIComponent(rec.name)}`, {
        path: e.path,
        description: e.description ?? "",
      });
      await refresh();
      onChanged && onChanged();
    } catch (err) { alert(err.message); }
  };
  const renameRepo = async (rec) => {
    const next = prompt(`Rename repository "${rec.name}" to:`, rec.name);
    if (!next || next === rec.name) return;
    try {
      await api("POST", `/api/repositories/${encodeURIComponent(rec.name)}/rename`, {
        new_name: next,
      });
      await refresh();
      onChanged && onChanged(rec.name, next);
    } catch (err) { alert(err.message); }
  };
  const deleteRepo = async (rec) => {
    const msg = rec.file_count > 0
      ? `Delete repository "${rec.name}"?\n\n` +
        `${rec.file_count} file(s) currently reference this repository. ` +
        `Their Repository field will be cleared (files themselves will not be removed).\n\n` +
        `Proceed?`
      : `Delete repository "${rec.name}"?`;
    if (!confirm(msg)) return;
    try {
      await api("DELETE", `/api/repositories/${encodeURIComponent(rec.name)}`);
      await refresh();
      onChanged && onChanged(rec.name, null);
    } catch (err) { alert(err.message); }
  };
  const createRepo = async () => {
    if (!newName.trim() || !newPath.trim()) {
      alert("Name and Path are required.");
      return;
    }
    try {
      await api("POST", "/api/repositories", {
        name: newName.trim(),
        path: newPath.trim(),
        description: newDesc.trim(),
      });
      setNewName(""); setNewPath(""); setNewDesc("");
      await refresh();
      onChanged && onChanged();
    } catch (err) { alert(err.message); }
  };
  const pickFolder = async (cb) => {
    try {
      const r = await api("POST", "/api/pick-folder");
      if (r.path) cb(r.path);
    } catch (err) { alert(err.message); }
  };

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal modal-wide" onClick={e => e.stopPropagation()}>
        <div className="modal-head">
          <h3>📁 Repositories (master data)</h3>
          <button className="modal-close" onClick={onClose} title="Close (Esc)">✕</button>
        </div>
        <div className="modal-body">
          <input
            className="modal-search"
            type="text"
            placeholder="Filter by name, path or description…"
            value={filter}
            onChange={e => setFilter(e.target.value)}
            autoFocus
          />

          {/* Add new repository */}
          <div className="repo-add-row">
            <input
              type="text" placeholder="New repository name"
              value={newName} onChange={e => setNewName(e.target.value)}
              title="Unique name for the new repository (required)"
            />
            <input
              type="text" placeholder="Folder path (required)"
              value={newPath} onChange={e => setNewPath(e.target.value)}
              title="Absolute folder path for this repository (required)"
            />
            <button onClick={() => pickFolder(setNewPath)} title="Browse for folder">📂</button>
            <input
              type="text" placeholder="Description (optional)"
              value={newDesc} onChange={e => setNewDesc(e.target.value)}
              style={{ minWidth: 160 }}
              title="Free-form description"
            />
            <button className="primary" onClick={createRepo}
                    title="Create the new repository">➕ Add</button>
          </div>

          {loading && <div className="repo-empty">Loading…</div>}
          {err && <div className="error-text">Error: {err}</div>}

          {!loading && !err && (
            <div style={{ overflow: "auto", border: "1px solid var(--border)", borderRadius: 6 }}>
              <table className="repo-table">
                <thead>
                  <tr>
                    <th>Name</th>
                    <th>Path</th>
                    <th>Description</th>
                    <th title="Number of files referencing this repository">Files</th>
                    <th>Created</th>
                    <th></th>
                  </tr>
                </thead>
                <tbody>
                  {filtered.map(rec => {
                    const editing = !!edits[rec.name];
                    const e = edits[rec.name] || {};
                    const selected = currentSelection === rec.name;
                    return (
                      <tr key={rec.name} className={selected ? "selected-repo" : ""}>
                        <td>
                          <div style={{ fontWeight: 600 }}>{rec.name}</div>
                          {selected && <div className="muted" style={{ fontSize: 11 }}>(selected)</div>}
                        </td>
                        <td style={{ minWidth: 200 }}>
                          {editing ? (
                            <div style={{ display: "flex", gap: 4 }}>
                              <input type="text" value={e.path || ""}
                                     onChange={ev => updateEditField(rec.name, "path", ev.target.value)}
                                     placeholder="Path (required)" />
                              <button onClick={() => pickFolder(p => updateEditField(rec.name, "path", p))}
                                      title="Browse">📂</button>
                            </div>
                          ) : (
                            rec.path
                              ? <span style={{ fontFamily: "ui-monospace,Menlo,Consolas,monospace", fontSize: 11 }}>{rec.path}</span>
                              : <span className="repo-pill-empty">no path set — click ✎ to add one</span>
                          )}
                        </td>
                        <td style={{ minWidth: 160 }}>
                          {editing ? (
                            <input type="text" value={e.description || ""}
                                   onChange={ev => updateEditField(rec.name, "description", ev.target.value)}
                                   placeholder="Description" />
                          ) : (
                            rec.description || <span className="muted">—</span>
                          )}
                        </td>
                        <td>{rec.file_count}</td>
                        <td><span className="muted" style={{ fontSize: 11 }}>{rec.created_at}</span></td>
                        <td>
                          <div className="repo-actions">
                            {!editing && (
                              <>
                                <button onClick={() => onPick && onPick(rec.name)}
                                        title="Select this repository for the header (will be used by Start)">
                                  ✓ Select
                                </button>
                                <button onClick={() => startEdit(rec)} title="Edit path/description">✎</button>
                                <button onClick={() => renameRepo(rec)} title="Rename this repository">📝</button>
                                <button className="danger" onClick={() => deleteRepo(rec)}
                                        title="Delete this repository (referencing files will have their Repository cleared)">🗑</button>
                              </>
                            )}
                            {editing && (
                              <>
                                <button className="primary" onClick={() => saveEdit(rec)}
                                        title="Save changes">💾</button>
                                <button onClick={() => cancelEdit(rec.name)} title="Cancel">✕</button>
                              </>
                            )}
                          </div>
                        </td>
                      </tr>
                    );
                  })}
                  {filtered.length === 0 && (
                    <tr><td colSpan={6} className="repo-empty">
                      {items.length === 0
                        ? "No repositories yet. Use the form above to create one."
                        : "No matches for that filter."}
                    </td></tr>
                  )}
                </tbody>
              </table>
            </div>
          )}
        </div>
        <div className="modal-foot">
          <span className="muted" style={{ marginRight: "auto", fontSize: 12 }}>
            Tip: a repository's <b>Path</b> is required. It is used both to scan files when you press Start, and to open files later.
          </span>
          <button onClick={onClose} title="Close">Close</button>
        </div>
      </div>
    </div>
  );
}

function FileDetailPanel({ relativePath, currentProgress, onClose, onReevaluate, onEdited, onOpenSibling, onRemoved }) {
  const [rec, setRec] = useState(null);
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(false);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState({});
  const [kv, setKv] = useState([]);
  const [kvSavingHint, setKvSavingHint] = useState("");
  const [siblings, setSiblings] = useState([]);

  const isCurrent = currentProgress && currentProgress.relative_path === relativePath;

  const load = useCallback(async () => {
    if (!relativePath) return;
    setLoading(true); setErr("");
    try {
      const r = await api("GET", `/api/file?relative_path=${encodeURIComponent(relativePath)}`);
      setRec(r);
      const e = r.extraction || {};
      const ne = e.named_entities || {};
      setDraft({
        title: e.title || "",
        description: e.description || "",
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
        source_url_1: e.source_url_1 || "",
        source_url_2: e.source_url_2 || "",
        source_url_3: e.source_url_3 || "",
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
      const cp = Array.isArray(e.custom_properties) ? e.custom_properties : [];
      setKv(cp.map(p => ({ key: p.key || "", value: p.value || "" })));
      setKvSavingHint("");
    } catch (e) { setErr(e.message); setRec(null); }
    finally { setLoading(false); }
  }, [relativePath]);

  const addKv = () => setKv(prev => prev.length >= MAX_CUSTOM_PROPERTIES ? prev : [...prev, { key: "", value: "" }]);
  const updateKv = (i, field, val) => setKv(prev => prev.map((p, idx) => idx === i ? { ...p, [field]: val } : p));
  const removeKv = (i) => setKv(prev => prev.filter((_, idx) => idx !== i));
  const saveKv = async () => {
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

  useEffect(() => {
    let aborted = false;
    if (!rec || !rec.is_duplicate) {
      setSiblings([]);
      return;
    }
    api("GET", `/api/file/dup-siblings?relative_path=${encodeURIComponent(relativePath)}`)
      .then(r => { if (!aborted) setSiblings(Array.isArray(r.siblings) ? r.siblings : []); })
      .catch(() => { if (!aborted) setSiblings([]); });
    return () => { aborted = true; };
  }, [rec, relativePath]);

  useEffect(() => {
    if (!isCurrent || editing) return;
    const id = setInterval(load, 1500);
    return () => clearInterval(id);
  }, [isCurrent, editing, load]);

  const setField = (k, v) => setDraft(prev => ({ ...prev, [k]: v }));

  const saveEdits = async () => {
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

    if (cmp(e.description ?? "", draft.description)) {
      const d = (draft.description || "").slice(0, DESCRIPTION_MAX);
      payload.description = d;
    }
    if (cmp(e.summary ?? "", draft.summary)) payload.summary = draft.summary;
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
          {!editing && <button onClick={() => setEditing(true)} title="Edit fields">✎ Edit</button>}
          {editing && <button className="primary" onClick={saveEdits} title="Save edits">💾 Save</button>}
          {editing && <button onClick={() => { setEditing(false); load(); }} title="Cancel">Cancel</button>}
          {!editing && <button
              onClick={async () => {
                try {
                  await api("POST", "/api/open-file-location", { relative_path: relativePath });
                } catch (err) {
                  alert(`Could not open file location:\n${err.message}`);
                }
              }}
              title="Open the OS file explorer at this file's folder">
            📁 Open location
          </button>}
          {!editing && <button onClick={() => onReevaluate(relativePath, false)} title="Re-evaluate">↻ Re-eval</button>}
          {!editing && <button onClick={() => onReevaluate(relativePath, true)} title="Re-evaluate with thinking">↻ + 🧠</button>}
          {!editing && <button className="danger"
              onClick={async () => {
                if (!confirm(
                  `Remove this file from the registry?\n\n${relativePath}\n\n` +
                  `The file on disk is NOT deleted.`
                )) return;
                try {
                  await api("POST", "/api/files/delete", { relative_paths: [relativePath] });
                  onRemoved && onRemoved(relativePath);
                } catch (err) {
                  alert(`Could not remove from registry:\n${err.message}`);
                }
              }}
              title="Remove from registry">
            🗑 Remove
          </button>}
          <button onClick={load} title="Reload">🔄</button>
          <button onClick={onClose} title="Close">✕</button>
        </div>
      </div>

      {loading && !rec && <div className="detail-body muted">Loading...</div>}
      {err && <div className="detail-body error-text">Error: {err}</div>}

      {rec && (
        <div className="detail-body">
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
            <PropRow label="ID" value={rec.id} />
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
            <PropRow label="Repository path" value={rec.repository_path} />
            <PropRow label="Relative path" value={rec.relative_folder_path} />
            <PropRow label="File name" value={rec.file_name} />
            <PropRow label="Size" value={fmtBytes(rec.file_size)} />
            <PropRow label="Pages / slides" value={rec.page_count} />
            <PropRow label="Extension" value={rec.extension} />
            <PropRow label="SHA-256" value={rec.sha256} />
            <PropRow label="OS created" value={rec.os_created} />
            <PropRow label="OS modified" value={rec.os_modified} />
            <PropRow label="Indexed at" value={rec.indexed_at} />
            <PropRow label="Indexing started" value={rec.indexing_started_at} />
            <PropRow label="Indexing completed" value={rec.indexing_completed_at} />
            <PropRow label="Used thinking" value={rec.used_thinking ? "Yes" : "No"} />
            <PropRow label="Manually edited" value={rec.manually_edited ? "Yes" : "No"} />
            <PropRow label="Is duplicate" value={rec.is_duplicate ? "Yes" : "No"} />
          </div>

          {rec.is_duplicate && (
            <div className="card">
              <div className="card-title">
                🟰 Duplicates ({siblings.length} other file{siblings.length === 1 ? "" : "s"} with the same SHA-256)
              </div>
              {siblings.length === 0 && (
                <div className="muted" style={{ fontSize: "12px" }}>Loading siblings…</div>
              )}
              <div className="dup-sibs">
                {siblings.slice(0, 20).map(s => (
                  <div className="dup-sib-row" key={s.relative_path}
                       onClick={() => onOpenSibling && onOpenSibling(s.relative_path)}
                       title={`Open: ${s.relative_path}`}>
                    <span className={`dup-status status-${s.status}`}>{s.status}</span>
                    <span className="truncate" style={{ flex: 1 }}>{s.relative_path}</span>
                    <span className="muted">{fmtBytes(s.file_size)}</span>
                  </div>
                ))}
                {siblings.length > 20 && (
                  <div className="muted" style={{ fontSize: "11px", padding: "4px 6px" }}>
                    … and {siblings.length - 20} more
                  </div>
                )}
              </div>
            </div>
          )}

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
            <div className="card-title">Description {editing && `(editable, max ${DESCRIPTION_MAX} chars)`}</div>
            {editing ? (
              <div className="prop-row">
                <div className="prop-label">Description</div>
                <div className="prop-value">
                  <textarea
                    value={draft.description ?? ""}
                    onChange={ev => setField("description", ev.target.value.slice(0, DESCRIPTION_MAX))}
                    rows={3}
                    style={{ width: "100%" }}
                  />
                  <div className="muted" style={{ fontSize: "11px", textAlign: "right" }}>
                    {(draft.description || "").length} / {DESCRIPTION_MAX}
                  </div>
                </div>
              </div>
            ) : (
              <div className="summary-text">{e.description || <span className="muted">—</span>}</div>
            )}
          </div>

          <div className="card">
            <div className="card-title">Summary {editing && "(editable)"}</div>
            {editing
              ? <EditField label="" value={draft.summary} onChange={v => setField("summary", v)} multiline />
              : <div className="summary-text">{e.summary || <span className="muted">—</span>}</div>
            }
          </div>

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

          <div className="card">
            <div className="card-title">
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
                  <input className="kv-key" type="text" placeholder="key"
                    value={p.key}
                    onChange={ev => updateKv(i, "key", ev.target.value)}
                  />
                  <input type="text" placeholder="value"
                    value={p.value}
                    onChange={ev => updateKv(i, "value", ev.target.value)}
                  />
                  <button className="kv-remove" onClick={() => removeKv(i)} title="Remove">🗑</button>
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
              <button className="primary" onClick={saveKv} title="Save custom properties">💾 Save custom</button>
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

// ---------- Settings dialog ----------
// Modal that lets users edit runtime settings (LLM, processing, scanner, …).
// Calls GET/PUT/DELETE /api/settings; values immediately picked up by the
// worker on its next loop iteration.
const SETTINGS_GROUPS = [
  {
    name: "LLM",
    keys: [
      "llm.base_url",
      "llm.api_key",
      "llm.model",
      "llm.request_timeout_seconds",
      "llm.temperature",
      "llm.top_p",
      "llm.top_k",
      "llm.presence_penalty",
      "llm.thinking_default",
      "llm.thinking_on_low_quality",
      "llm.low_quality_threshold",
      "llm.max_output_tokens",
    ],
  },
  {
    name: "Processing",
    keys: ["processing.max_error_retries", "excel_write_every_n_files"],
  },
  {
    name: "Text extraction",
    keys: [
      "extract.head_chars",
      "extract.middle_chars",
      "extract.tail_chars",
      "extract.max_file_size_bytes",
    ],
  },
  {
    name: "Scanner",
    keys: ["include_extensions", "ignore_dir_names"],
  },
  {
    name: "Storage",
    keys: ["registry_xlsx"],
  },
  {
    name: "Server (restart required)",
    keys: ["server.host", "server.port"],
  },
];

const SETTINGS_HELP = {
  "llm.base_url": "OpenAI-compatible LLM endpoint URL (e.g. LM Studio).",
  "llm.api_key": "Bearer token. LM Studio ignores the value but the field is required.",
  "llm.model": "Exact model identifier loaded in your LLM server.",
  "llm.request_timeout_seconds":
    "HTTP timeout (seconds) for the entire LLM request. Set by this app, not by LM Studio.",
  "llm.temperature": "Sampling temperature (0–2). Lower = more deterministic.",
  "llm.top_p": "Nucleus sampling threshold (0–1).",
  "llm.top_k": "Top-K sampling cutoff (defined but not currently sent in requests).",
  "llm.presence_penalty":
    "Defined but not currently sent (some LLM backends reject it).",
  "llm.thinking_default":
    "If on, the first-pass LLM call uses thinking mode (slower, smarter).",
  "llm.thinking_on_low_quality":
    "If on, automatically retry with thinking ON when quality_score is below the threshold.",
  "llm.low_quality_threshold": "Quality threshold (0–1) that triggers a thinking-mode rerun.",
  "llm.max_output_tokens": "Max tokens the LLM may produce per call.",
  "processing.max_error_retries":
    "After this many consecutive failures, the file is no longer auto-retried. Set its status to 'pending' or Re-evaluate to clear the counter.",
  "excel_write_every_n_files":
    "Refresh the on-disk registry.xlsx after this many files complete (and at end of run).",
  "extract.head_chars": "Characters from the start of the document sent to the LLM.",
  "extract.middle_chars": "Characters sampled from the document's middle.",
  "extract.tail_chars": "Characters from the end of the document sent to the LLM.",
  "extract.max_file_size_bytes":
    "Skip files bigger than this many bytes during scan. 0 = no limit.",
  "include_extensions":
    "List of file extensions (one per line) the scanner considers. Each entry should start with a dot.",
  "ignore_dir_names":
    "Folders to skip during scan (case-insensitive, one per line).",
  "registry_xlsx":
    "Where to write the registry Excel file. Empty = <repository path>/registry.xlsx.",
  "server.host": "Bind address. Editable in config.yaml only; restart required.",
  "server.port": "TCP port. Editable in config.yaml only; restart required.",
};

function _settingType(key, value) {
  if (Array.isArray(value)) return "list";
  if (typeof value === "boolean") return "bool";
  if (typeof value === "number") return "number";
  return "string";
}

function SettingsDialog({ open, onClose }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");
  // Local drafts keyed by setting key (only for fields the user has touched).
  const [drafts, setDrafts] = useState({});
  const [savingKey, setSavingKey] = useState("");

  const refresh = useCallback(async () => {
    setLoading(true); setErr("");
    try {
      const r = await api("GET", "/api/settings");
      setData(r);
      setDrafts({});
    } catch (e) { setErr(e.message); }
    finally { setLoading(false); }
  }, []);

  useEffect(() => { if (open) refresh(); }, [open, refresh]);
  useEffect(() => {
    if (!open) return;
    const onKey = (e) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;

  const isOverridden = (key) => data?.overridden_keys?.includes(key);
  const isRestartRequired = (key) => data?.restart_required_keys?.includes(key);
  const currentVal = (key) => data?.current?.[key];
  const defaultVal = (key) => data?.defaults?.[key];

  const draftValueOrCurrent = (key) => {
    if (key in drafts) return drafts[key];
    return currentVal(key);
  };

  const setDraft = (key, val) => {
    setDrafts(prev => ({ ...prev, [key]: val }));
  };

  const coerce = (key, raw) => {
    const t = _settingType(key, currentVal(key));
    if (t === "bool") return !!raw;
    if (t === "number") {
      const n = Number(raw);
      if (Number.isNaN(n)) throw new Error("Not a valid number");
      return n;
    }
    if (t === "list") {
      if (Array.isArray(raw)) return raw;
      // Multi-line input → trimmed non-empty entries.
      return String(raw).split(/\r?\n/).map(s => s.trim()).filter(Boolean);
    }
    return String(raw ?? "");
  };

  const saveSetting = async (key) => {
    if (isRestartRequired(key)) {
      alert("This setting can only be changed by editing config.yaml and restarting the application.");
      return;
    }
    setSavingKey(key); setErr("");
    try {
      const value = coerce(key, draftValueOrCurrent(key));
      const r = await api("PUT", "/api/settings", { key, value });
      setData(r);
      setDrafts(prev => { const c = { ...prev }; delete c[key]; return c; });
    } catch (e) { setErr(`${key}: ${e.message}`); }
    finally { setSavingKey(""); }
  };

  const resetSetting = async (key) => {
    if (isRestartRequired(key)) return;
    setSavingKey(key); setErr("");
    try {
      const r = await api("DELETE", `/api/settings/${encodeURIComponent(key)}`);
      setData(r);
      setDrafts(prev => { const c = { ...prev }; delete c[key]; return c; });
    } catch (e) { setErr(`${key}: ${e.message}`); }
    finally { setSavingKey(""); }
  };

  const resetAll = async () => {
    if (!confirm("Reset ALL settings to their defaults? This drops every override stored in the database.")) return;
    setLoading(true); setErr("");
    try {
      const r = await api("POST", "/api/settings/reset-all");
      setData(r);
      setDrafts({});
    } catch (e) { setErr(e.message); }
    finally { setLoading(false); }
  };

  const renderInput = (key) => {
    const cur = currentVal(key);
    const draft = draftValueOrCurrent(key);
    const t = _settingType(key, cur);
    const disabled = isRestartRequired(key);
    if (t === "bool") {
      return (
        <input type="checkbox" className="checkbox"
          disabled={disabled}
          checked={!!draft}
          onChange={e => setDraft(key, e.target.checked)} />
      );
    }
    if (t === "list") {
      const text = Array.isArray(draft) ? draft.join("\n") : String(draft || "");
      return (
        <textarea rows={6} disabled={disabled}
          style={{ width: "100%", fontFamily: "ui-monospace,Menlo,Consolas,monospace", fontSize: 12 }}
          value={text}
          onChange={e => setDraft(key, e.target.value)} />
      );
    }
    return (
      <input type={t === "number" ? "number" : "text"} disabled={disabled}
        value={draft ?? ""}
        onChange={e => setDraft(key, e.target.value)}
        style={{ width: "100%" }} />
    );
  };

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal modal-wide" onClick={e => e.stopPropagation()}>
        <div className="modal-head">
          <h3>⚙️ Settings</h3>
          <button className="modal-close" onClick={onClose} title="Close (Esc)">✕</button>
        </div>
        <div className="modal-body">
          {loading && !data && <div className="repo-empty">Loading…</div>}
          {err && <div className="error-text" style={{ marginBottom: 8 }}>Error: {err}</div>}
          {data && (
            <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
              {SETTINGS_GROUPS.map(group => (
                <div key={group.name} className="card">
                  <div className="card-title">{group.name}</div>
                  {group.keys.map(key => {
                    const overridden = isOverridden(key);
                    const restart = isRestartRequired(key);
                    const dirty = key in drafts;
                    return (
                      <div className="prop-row" key={key} style={{ alignItems: "flex-start" }}>
                        <div className="prop-label" style={{ minWidth: 220 }}>
                          <div style={{ fontFamily: "ui-monospace,Menlo,Consolas,monospace", fontSize: 12 }}>
                            {key}
                            {overridden && <span className="muted" style={{ marginLeft: 6, fontSize: 11 }}>(modified)</span>}
                            {restart && <span className="muted" style={{ marginLeft: 6, fontSize: 11 }}>(restart required)</span>}
                          </div>
                          {SETTINGS_HELP[key] && (
                            <div className="muted" style={{ fontSize: 11, marginTop: 2 }}>{SETTINGS_HELP[key]}</div>
                          )}
                        </div>
                        <div className="prop-value" style={{ flex: 1 }}>
                          {renderInput(key)}
                          <div style={{ display: "flex", gap: 6, marginTop: 4, alignItems: "center" }}>
                            {!restart && (
                              <button className="primary"
                                      disabled={!dirty || savingKey === key}
                                      onClick={() => saveSetting(key)}
                                      title="Save this setting">
                                💾 Save
                              </button>
                            )}
                            {overridden && !restart && (
                              <button onClick={() => resetSetting(key)}
                                      disabled={savingKey === key}
                                      title={`Reset to default (${JSON.stringify(defaultVal(key))})`}>
                                🔄 Reset
                              </button>
                            )}
                            {dirty && (
                              <span className="muted" style={{ fontSize: 11 }}>unsaved</span>
                            )}
                          </div>
                        </div>
                      </div>
                    );
                  })}
                </div>
              ))}
            </div>
          )}
        </div>
        <div className="modal-foot">
          <button onClick={resetAll} title="Drop all overrides; revert every field to its YAML/default value.">
            🔄 Reset all to defaults
          </button>
          <span className="muted" style={{ marginLeft: "auto", fontSize: 12 }}>
            Changes take effect on the worker's next loop iteration. Server settings need a restart.
          </span>
          <button onClick={onClose} title="Close">Close</button>
        </div>
      </div>
    </div>
  );
}


// ---------- main app ----------
function App() {
  const [config, setConfig] = useState(null);
  const [progress, setProgress] = useState({
    state: "idle", target_folder: "", repository: "",
    total: 0, done: 0, error: 0, skipped: 0, pending: 0, processing: 0,
    current_file: "", last_message: "", started_at: null,
    current_file_progress: null,
  });
  // The currently-selected repository (drives Start + grid filter).
  const [repository, setRepository] = useState("");
  // Cached repositories list (so we can show the selected repo's path inline)
  const [repositoriesCache, setRepositoriesCache] = useState([]);
  const [files, setFiles] = useState([]);
  const [statusFilter, setStatusFilter] = useState("");
  const [search, setSearch] = useState("");
  const [shaFilter, setShaFilter] = useState("");
  const [dupOnly, setDupOnly] = useState(false);
  const [selected, setSelected] = useState(new Set());
  const [logLines, setLogLines] = useState([]);
  const [useThinking, setUseThinking] = useState(false);
  const [verbosity, setVerbosity] = useState("normal");
  const [openedPath, setOpenedPath] = useState("");
  const [tick, setTick] = useState(0);
  const [bulkRepo, setBulkRepo] = useState("");
  const [bulkStatus, setBulkStatus] = useState("");
  const [repoMgrOpen, setRepoMgrOpen] = useState(false);
  const [bulkRepoMgrOpen, setBulkRepoMgrOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [rightWidth, setRightWidth] = useState(() => {
    const saved = parseInt(localStorage.getItem("docregistrar.rightWidth") || "", 10);
    if (!Number.isNaN(saved) && saved >= 280 && saved <= 1600) return saved;
    return 460;
  });
  const [dragging, setDragging] = useState(false);
  const wsRef = useRef(null);
  const verbosityRef = useRef(verbosity);
  useEffect(() => { verbosityRef.current = verbosity; }, [verbosity]);

  // The selected repository as an object (or null), pulled from the cache.
  const selectedRepoObj = useMemo(
    () => repositoriesCache.find(r => r.name === repository) || null,
    [repository, repositoriesCache]
  );
  const startDisabledReason = useMemo(() => {
    if (!repository) return "Select a repository first (Browse repos).";
    if (!selectedRepoObj) return "Selected repository not found. Refresh the repos list.";
    if (!selectedRepoObj.path) return "Selected repository has no path configured. Edit it (Browse repos) and set its Path.";
    return "";
  }, [repository, selectedRepoObj]);

  const refreshRepositories = useCallback(async () => {
    try {
      const r = await api("GET", "/api/repositories");
      setRepositoriesCache(Array.isArray(r.items) ? r.items : []);
    } catch { /* ignore */ }
  }, []);
  useEffect(() => { refreshRepositories(); }, [refreshRepositories]);

  // Splitter drag handlers
  useEffect(() => {
    if (!dragging) {
      document.body.classList.remove("is-dragging-splitter");
      return;
    }
    document.body.classList.add("is-dragging-splitter");
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
      document.body.classList.remove("is-dragging-splitter");
    };
  }, [dragging, rightWidth]);

  useEffect(() => {
    const id = setInterval(() => setTick(t => t + 1), 1000);
    return () => clearInterval(id);
  }, []);

  useEffect(() => {
    api("GET", "/api/config").then(c => setConfig(c)).catch(console.error);

    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(`${proto}//${location.host}/ws`);
    wsRef.current = ws;
    ws.onmessage = (ev) => {
      try {
        const m = JSON.parse(ev.data);
        if (m.type === "progress") {
          setProgress(m.data);
          // If the worker is running and broadcasting a repository, sync our
          // header selection so the UI reflects what's actually being processed.
          if (m.data.repository && !repository) {
            setRepository(m.data.repository);
          }
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
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const refreshFiles = useCallback(async () => {
    try {
      const params = new URLSearchParams();
      if (statusFilter) params.set("status", statusFilter);
      if (search) params.set("search", search);
      if (shaFilter) params.set("sha256", shaFilter);
      if (dupOnly) params.set("duplicates_only", "true");
      // Filter the grid by the selected repository (empty -> files w/o repo).
      params.set("repository", repository.trim());
      params.set("limit", "2000");
      const r = await api("GET", `/api/files?${params.toString()}`);
      setFiles(r.items);
    } catch (e) { console.error(e); }
  }, [statusFilter, search, shaFilter, dupOnly, repository]);

  useEffect(() => {
    refreshFiles();
    const id = setInterval(refreshFiles, 3000);
    return () => clearInterval(id);
  }, [refreshFiles]);

  useEffect(() => {
    let aborted = false;
    const tickProgress = async () => {
      try {
        const p = await api("GET", "/api/progress");
        if (!aborted) setProgress(p);
      } catch { /* ignore */ }
    };
    const id = setInterval(tickProgress, 3000);
    return () => { aborted = true; clearInterval(id); };
  }, []);

  // Actions
  const onStart = async () => {
    if (startDisabledReason) {
      alert(startDisabledReason);
      return;
    }
    try {
      await api("POST", "/api/start", { repository: repository.trim() });
    } catch (e) { alert(e.message); }
  };
  const onPause  = () => api("POST", "/api/pause").catch(e => alert(e.message));
  const onResume = () => api("POST", "/api/resume").catch(e => alert(e.message));
  const onStop   = () => api("POST", "/api/stop").catch(e => alert(e.message));
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
      if (r.skipped_manual) msg += `\n${r.skipped_manual} manually-edited file(s) were SKIPPED. Use Force to override.`;
      if (r.skipped_status) msg += `\n${r.skipped_status} file(s) in 'skipped' status were NOT re-evaluated.`;
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
      let msg = `Queued ${r.reset} file(s) for re-evaluation (force).`;
      if (r.skipped_status) msg += `\n${r.skipped_status} file(s) in 'skipped' status were NOT re-evaluated.`;
      alert(msg);
      setSelected(new Set());
      refreshFiles();
    }
  };
  const onReevaluateOne = async (path, withThinking) => {
    const r = await reevaluatePaths([path], withThinking, false);
    if (r) {
      if (r.skipped_status) {
        alert("This file is in 'skipped' status and cannot be re-evaluated.\n\nSet its status to 'pending' first.");
      } else if (r.skipped_manual) {
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

  const onSkipDupSiblings = async () => {
    if (selected.size === 0) { alert("Select at least one file."); return; }
    if (!confirm(
      `For each selected file, every OTHER file with the same SHA-256 will be marked as 'skipped'. The selected files themselves are NOT modified.\n\nProceed?`
    )) return;
    try {
      const r = await api("POST", "/api/files/skip-dup-siblings", {
        relative_paths: Array.from(selected),
      });
      alert(`Marked ${r.updated} duplicate sibling(s) as skipped.`);
      refreshFiles();
    } catch (e) { alert(e.message); }
  };

  const onRemoveFromRegistry = async (paths, opts = {}) => {
    const list = Array.isArray(paths) ? paths : [paths];
    if (list.length === 0) return false;
    const skipConfirm = !!opts.skipConfirm;
    if (!skipConfirm) {
      const msg = list.length === 1
        ? `Remove this file from the registry?\n\n${list[0]}\n\nThe file on disk is NOT deleted.`
        : `Remove ${list.length} files from the registry?\n\nThe files on disk are NOT deleted.`;
      if (!confirm(msg)) return false;
    }
    try {
      const r = await api("POST", "/api/files/delete", { relative_paths: list });
      if (openedPath && list.includes(openedPath)) setOpenedPath("");
      setSelected(prev => {
        const ns = new Set(prev);
        for (const p of list) ns.delete(p);
        return ns;
      });
      refreshFiles();
      return r.deleted || 0;
    } catch (e) {
      alert(`Could not remove from registry:\n${e.message}`);
      return false;
    }
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
    if (progress.state === "idle" || progress.state === "error") return "";
    const t0 = new Date(progress.started_at).getTime();
    return fmtMs(Date.now() - t0);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [progress.started_at, progress.state, tick]);

  const pct = progress.total > 0 ? Math.round(((progress.done + progress.error) / progress.total) * 100) : 0;
  const running = ["scanning", "running", "paused", "stopping"].includes(progress.state);
  const paused = progress.state === "paused";
  const cfp = progress.current_file_progress;

  return (
    <div className="app">
      <div className="header">
        <h1>📚 docregistrar — local document indexer</h1>
        <div className="row">
          {/* Repository selector pill (shows the selected repo + its path) */}
          <div className="repo-pill" title={
            selectedRepoObj
              ? `Selected repository: ${selectedRepoObj.name}\nPath: ${selectedRepoObj.path || '(none — set one to enable Start)'}\nDescription: ${selectedRepoObj.description || '—'}`
              : "No repository selected. Click 'Browse repos' to pick or create one."
          }>
            <span>📁 Repository:</span>
            {repository
              ? <>
                  <span className="repo-pill-name">{repository}</span>
                  {selectedRepoObj?.path
                    ? <span className="repo-pill-path">{selectedRepoObj.path}</span>
                    : <span className="repo-pill-empty">(no path set)</span>}
                </>
              : <span className="repo-pill-empty">(none selected)</span>}
          </div>
          <button onClick={() => setRepoMgrOpen(true)}
                  title="Open the repository management dialog (create / edit / delete / select)">
            🔎 Browse repos
          </button>
          {repository && (
            <button onClick={() => setRepository("")}
                    title="Clear the repository selection (the grid will then show files with no repository)">
              ✕ Clear
            </button>
          )}
          <div className="spacer" style={{ flex: 1 }}></div>
          {!running && (
            <button className="primary" onClick={onStart}
                    disabled={!!startDisabledReason}
                    title={startDisabledReason || "Scan the selected repository's folder and start processing"}>
              ▶ Start
            </button>
          )}
          {running && !paused && <button onClick={onPause}
                                          title="Pause processing">⏸ Pause</button>}
          {paused && <button className="primary" onClick={onResume}
                              title="Resume processing">▶ Resume</button>}
          {running && <button className="danger" onClick={onStop}
                              title="Stop processing">⏹ Stop</button>}
          <button onClick={onDownload} title="Download current registry as Excel">
            ⬇ Download .xlsx
          </button>
          <button onClick={() => setSettingsOpen(true)}
                  title="Open the settings dialog (LLM, processing, scanner, …)">
            ⚙️ Settings
          </button>
        </div>

        <div className="progress-bar"><div style={{ width: `${pct}%` }}></div></div>

        <div className="status-line">
          <span className={`pill state-${progress.state}`}>{progress.state.toUpperCase()}</span>
          <span>📁 {progress.target_folder || "(no folder)"}</span>
          {progress.repository && <span>📦 {progress.repository}</span>}
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
        <div className="left-pane">
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
            />
            <button onClick={refreshFiles} title="Reload the file list">↻ Refresh</button>
            <label>
              <input type="checkbox" className="checkbox"
                     checked={dupOnly}
                     onChange={e => setDupOnly(e.target.checked)} />
              {" "}duplicates only
            </label>
            {shaFilter && (
              <span className="active-filter" title={`Filtering by SHA-256: ${shaFilter}`}>
                SHA: {shaFilter.slice(0, 8)}…
                <button onClick={() => setShaFilter("")} title="Clear">×</button>
              </span>
            )}
            <div className="spacer"></div>
            <label>Verbosity:</label>
            <select value={verbosity} onChange={e => setVerbosity(e.target.value)}>
              <option value="quiet">quiet</option>
              <option value="normal">normal</option>
              <option value="verbose">verbose</option>
            </select>
            <label>
              <input type="checkbox" className="checkbox"
                     checked={useThinking}
                     onChange={e => setUseThinking(e.target.checked)} />
              {" "}thinking mode
            </label>
            <button className="primary" onClick={onReevaluateBatch} disabled={selected.size === 0}
                    title="Re-evaluate selected files (manually-edited rows are skipped)">
              ↻ Re-evaluate {selected.size > 0 ? `(${selected.size})` : ""}
            </button>
            <button onClick={onReevaluateBatchForce} disabled={selected.size === 0}
                    title="Re-evaluate including manually-edited files">
              ↻ Force
            </button>
          </div>

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
              <button onClick={() => setBulkRepoMgrOpen(true)}
                      title="Browse repositories and pick one for the bulk-edit field">
                🔎
              </button>
              <label>set Status:</label>
              <select value={bulkStatus} onChange={e => setBulkStatus(e.target.value)}>
                <option value="">(no change)</option>
                {STATUS_OPTIONS.map(s => <option key={s} value={s}>{s}</option>)}
              </select>
              <button className="primary" onClick={onBulkApply}>Apply</button>
              <button onClick={onSkipDupSiblings}
                      title="Mark every OTHER file with the same SHA-256 as 'skipped'"
                      disabled={selected.size === 0}>
                Skip dup siblings
              </button>
              <button className="danger"
                      onClick={() => onRemoveFromRegistry(Array.from(selected))}
                      disabled={selected.size === 0}
                      title="Permanently remove the selected files from the registry">
                🗑 Remove from registry
              </button>
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
                  <th>Dup</th>
                  <th>Repository</th>
                  <th>File</th>
                  <th>Path</th>
                  <th>Type</th>
                  <th>Title</th>
                  <th>Date</th>
                  <th>Conf.</th>
                  <th>Size</th>
                  <th>✎</th>
                  <th>SHA-256</th>
                </tr>
              </thead>
              <tbody>
                {files.map(f => (
                  <tr key={f.relative_path}
                      className={`${selected.has(f.relative_path) ? "selected" : ""} ${openedPath === f.relative_path ? "opened" : ""} ${f.is_duplicate ? "dup-row" : ""}`}
                      onClick={() => setOpenedPath(f.relative_path)}>
                    <td onClick={e => e.stopPropagation()}>
                      <input type="checkbox" className="checkbox"
                        checked={selected.has(f.relative_path)}
                        onChange={() => toggleSel(f.relative_path)} />
                    </td>
                    <td className={`status-cell status-${f.status}`}
                        title={
                          (f.error ? `Error: ${f.error}\n` : "") +
                          (f.error_count ? `Failed attempts: ${f.error_count}` : f.status)
                        }>
                      {f.status}
                      {f.error_count > 0 && (
                        <span className="muted" style={{ marginLeft: 4, fontSize: 10 }}>
                          (×{f.error_count})
                        </span>
                      )}
                    </td>
                    <td>{f.quality_score !== "" && f.quality_score != null ? Number(f.quality_score).toFixed(2) : ""}</td>
                    <td className="dup-cell" title={f.is_duplicate ? "This file has duplicates (same SHA-256)" : ""}>{f.is_duplicate ? "🟰" : ""}</td>
                    <td className="truncate" title={f.repository}>{f.repository || ""}</td>
                    <td className="truncate" title={`Click to open: ${f.file_name}`}
                        onClick={async e => {
                          e.stopPropagation();
                          setOpenedPath(f.relative_path);
                          try {
                            await api("POST", "/api/open-file", { relative_path: f.relative_path });
                          } catch (err) {
                            alert(`Could not open file:\n${err.message}`);
                          }
                        }}
                        style={{ color: "var(--accent-2)", cursor: "pointer" }}>{f.file_name}</td>
                    <td className="truncate" title={f.relative_path}>{f.relative_path}</td>
                    <td>{f.document_type || f.extension}</td>
                    <td className="truncate" title={f.title}>{f.title}</td>
                    <td>{f.document_date}</td>
                    <td>{f.confidentiality}</td>
                    <td>{fmtBytes(f.file_size)}</td>
                    <td>{f.manually_edited ? "✎" : ""}</td>
                    <td className="sha-cell" title={`SHA-256: ${f.sha256}\nClick to filter by this hash`}
                        onClick={e => { e.stopPropagation(); setShaFilter(f.sha256); }}>
                      {f.sha256 ? f.sha256.slice(0, 8) + "…" : ""}
                    </td>
                  </tr>
                ))}
                {files.length === 0 && (
                  <tr><td colSpan={14} style={{ padding: 20, textAlign: "center", color: "var(--muted)" }}>
                    No files. {!repository
                      ? "Select a repository (Browse repos) and click Start to scan."
                      : "Click Start to scan the selected repository's folder."}
                  </td></tr>
                )}
              </tbody>
            </table>
          </div>
        </div>

        <div className={`splitter ${dragging ? "dragging" : ""}`}
             title="Drag to resize the side panel"
             onMouseDown={() => setDragging(true)} />

        <div className="right-pane">
          {openedPath ? (
            <FileDetailPanel
              relativePath={openedPath}
              currentProgress={cfp}
              onClose={() => setOpenedPath("")}
              onReevaluate={onReevaluateOne}
              onEdited={() => { refreshFiles(); refreshRepositories(); }}
              onOpenSibling={path => setOpenedPath(path)}
              onRemoved={() => {
                setOpenedPath("");
                setSelected(prev => {
                  const ns = new Set(prev);
                  ns.delete(openedPath);
                  return ns;
                });
                refreshFiles();
              }}
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

      {/* Repository management dialogs (header + bulk-edit) */}
      <RepositoryManagerDialog
        open={repoMgrOpen}
        currentSelection={repository}
        onPick={(value) => {
          setRepository(value);
          setRepoMgrOpen(false);
          refreshRepositories();
        }}
        onChanged={(oldName, newName) => {
          // If the currently-selected repo got renamed or deleted, react.
          refreshRepositories();
          if (oldName && oldName === repository) {
            if (newName === null) {
              setRepository(""); // deleted
            } else if (newName) {
              setRepository(newName); // renamed
            }
          }
          refreshFiles();
        }}
        onClose={() => setRepoMgrOpen(false)}
      />
      <RepositoryManagerDialog
        open={bulkRepoMgrOpen}
        currentSelection={bulkRepo}
        onPick={(value) => {
          setBulkRepo(value);
          setBulkRepoMgrOpen(false);
          refreshRepositories();
        }}
        onChanged={() => { refreshRepositories(); refreshFiles(); }}
        onClose={() => setBulkRepoMgrOpen(false)}
      />
      <SettingsDialog
        open={settingsOpen}
        onClose={() => setSettingsOpen(false)}
      />
    </div>
  );
}

ReactDOM.createRoot(document.getElementById("root")).render(<App />);
