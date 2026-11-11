import { html, useState, useEffect, useRef } from '../../vendor/preact-htm.js';
import { api, qs } from '../api.js';
import { Icon, toast, confirm } from '../ui.js';

let xtermP = null;
function loadXterm() {
  if (!xtermP) xtermP = new Promise((res, rej) => {
    const l = document.createElement('link'); l.rel = 'stylesheet'; l.href = 'vendor/xterm.css'; document.head.appendChild(l);
    const add = (src) => new Promise((ok, bad) => { const s = document.createElement('script'); s.src = src; s.onload = ok; s.onerror = bad; document.head.appendChild(s); });
    add('vendor/xterm.js').then(() => add('vendor/xterm-addon-fit.js')).then(() => res(window)).catch(rej);
  });
  return xtermP;
}

const THEME = { background: '#080b0f', foreground: '#e6edf3', cursor: '#22d3ee', selectionBackground: '#22d3ee55',
  black: '#0b0f14', brightBlack: '#5c6878', blue: '#60a5fa', brightBlue: '#93c5fd', cyan: '#22d3ee', green: '#34d399',
  red: '#f87171', yellow: '#fbbf24', magenta: '#c084fc', white: '#e6edf3' };

// One xterm bound to one server session; stays mounted (hidden) when not active.
function Term({ sid, active, onId, onExit }) {
  const el = useRef();
  const state = useRef({});
  useEffect(() => {
    let disposed = false;
    loadXterm().then((w) => {
      if (disposed) return;
      const term = new w.Terminal({ fontFamily: 'ui-monospace, "JetBrains Mono", Menlo, Consolas, monospace', fontSize: 13.5,
        cursorBlink: true, scrollback: 5000, theme: THEME, allowProposedApi: true });
      const fit = new w.FitAddon.FitAddon();
      term.loadAddon(fit);
      term.open(el.current);
      try { fit.fit(); } catch {}
      state.current = { term, fit };
      let id = sid;
      // raw socket for binary frames
      const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
      let ws, closed = false, retry = 500, first = true;
      const open = () => {
        ws = new WebSocket(`${proto}//${location.host}/ws/terminal?${qs({ session: id, cols: term.cols, rows: term.rows })}`);
        ws.binaryType = 'arraybuffer';
        ws.onmessage = (e) => {
          if (typeof e.data === 'string') {
            const m = JSON.parse(e.data);
            if (m.t === 'hello') {
              if (!first) term.reset(); // server replays its buffer on every attach
              first = false; retry = 500;
              if (!id) { id = m.id; onId(m.id); }
            }
            else if (m.t === 'exit') { term.write('\r\n\x1b[33m[session ended]\x1b[0m\r\n'); closed = true; onExit(); }
            else if (m.t === 'error') { term.write(`\r\n\x1b[31m${m.error}\x1b[0m\r\n`); closed = true; }
          } else term.write(new Uint8Array(e.data));
        };
        ws.onclose = () => { if (!closed && !disposed) { term.write('\r\n\x1b[2m[connection lost, reconnecting…]\x1b[0m'); setTimeout(open, retry); retry = Math.min(retry * 2, 8000); } };
      };
      open();
      const send = (o) => ws && ws.readyState === 1 && ws.send(JSON.stringify(o));
      term.onData(d => send({ t: 'in', d }));
      term.onResize(({ cols, rows }) => send({ t: 'resize', cols, rows }));
      state.current.send = send;
      const ro = new ResizeObserver(() => { try { fit.fit(); } catch {} });
      ro.observe(el.current);
      state.current.cleanup = () => { closed = true; ro.disconnect(); ws && ws.close(); term.dispose(); };
    }).catch(() => toast('Failed to load terminal library', 'bad'));
    return () => { disposed = true; state.current.cleanup && state.current.cleanup(); };
  }, []);
  useEffect(() => {
    if (active && state.current.term) { setTimeout(() => { try { state.current.fit.fit(); } catch {} state.current.term.focus(); }, 0); }
  }, [active]);
  return html`<div class="term-host" style=${{ display: active ? 'block' : 'none' }} ref=${el}>${active && html`<${KeyBar} send=${(d) => state.current.send && state.current.send({ t: 'in', d })} />`}</div>`;
}

// extra keys for phones / tablets
function KeyBar({ send }) {
  const keys = [['Esc', '\x1b'], ['Tab', '\t'], ['Ctrl-C', '\x03'], ['Ctrl-D', '\x04'], ['↑', '\x1b[A'], ['↓', '\x1b[B'], ['←', '\x1b[D'], ['→', '\x1b[C']];
  return html`<div class="keybar">${keys.map(([l, d]) => html`<button class="btn sm" onMouseDown=${e => { e.preventDefault(); send(d); }}>${l}</button>`)}</div>`;
}

export function Terminal() {
  const [tabs, setTabs] = useState(null); // [{key, id, label, ended}]
  const [active, setActive] = useState(null);
  const [enabled, setEnabled] = useState(true);
  const n = useRef(0);

  useEffect(() => {
    api('/terminal/sessions').then(d => {
      setEnabled(d.enabled);
      const alive = d.sessions.filter(s => s.alive);
      const t = alive.map(s => ({ key: 'k' + (++n.current), id: s.id, label: `Shell ${n.current}`, owner: s.user, created: s.created }));
      if (!t.length && d.enabled) t.push({ key: 'k' + (++n.current), id: null, label: `Shell ${n.current}` });
      setTabs(t); setActive(t[0]?.key);
    }).catch(e => toast(e.message, 'bad'));
  }, []);

  const add = () => { const k = 'k' + (++n.current); setTabs(t => [...t, { key: k, id: null, label: `Shell ${n.current}` }]); setActive(k); };
  const close = async (tab) => {
    if (!tab.ended && !(await confirm({ title: `Close ${tab.label}?`, body: 'The shell and anything running in it will be stopped.', ok: 'Close', danger: true }))) return;
    if (tab.id) api('/terminal/sessions/' + tab.id, { method: 'DELETE' }).catch(() => {});
    setTabs(ts => { const r = ts.filter(x => x.key !== tab.key); if (active === tab.key) setActive(r[r.length - 1]?.key); return r; });
  };
  const patch = (key, p) => setTabs(ts => ts.map(x => x.key === key ? { ...x, ...p } : x));

  if (!tabs) return html`<div class="page"><div class="empty"><span class="spinner"></span></div></div>`;
  if (!enabled) return html`<div class="page"><div class="page-head"><h1>Terminal</h1></div><div class="banner warn">The terminal is disabled in config.yaml (terminal.enabled).</div></div>`;

  return html`<div class="page-full">
    <div class="page-head" style="margin-bottom:8px">
      <h1>Terminal</h1>
      <div class="term-tabs grow">
        ${tabs.map(t => html`<div class="term-tab ${t.key === active ? 'active' : ''}" onClick=${() => setActive(t.key)}>
          <span class="dot ${t.ended ? '' : 'ok'}"></span>${t.label}${t.owner ? html` <span class="faint" style="font-size:11px">${t.owner.split('@')[0]}</span>` : ''}
          <button title="Close" onClick=${e => { e.stopPropagation(); close(t); }}>×</button></div>`)}
        <button class="btn sm" onClick=${add} title="New terminal"><${Icon} name="plus"/></button>
      </div>
    </div>
    <div class="term-area">
      ${tabs.length === 0 && html`<div class="empty">No terminals. <button class="btn" onClick=${add}>Open one</button></div>`}
      ${tabs.map(t => html`<${Term} key=${t.key} sid=${t.id} active=${t.key === active}
        onId=${(id) => patch(t.key, { id })} onExit=${() => patch(t.key, { ended: true })} />`)}
    </div>
    <div class="faint" style="font-size:12px;margin-top:6px">Shells run as the panel user and keep running if your connection drops; reopen this page to reattach. Closed tabs end the shell.</div>
  </div>`;
}
