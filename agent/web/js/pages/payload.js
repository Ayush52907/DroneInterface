import { html, useState, useEffect } from '../../vendor/preact-htm.js';
import { api } from '../api.js';
import { Icon, Modal, toast, confirm, fmtAgo } from '../ui.js';

const BAUDS = [57600, 115200, 230400, 460800, 921600];
const newPos = (name, pwm, kind) => ({ name, pwm, kind });

function Editor({ data, onClose, onSaved }) {
  const [d, setD] = useState(JSON.parse(JSON.stringify({ port: data.port, baud: data.baud, configs: data.configs })));
  const [busy, setBusy] = useState(false);
  const setC = (i, patch) => setD(x => ({ ...x, configs: x.configs.map((c, j) => j === i ? { ...c, ...patch } : c) }));
  const setP = (i, k, patch) => setC(i, { positions: d.configs[i].positions.map((p, j) => j === k ? { ...p, ...patch } : p) });
  const moveC = (i, dl) => setD(x => { const n = [...x.configs], j = i + dl; if (j < 0 || j >= n.length) return x; [n[i], n[j]] = [n[j], n[i]]; return { ...x, configs: n }; });
  const save = async () => {
    setBusy(true);
    try { await api('/payload', { method: 'PUT', body: { config: d } }); toast('Payload configurations saved', 'ok'); onSaved(); onClose(); }
    catch (e) { toast(e.message, 'bad'); }
    setBusy(false);
  };
  const reset = async () => {
    if (!(await confirm({ title: 'Reset to the defaults from drop_drop.py?', body: 'Servos 11 and 12 with Low 1100 / Mid 1500 / High 1900. Your own configurations will be replaced.', ok: 'Reset', danger: true }))) return;
    await api('/payload/reset', { method: 'POST', body: {} }); onSaved(); onClose();
  };
  return html`<${Modal} wide title="Payload configurations" onClose=${onClose}
      foot=${html`<button class="btn ghost grow" style="justify-content:flex-start" onClick=${reset}>Reset to defaults</button>
        <button class="btn" onClick=${onClose}>Cancel</button><button class="btn primary" disabled=${busy} onClick=${save}>Save</button>`}>
    <div style="overflow:auto;min-height:0;display:grid;gap:12px;align-content:start">
      <div class="card" style="padding:12px">
        <b style="font-size:13px">Flight controller connection</b>
        <div class="row" style="gap:10px;margin-top:8px;flex-wrap:wrap">
          <label class="field grow">Serial port<input class="input mono" value=${d.port} onInput=${e => setD(x => ({ ...x, port: e.target.value }))} /></label>
          <label class="field">Baud<select class="input" value=${String(d.baud)} onChange=${e => setD(x => ({ ...x, baud: +e.target.value }))}>
            ${BAUDS.map(b => html`<option value=${String(b)} selected=${d.baud === b}>${b}</option>`)}</select></label>
        </div>
        <div class="faint" style="font-size:12px;margin-top:6px">drop_drop.py uses /dev/ttyUSB0 at 115200. The port is only opened for a moment per command.</div>
      </div>
      ${d.configs.map((c, i) => html`<div class="card" style="padding:12px" key=${c.id || i}>
        <div class="row" style="gap:8px;flex-wrap:wrap">
          <input class="input grow" style="min-width:180px;font-weight:600" placeholder="Name, e.g. Servo 12 (payloads 1 & 2)" value=${c.name} onInput=${e => setC(i, { name: e.target.value })} />
          <label class="row" style="gap:6px;font-size:13px">Servo output<input class="input" type="number" min="1" max="32" style="width:70px" value=${c.servo} onInput=${e => setC(i, { servo: e.target.value })} /></label>
          <button class="btn ghost sm" onClick=${() => moveC(i, -1)}>↑</button><button class="btn ghost sm" onClick=${() => moveC(i, 1)}>↓</button>
          <button class="btn ghost sm" title="Delete configuration" onClick=${async () => { if (await confirm({ title: `Delete "${c.name || 'configuration'}"?`, ok: 'Delete', danger: true })) setD(x => ({ ...x, configs: x.configs.filter((_, j) => j !== i) })); }}><${Icon} name="trash"/></button>
        </div>
        <div style="display:grid;gap:6px;margin-top:10px">
          ${c.positions.map((p, k) => html`<div class="step-row" key=${p.id || k}>
            <input class="input grow" placeholder="Position name" value=${p.name} onInput=${e => setP(i, k, { name: e.target.value })} />
            <input class="input mono" type="number" min="500" max="2500" step="10" style="width:90px" value=${p.pwm} onInput=${e => setP(i, k, { pwm: e.target.value })} /><span class="faint">µs</span>
            <select class="input" value=${p.kind} onChange=${e => setP(i, k, { kind: e.target.value })}>
              <option value="rest" selected=${p.kind === 'rest'}>rest / locked</option><option value="release" selected=${p.kind === 'release'}>release (confirm)</option></select>
            <button class="btn ghost sm" onClick=${() => setC(i, { positions: c.positions.filter((_, j) => j !== k) })}><${Icon} name="x"/></button>
          </div>`)}
          <div class="row" style="gap:8px;flex-wrap:wrap">
            <button class="btn sm" onClick=${() => setC(i, { positions: [...c.positions, newPos('', 1500, 'rest')] })}><${Icon} name="plus"/>Add position</button>
            <span class="grow"></span>
            <label class="row" style="gap:6px;font-size:13px" title="After a release position, go back to the rest position automatically (like drop_drop.py does after 5 s). 0 = off.">
              Return to rest after<input class="input" type="number" min="0" max="120" step="0.5" style="width:70px" value=${c.return_after || 0} onInput=${e => setC(i, { return_after: e.target.value })} />s</label>
          </div>
        </div>
      </div>`)}
      <div><button class="btn" onClick=${() => setD(x => ({ ...x, configs: [...x.configs, { name: '', servo: 9, return_after: 0, positions: [newPos('Low', 1100, 'release'), newPos('Mid', 1500, 'rest'), newPos('High', 1900, 'release')] }] }))}>
        <${Icon} name="plus"/>Add configuration</button></div>
    </div>
  <//>`;
}

function ServoCard({ c, data, onDone }) {
  const [busy, setBusy] = useState(null);
  const [custom, setCustom] = useState('');
  const out = data.outputs[String(c.servo)];
  const last = data.last[c.id];
  const send = async (pwm, label, kind) => {
    if (kind === 'release' && !(await confirm({ title: `${label}?`, ok: 'Send', danger: true,
      body: `Servo output ${c.servo} → ${pwm} µs. This can release a payload.${c.return_after ? `\nIt returns to rest after ${c.return_after} s.` : ''}` }))) return;
    if (kind === 'custom' && !(await confirm({ title: `Set servo ${c.servo} to ${pwm} µs?`, ok: 'Send', danger: true, body: 'A custom value can release a payload.' }))) return;
    setBusy(pwm);
    try {
      const r = await api(`/payload/${c.id}/set`, { body: { pwm } });
      toast(`Servo ${c.servo} → ${pwm} µs: FC ${r.ack}${r.readback ? `, output reads ${r.readback} µs` : ''}${r.returning_in ? `; back to rest in ${r.returning_in} s` : ''}`, 'ok');
    } catch (e) { toast(e.message, 'bad'); }
    setBusy(null); onDone();
  };
  return html`<div class="card">
    <div class="row" style="margin-bottom:10px;flex-wrap:wrap">
      <b>${c.name}</b><span class="pill">servo ${c.servo}</span>
      ${c.return_after > 0 && html`<span class="pill info" title="After a release position it goes back to rest automatically">auto-rest ${c.return_after}s</span>`}
      ${data.pending_return.includes(c.id) && html`<span class="pill warn">returning to rest…</span>`}
      <span class="grow"></span>
      ${out ? html`<span class="mono" style="font-size:13px" title=${'read ' + fmtAgo(out.t)}>FC output: <b>${out.pwm ? out.pwm + ' µs' : 'no signal'}</b> <span class="faint">(${fmtAgo(out.t)})</span></span>` : html`<span class="faint" style="font-size:12px">output not read yet</span>`}
    </div>
    <div class="servo-grid">
      ${c.positions.map(p => html`<button class="servo-btn ${p.kind} ${out && out.pwm && out.pwm === p.pwm ? 'current' : ''}" disabled=${busy !== null}
          onClick=${() => send(p.pwm, p.name, p.kind)}>
        ${busy === p.pwm ? html`<span class="spinner"></span>` : ''}
        <b>${p.name}</b><span class="mono">${p.pwm} µs</span>
        ${out && out.pwm && out.pwm === p.pwm && html`<span class="servo-now">current</span>`}
      </button>`)}
    </div>
    <div class="row" style="gap:6px;margin-top:10px;flex-wrap:wrap">
      <input class="input mono" type="number" min="500" max="2500" step="10" style="width:110px" placeholder="custom µs" value=${custom} onInput=${e => setCustom(e.target.value)} />
      <button class="btn sm" disabled=${busy !== null || !custom} onClick=${() => send(+custom, `${custom} µs`, 'custom')}>Send</button>
      <span class="grow"></span>
      ${last && html`<span class="faint" style="font-size:12px">Last: ${last.label} by ${last.user} ${fmtAgo(last.t)} · FC ${last.ack}${last.readback ? `, read back ${last.readback} µs` : ''}</span>`}
    </div>
  </div>`;
}

export function Payload() {
  const [data, setData] = useState(null);
  const [edit, setEdit] = useState(false);
  const [reading, setReading] = useState(false);
  const [fc, setFc] = useState(null);
  const load = () => api('/payload').then(setData).catch(e => toast(e.message, 'bad'));
  useEffect(() => { load(); const t = setInterval(load, 3000); return () => clearInterval(t); }, []);
  const read = async () => {
    setReading(true);
    try { const r = await api('/payload/read', { method: 'POST', body: {} }); setFc(r.fc); toast(`Read servo outputs from the FC (${r.ms} ms)`, 'ok'); load(); }
    catch (e) { toast(e.message, 'bad'); }
    setReading(false);
  };
  if (!data) return html`<div class="page"><div class="empty"><span class="spinner"></span></div></div>`;
  const ps = data.port_status;
  return html`<div class="page" style="max-width:1100px">
    <div class="page-head">
      <h1>Payload</h1>
      <span class="pill ${!ps.present ? 'bad' : ps.busy ? 'warn' : 'ok'}" title=${ps.port}>
        <span class="dot ${!ps.present ? 'bad' : ps.busy ? 'warn' : 'ok'}"></span>
        ${!ps.present ? `${ps.port} not found` : ps.busy ? `FC port in use by ${ps.owners.join(', ')}` : `FC port ${ps.port} free`}</span>
      ${fc && html`<span class="pill ${fc.armed ? 'bad' : ''}">${fc.armed ? 'FC ARMED' : 'FC disarmed'}</span>`}
      <span class="grow"></span>
      <button class="btn" disabled=${reading || !ps.present || ps.busy} onClick=${read} title="Connect briefly and read the FC's current servo outputs (moves nothing)">
        ${reading ? html`<span class="spinner"></span>` : html`<${Icon} name="refresh"/>`}Read outputs</button>
      <button class="btn" onClick=${() => setEdit(true)}><${Icon} name="edit"/>Configure</button>
    </div>
    ${ps.present && ps.busy && html`<div class="banner warn">${ps.port} is open in ${ps.owners.join(', ')}. The panel won't send servo commands while another program uses the FC link, so its MAVLink stream isn't corrupted. Stop it first if you need manual control.</div>`}
    ${fc && fc.armed && html`<div class="banner">The flight controller reports it is ARMED.</div>`}
    <div class="grid" style="gap:14px">
      ${data.configs.map(c => html`<${ServoCard} key=${c.id} c=${c} data=${data} onDone=${load} />`)}
      ${data.configs.length === 0 && html`<div class="empty">No configurations. Press Configure to add one.</div>`}
    </div>
    <div class="faint" style="font-size:12px;margin-top:12px">Each press opens ${data.port} for about a second, sends MAVLink DO_SET_SERVO to the flight controller, waits for its acknowledgement and reads the servo output back. Every command is recorded in Activity.</div>
    ${edit && html`<${Editor} data=${data} onClose=${() => setEdit(false)} onSaved=${load} />`}
  </div>`;
}
