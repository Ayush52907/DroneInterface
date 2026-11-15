import { html, useState, useEffect } from '../../vendor/preact-htm.js';
import { api } from '../api.js';
import { Icon, toast, confirm } from '../ui.js';

const FIX = { 0: 'no GPS', 1: 'no fix', 2: '2D fix', 3: '3D fix', 4: 'DGPS', 5: 'RTK float', 6: 'RTK fixed' };

function FcCard() {
  const [fc, setFc] = useState(null);
  const load = () => api('/fc').then(setFc).catch(() => {});
  useEffect(() => { load(); const t = setInterval(load, 3000); return () => clearInterval(t); }, []);
  if (!fc) return null;
  const g = fc.gps, rc = fc.rc, rs = fc.rc_sensor;
  return html`<div class="card">
    <h3>Flight controller (live)</h3>
    ${fc.mode === 'direct' ? html`<div class="muted" style="font-size:13px">mavlink-router is off, so the panel only reads the FC on demand (Preflight / Payload).</div>`
      : html`<div class="kv">
        <span>Link</span><span class=${fc.connected ? 'ok' : 'bad'}>${fc.connected ? `connected (heartbeat ${fc.age}s ago)` : 'no heartbeat' + (fc.error ? ` (${fc.error})` : '')}</span>
        ${fc.connected && html`<span>Armed</span><span class=${fc.armed ? 'bad' : ''}>${fc.armed ? 'ARMED' : 'disarmed'}</span>`}
        ${fc.connected && fc.link && html`<span>Link quality</span><span class=${fc.link.loss_pct > 2 ? 'bad' : ''}>${fc.link.msgs_per_s} msg/s · ${fc.link.loss_pct}% lost (last ${fc.link.window_s}s)</span>`}
        <span>GPS</span><span>${g ? `${FIX[g.fix] || g.fix} · ${g.sats ?? '?'} sats · HDOP ${g.hdop ?? '?'}` : '–'}</span>
        <span>RC</span><span>${rs ? (rs.healthy ? 'receiver healthy' : rs.present ? 'failsafe / unhealthy' : 'no receiver') : '–'}${rc && rc.quality != null ? ` · ${rc.quality}%` : ''}</span>
      </div>`}
  </div>`;
}

export function Settings() {
  const [d, setD] = useState(null);
  const [text, setText] = useState('');
  const [check, setCheck] = useState(null);
  const [busy, setBusy] = useState(false);
  const [log, setLog] = useState(null);
  const [showConf, setShowConf] = useState(false);
  const load = () => api('/drone').then(r => { setD(r); setText(r.text); setCheck(r.error ? { ok: false, error: r.error } : null); }).catch(e => toast(e.message, 'bad'));
  useEffect(() => { load(); }, []);
  const validate = async () => setCheck(await api('/drone/validate', { body: { text } }));
  const save = async () => {
    const v = await api('/drone/validate', { body: { text } });
    setCheck(v);
    if (!v.ok) { toast('Not saved: ' + v.error, 'bad'); return; }
    const wasOn = /mavlink_router:[\s\S]*?enabled:\s*true/.test(d.text), nowOn = /mavlink_router:[\s\S]*?enabled:\s*true/.test(text);
    if (wasOn !== nowOn && !(await confirm({ title: nowOn ? 'Enable mavlink-router?' : 'Disable mavlink-router?', ok: 'Save & apply', danger: !nowOn,
      body: nowOn ? 'mavlink-router will take the FC port. Any program still opening the FC port directly must be switched to its ~/drone-env/<name>.env first, or applying will be refused.'
        : 'Programs connected through mavlink-router (udpin:127.0.0.1:…) will lose the FC until their env files point at the serial port again (done automatically) and they are restarted.' }))) return;
    setBusy(true); setLog(null);
    try { const r = await api('/drone', { method: 'PUT', body: { text, apply: true } }); setLog({ ok: true, lines: r.log }); toast('drone.yaml saved and applied', 'ok'); }
    catch (e) { setLog({ ok: false, lines: e.message.split('\n') }); toast('Not applied: drone.yaml was put back as it was', 'bad'); }
    setBusy(false); load();
  };
  if (!d) return html`<div class="page"><div class="empty"><span class="spinner"></span></div></div>`;
  const dirty = text !== d.text;
  const r = d.router;
  return html`<div class="page" style="max-width:1200px">
    <div class="page-head"><h1>Drone settings</h1><span class="mono faint" style="font-size:12px">${d.path}</span></div>
    <div class="settings-layout">
      <div style="display:grid;gap:10px;grid-template-rows:auto 1fr auto;min-height:0">
        <div class="row" style="flex-wrap:wrap">
          <span class="muted" style="font-size:13px">One file for the FC connection, mavlink-router, payload servos and preflight thresholds.</span><span class="grow"></span>
          <button class="btn" onClick=${validate}>Check</button>
          <button class="btn" disabled=${!dirty} onClick=${() => { setText(d.text); setCheck(null); }}>Revert</button>
          <button class="btn primary" disabled=${busy} onClick=${save}>${busy ? html`<span class="spinner"></span>` : html`<${Icon} name="check"/>`}Save & apply</button>
        </div>
        <textarea class="editor" spellcheck="false" style="min-height:520px" value=${text} onInput=${e => { setText(e.target.value); setCheck(null); }} />
        ${check && html`<div class="banner ${check.ok ? 'info-b' : ''}">${check.ok ? '✓ Valid' : '✗ ' + check.error}</div>`}
        ${log && html`<pre class="pre" style=${{ borderColor: log.ok ? '#1f6b4f' : '#7a2c2f' }}>${log.lines.join('\n')}</pre>`}
      </div>
      <div style="display:grid;gap:12px;align-content:start">
        <div class="card">
          <h3>mavlink-router</h3>
          <div class="kv">
            <span>Installed</span><span>${r.installed ? 'yes' : 'no'}</span>
            <span>Running</span><span class=${r.active === 'active' ? 'ok' : 'muted'}>${r.active || '–'}</span>
            <span>At boot</span><span>${r.enabled || '–'}</span>
          </div>
          <button class="btn sm" style="margin-top:10px" onClick=${() => setShowConf(!showConf)}>${showConf ? 'Hide' : 'Show'} generated config</button>
          ${showConf && html`<pre class="pre" style="margin-top:8px;font-size:11.5px">${d.router_conf}</pre>`}
        </div>
        <${FcCard} />
        <div class="card">
          <h3>Programs and their FC connection</h3>
          <div class="list-rows">
            ${Object.entries(d.clients).map(([name, url]) => {
              const svc = d.services.find(s => s.client === name);
              return html`<div style="align-items:flex-start">
                <div class="grow" style="min-width:0"><b>${name}</b><div class="mono faint" style="font-size:11.5px">MAVLINK_PORT=${url}</div></div>
                ${name === 'panel' ? html`<span class="pill info">this panel</span>`
                  : !svc || !svc.exists ? html`<span class="pill" title="no ${name}.service">no service</span>`
                  : svc.uses_env ? html`<span class="pill ok" title=${`${name}.service loads ${d.env_dir}/${name}.env`}>uses drone.yaml</span>`
                  : html`<span class="pill warn" title=${`Add EnvironmentFile=${d.env_dir}/${name}.env to ${name}.service`}>not linked yet</span>`}
              </div>`;
            })}
          </div>
          <div class="faint" style="font-size:11.5px;margin-top:8px">A service follows drone.yaml once its unit has <span class="mono">EnvironmentFile=${d.env_dir}/${"<name>"}.env</span>.</div>
        </div>
      </div>
    </div>
  </div>`;
}
