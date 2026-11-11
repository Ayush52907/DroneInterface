import { html, useState, useEffect, useRef } from '../vendor/preact-htm.js';

// ---------------------------------------------------------------- formatters
export function fmtBytes(n, digits = 1) {
  if (n === null || n === undefined || isNaN(n)) return '–';
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (Math.abs(n) >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i === 0 ? 0 : digits)} ${u[i]}`;
}
export const fmtRate = (n) => (n === null || n === undefined) ? '–' : fmtBytes(n) + '/s';
export function fmtDur(s) {
  if (s === null || s === undefined) return '–';
  s = Math.floor(s);
  const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
  if (d) return `${d}d ${h}h`;
  if (h) return `${h}h ${m}m`;
  if (m) return `${m}m ${s % 60}s`;
  return `${s}s`;
}
export function fmtTime(ts, withDate = false) {
  const d = new Date(ts * 1000);
  const t = d.toLocaleTimeString([], { hour12: false });
  return withDate ? `${d.toLocaleDateString([], { month: 'short', day: 'numeric' })} ${t}` : t;
}
export function fmtAgo(ts) {
  const s = Date.now() / 1000 - ts;
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  if (s < 86400 * 30) return `${Math.floor(s / 86400)} d ago`;
  return new Date(ts * 1000).toLocaleDateString();
}
export const level = (v, warn, bad) => v === null || v === undefined ? '' : v >= bad ? 'bad' : v >= warn ? 'warn' : '';

// ---------------------------------------------------------------- icons
const P = {
  dash: 'M3 13h8V3H3zm10 8h8V11h-8zM3 21h8v-6H3zm10-18v6h8V3z',
  services: 'M12 15.5A3.5 3.5 0 1 1 12 8.5a3.5 3.5 0 0 1 0 7m7.4-2.5a7.6 7.6 0 0 0 0-2l2.1-1.6-2-3.4-2.5 1a7.4 7.4 0 0 0-1.7-1L15 3h-4l-.4 2.6a7.4 7.4 0 0 0-1.7 1l-2.4-1-2 3.4L6.6 11a7.6 7.6 0 0 0 0 2l-2.1 1.6 2 3.4 2.5-1a7.4 7.4 0 0 0 1.7 1L11 21h4l.4-2.6a7.4 7.4 0 0 0 1.7-1l2.4 1 2-3.4z',
  logs: 'M4 5h16M4 9h16M4 13h10M4 17h13M4 21h7',
  files: 'M3 6a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z',
  activity: 'M3 12h4l3-8 4 16 3-8h4',
  file: 'M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9zM14 3v6h6',
  play: 'M7 5v14l11-7z',
  stop: 'M6 6h12v12H6z',
  restart: 'M4 12a8 8 0 1 0 2.3-5.6M4 4v5h5',
  upload: 'M12 16V4m0 0L7 9m5-5 5 5M4 20h16',
  download: 'M12 4v12m0 0-5-5m5 5 5-5M4 20h16',
  plus: 'M12 5v14M5 12h14',
  trash: 'M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3',
  edit: 'M4 20h4L19 9l-4-4L4 16zM14 6l4 4',
  copy: 'M9 9h11v11H9zM5 15H4V4h11v1',
  move: 'M5 12h14m0 0-5-5m5 5-5 5',
  x: 'M6 6l12 12M18 6 6 18',
  refresh: 'M20 12a8 8 0 1 1-2.3-5.6M20 4v5h-5',
  pause: 'M7 5h3v14H7zm7 0h3v14h-3z',
  search: 'M11 18a7 7 0 1 1 0-14 7 7 0 0 1 0 14m10 3-5.2-5.2',
  eye: 'M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12m10 3a3 3 0 1 0 0-6 3 3 0 0 0 0 6',
  eyeoff: 'M3 3l18 18M10.6 5.1A10 10 0 0 1 12 5c6 0 10 7 10 7a17 17 0 0 1-3.2 3.9M6.6 6.6C3.9 8.3 2 12 2 12s4 7 10 7a9.8 9.8 0 0 0 5.4-1.6M9.9 9.9a3 3 0 0 0 4.2 4.2',
  left: 'M15 18l-6-6 6-6',
  right: 'M9 6l6 6-6 6',
  camera: 'M3 8a2 2 0 0 1 2-2h2l2-3h6l2 3h2a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2zm9 9a4 4 0 1 0 0-8 4 4 0 0 0 0 8z',
  data: 'M4 6c0-1.7 3.6-3 8-3s8 1.3 8 3-3.6 3-8 3-8-1.3-8-3zm0 0v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3',
  grid: 'M4 4h7v7H4zm9 0h7v7h-7zM4 13h7v7H4zm9 0h7v7h-7z',
  bolt: 'M13 2 4 14h7l-1 8 9-12h-7z',
  map: 'M9 4 3 6v14l6-2 6 2 6-2V4l-6 2zm0 0v14m6-12v14',
  payload: 'M12 3v6m0 0-3-3m3 3 3-3M5 13h14l-2 8H7zM9 13V11h6v2',
  target: 'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zm0-5a4 4 0 1 0 0-8 4 4 0 0 0 0 8zm0-14v3m0 14v3M2 12h3m14 0h3',
  zoomin: 'M11 18a7 7 0 1 1 0-14 7 7 0 0 1 0 14zm10 3-5.2-5.2M11 8v6M8 11h6',
  zoomout: 'M11 18a7 7 0 1 1 0-14 7 7 0 0 1 0 14zm10 3-5.2-5.2M8 11h6',
  record: 'M12 17a5 5 0 1 0 0-10 5 5 0 0 0 0 10z',
  expand: 'M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5',
  tail: 'M4 6h16M4 12h16M4 18h9m4-2 3 3 3-3',
  home: 'M3 11 12 3l9 8v9a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z',
  star: 'M12 3l2.7 5.6 6.1.9-4.4 4.3 1 6.1L12 17l-5.4 2.9 1-6.1L3.2 9.5l6.1-.9z',
  up: 'M12 19V5m0 0-6 6m6-6 6 6',
  link: 'M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1',
  more: 'M5 12h.01M12 12h.01M19 12h.01',
  power: 'M12 3v9M6.3 6.3a8 8 0 1 0 11.4 0',
  check: 'M5 12l5 5L20 7',
  chip: 'M9 3v2M15 3v2M9 19v2M15 19v2M3 9h2M3 15h2M19 9h2M19 15h2M7 5h10a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2zm3 5h4v4h-4z',
  preflight: 'M9 12l2 2 4-4M12 3l7 3v6c0 4.5-3 7.5-7 9-4-1.5-7-4.5-7-9V6z',
  ros: 'M12 5a2 2 0 1 0 0-4 2 2 0 0 0 0 4zm-7 9a2 2 0 1 0 0-4 2 2 0 0 0 0 4zm14 0a2 2 0 1 0 0-4 2 2 0 0 0 0 4zm-7 9a2 2 0 1 0 0-4 2 2 0 0 0 0 4zM12 5v14M6.7 11.3 10.5 7M17.3 11.3 13.5 7M6.7 12.7l3.8 4.3M17.3 12.7l-3.8 4.3',
  terminal: 'M4 5h16a1 1 0 0 1 1 1v12a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1zm3 4 3 3-3 3m5 0h5',
  usb: 'M12 2v14m0 0a3 3 0 1 0 0 6 3 3 0 0 0 0-6zM12 6l-4 4v2m4-5 4 3v2M7 12h2v2H7zm8-2h2v2h-2z',
  wifi: 'M2 9a15 15 0 0 1 20 0M5 12.5a10 10 0 0 1 14 0M8.5 16a5 5 0 0 1 7 0M12 20h.01',
  plug: 'M9 2v6m6-6v6M6 8h12v3a6 6 0 0 1-12 0zm6 9v5',
};
export function Icon({ name, cls = '' }) {
  return html`<svg class=${cls} viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d=${P[name] || ''}/></svg>`;
}

// ---------------------------------------------------------------- charts
export function Spark({ data, max, color = 'var(--accent)', h = 40 }) {
  const vals = data.filter(v => v !== null && v !== undefined);
  if (vals.length < 2) return html`<svg class="spark" style=${{ height: h + 'px' }}></svg>`;
  const W = 300, n = data.length;
  const top = max ?? Math.max(...vals, 1) * 1.15;
  const pts = data.map((v, i) => [i / (n - 1) * W, h - 2 - ((v ?? 0) / top) * (h - 4)]);
  const line = pts.map((p, i) => (i ? 'L' : 'M') + p[0].toFixed(1) + ' ' + p[1].toFixed(1)).join('');
  return html`<svg class="spark" viewBox="0 0 ${W} ${h}" preserveAspectRatio="none" style=${{ height: h + 'px' }}>
    <path d="${line}L${W} ${h}L0 ${h}Z" fill=${color} opacity=".12"/>
    <path d=${line} fill="none" stroke=${color} stroke-width="1.6" vector-effect="non-scaling-stroke"/>
  </svg>`;
}
export function Bar({ pct, cls }) {
  const p = Math.max(0, Math.min(100, pct || 0));
  return html`<div class="bar ${cls || level(p, 75, 90)}"><i style=${{ width: p + '%' }}></i></div>`;
}

// ---------------------------------------------------------------- global toasts / dialogs
const listeners = new Set();
let state = { toasts: [], dialog: null };
const emit = () => listeners.forEach(f => f(state));
let tid = 0;
export function toast(msg, kind = '') {
  const id = ++tid;
  state = { ...state, toasts: [...state.toasts, { id, msg, kind }] };
  emit();
  setTimeout(() => { state = { ...state, toasts: state.toasts.filter(t => t.id !== id) }; emit(); }, kind === 'bad' ? 7000 : 3500);
}
// confirm({title, body, ok, danger}) -> Promise<boolean>
export function confirm(opts) {
  return new Promise(resolve => { state = { ...state, dialog: { type: 'confirm', ...opts, resolve } }; emit(); });
}
// prompt({title, label, value, ok}) -> Promise<string|null>
export function prompt(opts) {
  return new Promise(resolve => { state = { ...state, dialog: { type: 'prompt', ...opts, resolve } }; emit(); });
}
function closeDialog(v) {
  const d = state.dialog;
  state = { ...state, dialog: null };
  emit();
  d && d.resolve(v);
}

export function GlobalLayer() {
  const [s, set] = useState(state);
  useEffect(() => { listeners.add(set); return () => listeners.delete(set); }, []);
  return html`
    <div class="toasts">${s.toasts.map(t => html`<div class="toast ${t.kind}" key=${t.id}>${t.msg}</div>`)}</div>
    ${s.dialog && html`<${Dialog} d=${s.dialog} />`}`;
}

function Dialog({ d }) {
  const [val, setVal] = useState(d.value || '');
  const submit = (e) => { e && e.preventDefault(); closeDialog(d.type === 'prompt' ? val : true); };
  useEffect(() => {
    const k = (e) => { if (e.key === 'Escape') closeDialog(d.type === 'prompt' ? null : false); };
    window.addEventListener('keydown', k);
    return () => window.removeEventListener('keydown', k);
  }, []);
  return html`<div class="overlay center" onClick=${(e) => e.target === e.currentTarget && closeDialog(d.type === 'prompt' ? null : false)}>
    <form class="modal" onSubmit=${submit}>
      <h2>${d.title}</h2>
      ${d.body && html`<div class="muted" style="white-space:pre-wrap">${d.body}</div>`}
      ${d.type === 'prompt' && html`<label class="muted" style="display:grid;gap:6px">${d.label}
        <input class="input" value=${val} onInput=${e => setVal(e.target.value)} ref=${el => el && !el._f && (el._f = 1, el.focus(), el.select())} /></label>`}
      <div class="row" style="justify-content:flex-end">
        <button type="button" class="btn" onClick=${() => closeDialog(d.type === 'prompt' ? null : false)}>Cancel</button>
        <button type="submit" class="btn ${d.danger ? 'danger solid' : 'primary'}" ref=${el => d.type === 'confirm' && el && !el._f && (el._f = 1, el.focus())}>${d.ok || 'OK'}</button>
      </div>
    </form>
  </div>`;
}

// Escape closes only the top-most modal/drawer
const layerStack = [];
function useEscape(onClose) {
  const me = useRef({});
  useEffect(() => {
    layerStack.push(me.current);
    return () => { const i = layerStack.indexOf(me.current); if (i >= 0) layerStack.splice(i, 1); };
  }, []);
  useEffect(() => {
    const k = (e) => { if (e.key === 'Escape' && layerStack[layerStack.length - 1] === me.current) { e.stopImmediatePropagation(); onClose(); } };
    window.addEventListener('keydown', k);
    return () => window.removeEventListener('keydown', k);
  }, [onClose]);
}

export function Modal({ title, onClose, children, wide, foot, head }) {
  useEscape(onClose);
  return html`<div class="overlay center" onClick=${(e) => e.target === e.currentTarget && onClose()}>
    <div class="modal ${wide ? 'wide' : ''}">
      <div class="row"><h2 class="grow ellipsis">${title}</h2>${head}<button class="btn ghost sm" onClick=${onClose}><${Icon} name="x"/></button></div>
      ${children}
      ${foot && html`<div class="row" style="justify-content:flex-end;flex-wrap:wrap">${foot}</div>`}
    </div>
  </div>`;
}

export function Drawer({ title, onClose, children, head }) {
  useEscape(onClose);
  return html`<div class="overlay" onClick=${(e) => e.target === e.currentTarget && onClose()}>
    <div class="drawer">
      <div class="drawer-head"><h2 class="grow ellipsis">${title}</h2>${head}<button class="btn ghost sm" onClick=${onClose}><${Icon} name="x"/></button></div>
      <div class="drawer-body">${children}</div>
    </div>
  </div>`;
}

export function AddPF({ check, label = 'Preflight' }) {
  return html`<button class="btn ghost sm" title="Add to preflight checklist" onClick=${async (e) => {
    e.stopPropagation();
    const { addPreflightCheck } = await import('./preflight-add.js');
    addPreflightCheck(check);
  }}><${Icon} name="preflight"/><span class="hide-sm">${label}</span></button>`;
}

export function StatePill({ active, sub }) {
  const cls = active === 'active' ? 'ok' : active === 'failed' ? 'bad' : (active === 'activating' || active === 'deactivating' || active === 'reloading') ? 'warn' : '';
  return html`<span class="pill ${cls}"><span class="dot ${cls}"></span>${sub && sub !== 'dead' ? sub : active}</span>`;
}

export function EnabledPill({ state }) {
  if (!state) return html`<span class="faint">–</span>`;
  const cls = state === 'enabled' ? 'info' : state === 'masked' ? 'bad' : '';
  return html`<span class="pill ${cls}">${state}</span>`;
}

// persisted UI preference (safe if storage is unavailable)
export function usePref(key, init) {
  const [v, setV] = useState(() => {
    try { const s = localStorage.getItem('dp.' + key); return s === null ? init : JSON.parse(s); } catch { return init; }
  });
  const set = (nv) => {
    setV(prev => {
      const val = typeof nv === 'function' ? nv(prev) : nv;
      try { localStorage.setItem('dp.' + key, JSON.stringify(val)); } catch {}
      return val;
    });
  };
  return [v, set];
}
