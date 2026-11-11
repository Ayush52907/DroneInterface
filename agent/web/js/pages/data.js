import { html, useState, useEffect } from '../../vendor/preact-htm.js';
import { go } from '../ctx.js';
import { api, qs } from '../api.js';
import { Icon, Modal, toast, confirm, prompt, fmtBytes, fmtDur, fmtAgo, Bar } from '../ui.js';
import { Viewer } from './files.js';

const PAGE = 30;

function when(it) {
  if (!it.start) return '–';
  const s = new Date(it.start * 1000);
  const date = s.toLocaleDateString([], { day: 'numeric', month: 'short', year: 'numeric' });
  const t = (x) => new Date(x * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false });
  return html`<div>${date}</div><div class="faint" style="font-size:12px">${t(it.start)}–${t(it.end)} · ${fmtDur(it.end - it.start)}</div>`;
}

function Gallery({ src, item, onClose }) {
  const [list, setList] = useState(null);
  const [view, setView] = useState(null);
  const [limit, setLimit] = useState(120);
  useEffect(() => {
    api(`/data/${src.idx}/${encodeURIComponent(item.id)}/files?kind=images`).then(d => setList(d.files)).catch(e => toast(e.message, 'bad'));
  }, []);
  const paths = (list || []).map(f => f.path);
  return html`<${Modal} wide title=${`${src.name} · ${item.label}${item.note ? ' · ' + item.note : ''}`} onClose=${onClose}
      foot=${html`<span class="faint grow" style="font-size:12px">${list ? `${list.length} images` : ''}</span>`}>
    <div style="overflow:auto;min-height:0">
      ${!list ? html`<div class="empty"><span class="spinner"></span></div>` : list.length === 0 ? html`<div class="empty">No images</div>` : html`
        <div class="thumbs">${list.slice(0, limit).map(f => html`<button class="thumb" key=${f.path} onClick=${() => setView(f.path)} title=${f.folder + '/' + f.name}>
          <img loading="lazy" src=${'/api/files/thumb?' + qs({ path: f.path, w: 320 })} alt=${f.name} />
          <span class="ellipsis">${f.name}</span></button>`)}</div>
        ${list.length > limit && html`<div style="text-align:center;margin:12px"><button class="btn" onClick=${() => setLimit(l => l + 240)}>Show more (${list.length - limit} left)</button></div>`}`}
    </div>
    ${view && html`<${Viewer} path=${view} siblings=${paths} onNavigate=${setView} onClose=${() => setView(null)} />`}
  <//>`;
}

// Parses "id, lat, lon, ..." lines (comments with #) into a table with map links.
function ResultView({ file, onClose }) {
  const [d, setD] = useState(null);
  const [raw, setRaw] = useState(false);
  useEffect(() => { api('/files/read?' + qs({ path: file.path })).then(setD).catch(e => toast(e.message, 'bad')); }, []);
  let header = null, rows = [], notes = [];
  if (d && !d.binary) {
    for (const line of d.content.split('\n')) {
      const t = line.trim();
      if (!t) continue;
      if (t.startsWith('#')) { if (!header && /lat/i.test(t) && t.includes(',')) header = t.replace(/^#\s*(data lines:\s*)?/i, '').split(/\(|  /)[0].split(',').map(x => x.trim()); else notes.push(t.slice(1).trim()); continue; }
      const c = t.split(',').map(x => x.trim());
      if (c.length >= 3) rows.push(c);
    }
  }
  const li = header ? header.findIndex(h => /^lat/i.test(h)) : 1;
  const lo = header ? header.findIndex(h => /^lon/i.test(h)) : 2;
  const geo = rows.length > 0 && rows.every(r => Math.abs(+r[li]) <= 90 && Math.abs(+r[lo]) <= 180 && !isNaN(+r[li]) && !isNaN(+r[lo]));
  const cols = header || (rows[0] || []).map((_, i) => i === li ? 'lat' : i === lo ? 'lon' : `col ${i + 1}`);
  return html`<${Modal} wide title=${file.name} onClose=${onClose}
      head=${html`<button class="btn sm" onClick=${() => setRaw(!raw)}>${raw ? 'Table' : 'Raw text'}</button><a class="btn sm" href=${'/api/files/download?' + qs({ path: file.path })}><${Icon} name="download"/></a>`}
      foot=${html`<span class="faint grow" style="font-size:12px">${rows.length} rows · modified ${file.mtime ? fmtAgo(file.mtime) : ''}${geo ? ' · click a location to open it in Google Maps' : ''}</span>`}>
    <div style="overflow:auto;min-height:0">
      ${!d ? html`<div class="empty"><span class="spinner"></span></div>` : raw || !rows.length ? html`<pre class="pre" style="max-height:none">${d.content}</pre>` : html`
        ${notes.length > 0 && html`<div class="faint" style="font-size:12px;margin-bottom:8px">${notes.slice(0, 12).map(n => html`<div>${n}</div>`)}</div>`}
        <table class="csv" style="width:100%"><thead><tr>${cols.map(c => html`<th>${c}</th>`)}${geo && html`<th>map</th>`}</tr></thead>
          <tbody>${rows.slice(0, 2000).map(r => html`<tr>${r.map(c => html`<td>${c}</td>`)}
            ${geo && html`<td><a href=${`https://www.google.com/maps?q=${r[li]},${r[lo]}`} target="_blank" rel="noopener">open ↗</a></td>`}</tr>`)}</tbody></table>`}
    </div>
  <//>`;
}

function Source({ s, reload }) {
  const [show, setShow] = useState(PAGE);
  const [gallery, setGallery] = useState(null);
  const [busy, setBusy] = useState(null);
  const [resultFile, setResultFile] = useState(null);
  const base = (it) => `/data/${s.idx}/${encodeURIComponent(it.id)}`;

  const del = async (it) => {
    const ok = await confirm({ title: `Delete ${it.label}?`, danger: true, ok: `Delete ${it.count} files`,
      body: `This permanently deletes ${it.count} files (${fmtBytes(it.size)}) from the Jetson:\n${it.by_folder.map(b => `• ${b.folder}: ${b.count} files`).join('\n')}\n\nDownload it first if you need it.` });
    if (!ok) return;
    setBusy(it.id);
    try { const r = await api(base(it) + '/delete', { method: 'POST', body: {} }); toast(`Deleted ${r.deleted} files`, 'ok'); reload(); }
    catch (e) { toast(e.message, 'bad'); }
    setBusy(null);
  };
  const note = async (it) => {
    const v = await prompt({ title: `Note for ${it.label}`, label: 'e.g. "Field A, survey 2, good weather"', value: it.note, ok: 'Save' });
    if (v === null) return;
    try { await api(base(it) + '/note', { body: { note: v } }); reload(); } catch (e) { toast(e.message, 'bad'); }
  };
  const openFolder = (it) => {
    const f = s.folders.find(x => x.exists);
    if (!f) return;
    go('files', { path: s.mode === 'runs' ? f.path + '/' + it.id : f.path });
  };

  return html`<div class="card" style="padding:0;margin-bottom:16px">
    <div class="row" style="padding:12px 16px;border-bottom:1px solid var(--line);flex-wrap:wrap">
      <h3 style="margin:0">${s.name}</h3>
      <span class="pill">${s.mode === 'runs' ? 'numbered runs' : 'grouped by time'}</span>
      <span class="faint" style="font-size:12px">${s.items.length} ${s.mode === 'runs' ? 'runs' : 'sessions'} · ${s.total_count} files · ${fmtBytes(s.total_size)}</span>
      <span class="grow"></span>
      <span class="faint mono hide-sm" style="font-size:11px">${s.folders.map(f => html`<span class=${f.exists ? '' : 'bad'} title=${f.exists ? '' : 'folder missing'} style="margin-left:8px">${f.path.replace(/^\/home\/[^/]+/, '~')}</span>`)}</span>
    </div>
    ${s.files && s.files.length > 0 && html`<div class="row" style="padding:10px 16px;border-bottom:1px solid var(--line);flex-wrap:wrap;gap:8px">
      <span class="muted" style="font-size:12px">LATEST RESULTS</span>
      ${s.files.map(f => f.exists ? html`<span class="chip" style="padding-right:4px">
          <${Icon} name="file" cls="ficon" /><b class="mono" style="font-size:12.5px">${f.name}</b><span class="faint" style="font-size:12px">${fmtAgo(f.mtime)} · ${fmtBytes(f.size)}</span>
          <button class="btn ghost sm" onClick=${() => setResultFile(f)} title="View"><${Icon} name="eye"/></button>
          <a class="btn ghost sm" href=${'/api/files/download?' + qs({ path: f.path })} title="Download"><${Icon} name="download"/></a></span>`
        : html`<span class="chip faint" title="not found">${f.name} (missing)</span>`)}
      <span class="faint" style="font-size:11.5px">overwritten every run · included in the newest ${s.mode === 'runs' ? 'run' : 'session'}'s zip</span>
    </div>`}
    ${s.items.length === 0 ? html`<div class="empty">No data yet</div>` : html`
    <div style="overflow:auto"><table class="table data-table">
      <thead><tr><th>${s.mode === 'runs' ? 'Run' : 'Session'}</th><th>When</th><th class="hide-sm">Contents</th><th style="text-align:right">Size</th><th></th></tr></thead>
      <tbody>${s.items.slice(0, show).map(it => html`<tr key=${it.id}>
        <td><b>${it.label}</b>${it.note && html`<div class="info" style="font-size:12px">${it.note}</div>`}</td>
        <td>${when(it)}</td>
        <td class="hide-sm" style="font-size:12.5px">${it.by_folder.map(b => html`<span class="pill" style="margin:1px 4px 1px 0">${b.folder} <b>${b.count}</b></span>`)}
          ${it.count === 0 && html`<span class="faint">empty</span>`}</td>
        <td class="num" style="text-align:right;white-space:nowrap">${fmtBytes(it.size)}<div class="faint" style="font-size:11px">${it.count} files${it.images ? ` · ${it.images} img` : ''}</div></td>
        <td><div class="row" style="gap:2px;justify-content:flex-end">
          ${it.images > 0 && html`<button class="btn sm" onClick=${() => setGallery(it)} title="View images"><${Icon} name="grid"/><span class="hide-sm">Images</span></button>`}
          <a class="btn ghost sm" href=${'/api' + base(it) + '/download'} title="Download as zip"><${Icon} name="download"/></a>
          <button class="btn ghost sm hide-sm" onClick=${() => openFolder(it)} title="Open in Files"><${Icon} name="files"/></button>
          <button class="btn ghost sm" onClick=${() => note(it)} title="Add a note"><${Icon} name="edit"/></button>
          <button class="btn ghost sm" disabled=${busy === it.id} onClick=${() => del(it)} title="Delete"><${Icon} name="trash"/></button>
        </div></td>
      </tr>`)}</tbody>
    </table></div>
    ${s.items.length > show && html`<div style="text-align:center;padding:10px"><button class="btn sm" onClick=${() => setShow(n => n + PAGE)}>Show more (${s.items.length - show} older)</button></div>`}`}
    ${gallery && html`<${Gallery} src=${s} item=${gallery} onClose=${() => setGallery(null)} />`}
    ${resultFile && html`<${ResultView} file=${resultFile} onClose=${() => setResultFile(null)} />`}
  </div>`;
}

function SourcesEditor({ onClose, onSaved }) {
  const [d, setD] = useState(null);
  const [list, setList] = useState([]);
  const [busy, setBusy] = useState(false);
  const [adding, setAdding] = useState({});
  useEffect(() => { api('/data/sources').then(r => { setD(r); setList(r.sources); }).catch(e => toast(e.message, 'bad')); }, []);
  const upd = (i, patch) => setList(l => l.map((x, j) => j === i ? { ...x, ...patch } : x));
  const addFolder = (i) => {
    const f = (adding[i] || '').trim();
    if (!f) return;
    const all = [...list[i].folders, ...(list[i].files || [])];
    if (!all.includes(f)) upd(i, { folders: [...list[i].folders, f] }); // the server sorts folders vs files
    setAdding(a => ({ ...a, [i]: '' }));
  };
  const move = (i, dlt) => setList(l => { const n = [...l], j = i + dlt; if (j < 0 || j >= n.length) return l; [n[i], n[j]] = [n[j], n[i]]; return n; });
  const save = async () => {
    setBusy(true);
    try { await api('/data/sources', { method: 'PUT', body: { items: list } }); toast('Flight data sources saved', 'ok'); onSaved(); onClose(); }
    catch (e) { toast(e.message, 'bad'); }
    setBusy(false);
  };
  const reset = async () => {
    if (!(await confirm({ title: 'Reset to the defaults from config.yaml?', ok: 'Reset', danger: true }))) return;
    const r = await api('/data/sources/reset', { method: 'POST', body: {} }); setList(r.sources); onSaved();
  };
  const cand = d ? d.candidates : [];
  const hint = (p) => { const c = cand.find(x => x.path === p); return c ? (c.numbered ? `${c.numbered} numbered run folders` : `${c.files} files${c.images ? `, ${c.images} images` : ''}`) : ''; };
  return html`<${Modal} wide title="Flight data sources" onClose=${onClose}
      foot=${html`<button class="btn ghost grow" style="justify-content:flex-start" onClick=${reset}>Reset to defaults</button>
        <button class="btn" onClick=${onClose}>Cancel</button><button class="btn primary" disabled=${busy} onClick=${save}>Save</button>`}>
    <div style="overflow:auto;min-height:0;display:grid;gap:12px;align-content:start">
      <datalist id="dp-folders">${cand.map(c => html`<option value=${c.path}>${c.numbered ? `${c.numbered} run folders` : `${c.files} files`}</option>`)}</datalist>
      <div class="faint" style="font-size:12.5px">
        <b>Numbered runs</b>: folders that each contain run subfolders <span class="mono">1, 2, 3…</span>; the same number across folders is one run (e.g. <span class="mono">geo_gps/4 + geo_dets/4</span>).
        <b>Time sessions</b>: flat folders; files are grouped wherever there's a gap of over 10 minutes.
        You can also add single <b>result files</b> (e.g. <span class="mono">~/targets_clustered.txt</span>) that every run overwrites; they're shown as "latest results".
      </div>
      ${!d ? html`<div class="empty"><span class="spinner"></span></div>` : list.map((src, i) => html`<div class="card" key=${i} style="padding:12px">
        <div class="row" style="gap:8px;flex-wrap:wrap">
          <input class="input grow" style="min-width:160px;font-weight:600" placeholder="Name" value=${src.name} onInput=${e => upd(i, { name: e.target.value })} />
          <select class="input" value=${src.mode} onChange=${e => upd(i, { mode: e.target.value })}>
            <option value="runs">Numbered runs</option><option value="sessions">Time sessions</option></select>
          <button class="btn ghost sm" onClick=${() => move(i, -1)}>↑</button><button class="btn ghost sm" onClick=${() => move(i, 1)}>↓</button>
          <button class="btn ghost sm" title="Remove source" onClick=${() => setList(l => l.filter((_, j) => j !== i))}><${Icon} name="trash"/></button>
        </div>
        <div class="chips" style="margin:10px 0">
          ${src.folders.map(f => html`<span class="chip mono" title=${hint(f)}>${f}${hint(f) && html`<span class="faint" style="font-family:var(--sans)"> · ${hint(f)}</span>`}
            <button onClick=${() => upd(i, { folders: src.folders.filter(x => x !== f) })}>×</button></span>`)}
          ${(src.files || []).map(f => html`<span class="chip mono" title="result file">📄 ${f}<button onClick=${() => upd(i, { files: src.files.filter(x => x !== f) })}>×</button></span>`)}
          ${src.folders.length === 0 && !(src.files || []).length && html`<span class="faint" style="font-size:12px">No folders yet</span>`}
        </div>
        <div class="row" style="gap:6px">
          <input class="input grow mono" list="dp-folders" placeholder="~/target_images or a results file like ~/targets.txt" value=${adding[i] || ''}
            onInput=${e => setAdding(a => ({ ...a, [i]: e.target.value }))} onKeyDown=${e => { if (e.key === 'Enter') addFolder(i); }} />
          <button class="btn sm" onClick=${() => addFolder(i)}><${Icon} name="plus"/>Add</button>
        </div>
      </div>`)}
      ${d && html`<div><button class="btn" onClick=${() => setList(l => [...l, { name: '', mode: 'sessions', folders: [], files: [] }])}><${Icon} name="plus"/>Add data source</button></div>`}
      ${d && d.origin === 'config' && html`<div class="faint" style="font-size:12px">Currently using the defaults from config.yaml. Saving here stores your own configuration in the panel.</div>`}
    </div>
  <//>`;
}

export function Data() {
  const [d, setD] = useState(null);
  const [loading, setLoading] = useState(false);
  const [cfg, setCfg] = useState(false);
  const load = () => { setLoading(true); return api('/data').then(setD).catch(e => toast(e.message, 'bad')).finally(() => setLoading(false)); };
  useEffect(() => { load(); }, []);
  if (!d) return html`<div class="page"><div class="empty"><span class="spinner"></span></div></div>`;
  const pct = d.disk.used / d.disk.total * 100;
  const dataTotal = d.sources.reduce((a, s) => a + s.total_size, 0);
  return html`<div class="page">
    <div class="page-head">
      <h1>Flight data</h1><span class="grow"></span>
      <button class="btn" onClick=${() => setCfg(true)}><${Icon} name="services"/>Configure</button>
      <button class="btn" disabled=${loading} onClick=${load}>${loading ? html`<span class="spinner"></span>` : html`<${Icon} name="refresh"/>`}</button>
    </div>
    <div class="card" style="margin-bottom:16px">
      <div class="row" style="flex-wrap:wrap;margin-bottom:8px">
        <b>Storage</b><span class="grow"></span>
        <span class="num">${fmtBytes(d.disk.free)} free of ${fmtBytes(d.disk.total)}</span>
        <span class="faint">· flight data uses ${fmtBytes(dataTotal)}</span>
      </div>
      <${Bar} pct=${pct} cls=${pct > 95 ? 'bad' : pct > 85 ? 'warn' : ''} />
    </div>
    ${d.sources.length === 0 && html`<div class="banner warn">No data folders configured. Press <b>Configure</b> to add some.</div>`}
    ${d.sources.map(s => html`<${Source} key=${s.idx + s.name} s=${s} reload=${load} />`)}
    ${cfg && html`<${SourcesEditor} onClose=${() => setCfg(false)} onSaved=${load} />`}
  </div>`;
}
