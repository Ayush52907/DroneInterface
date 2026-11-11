import { html, useState, useEffect, useRef, useCallback, useContext } from '../../vendor/preact-htm.js';
import { AppCtx, go } from '../ctx.js';
import { api, socket, qs, rawUrl, downloadUrl } from '../api.js';
import { Icon, Modal, toast, confirm, prompt, fmtBytes, fmtAgo, Bar, usePref } from '../ui.js';

const CHUNK = 8 * 1024 * 1024;
const join = (dir, name) => (dir.endsWith('/') ? dir : dir + '/') + name;
const base = (p) => p.split('/').filter(Boolean).pop() || '/';
const ext = (n) => (n.includes('.') ? n.split('.').pop().toLowerCase() : '');
const LANG = { py: 'python', sh: 'bash', bash: 'bash', yaml: 'yaml', yml: 'yaml', json: 'json', xml: 'xml', launch: 'xml',
  urdf: 'xml', sdf: 'xml', c: 'c', h: 'cpp', cpp: 'cpp', hpp: 'cpp', cc: 'cpp', cu: 'cpp', js: 'javascript', ts: 'typescript',
  service: 'ini', ini: 'ini', conf: 'ini', cfg: 'ini', toml: 'ini', md: 'markdown', rs: 'rust', go: 'go', lua: 'lua',
  sql: 'sql', cmake: 'makefile', mk: 'makefile', diff: 'diff', css: 'css', html: 'xml', rules: 'ini', txt: 'plaintext', log: 'plaintext' };
const MEDIA_RE = /\.(png|jpe?g|gif|webp|bmp|svg|mp4|webm|mkv|mov|avi|m4v)$/i;
const IMAGE_RE = /\.(png|jpe?g|gif|webp|bmp|svg)$/i;
const langOf = (name) => LANG[ext(name)] || (name === 'Makefile' ? 'makefile' : name === 'CMakeLists.txt' ? 'makefile' : null);

let hljsP = null;
function loadHljs() {
  if (!hljsP) hljsP = new Promise((res) => {
    const l = document.createElement('link'); l.rel = 'stylesheet'; l.href = 'vendor/hljs-github-dark.css'; document.head.appendChild(l);
    const s = document.createElement('script'); s.src = 'vendor/highlight.min.js'; s.onload = () => res(window.hljs); s.onerror = () => res(null);
    document.head.appendChild(s);
  });
  return hljsP;
}

function FileIcon({ e }) {
  return e.type === 'dir' ? html`<${Icon} name="files" cls="ficon dir"/>` : html`<${Icon} name="file" cls="ficon"/>`;
}

// ---------------------------------------------------------------- viewers
function CodeView({ text, name }) {
  const ref = useRef();
  const lines = text.split('\n');
  useEffect(() => {
    const lang = langOf(name);
    if (!lang || lang === 'plaintext' || text.length > 400000) return;
    let gone = false;
    loadHljs().then(h => {
      if (gone || !h || !ref.current || !h.getLanguage(lang)) return;
      const out = h.highlight(text, { language: lang, ignoreIllegals: true }).value;
      // split highlighted HTML into lines while keeping spans balanced
      const res = []; let open = [];
      for (const ln of out.split('\n')) {
        const prefix = open.join('');
        const tags = ln.match(/<span[^>]*>|<\/span>/g) || [];
        for (const t of tags) t === '</span>' ? open.pop() : open.push(t);
        res.push('<span class="l">' + prefix + ln + '</span>'.repeat(open.length) + '</span>');
      }
      ref.current.innerHTML = res.join('');
    });
    return () => { gone = true; };
  }, [text, name]);
  return html`<pre class="code" ref=${ref}>${lines.map(l => html`<span class="l">${l || ' '}</span>`)}</pre>`;
}

function CsvView({ text }) {
  const rows = text.split('\n').slice(0, 2000).filter(r => r.length).map(r => r.split(text.includes('\t') && !text.includes(',') ? '\t' : ','));
  if (!rows.length) return html`<div class="empty">Empty file</div>`;
  return html`<div style="overflow:auto"><table class="csv"><thead><tr>${rows[0].map(c => html`<th>${c}</th>`)}</tr></thead>
    <tbody>${rows.slice(1).map(r => html`<tr>${r.map(c => html`<td>${c}</td>`)}</tr>`)}</tbody></table></div>`;
}

function TailView({ path }) {
  const [text, setText] = useState('');
  const [paused, setPaused] = useState(false);
  const [note, setNote] = useState('');
  const [conn, setConn] = useState('connecting');
  const ref = useRef();
  const pausedRef = useRef(false); pausedRef.current = paused;
  const hold = useRef('');
  useEffect(() => {
    const s = socket('/ws/tail?' + qs({ path }), {
      onState: setConn,
      onMessage: (m) => {
        if (m.type === 'error') { setNote(m.error); return; }
        if (m.note) setNote(m.note + ' at ' + new Date().toLocaleTimeString());
        if (pausedRef.current && !m.reset) { hold.current += m.data; return; }
        setText(t => { const n = (m.reset ? '' : t) + hold.current + m.data; hold.current = ''; return n.length > 1500000 ? n.slice(-1000000) : n; });
      },
    });
    return () => s.close();
  }, [path]);
  useEffect(() => { if (!paused && hold.current) { setText(t => t + hold.current); hold.current = ''; } }, [paused]);
  useEffect(() => { if (!paused && ref.current) ref.current.scrollTop = ref.current.scrollHeight; }, [text]);
  return html`<div style="display:grid;grid-template-rows:auto 1fr;gap:8px;min-height:0;height:100%">
    <div class="row"><span class="pill ${conn === 'open' ? 'ok' : 'warn'}"><span class="dot ${conn === 'open' ? 'ok' : 'warn'}"></span>${conn === 'open' ? (paused ? 'paused' : 'following') : 'connecting'}</span>
      <span class="faint grow" style="font-size:12px">${note}</span>
      <button class="btn sm ${paused ? 'on' : ''}" onClick=${() => setPaused(!paused)}><${Icon} name=${paused ? 'play' : 'pause'}/>${paused ? 'Resume' : 'Pause'}</button>
      <button class="btn sm" onClick=${() => setText('')}><${Icon} name="trash"/>Clear</button></div>
    <pre class="pre" style="max-height:none;height:100%;white-space:pre-wrap" ref=${ref}>${text}</pre>
  </div>`;
}

export function Viewer({ path, onClose, onSaved, startMode, siblings = [], onNavigate }) {
  const [mode, setMode] = useState(startMode || 'view');
  const [d, setD] = useState(null);
  const [err, setErr] = useState(null);
  const [draft, setDraft] = useState('');
  const [saving, setSaving] = useState(false);
  const name = base(path);
  const idx = siblings.indexOf(path);
  const canNav = idx !== -1 && siblings.length > 1;
  const nav = (d) => { if (canNav) onNavigate(siblings[(idx + d + siblings.length) % siblings.length]); };
  useEffect(() => {
    if (!canNav) return;
    const k = (e) => {
      const tag = (e.target.tagName || '').toLowerCase();
      if (tag === 'input' || tag === 'textarea' || e.altKey || e.ctrlKey || e.metaKey) return;
      if (e.key === 'ArrowRight' || e.key === 'ArrowDown') { e.preventDefault(); nav(1); }
      else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') { e.preventDefault(); nav(-1); }
    };
    window.addEventListener('keydown', k);
    // preload neighbouring images so switching is instant
    [1, -1].forEach(d => { const p = siblings[(idx + d + siblings.length) % siblings.length]; if (IMAGE_RE.test(p)) new Image().src = rawUrl(p); });
    return () => window.removeEventListener('keydown', k);
  }, [path, siblings]);
  const kindGuess = /\.(png|jpe?g|gif|webp|bmp|svg)$/i.test(name) ? 'image' : /\.(mp4|webm|mkv|mov|avi|m4v)$/i.test(name) ? 'video'
    : /\.(mp3|wav|ogg|flac|m4a)$/i.test(name) ? 'audio' : /\.pdf$/i.test(name) ? 'pdf' : 'text';

  const load = useCallback(() => {
    if (kindGuess !== 'text') { setD({ kind: kindGuess }); return; }
    api('/files/read?' + qs({ path })).then(r => { setD(r); setDraft(r.content); }).catch(e => setErr(e.message));
  }, [path]);
  useEffect(() => { setD(null); setErr(null); setMode(startMode || 'view'); load(); }, [load]);

  const dirty = mode === 'edit' && d && draft !== d.content;
  const close = async () => {
    if (dirty && !(await confirm({ title: 'Discard unsaved changes?', danger: true, ok: 'Discard' }))) return;
    onClose();
  };
  const save = async () => {
    setSaving(true);
    try {
      await api('/files/write', { body: { path, content: draft, mtime: d.mtime } });
      toast('Saved ' + name, 'ok');
      setMode('view');
      load();
      onSaved && onSaved();
    } catch (e) { toast(e.message, 'bad'); }
    setSaving(false);
  };
  useEffect(() => {
    const k = (e) => { if ((e.ctrlKey || e.metaKey) && e.key === 's' && mode === 'edit') { e.preventDefault(); save(); } };
    window.addEventListener('keydown', k);
    return () => window.removeEventListener('keydown', k);
  });

  let body;
  if (err) body = html`<div class="banner">${err}</div>`;
  else if (!d) body = html`<div class="empty"><span class="spinner"></span></div>`;
  else if (mode === 'tail') body = html`<${TailView} path=${path} />`;
  else if (d.kind === 'image') body = html`<div class="media-stage"><img key=${path} src=${rawUrl(path)} alt=${name} draggable="false" /></div>`;
  else if (d.kind === 'video') body = html`<div class="media-stage"><video key=${path} src=${rawUrl(path)} controls autoplay muted playsinline /></div>`;
  else if (d.kind === 'audio') body = html`<audio src=${rawUrl(path)} controls style="margin:auto" />`;
  else if (d.kind === 'pdf') body = html`<iframe src=${rawUrl(path)} style="border:0;width:100%;height:100%;background:#fff" />`;
  else if (d.binary) body = html`<div class="empty">Binary file (${fmtBytes(d.size)}). Download it to open locally.</div>`;
  else if (mode === 'edit') body = html`<textarea class="editor" spellcheck="false" value=${draft} onInput=${e => setDraft(e.target.value)}
      onKeyDown=${e => { if (e.key === 'Tab') { e.preventDefault(); const t = e.target, s = t.selectionStart; t.setRangeText('    ', s, t.selectionEnd, 'end'); setDraft(t.value); } }} />`;
  else if (ext(name) === 'csv' || ext(name) === 'tsv') body = html`<${CsvView} text=${d.content} />`;
  else body = html`<${CodeView} text=${d.content} name=${name} />`;

  const isText = d && d.kind === 'text' && !d.binary;
  const isMedia = d && (d.kind === 'image' || d.kind === 'video');
  // swipe left/right on phones (ignored while pinch-zoomed)
  const touch = useRef(null);
  const onTouchStart = (e) => { touch.current = e.touches.length === 1 ? { x: e.touches[0].clientX, y: e.touches[0].clientY, t: Date.now() } : null; };
  const onTouchEnd = (e) => {
    const s0 = touch.current; touch.current = null;
    if (!s0 || !canNav || (window.visualViewport && window.visualViewport.scale > 1.05)) return;
    const t = e.changedTouches[0], dx = t.clientX - s0.x, dy = t.clientY - s0.y;
    if (Math.abs(dx) > 50 && Math.abs(dx) > Math.abs(dy) * 1.5 && Date.now() - s0.t < 800) nav(dx < 0 ? 1 : -1);
  };
  const foot = html`
    ${d && d.truncated && mode !== 'tail' && html`<span class="warn grow" style="font-size:12px">Showing first ${fmtBytes(d.content.length)} of ${fmtBytes(d.size)}. Use Live tail or download for the rest.</span>`}
    ${d && d.size !== undefined && !d.truncated && html`<span class="faint grow" style="font-size:12px">${fmtBytes(d.size)}${d.mtime ? ' · modified ' + fmtAgo(d.mtime) : ''}</span>`}
    ${(!d || d.size === undefined) && html`<span class="grow faint" style="font-size:12px">${canNav ? html`<b class="num" style="color:var(--text)">${idx + 1} / ${siblings.length}</b><span class="hide-sm"> · use ← → keys</span><span class="show-sm"> · swipe to switch</span>` : ''}</span>`}
    ${canNav && html`<button class="btn" onClick=${() => nav(-1)}><${Icon} name="left"/>Prev</button><button class="btn" onClick=${() => nav(1)}>Next<${Icon} name="right"/></button>`}
    ${mode === 'edit' ? html`
      <button class="btn" onClick=${() => { setDraft(d.content); setMode('view'); }}>Cancel</button>
      <button class="btn primary" disabled=${saving || !dirty} onClick=${save}>${saving ? 'Saving…' : 'Save'} <span class="faint" style="font-weight:400">Ctrl+S</span></button>` : html`
      ${isText && mode !== 'tail' && html`<button class="btn" onClick=${() => setMode('tail')}><${Icon} name="tail"/>Live tail</button>`}
      ${mode === 'tail' && html`<button class="btn" onClick=${() => { setMode('view'); load(); }}><${Icon} name="eye"/>Static view</button>`}
      ${isText && !d.truncated && mode !== 'tail' && html`<button class="btn" onClick=${() => setMode('edit')}><${Icon} name="edit"/>Edit</button>`}
      <a class="btn" href=${downloadUrl(path)}><${Icon} name="download"/>Download</a>`}`;
  return html`<${Modal} wide title=${html`${name}${dirty ? html` <span class="warn">●</span>` : ''} <span class="faint mono hide-sm" style="font-size:12px;font-weight:400">${path}</span>`} onClose=${close} foot=${foot}>
    <div class="viewer-body ${isMedia ? 'media' : ''}" style="position:relative" onTouchStart=${onTouchStart} onTouchEnd=${onTouchEnd}>${body}
      ${canNav && html`<button class="nav-arrow left" title="Previous (←)" onClick=${() => nav(-1)}><${Icon} name="left"/></button>
        <button class="nav-arrow right" title="Next (→)" onClick=${() => nav(1)}><${Icon} name="right"/></button>`}
    </div>
  <//>`;
}

// ---------------------------------------------------------------- uploads
function useUploads(onDone) {
  const [list, setList] = useState([]);
  const upd = (id, patch) => setList(l => l.map(u => u.id === id ? { ...u, ...patch } : u));
  const start = async (files, dir) => {
    for (const file of files) {
      const id = Math.random().toString(36).slice(2);
      const dest = join(dir, file.name);
      const ctl = { cancel: false };
      setList(l => [...l, { id, name: file.name, size: file.size, sent: 0, state: 'checking', ctl }]);
      try {
        const st = await api('/files/upload?' + qs({ path: dest }));
        let overwrite = false;
        if (st.exists) {
          overwrite = await confirm({ title: `Replace ${file.name}?`, body: `A file with this name already exists in ${dir}.`, ok: 'Replace', danger: true });
          if (!overwrite) { upd(id, { state: 'skipped' }); continue; }
        }
        let offset = st.partial && st.partial < file.size ? st.partial : 0;
        const t0 = Date.now(), off0 = offset;
        upd(id, { state: 'uploading', sent: offset, resumed: offset > 0 });
        while (true) {
          if (ctl.cancel) { upd(id, { state: 'cancelled' }); break; }
          const end = Math.min(offset + CHUNK, file.size);
          const res = await fetch('/api/files/upload?' + qs({ path: dest, offset, total: file.size, overwrite: overwrite ? 'true' : '' }),
            { method: 'PUT', body: file.slice(offset, end) });
          const j = await res.json().catch(() => ({}));
          if (res.status === 409 && j.partial !== undefined) { offset = j.partial; continue; }
          if (!res.ok) throw new Error(j.detail || res.statusText);
          offset = end;
          const secs = (Date.now() - t0) / 1000;
          upd(id, { sent: offset, speed: secs > 0 ? (offset - off0) / secs : 0 });
          if (j.done) { upd(id, { state: 'done' }); onDone(); break; }
        }
      } catch (e) {
        upd(id, { state: 'error', error: e.message });
      }
    }
  };
  const clear = () => setList(l => l.filter(u => u.state === 'uploading' || u.state === 'checking'));
  return { list, start, clear };
}

// ---------------------------------------------------------------- page
export function Files({ params }) {
  const { me } = useContext(AppCtx);
  const roots = me.files.roots;
  const [path, setPath] = useState(params.path || roots[0]);
  const [d, setD] = useState(null);
  const [err, setErr] = useState(null);
  const [sel, setSel] = useState(new Set());
  const [view, setView] = useState(null); // {path, mode}
  const [showHidden, setShowHidden] = usePref('files.hidden', false);
  const [filter, setFilter] = useState('');
  const [sort, setSort] = usePref('files.sort', { k: 'name', asc: true });
  const [clip, setClip] = useState(null); // {mode, paths}
  const [drag, setDrag] = useState(false);
  const [busy, setBusy] = useState(false);
  const fileInput = useRef();

  const load = useCallback((p = path) => {
    setErr(null);
    return api('/files/list?' + qs({ path: p })).then(r => { setD(r); setPath(r.path); }).catch(e => setErr(e.message));
  }, [path]);
  const uploads = useUploads(() => load());

  useEffect(() => { load(path); setSel(new Set()); setFilter(''); }, [path]);
  useEffect(() => { if (params.path && params.path !== path) setPath(params.path); }, [params.path]);
  useEffect(() => { if (params.open) setView({ path: params.open, mode: params.mode }); }, [params.open]);

  const nav = (p) => go('files', { path: p });

  const entries = d ? d.entries
    .filter(e => showHidden || !e.name.startsWith('.'))
    .filter(e => !filter || e.name.toLowerCase().includes(filter.toLowerCase()))
    .sort((a, b) => {
      if (a.type === 'dir' && b.type !== 'dir') return -1;
      if (b.type === 'dir' && a.type !== 'dir') return 1;
      const k = sort.k, m = sort.asc ? 1 : -1;
      if (k === 'size') return ((a.size || 0) - (b.size || 0)) * m;
      if (k === 'mtime') return ((a.mtime || 0) - (b.mtime || 0)) * m;
      return a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: 'base' }) * m;
    }) : [];

  const open = (e) => {
    const p = join(d.path, e.name);
    if (e.link && !e.link_ok) { toast('Symlink points outside the allowed folders', 'bad'); return; }
    if (e.type === 'dir') nav(p); else setView({ path: p });
  };
  const toggle = (name, ev) => {
    ev && ev.stopPropagation();
    setSel(s => { const n = new Set(s); n.has(name) ? n.delete(name) : n.add(name); return n; });
  };
  const selPaths = [...sel].map(n => join(d.path, n));

  const wrap = async (fn) => { setBusy(true); try { await fn(); } catch (e) { toast(e.message, 'bad'); } setBusy(false); };

  const mkdir = async () => {
    const name = await prompt({ title: 'New folder', label: 'Folder name', ok: 'Create' });
    if (name) wrap(async () => { await api('/files/mkdir', { body: { parent: d.path, name } }); await load(); });
  };
  const newFile = async () => {
    const name = await prompt({ title: 'New file', label: 'File name', ok: 'Create' });
    if (name) wrap(async () => { await api('/files/write', { body: { path: join(d.path, name), content: '' } }); await load(); setView({ path: join(d.path, name), mode: 'edit' }); });
  };
  const rename = async (e) => {
    const name = await prompt({ title: 'Rename', label: 'New name', value: e.name, ok: 'Rename' });
    if (name && name !== e.name) wrap(async () => { await api('/files/rename', { body: { path: join(d.path, e.name), name } }); await load(); });
  };
  const del = async (paths) => {
    const names = paths.map(base);
    const ok = await confirm({ title: `Delete ${names.length === 1 ? names[0] : names.length + ' items'}?`, danger: true, ok: 'Delete',
      body: 'This cannot be undone.' + (names.length > 1 ? '\n\n' + names.slice(0, 12).join('\n') + (names.length > 12 ? '\n…' : '') : '') });
    if (ok) wrap(async () => { await api('/files/delete', { body: { paths } }); setSel(new Set()); await load(); toast(`Deleted ${names.length} item(s)`, 'ok'); });
  };
  const paste = async () => {
    const run = (overwrite) => api('/files/transfer', { body: { paths: clip.paths, dest: d.path, mode: clip.mode, overwrite } });
    wrap(async () => {
      try { await run(false); }
      catch (e) {
        if (e.status !== 409) throw e;
        const ok = await confirm({ title: 'Replace existing items?', danger: true, ok: 'Replace', body: e.message });
        if (!ok) return;
        await run(true);
      }
      toast(`${clip.mode === 'copy' ? 'Copied' : 'Moved'} ${clip.paths.length} item(s)`, 'ok');
      if (clip.mode === 'move') setClip(null);
      await load();
    });
  };

  const onDrop = (e) => {
    e.preventDefault(); setDrag(false);
    const files = [...(e.dataTransfer.files || [])];
    if (files.length) uploads.start(files, d.path);
  };

  // breadcrumbs relative to the root
  const crumbs = [];
  if (d) {
    const rel = d.path.slice(d.root.length).split('/').filter(Boolean);
    crumbs.push({ label: d.root === '/' ? '/' : base(d.root), path: d.root });
    rel.forEach((seg, i) => crumbs.push({ label: seg, path: join(d.root, rel.slice(0, i + 1).join('/')) }));
  }
  const sortBy = (k) => setSort(s => ({ k, asc: s.k === k ? !s.asc : true }));
  const arrow = (k) => sort.k === k ? (sort.asc ? ' ↑' : ' ↓') : '';
  const allSel = entries.length > 0 && entries.every(e => sel.has(e.name));

  return html`<div class="page-full">
    <div class="page-head" style="margin-bottom:10px">
      <h1>Files</h1>
      <div class="crumbs grow">${crumbs.map((c, i) => html`${i > 0 && html`<span class="faint">/</span>`}<button onClick=${() => nav(c.path)}>${c.label}</button>`)}</div>
      ${d && d.disk && html`<span class="faint hide-sm" style="font-size:12px">${fmtBytes(d.disk.free)} free</span>`}
    </div>

    <div class="toolbar">
      <button class="btn" disabled=${!d || !d.parent} onClick=${() => nav(d.parent)} title="Up one folder"><${Icon} name="up"/></button>
      <button class="btn" onClick=${() => load()} title="Refresh"><${Icon} name="refresh"/></button>
      <button class="btn primary" onClick=${() => fileInput.current.click()}><${Icon} name="upload"/>Upload</button>
      <input type="file" multiple style="display:none" ref=${fileInput} onChange=${e => { uploads.start([...e.target.files], d.path); e.target.value = ''; }} />
      <button class="btn" onClick=${mkdir}><${Icon} name="plus"/><span class="hide-sm">Folder</span></button>
      <button class="btn hide-sm" onClick=${newFile}><${Icon} name="file"/>New file</button>
      ${clip && html`<button class="btn on" disabled=${busy} onClick=${paste}><${Icon} name=${clip.mode === 'copy' ? 'copy' : 'move'}/>Paste ${clip.paths.length} here</button>
        <button class="btn ghost sm" onClick=${() => setClip(null)} title="Clear clipboard"><${Icon} name="x"/></button>`}
      <span class="grow"></span>
      ${busy && html`<span class="spinner"></span>`}
      <input class="input" style="width:160px" placeholder="Filter…" value=${filter} onInput=${e => setFilter(e.target.value)} />
      <label class="row muted" style="font-size:13px;gap:5px"><input type="checkbox" checked=${showHidden} onChange=${() => setShowHidden(!showHidden)} />Hidden</label>
    </div>

    ${sel.size > 0 && html`<div class="selbar">
      <b>${sel.size} selected</b><span class="grow"></span>
      ${sel.size === 1 && html`<a class="btn sm" href=${downloadUrl(selPaths[0])}><${Icon} name="download"/>Download</a>`}
      <button class="btn sm" onClick=${() => { setClip({ mode: 'copy', paths: selPaths }); setSel(new Set()); toast('Copied. Open the destination folder and press Paste'); }}><${Icon} name="copy"/>Copy</button>
      <button class="btn sm" onClick=${() => { setClip({ mode: 'move', paths: selPaths }); setSel(new Set()); toast('Cut. Open the destination folder and press Paste'); }}><${Icon} name="move"/>Move</button>
      <button class="btn sm danger" onClick=${() => del(selPaths)}><${Icon} name="trash"/>Delete</button>
      <button class="btn sm ghost" onClick=${() => setSel(new Set())}>Clear</button>
    </div>`}

    <div class="files-layout">
      <div class="bookmarks">
        ${roots.map(r => html`<button onClick=${() => nav(r)}><${Icon} name="home" cls="ficon"/>${base(r)}</button>`)}
        ${me.files.bookmarks.map(b => html`<button onClick=${() => nav(b.path)} title=${b.path}><${Icon} name="star" cls="ficon"/><span class="ellipsis">${b.name}</span></button>`)}
        ${uploads.list.length > 0 && html`<div class="uploads" style="margin-top:14px">
          <div class="row"><b class="grow" style="font-size:12px;color:var(--muted)">UPLOADS</b><button class="btn ghost sm" onClick=${uploads.clear}>Clear</button></div>
          ${uploads.list.map(u => html`<div class="upload-row">
            <span class="ellipsis" title=${u.name}>${u.name}</span>
            <span class=${u.state === 'error' ? 'bad' : u.state === 'done' ? 'ok' : 'faint'}>
              ${u.state === 'uploading' ? Math.floor(u.sent / Math.max(u.size, 1) * 100) + '%' : u.state}
              ${u.state === 'uploading' && html` <button class="btn ghost sm" style="padding:0 4px" onClick=${() => { u.ctl.cancel = true; }}>×</button>`}</span>
            ${u.state === 'uploading' && html`<${Bar} pct=${u.sent / Math.max(u.size, 1) * 100} cls="" />
              <span class="faint" style="font-size:11px;grid-column:1/-1">${fmtBytes(u.sent)} / ${fmtBytes(u.size)} · ${fmtBytes(u.speed || 0)}/s${u.resumed ? ' · resumed' : ''}</span>`}
            ${u.error && html`<span class="bad" style="font-size:11px;grid-column:1/-1">${u.error}</span>`}
          </div>`)}
        </div>`}
      </div>

      <div class="table-wrap" style="position:relative;min-height:0"
        onDragOver=${e => { e.preventDefault(); setDrag(true); }} onDragLeave=${e => { if (!e.currentTarget.contains(e.relatedTarget)) setDrag(false); }} onDrop=${onDrop}>
        ${drag && html`<div class="drop-zone">Drop files to upload to ${d ? base(d.path) : ''}</div>`}
        ${err ? html`<div class="banner" style="margin:12px">${err}</div>` : !d ? html`<div class="empty"><span class="spinner"></span></div>` : html`
        <table class="table">
          <thead><tr>
            <th style="width:32px"><input type="checkbox" checked=${allSel} onChange=${() => setSel(allSel ? new Set() : new Set(entries.map(e => e.name)))} /></th>
            <th style="cursor:pointer" onClick=${() => sortBy('name')}>Name${arrow('name')}</th>
            <th style="cursor:pointer;text-align:right;width:100px" onClick=${() => sortBy('size')}>Size${arrow('size')}</th>
            <th class="hide-sm" style="cursor:pointer;width:130px" onClick=${() => sortBy('mtime')}>Modified${arrow('mtime')}</th>
            <th style="width:1px"></th>
          </tr></thead>
          <tbody>
            ${entries.length === 0 && html`<tr><td colspan="5"><div class="empty">${filter ? 'No matches' : 'Empty folder. Drop files here to upload.'}</div></td></tr>`}
            ${entries.map(e => html`<tr class="click ${sel.has(e.name) ? 'sel' : ''}" key=${e.name} onClick=${() => open(e)}>
              <td onClick=${ev => toggle(e.name, ev)}><input type="checkbox" checked=${sel.has(e.name)} onClick=${ev => ev.stopPropagation()} onChange=${() => toggle(e.name)} /></td>
              <td><div class="row" style="gap:8px"><${FileIcon} e=${e}/><span class="ellipsis ${e.name.startsWith('.') ? 'muted' : ''}">${e.name}</span>
                ${e.link && html`<span class="pill" title="symlink"><${Icon} name="link" cls="ficon" />link</span>`}
                ${e.exec && html`<span class="pill faint" style="font-size:11px">exec</span>`}</div></td>
              <td class="num muted" style="text-align:right">${e.type === 'file' ? fmtBytes(e.size) : ''}</td>
              <td class="hide-sm muted" title=${e.mtime ? new Date(e.mtime * 1000).toLocaleString() : ''}>${e.mtime ? fmtAgo(e.mtime) : ''}</td>
              <td onClick=${ev => ev.stopPropagation()}><div class="row" style="gap:2px">
                ${e.type === 'file' ? html`<button class="btn ghost sm hide-sm" title="Live tail" onClick=${() => setView({ path: join(d.path, e.name), mode: 'tail' })}><${Icon} name="tail"/></button>`
                  : html`<span class="btn ghost sm hide-sm" style="visibility:hidden"><${Icon} name="tail"/></span>`}
                <a class="btn ghost sm" title=${e.type === 'dir' ? 'Download as zip' : 'Download'} href=${downloadUrl(join(d.path, e.name))}><${Icon} name="download"/></a>
                <button class="btn ghost sm hide-sm" title="Rename" onClick=${() => rename(e)}><${Icon} name="edit"/></button>
                <button class="btn ghost sm hide-sm" title="Delete" onClick=${() => del([join(d.path, e.name)])}><${Icon} name="trash"/></button>
              </div></td>
            </tr>`)}
          </tbody>
        </table>`}
      </div>
    </div>

    ${view && html`<${Viewer} path=${view.path} startMode=${view.mode} onClose=${() => setView(null)} onSaved=${() => load()}
      siblings=${d && MEDIA_RE.test(view.path) ? entries.filter(e => e.type === 'file' && MEDIA_RE.test(e.name)).map(e => join(d.path, e.name)) : []}
      onNavigate=${(p) => setView({ path: p })} />`}
  </div>`;
}
