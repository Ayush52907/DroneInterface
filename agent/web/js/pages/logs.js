import { html, useState, useEffect, useRef, useMemo } from '../../vendor/preact-htm.js';
import { go } from '../ctx.js';
import { api, socket, qs } from '../api.js';
import { Icon, fmtTime } from '../ui.js';

const MAX = 5000;
const PRIOS = [
  { v: '', label: 'All levels' }, { v: '3', label: 'Errors' }, { v: '4', label: 'Warnings +' },
  { v: '6', label: 'Info +' },
];
const SINCE = [
  { v: '', label: 'Last 300 lines' }, { v: '10 min ago', label: 'Last 10 min' }, { v: '1 hour ago', label: 'Last hour' },
  { v: 'today', label: 'Today' }, { v: 'boot', label: 'Since boot' },
];
const PNAME = ['EMERG', 'ALERT', 'CRIT', 'ERR', 'WARN', 'NOTICE', 'INFO', 'DEBUG'];

function hue(s) { let h = 0; for (const c of s) h = (h * 31 + c.charCodeAt(0)) % 360; return h; }
const short = (n) => n.replace(/\.service$/, '');

function highlight(text, q) {
  if (!q) return text;
  const parts = [];
  const lo = text.toLowerCase(), ql = q.toLowerCase();
  let i = 0, j;
  while ((j = lo.indexOf(ql, i)) !== -1) {
    parts.push(text.slice(i, j), html`<mark>${text.slice(j, j + q.length)}</mark>`);
    i = j + q.length;
  }
  parts.push(text.slice(i));
  return parts;
}

function SourcePicker({ units, setUnits, kernel, setKernel }) {
  const [open, setOpen] = useState(false);
  const [svc, setSvc] = useState(null);
  const [q, setQ] = useState('');
  const ref = useRef();
  useEffect(() => { if (open && !svc) api('/services').then(setSvc).catch(() => {}); }, [open]);
  useEffect(() => {
    const h = (e) => { if (ref.current && !ref.current.contains(e.target)) setOpen(false); };
    document.addEventListener('mousedown', h);
    return () => document.removeEventListener('mousedown', h);
  }, []);
  const toggle = (n) => setUnits(units.includes(n) ? units.filter(u => u !== n) : [...units, n]);
  const f = (s) => !q || s.name.toLowerCase().includes(q.toLowerCase());
  return html`<div class="picker" ref=${ref}>
    <button class="btn" onClick=${() => setOpen(!open)}><${Icon} name="plus"/>Add source</button>
    ${open && html`<div class="picker-menu">
      <input class="input" style="width:100%;margin-bottom:4px" placeholder="Filter…" value=${q} onInput=${e => setQ(e.target.value)} ref=${el => el && !el._f && (el._f = 1, el.focus())}/>
      <label class="opt"><input type="checkbox" checked=${kernel} onChange=${() => setKernel(!kernel)}/>Kernel messages (dmesg)</label>
      ${!svc ? html`<div class="opt"><span class="spinner"></span></div>` : html`
        <div class="grp">My services</div>
        ${svc.custom.filter(f).map(s => html`<label class="opt"><input type="checkbox" checked=${units.includes(s.name)} onChange=${() => toggle(s.name)}/>${short(s.name)}</label>`)}
        ${!svc.custom.length && html`<div class="opt faint">None</div>`}
        <div class="grp">System services</div>
        ${svc.system.filter(f).slice(0, 200).map(s => html`<label class="opt"><input type="checkbox" checked=${units.includes(s.name)} onChange=${() => toggle(s.name)}/><span class="mono" style="font-size:12px">${short(s.name)}</span></label>`)}`}
    </div>`}
  </div>`;
}

export function Logs({ params }) {
  const [units, setUnitsRaw] = useState(params.units ? params.units.split(',').filter(Boolean) : []);
  const [kernel, setKernel] = useState(params.kernel === '1');
  const [prio, setPrio] = useState('');
  const [since, setSince] = useState('');
  const [search, setSearch] = useState('');
  const [entries, setEntries] = useState([]);
  const [paused, setPaused] = useState(false);
  const [pending, setPending] = useState(0);
  const [conn, setConn] = useState('connecting');
  const [error, setError] = useState(null);
  const [follow, setFollow] = useState(true);
  const view = useRef();
  const buf = useRef([]);
  const pausedRef = useRef(false);
  pausedRef.current = paused;

  const setUnits = (u) => { setUnitsRaw(u); go('logs', { units: u.join(','), kernel: kernel ? '1' : '' }); };

  const query = { units: units.join(','), kernel: kernel ? 'true' : '', priority: prio, since, lines: since ? 5000 : 300 };
  const key = qs(query);

  useEffect(() => {
    buf.current = [];
    setEntries([]);
    setPending(0);
    setError(null);
    let flushT = null;
    const flush = () => {
      flushT = null;
      if (pausedRef.current) { setPending(buf.current.length); return; }
      const add = buf.current; buf.current = [];
      setPending(0);
      setEntries(prev => { const n = prev.concat(add); return n.length > MAX ? n.slice(n.length - MAX) : n; });
    };
    const s = socket('/ws/logs?' + key, {
      onState: (st) => { setConn(st); if (st === 'open') { buf.current = []; setEntries([]); } },
      onMessage: (m) => {
        if (m.type === 'entries') {
          buf.current.push(...m.entries);
          if (buf.current.length > MAX) buf.current = buf.current.slice(-MAX);
          if (!flushT) flushT = setTimeout(flush, 150);
        } else if (m.type === 'end' && m.error) setError(m.error);
      },
    });
    const resume = () => flush();
    window.addEventListener('dp-logs-resume', resume);
    return () => { s.close(); clearTimeout(flushT); window.removeEventListener('dp-logs-resume', resume); };
  }, [key]);

  useEffect(() => { if (!paused) window.dispatchEvent(new Event('dp-logs-resume')); }, [paused]);

  useEffect(() => {
    if (follow && view.current) view.current.scrollTop = view.current.scrollHeight;
  }, [entries, follow]);

  const onScroll = () => {
    const el = view.current;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
    if (atBottom !== follow) setFollow(atBottom);
  };

  const shown = useMemo(() => {
    if (!search) return entries;
    const q = search.toLowerCase();
    return entries.filter(e => e.msg.toLowerCase().includes(q) || e.id.toLowerCase().includes(q) || e.unit.toLowerCase().includes(q));
  }, [entries, search]);

  const multi = units.length !== 1 || kernel;
  const dlQuery = qs({ ...query, lines: 20000 });

  return html`<div class="page-full">
    <div class="page-head" style="margin-bottom:10px">
      <h1>Logs</h1>
      <span class="pill ${conn === 'open' ? 'ok' : 'warn'}"><span class="dot ${conn === 'open' ? 'ok' : 'warn'}"></span>${conn === 'open' ? (paused ? 'paused' : 'live') : 'connecting'}</span>
      <span class="grow"></span>
      <span class="faint num" style="font-size:12px">${search ? `${shown.length} / ` : ''}${entries.length} lines</span>
    </div>

    <div class="toolbar">
      <div class="chips">
        ${units.length === 0 && !kernel && html`<span class="chip" style="padding-right:10px">All system logs</span>`}
        ${units.map(u => html`<span class="chip"><span class="dot" style=${{ background: `hsl(${hue(u)} 70% 60%)` }}></span>${short(u)}<button onClick=${() => setUnits(units.filter(x => x !== u))}>×</button></span>`)}
        ${kernel && html`<span class="chip">kernel<button onClick=${() => setKernel(false)}>×</button></span>`}
      </div>
      <${SourcePicker} units=${units} setUnits=${setUnits} kernel=${kernel} setKernel=${setKernel} />
      <span class="grow"></span>
      <select class="input" value=${prio} onChange=${e => setPrio(e.target.value)}>${PRIOS.map(p => html`<option value=${p.v}>${p.label}</option>`)}</select>
      <select class="input" value=${since} onChange=${e => setSince(e.target.value)}>${SINCE.map(p => html`<option value=${p.v}>${p.label}</option>`)}</select>
    </div>
    <div class="toolbar">
      <div class="row grow" style="position:relative;min-width:180px">
        <input class="input grow" style="padding-left:30px" placeholder="Search in logs…" value=${search} onInput=${e => setSearch(e.target.value)} />
        <span style="position:absolute;left:9px;top:8px;color:var(--faint)"><${Icon} name="search" cls="ficon"/></span>
      </div>
      <button class="btn ${paused ? 'on' : ''}" onClick=${() => setPaused(!paused)}>
        <${Icon} name=${paused ? 'play' : 'pause'}/>${paused ? `Resume${pending ? ` (${pending} new)` : ''}` : 'Pause'}</button>
      <button class="btn" onClick=${() => { setEntries([]); buf.current = []; }} title="Clear screen"><${Icon} name="trash"/><span class="hide-sm">Clear</span></button>
      <a class="btn" href=${'/api/logs/download?' + dlQuery}><${Icon} name="download"/><span class="hide-sm">Download</span></a>
    </div>

    ${error && html`<div class="banner">${error}</div>`}

    <div class="log-view" ref=${view} onScroll=${onScroll}>
      ${shown.length === 0 && html`<div class="empty">${conn === 'open' ? 'Waiting for log entries…' : 'Connecting…'}</div>`}
      ${shown.map((e, i) => html`<div class="log-line p${e.p}" key=${i}>
        <span class="ts">${fmtTime(e.ts, since === 'boot' || since === 'today')}</span>
        ${multi && html`<span class="src" style=${{ color: `hsl(${hue(e.unit || e.id)} 70% 65%)` }} title=${e.unit || e.id}>${short(e.unit || e.id)}</span>`}
        <span class="msg">${e.p <= 4 && html`<b>${PNAME[e.p]} </b>`}${!multi && e.id ? html`<span class="faint">${e.id}: </span>` : ''}${highlight(e.msg, search)}</span>
      </div>`)}
    </div>
    ${!follow && html`<button class="btn primary" style="position:fixed;right:28px;bottom:84px;z-index:20" onClick=${() => setFollow(true)}><${Icon} name="download"/>Jump to latest</button>`}
  </div>`;
}
