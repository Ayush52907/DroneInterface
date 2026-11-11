import { html, useState, useEffect, useRef } from '../../vendor/preact-htm.js';
import { api } from '../api.js';
import { Icon, Drawer, AddPF, toast, fmtBytes } from '../ui.js';

const shortType = (t) => (t || '').replace('/msg/', '/');
const UNKNOWN = '_NODE_NAMESPACE_UNKNOWN_/_NODE_NAME_UNKNOWN_';
const nodeLabel = (n) => n === UNKNOWN ? '(discovering…)' : n;

function Echo({ topic, onClose }) {
  const [d, setD] = useState(null);
  const [busy, setBusy] = useState(false);
  const load = async () => {
    setBusy(true);
    try { setD(await api('/ros/echo', { body: { topic } })); } catch (e) { setD({ error: e.message }); }
    setBusy(false);
  };
  useEffect(() => { load(); }, [topic]);
  return html`<${Drawer} title=${topic} onClose=${onClose}
      head=${html`<button class="btn sm" disabled=${busy} onClick=${load}>${busy ? html`<span class="spinner"></span>` : html`<${Icon} name="refresh"/>`}Latest message</button>`}>
    ${!d ? html`<div class="empty"><span class="spinner"></span> Waiting for a message…</div>`
      : d.error ? html`<div class="banner">${d.error}</div>`
      : html`<div class="faint mono">${d.type}</div>
        ${d.message === null ? html`<div class="banner warn">${d.note}</div>`
          : html`<pre class="pre" style="max-height:none">${JSON.stringify(d.message, null, 2)}</pre>
            <div class="faint" style="font-size:12px">Long arrays are cut to 48 items.</div>`}`}
  <//>`;
}

export function Ros() {
  const [status, setStatus] = useState(null);
  const [g, setG] = useState(null);
  const [err, setErr] = useState(null);
  const [watch, setWatch] = useState(new Set());
  const [rates, setRates] = useState({});
  const [echo, setEcho] = useState(null);
  const [q, setQ] = useState('');
  const [loading, setLoading] = useState(false);
  const watchRef = useRef(watch); watchRef.current = watch;

  const loadGraph = async () => {
    setLoading(true);
    try { setG(await api('/ros/graph')); setErr(null); } catch (e) { setErr(e.message); }
    setLoading(false);
  };
  useEffect(() => {
    api('/ros/status').then(s => { setStatus(s); if (s.available) loadGraph(); }).catch(e => setErr(e.message));
  }, []);
  useEffect(() => {
    if (!status?.available) return;
    const t = setInterval(loadGraph, 5000);
    return () => clearInterval(t);
  }, [status]);
  useEffect(() => {
    if (!status?.available) return;
    api('/ros/watch', { body: { topics: [...watch] } }).then(r => setRates(r.rates || {})).catch(e => toast(e.message, 'bad'));
    if (!watch.size) return;
    const t = setInterval(() => api('/ros/rates').then(setRates).catch(() => {}), 1000);
    return () => clearInterval(t);
  }, [watch, status]);
  // stop measuring when leaving the page
  useEffect(() => () => { if (watchRef.current.size) api('/ros/watch', { body: { topics: [] } }).catch(() => {}); }, []);

  const toggleWatch = (t) => setWatch(w => { const n = new Set(w); n.has(t) ? n.delete(t) : n.add(t); return n; });

  if (!status) return html`<div class="page"><div class="empty">${err ? html`<div class="banner">${err}</div>` : html`<span class="spinner"></span>`}</div></div>`;
  if (!status.available) return html`<div class="page"><div class="page-head"><h1>ROS 2</h1></div>
    <div class="banner warn">ROS 2 was not found. Set <span class="mono">ros.setup</span> in config.yaml to your setup.bash files (missing: ${status.missing.join(', ') || 'none configured'}).</div></div>`;

  const topics = g ? g.topics.filter(t => !q || t.name.toLowerCase().includes(q.toLowerCase())) : [];
  const orphans = g ? g.topics.filter(t => t.subs.length > 0 && t.pubs.length === 0 && !t.name.startsWith('/parameter')) : [];

  return html`<div class="page">
    <div class="page-head">
      <h1>ROS 2</h1>
      ${g && html`<span class="pill">${g.nodes.length} nodes</span><span class="pill">${g.topics.length} topics</span>`}
      <span class="grow"></span>
      <input class="input" style="width:200px" placeholder="Filter topics…" value=${q} onInput=${e => setQ(e.target.value)} />
      <button class="btn" disabled=${loading} onClick=${loadGraph}>${loading ? html`<span class="spinner"></span>` : html`<${Icon} name="refresh"/>`}</button>
    </div>
    ${status.missing.length > 0 && html`<div class="banner warn">Not sourced (file missing): ${status.missing.join(', ')}</div>`}
    ${err && html`<div class="banner">${err}</div>`}
    ${!g && !err && html`<div class="empty"><span class="spinner"></span> Starting ROS 2 probe and discovering the graph (a few seconds)…</div>`}

    ${g && html`
      ${orphans.length > 0 && html`<div class="banner warn"><div><b>Topics with subscribers but no publisher:</b> ${orphans.map(t => html`<span class="mono" style="margin-right:10px">${t.name}</span>`)}
        <div style="font-size:12px;opacity:.8">Something is waiting for data that nothing sends. Usually the publishing node isn't running or has crashed.</div></div></div>`}

      <div class="grid ros-layout">
        <div class="card">
          <h3>Nodes</h3>
          <div class="list-rows">
            ${g.nodes.length === 0 && html`<div class="muted">No nodes found yet</div>`}
            ${g.nodes.map(n => html`<div><span class="dot ok"></span><span class="mono grow ellipsis">${n.full}</span>
              <${AddPF} check=${{ type: 'ros_node', node: n.full, name: `ROS node ${n.full}` }} label="" /></div>`)}
          </div>
          <div class="faint" style="font-size:12px;margin-top:10px">Graph refreshes every 5 s. Nodes in other ROS_DOMAIN_IDs are not visible.</div>
        </div>

        <div class="card" style="padding:0;overflow:auto">
          <table class="table">
            <thead><tr><th>Topic</th><th class="hide-sm">Publishers → Subscribers</th><th style="text-align:right">Rate</th><th></th></tr></thead>
            <tbody>${topics.map(t => {
              const r = rates[t.name];
              const watching = watch.has(t.name);
              return html`<tr key=${t.name}>
                <td><div class="mono" style="font-size:13px">${t.name}</div><div class="faint" style="font-size:12px">${t.types.map(shortType).join(', ')}</div></td>
                <td class="hide-sm" style="font-size:12.5px">
                  <span class=${t.pubs.length ? '' : 'bad'}>${t.pubs.length ? t.pubs.map(p => nodeLabel(p.node)).join(', ') : 'no publisher'}</span>
                  <span class="faint"> → </span>
                  <span class=${t.subs.length ? '' : 'faint'}>${t.subs.length ? t.subs.map(p => nodeLabel(p.node)).join(', ') : 'none'}</span></td>
                <td class="num" style="text-align:right;white-space:nowrap">${watching ? (r ? html`<b class=${r.hz > 0 ? 'ok' : 'warn'}>${r.hz} Hz</b><div class="faint" style="font-size:11px">${fmtBytes(r.bw)}/s${r.last_age !== null ? ` · ${r.last_age}s ago` : ''}</div>` : html`<span class="spinner"></span>`) : html`<span class="faint">–</span>`}</td>
                <td><div class="row" style="gap:2px;justify-content:flex-end">
                  <button class="btn ghost sm ${watching ? 'on' : ''}" title="Measure rate" onClick=${() => toggleWatch(t.name)}><${Icon} name="activity"/></button>
                  <button class="btn ghost sm" title="Show latest message" onClick=${() => setEcho(t.name)}><${Icon} name="eye"/></button>
                  <${AddPF} check=${{ type: 'ros_topic', topic: t.name, min_hz: r && r.hz > 0 ? Math.max(1, Math.floor(r.hz * 0.7)) : 0, name: `ROS topic ${t.name}` }} label="" />
                </div></td>
              </tr>`;
            })}</tbody>
          </table>
        </div>
      </div>
      <div class="faint" style="font-size:12px;margin-top:8px">
        <${Icon} name="activity" cls="ficon"/> measures the publish rate (best-effort QoS, no decoding, so it's cheap even for images).
        Adding a measured topic to preflight sets its minimum to 70% of the current rate.
      </div>`}
    ${echo && html`<${Echo} topic=${echo} onClose=${() => setEcho(null)} />`}
  </div>`;
}
