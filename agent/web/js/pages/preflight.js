import { html, useState, useEffect, useRef } from '../../vendor/preact-htm.js';
import { api } from '../api.js';
import { Icon, Modal, toast, confirm, fmtTime, usePref } from '../ui.js';

export const TYPE_INFO = {
  service: { label: 'Service running', fields: [['unit', 'Unit', 'rudra_camera.service']] },
  device: { label: 'Device file present', fields: [['path', 'Path', '/dev/ttyTHS1']] },
  serial_owner: { label: 'Serial port in use by a process', fields: [['path', 'Path', '/dev/ttyTHS1']] },
  usb: { label: 'USB device connected', fields: [['vid', 'Vendor ID', '10c4'], ['pid', 'Product ID', 'ea60']] },
  link: { label: 'Network link up', fields: [['iface', 'Interface', 'enP8p1s0']] },
  ping: { label: 'Host answers ping', fields: [['host', 'Host / IP', '192.168.144.25']] },
  tcp: { label: 'TCP port open', fields: [['host', 'Host / IP', '192.168.144.25'], ['port', 'Port', '8554', 'number']] },
  rtsp: { label: 'RTSP video server answering', fields: [['host', 'Host / IP', '192.168.144.25'], ['port', 'Port', '8554', 'number'], ['path', 'Path', '/main.264']] },
  ros_node: { label: 'ROS 2 node running', fields: [['node', 'Node', '/rudra_camera_node']] },
  ros_topic: { label: 'ROS 2 topic publishing', fields: [['topic', 'Topic', '/camera/image_raw'], ['min_hz', 'Minimum Hz (0 = any message)', '10', 'number']] },
  disk: { label: 'Free disk space', fields: [['path', 'Mount', '/'], ['min_gb', 'Minimum GB free', '5', 'number']] },
  temp: { label: 'Temperature below', fields: [['max_c', 'Max °C', '85', 'number']] },
  tailscale: { label: 'Tailscale connected', fields: [] },
};

const summary = (c) => (TYPE_INFO[c.type]?.fields || []).map(([k]) => c[k]).filter(v => v !== undefined && v !== '').join(' · ');

const VERDICT = {
  go: { cls: 'ok', title: 'ALL SYSTEMS GO', sub: 'Every check passed' },
  warn: { cls: 'warn', title: 'GO, WITH WARNINGS', sub: 'All critical checks passed' },
  nogo: { cls: 'bad', title: 'NO GO', sub: 'Critical checks failing' },
};

function StatusIcon({ s }) {
  const m = { pass: ['ok', 'check'], fail: ['bad', 'x'], unknown: ['warn', 'more'] }[s] || ['faint', 'more'];
  return html`<span class="pf-icon ${m[0]}"><${Icon} name=${m[1]}/></span>`;
}

function CheckEditor({ check, onSave, onClose }) {
  const [c, setC] = useState({ severity: 'critical', type: 'service', ...check });
  const info = TYPE_INFO[c.type];
  const set = (k, v) => setC(x => ({ ...x, [k]: v }));
  const submit = (e) => {
    e.preventDefault();
    const out = { ...c };
    for (const [k, , , kind] of info.fields) if (kind === 'number' && out[k] !== '' && out[k] !== undefined) out[k] = Number(out[k]);
    if (!out.name) out.name = info.label + (summary(out) ? ': ' + summary(out) : '');
    onSave(out);
  };
  return html`<${Modal} title=${check.id ? 'Edit check' : 'Add check'} onClose=${onClose}>
    <form style="display:grid;gap:12px" onSubmit=${submit} id="pf-form">
      <label class="field">Check type
        <select class="input" value=${c.type} onChange=${e => set('type', e.target.value)}>
          ${Object.entries(TYPE_INFO).map(([k, v]) => html`<option value=${k}>${v.label}</option>`)}
        </select></label>
      ${info.fields.map(([k, label, ph, kind]) => html`<label class="field">${label}
        <input class="input" type=${kind || 'text'} step="any" placeholder=${ph} value=${c[k] ?? ''} onInput=${e => set(k, e.target.value)} required=${!(c.type === 'rtsp' && k === 'path')} /></label>`)}
      <label class="field">Name (optional)
        <input class="input" placeholder=${info.label} value=${c.name || ''} onInput=${e => set('name', e.target.value)} /></label>
      <div class="row" style="gap:16px">
        <label class="row" style="gap:6px"><input type="radio" checked=${c.severity === 'critical'} onChange=${() => set('severity', 'critical')} />Critical <span class="faint">(blocks GO)</span></label>
        <label class="row" style="gap:6px"><input type="radio" checked=${c.severity === 'warning'} onChange=${() => set('severity', 'warning')} />Warning</label>
      </div>
      <div class="row" style="justify-content:flex-end"><button type="button" class="btn" onClick=${onClose}>Cancel</button><button class="btn primary">Save</button></div>
    </form>
  <//>`;
}

export function Preflight() {
  const [checks, setChecks] = useState(null);
  const [res, setRes] = useState(null);
  const [running, setRunning] = useState(false);
  const [auto, setAuto] = usePref('pf.auto', true);
  const [edit, setEdit] = useState(false);
  const [editing, setEditing] = useState(null);
  const busy = useRef(false);

  const loadChecks = () => api('/preflight/checks').then(d => setChecks(d.checks)).catch(e => toast(e.message, 'bad'));
  const run = async () => {
    if (busy.current) return;
    busy.current = true; setRunning(true);
    try { setRes(await api('/preflight/run', { method: 'POST', body: {} })); } catch (e) { toast(e.message, 'bad'); }
    busy.current = false; setRunning(false);
  };
  useEffect(() => { loadChecks().then(run); }, []);
  useEffect(() => {
    if (!auto || edit) return;
    const t = setInterval(run, 6000);
    return () => clearInterval(t);
  }, [auto, edit]);

  const saveAll = async (list) => {
    try {
      const d = await api('/preflight/checks', { method: 'PUT', body: { checks: list } });
      setChecks(d.checks); run();
    } catch (e) { toast(e.message, 'bad'); }
  };
  const upsert = (c) => { setEditing(null); saveAll(c.id ? checks.map(x => x.id === c.id ? c : x) : [...checks, c]); };
  const remove = async (c) => { if (await confirm({ title: `Remove "${c.name}"?`, ok: 'Remove', danger: true })) saveAll(checks.filter(x => x.id !== c.id)); };
  const move = (c, d) => {
    const i = checks.indexOf(c), j = i + d;
    if (j < 0 || j >= checks.length) return;
    const n = [...checks]; [n[i], n[j]] = [n[j], n[i]]; saveAll(n);
  };
  const toggle = (c) => saveAll(checks.map(x => x.id === c.id ? { ...x, enabled: !x.enabled } : x));

  if (!checks) return html`<div class="page"><div class="empty"><span class="spinner"></span></div></div>`;
  const byId = Object.fromEntries((res?.results || []).map(r => [r.id, r]));
  const v = res ? VERDICT[res.verdict] : null;

  const row = (c) => {
    const r = byId[c.id];
    return html`<div class="pf-row ${c.enabled === false ? 'disabled' : ''}" key=${c.id}>
      ${c.enabled === false ? html`<span class="pf-icon faint"><${Icon} name="pause"/></span>` : html`<${StatusIcon} s=${r?.status} />`}
      <div class="grow" style="min-width:0">
        <div style="font-weight:600">${c.name}</div>
        <div class="faint ellipsis" style="font-size:12px">${TYPE_INFO[c.type]?.label || c.type}${summary(c) ? ' · ' + summary(c) : ''}</div>
      </div>
      <div class="pf-detail ${r?.status === 'fail' ? 'bad' : r?.status === 'pass' ? 'muted' : 'warn'}">${c.enabled === false ? 'disabled' : r ? r.detail : '…'}</div>
      ${edit && html`<div class="row" style="gap:2px">
        <button class="btn ghost sm" title="Move up" onClick=${() => move(c, -1)}>↑</button>
        <button class="btn ghost sm" title="Move down" onClick=${() => move(c, 1)}>↓</button>
        <button class="btn ghost sm" title=${c.enabled === false ? 'Enable' : 'Disable'} onClick=${() => toggle(c)}><${Icon} name=${c.enabled === false ? 'play' : 'pause'}/></button>
        <button class="btn ghost sm" title="Edit" onClick=${() => setEditing(c)}><${Icon} name="edit"/></button>
        <button class="btn ghost sm" title="Remove" onClick=${() => remove(c)}><${Icon} name="trash"/></button></div>`}
    </div>`;
  };
  const crit = checks.filter(c => c.severity !== 'warning');
  const warn = checks.filter(c => c.severity === 'warning');

  return html`<div class="page" style="max-width:1000px">
    <div class="page-head">
      <h1>Preflight</h1>
      <span class="grow"></span>
      <label class="row muted" style="font-size:13px;gap:5px"><input type="checkbox" checked=${auto} onChange=${() => setAuto(!auto)} />Auto-refresh</label>
      <button class="btn ${edit ? 'on' : ''}" onClick=${() => setEdit(!edit)}><${Icon} name="edit"/>${edit ? 'Done' : 'Edit checklist'}</button>
      <button class="btn primary" disabled=${running} onClick=${run}>${running ? html`<span class="spinner"></span>` : html`<${Icon} name="refresh"/>`}Run now</button>
    </div>

    <div class="pf-hero ${v ? v.cls : ''}">
      ${v ? html`
        <div class="pf-verdict">${v.title}</div>
        <div>${v.sub} · <b>${res.counts.pass}/${res.counts.total}</b> passed${res.counts.critical_fail ? html` · <b>${res.counts.critical_fail}</b> critical failing` : ''}${res.counts.warning_fail ? html` · ${res.counts.warning_fail} warning(s)` : ''}</div>
        <div class="faint" style="font-size:12px">Checked ${fmtTime(res.t)} (${res.ms} ms)${auto && !edit ? ' · refreshes every 6 s' : ''}</div>` : html`<div class="pf-verdict muted">Running checks…</div>`}
    </div>

    ${edit && html`<div class="row" style="margin:14px 0"><button class="btn primary" onClick=${() => setEditing({})}><${Icon} name="plus"/>Add check</button>
      <span class="faint" style="font-size:12px">You can also add checks from the Services, Hardware and ROS 2 pages, using the <${Icon} name="preflight" cls="ficon"/> buttons.</span></div>`}

    <div class="card" style="margin-top:14px;padding:6px 16px">
      <h3 style="margin:10px 0 4px">Critical</h3>
      ${crit.length ? crit.map(row) : html`<div class="empty">No critical checks</div>`}
    </div>
    <div class="card" style="margin-top:14px;padding:6px 16px">
      <h3 style="margin:10px 0 4px">Warnings</h3>
      ${warn.length ? warn.map(row) : html`<div class="empty">No warning checks</div>`}
    </div>
    ${editing && html`<${CheckEditor} check=${editing} onSave=${upsert} onClose=${() => setEditing(null)} />`}
  </div>`;
}
