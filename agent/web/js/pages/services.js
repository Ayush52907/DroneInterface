import { html, useState, useEffect, useCallback, useContext } from '../../vendor/preact-htm.js';
import { AppCtx, go } from '../ctx.js';
import { api } from '../api.js';
import { Icon, Drawer, Modal, StatePill, EnabledPill, AddPF, toast, confirm, prompt, fmtBytes, fmtDur, usePref } from '../ui.js';

const short = (n) => n.replace(/\.service$/, '');

async function setView(unit, mode) {
  try {
    await api(`/services/${encodeURIComponent(unit)}/view`, { body: { mode } });
    toast(mode === 'hide' ? `${short(unit)} removed from My services` : mode === 'pin' ? `${short(unit)} added to My services` : `${short(unit)} restored`, 'ok');
    return true;
  } catch (e) { toast(e.message, 'bad'); return false; }
}

async function runAction(unit, act, isSelf) {
  const verbs = { start: 'Start', stop: 'Stop', restart: 'Restart', enable: 'Enable', disable: 'Disable', 'reset-failed': 'Reset' };
  if (isSelf && (act === 'stop' || act === 'restart' || act === 'disable')) {
    const ok = await confirm({ title: `${verbs[act]} the panel itself?`, danger: true, ok: verbs[act],
      body: `${unit} is the service running this web panel.${act === 'stop' ? '\nYou will lose access until you start it again over SSH.' : act === 'restart' ? '\nThe page will reconnect in a few seconds.' : '\nIt will not start on next boot.'}` });
    if (!ok) return null;
  } else if (act === 'stop' || act === 'disable') {
    const ok = await confirm({ title: `${verbs[act]} ${short(unit)}?`, danger: true, ok: verbs[act],
      body: act === 'stop' ? 'The service will stop running now.' : 'The service will no longer start automatically at boot.' });
    if (!ok) return null;
  }
  try {
    const r = await api(`/services/${encodeURIComponent(unit)}/${act}`, { method: 'POST', body: {} });
    toast(`${verbs[act]}: ${short(unit)} ✓`, 'ok');
    return r.info;
  } catch (e) {
    toast(`${verbs[act]} ${short(unit)} failed\n${e.message}`, 'bad');
    return null;
  }
}

function Actions({ s, onDone, small = true }) {
  const [busy, setBusy] = useState(null);
  const running = s.active === 'active' || s.active === 'activating' || s.active === 'reloading';
  const enabled = s.enabled === 'enabled';
  const canEnable = s.enabled === 'enabled' || s.enabled === 'disabled';
  const doIt = async (act, e) => {
    e && e.stopPropagation();
    setBusy(act);
    const info = await runAction(s.name, act, s.self);
    setBusy(null);
    onDone && onDone(info);
  };
  const cls = 'btn' + (small ? ' sm' : '');
  const b = (act, label, icon, extra = '') => html`<button class="${cls} ${extra}" disabled=${!!busy} onClick=${e => doIt(act, e)} title=${label}>
    ${busy === act ? html`<span class="spinner"></span>` : html`<${Icon} name=${icon}/>`}<span class=${small ? 'hide-sm' : ''}>${label}</span></button>`;
  return html`<div class="row" style="gap:6px;flex-wrap:wrap" onClick=${e => e.stopPropagation()}>
    ${running ? b('stop', 'Stop', 'stop', 'danger') : b('start', 'Start', 'play')}
    ${b('restart', 'Restart', 'restart')}
    ${canEnable && (enabled ? b('disable', 'Disable', 'power') : b('enable', 'Enable', 'check'))}
    ${s.active === 'failed' && b('reset-failed', 'Reset', 'refresh')}
  </div>`;
}

function VerifyBox({ res }) {
  if (!res) return null;
  return html`<pre class="pre" style=${{ maxHeight: '140px', borderColor: res.ok ? '#1f6b4f' : '#6b5417', color: res.ok ? 'var(--ok)' : 'var(--warn)' }}>${res.ok ? '✓ ' : '⚠ '}${res.output}</pre>`;
}

function UnitEditor({ unit, initial, onClose, onSaved }) {
  const [text, setText] = useState(initial);
  const [res, setRes] = useState(null);
  const [restart, setRestart] = useState(true);
  const [busy, setBusy] = useState(false);
  const verify = async () => { setRes(await api('/services/verify', { body: { name: unit, content: text } })); };
  const save = async () => {
    setBusy(true);
    try {
      const v = await api('/services/verify', { body: { name: unit, content: text } });
      setRes(v);
      if (!v.ok && !(await confirm({ title: 'Save with warnings?', body: v.output, ok: 'Save anyway', danger: true }))) { setBusy(false); return; }
      const r = await api(`/services/${encodeURIComponent(unit)}/unitfile`, { method: 'PUT', body: { content: text, restart } });
      toast(r.message, 'ok'); onSaved(); onClose();
    } catch (e) { toast(e.message, 'bad'); }
    setBusy(false);
  };
  const dirty = text !== initial;
  return html`<${Modal} wide title=${'Edit ' + unit} onClose=${async () => { if (!dirty || await confirm({ title: 'Discard changes?', ok: 'Discard', danger: true })) onClose(); }}
      foot=${html`<label class="row grow" style="gap:6px;font-size:13px"><input type="checkbox" checked=${restart} onChange=${e => setRestart(e.target.checked)} />Restart the service after saving</label>
        <button class="btn" onClick=${verify}>Check</button>
        <button class="btn primary" disabled=${busy || !dirty} onClick=${save}>${busy ? 'Saving…' : 'Save & reload'}</button>`}>
    <div style="display:grid;grid-template-rows:1fr auto;gap:8px;min-height:0">
      <textarea class="editor" spellcheck="false" value=${text} onInput=${e => { setText(e.target.value); setRes(null); }} />
      <div><${VerifyBox} res=${res} /><div class="faint" style="font-size:12px">Saved to /etc/systemd/system/${unit}; the previous version is backed up to /var/backups/drone-panel/units/.</div></div>
    </div>
  <//>`;
}

function genUnit(f) {
  const env = f.env.split('\n').map(l => l.trim()).filter(l => l && l.includes('=')).map(l => `Environment="${l.replace(/"/g, '\\"')}"`);
  return [
    '[Unit]', `Description=${f.description || f.name}`,
    ...(f.network ? ['After=network-online.target', 'Wants=network-online.target'] : []), '',
    '[Service]', 'Type=simple', `User=${f.user}`, `WorkingDirectory=${f.workdir}`, `ExecStart=${f.exec}`,
    `Restart=${f.restart}`, ...(f.restart !== 'no' ? [`RestartSec=${f.restartSec}`] : []),
    'Environment=PYTHONUNBUFFERED=1', ...env, '',
    '[Install]', 'WantedBy=multi-user.target', '',
  ].join('\n');
}

function NewService({ me, onClose, onCreated }) {
  const home = '/home/' + (me.files.roots[0] || '/home/orin').split('/').filter(Boolean).pop();
  const [f, setF] = useState({ name: '', description: '', exec: '', workdir: home, user: home.split('/').pop(), restart: 'on-failure', restartSec: 3, env: '', network: true, enable: true, start: false });
  const [raw, setRaw] = useState(null);
  const [res, setRes] = useState(null);
  const [busy, setBusy] = useState(false);
  const set = (k, v) => { setF(x => ({ ...x, [k]: v })); setRes(null); };
  const name = (f.name.trim().replace(/\.service$/, '').replace(/[^A-Za-z0-9@._-]/g, '_')) + '.service';
  const text = raw ?? genUnit(f);
  const execHint = f.exec.endsWith('.py') && !f.exec.includes(' ') && !f.exec.includes('python') ? 'Tip: use /usr/bin/python3 ' + f.exec : f.exec.endsWith('.sh') && !f.exec.startsWith('/bin/bash') ? 'Tip: use /bin/bash ' + f.exec : '';
  const create = async () => {
    if (!f.name.trim() || !text.includes('ExecStart=') || (!raw && !f.exec.trim())) { toast('Name and command are required', 'bad'); return; }
    setBusy(true);
    try {
      const v = await api('/services/verify', { body: { name, content: text } }); setRes(v);
      if (!v.ok && !(await confirm({ title: 'Create with warnings?', body: v.output, ok: 'Create anyway', danger: true }))) { setBusy(false); return; }
      const r = await api('/services/create', { body: { name, content: text, enable: f.enable, start: f.start } });
      toast(`Created ${name}\n${r.message}`, 'ok'); onCreated(name); onClose();
    } catch (e) { toast(e.message, 'bad'); }
    setBusy(false);
  };
  const L = (label, el, hint) => html`<label class="field">${label}${el}${hint && html`<span class="faint" style="font-size:11.5px">${hint}</span>`}</label>`;
  return html`<${Modal} wide title="New service" onClose=${onClose}
      foot=${html`<label class="row" style="gap:6px;font-size:13px"><input type="checkbox" checked=${f.enable} onChange=${e => set('enable', e.target.checked)} />Start at boot</label>
        <label class="row grow" style="gap:6px;font-size:13px"><input type="checkbox" checked=${f.start} onChange=${e => set('start', e.target.checked)} />Start now</label>
        <button class="btn" onClick=${async () => setRes(await api('/services/verify', { body: { name, content: text } }))}>Check</button>
        <button class="btn primary" disabled=${busy} onClick=${create}>${busy ? 'Creating…' : 'Create ' + name}</button>`}>
    <div class="newsvc">
      <div style="display:grid;gap:10px;align-content:start">
        ${L('Service name', html`<input class="input" placeholder="rudra_logger" value=${f.name} onInput=${e => set('name', e.target.value)} />`, `→ ${name}`)}
        ${L('Description', html`<input class="input" placeholder="What it does" value=${f.description} onInput=${e => set('description', e.target.value)} />`)}
        ${L('Command to run (full paths)', html`<input class="input mono" placeholder="/usr/bin/python3 /home/orin/my_script.py --arg" value=${f.exec} onInput=${e => set('exec', e.target.value)} />`, execHint || 'For ROS 2: /bin/bash -c \'source /opt/ros/jazzy/setup.bash && exec ros2 run pkg node\'')}
        <div class="row" style="gap:10px">${L('Working folder', html`<input class="input mono" value=${f.workdir} onInput=${e => set('workdir', e.target.value)} />`)}${L('Run as user', html`<input class="input" style="width:110px" value=${f.user} onInput=${e => set('user', e.target.value)} />`)}</div>
        <div class="row" style="gap:10px">${L('If it stops', html`<select class="input" value=${f.restart} onChange=${e => set('restart', e.target.value)}><option value="on-failure">restart on crash</option><option value="always">always restart</option><option value="no">don't restart</option></select>`)}
          ${f.restart !== 'no' && L('after (s)', html`<input class="input" type="number" min="1" style="width:80px" value=${f.restartSec} onInput=${e => set('restartSec', e.target.value)} />`)}</div>
        ${L('Environment (one per line)', html`<textarea class="input mono" rows="2" placeholder="ROS_DOMAIN_ID=0" value=${f.env} onInput=${e => set('env', e.target.value)} />`)}
        <label class="row" style="gap:6px;font-size:13px"><input type="checkbox" checked=${f.network} onChange=${e => set('network', e.target.checked)} />Wait for network at boot</label>
      </div>
      <div style="display:grid;grid-template-rows:auto 1fr auto;gap:6px;min-height:0">
        <div class="row"><b class="grow" style="font-size:13px">Unit file</b>${raw === null ? html`<button class="btn sm" onClick=${() => setRaw(genUnit(f))}><${Icon} name="edit"/>Edit raw</button>` : html`<button class="btn sm" onClick=${() => setRaw(null)}>Back to form</button>`}</div>
        <textarea class="editor" spellcheck="false" readOnly=${raw === null} value=${text} onInput=${e => setRaw(e.target.value)} style=${{ minHeight: '260px', opacity: raw === null ? 0.85 : 1 }} />
        <${VerifyBox} res=${res} />
      </div>
    </div>
  <//>`;
}

function Detail({ unit, isCustom, onClose, onChanged }) {
  const [d, setD] = useState(null);
  const load = useCallback(() => api('/services/' + encodeURIComponent(unit)).then(setD).catch(e => toast(e.message, 'bad')), [unit]);
  useEffect(() => { load(); const t = setInterval(load, 5000); return () => clearInterval(t); }, [load]);
  const i = d && d.info;
  const [uf, setUf] = useState(null);
  const [editing, setEditing] = useState(false);
  useEffect(() => { api(`/services/${encodeURIComponent(unit)}/unitfile`).then(setUf).catch(() => {}); }, [unit, editing]);
  const del = async () => {
    const typed = await prompt({ title: `Delete ${unit}?`, label: `This stops and disables it, and removes ${uf.path} (a backup is kept). Type the service name to confirm:`, ok: 'Delete service' });
    if (typed === null) return;
    if (typed.trim().replace(/\.service$/, '') !== short(unit)) { toast('Name did not match. Nothing deleted.', 'bad'); return; }
    try { const r = await api('/services/' + encodeURIComponent(unit), { method: 'DELETE' }); toast(r.message, 'ok'); onChanged(); onClose(); }
    catch (e) { toast(e.message, 'bad'); }
  };
  return html`<${Drawer} title=${short(unit)} onClose=${onClose}
      head=${html`${isCustom
        ? html`<button class="btn sm" title="Remove from My services (the service itself is not changed)" onClick=${async () => { if (await setView(unit, 'hide')) { onChanged(); onClose(); } }}><${Icon} name="eyeoff"/><span class="hide-sm">Hide</span></button>`
        : html`<button class="btn sm" onClick=${async () => { if (await setView(unit, 'pin')) { onChanged(); onClose(); } }}><${Icon} name="star"/><span class="hide-sm">Add to My services</span></button>`}
      <${AddPF} check=${{ type: 'service', unit, name: `${short(unit)} running` }} /><button class="btn sm" onClick=${() => go('logs', { units: unit })}><${Icon} name="logs"/>Live logs</button>`}>
    ${!d ? html`<div class="empty"><span class="spinner"></span></div>` : html`
      ${i && html`<div class="card">
        <div class="row wrap" style="margin-bottom:12px"><${StatePill} active=${i.active} sub=${i.sub}/><${EnabledPill} state=${i.enabled}/>
          ${i.self && html`<span class="pill warn">runs this panel</span>`}<span class="grow"></span></div>
        <${Actions} s=${i} small=${false} onDone=${() => { load(); onChanged(); }} />
        <div class="kv" style="margin-top:14px">
          <span>Description</span><span>${i.description || '–'}</span>
          <span>Main PID</span><span class="mono">${i.pid || '–'}</span>
          <span>Uptime</span><span>${fmtDur(i.uptime)}</span>
          <span>Memory</span><span>${fmtBytes(i.memory)}</span>
          <span>CPU</span><span>${i.cpu !== null ? i.cpu + '%' : '–'}</span>
          <span>Tasks</span><span>${i.tasks ?? '–'}</span>
          <span>Restarts</span><span>${i.restarts ?? '–'} <span class="faint">(policy: ${i.restart_policy})</span></span>
          <span>Last result</span><span class=${i.result !== 'success' ? 'bad' : ''}>${i.result} <span class="faint">exit ${i.exit_status}</span></span>
          <span>Runs as</span><span class="mono">${i.user}</span>
          ${i.workdir && html`<span>Working dir</span><span class="mono">${i.workdir}</span>`}
          <span>Unit file</span><span class="mono" style="word-break:break-all">${i.path}</span>
        </div>
      </div>`}
      <div><div class="row" style="margin-bottom:6px"><h3 class="muted grow" style="font-size:12px;margin:0">UNIT FILE</h3>
        ${uf && uf.editable && uf.installed && html`<button class="btn sm" onClick=${() => setEditing(true)}><${Icon} name="edit"/>Edit</button>
          <button class="btn sm danger" disabled=${i && i.self} onClick=${del}><${Icon} name="trash"/>Delete service</button>`}
        ${uf && !uf.editable && html`<span class="faint" style="font-size:12px" title=${uf.reason}>read-only (${uf.reason})</span>`}</div>
        <pre class="pre">${d.unit_file}</pre></div>
      ${editing && uf && html`<${UnitEditor} unit=${unit} initial=${uf.content} onClose=${() => setEditing(false)} onSaved=${() => { load(); onChanged(); }} />`}
      <div><div class="row" style="margin-bottom:6px"><h3 class="muted grow" style="font-size:12px;margin:0">RECENT LOGS</h3>
        <a class="btn sm" href=${'/api/logs/download?units=' + encodeURIComponent(unit) + '&lines=20000'}><${Icon} name="download"/>Download</a></div>
        <pre class="pre" style="max-height:420px" ref=${el => el && (el.scrollTop = el.scrollHeight)}>${d.logs || 'No log entries.'}</pre></div>
    `}
  <//>`;
}

export function Services({ params }) {
  const { stats } = useContext(AppCtx);
  const [data, setData] = useState(null);
  const [sel, setSel] = useState(null);
  const [q, setQ] = useState('');
  const [filter, setFilter] = useState(params.filter || 'all');
  const [showSystem, setShowSystem] = usePref('svc.showSystem', false);
  const [showHidden, setShowHidden] = useState(false);
  const [newSvc, setNewSvc] = useState(false);
  const { me } = useContext(AppCtx);

  const load = useCallback(() => api('/services').then(setData).catch(e => toast(e.message, 'bad')), []);
  useEffect(() => { load(); const t = setInterval(load, 5000); return () => clearInterval(t); }, [load]);
  useEffect(() => { if (params.filter === 'failed') setShowSystem(true); }, []);

  if (!data) return html`<div class="page"><div class="empty"><span class="spinner"></span></div></div>`;
  const failed = (stats && stats.failed_services) || [];

  const match = (s) => {
    if (q && !(s.name + ' ' + s.description).toLowerCase().includes(q.toLowerCase())) return false;
    if (filter === 'running') return s.active === 'active';
    if (filter === 'failed') return s.active === 'failed';
    if (filter === 'stopped') return s.active === 'inactive';
    if (filter === 'enabled') return s.enabled === 'enabled';
    return true;
  };
  const custom = data.custom.filter(match);
  const system = data.system.filter(match);
  const updateOne = (info) => info && setData(d => ({ ...d, custom: d.custom.map(c => c.name === info.name ? info : c) }));

  return html`<div class="page">
    <div class="page-head">
      <h1>Services</h1>
      <button class="btn primary sm" onClick=${() => setNewSvc(true)}><${Icon} name="plus"/>New service</button>
      <span class="grow"></span>
      <div class="row" style="position:relative">
        <input class="input" style="width:220px;padding-left:30px" placeholder="Search services…" value=${q} onInput=${e => setQ(e.target.value)} />
        <span style="position:absolute;left:9px;top:8px;color:var(--faint)"><${Icon} name="search" cls="ficon"/></span>
      </div>
      <select class="input" value=${filter} onChange=${e => setFilter(e.target.value)}>
        <option value="all">All states</option><option value="running">Running</option>
        <option value="failed">Failed</option><option value="stopped">Stopped</option><option value="enabled">Enabled at boot</option>
      </select>
      <button class="btn" onClick=${load} title="Refresh"><${Icon} name="refresh"/></button>
    </div>

    ${failed.length > 0 && filter !== 'failed' && html`<div class="banner"><b>${failed.length} failed:</b><span class="grow ellipsis">${failed.join(', ')}</span>
      <button class="btn sm" onClick=${() => { setFilter('failed'); setShowSystem(true); }}>Show</button></div>`}

    <div class="card" style="padding:0;margin-bottom:16px">
      <div class="row" style="padding:12px 16px;border-bottom:1px solid var(--line)">
        <h3 style="margin:0">My services</h3><span class="faint" style="font-size:12px">${data.custom.length} unit${data.custom.length === 1 ? '' : 's'}: ones you created, plus any you added with ★. Use <${Icon} name="eyeoff" cls="ficon"/> to hide one.</span>
      </div>
      ${custom.length === 0 ? html`<div class="empty">${data.custom.length ? 'No services match the filter.' : html`No custom services found.<br/><span class="faint">Units you add to /etc/systemd/system show up here automatically, or pin one in config.yaml.</span>`}</div>` : html`
      <div style="overflow:auto"><table class="table svc-table">
        <thead><tr><th>Service</th><th>State</th><th class="hide-sm">Boot</th><th class="hide-sm">Uptime</th><th class="hide-sm">CPU</th><th class="hide-sm">Memory</th><th class="hide-sm">Restarts</th><th></th></tr></thead>
        <tbody>${custom.map(s => html`<tr class="click" key=${s.name} onClick=${() => setSel(s.name)}>
          <td><div style="font-weight:600">${short(s.name)}${s.self && html` <span class="pill warn" style="font-weight:400">panel</span>`}</div><div class="faint ellipsis" style="font-size:12px;max-width:320px">${s.description}</div></td>
          <td><${StatePill} active=${s.active} sub=${s.sub}/></td>
          <td class="hide-sm"><${EnabledPill} state=${s.enabled}/></td>
          <td class="hide-sm num">${fmtDur(s.uptime)}</td>
          <td class="hide-sm num">${s.cpu !== null && s.active === 'active' ? s.cpu + '%' : '–'}</td>
          <td class="hide-sm num">${s.active === 'active' ? fmtBytes(s.memory) : '–'}</td>
          <td class="hide-sm num ${s.restarts > 0 ? 'warn' : ''}">${s.restarts ?? '–'}</td>
          <td class="svc-act" style="text-align:right"><div class="row" style="gap:6px;justify-content:flex-end;flex-wrap:wrap"><${Actions} s=${s} onDone=${(info) => { updateOne(info); load(); }} />
            <button class="btn ghost sm" title="Remove from My services (the service itself is not changed)" onClick=${async (e) => { e.stopPropagation(); if (await setView(s.name, 'hide')) load(); }}><${Icon} name="eyeoff"/></button></div></td>
        </tr>`)}</tbody>
      </table></div>`}
    </div>

    ${data.hidden.length > 0 && html`<div class="card" style="padding:0;margin-bottom:16px">
      <button class="row btn ghost" style="width:100%;padding:12px 16px;justify-content:flex-start" onClick=${() => setShowHidden(!showHidden)}>
        <h3 style="margin:0">${showHidden ? '▾' : '▸'} Hidden from My services</h3><span class="faint" style="font-size:12px">${data.hidden.length}</span>
      </button>
      ${showHidden && html`<div class="list-rows" style="padding:0 16px 8px;border-top:1px solid var(--line)">${data.hidden.map(h => html`<div>
        <span class="mono grow">${short(h.name)}</span><${StatePill} active=${h.active} sub=${h.sub}/>
        <button class="btn sm" onClick=${async () => { if (await setView(h.name, 'reset')) load(); }}><${Icon} name="eye"/>Show again</button></div>`)}</div>`}
    </div>`}

    <div class="card" style="padding:0">
      <button class="row btn ghost" style="width:100%;padding:12px 16px;border-radius:var(--radius) var(--radius) 0 0;justify-content:flex-start" onClick=${() => setShowSystem(!showSystem)}>
        <h3 style="margin:0">${showSystem ? '▾' : '▸'} System services</h3><span class="faint" style="font-size:12px">${system.length} of ${data.system.length}</span>
      </button>
      ${showSystem && html`<div style="overflow:auto;max-height:70vh;border-top:1px solid var(--line)"><table class="table">
        <thead><tr><th>Service</th><th>State</th><th class="hide-sm">Boot</th><th class="hide-sm">Description</th><th></th></tr></thead>
        <tbody>${system.map(s => html`<tr class="click" key=${s.name} onClick=${() => setSel(s.name)}>
          <td class="mono" style="font-size:13px">${short(s.name)}</td>
          <td><${StatePill} active=${s.active} sub=${s.sub}/></td>
          <td class="hide-sm"><${EnabledPill} state=${s.enabled}/></td>
          <td class="hide-sm muted ellipsis" style="max-width:420px">${s.description}</td>
          <td style="text-align:right">${s.hidden ? html`<span class="faint" style="font-size:12px">hidden</span>` : html`<button class="btn ghost sm" title="Add to My services" onClick=${async (e) => { e.stopPropagation(); if (await setView(s.name, 'pin')) load(); }}><${Icon} name="star"/></button>`}</td>
        </tr>`)}</tbody>
      </table></div>`}
    </div>

    ${newSvc && html`<${NewService} me=${me} onClose=${() => setNewSvc(false)} onCreated=${(n) => { load(); setSel(n); }} />`}
    ${sel && html`<${Detail} unit=${sel} isCustom=${data.custom.some(c => c.name === sel)} onClose=${() => setSel(null)} onChanged=${load} />`}
  </div>`;
}
