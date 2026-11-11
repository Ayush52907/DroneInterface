import { html, useState, useEffect, useRef } from '../../vendor/preact-htm.js';
import { api } from '../api.js';
import { Icon, Modal, toast, confirm, fmtAgo } from '../ui.js';

const COLORS = { default: '', green: 'act-green', red: 'act-red', blue: 'act-blue', amber: 'act-amber' };
const STEP_LABEL = { service: 'Service', gimbal: 'Gimbal → flight position', command: 'Run command', wait: 'Wait' };

export function stepText(s) {
  if (s.type === 'service') return `${s.op} ${s.unit.replace('.service', '')}${s.wait === false ? '' : ' (wait)'}`;
  if (s.type === 'gimbal') return 'gimbal → flight position';
  if (s.type === 'command') return `$ ${s.cmd}`;
  if (s.type === 'wait') return `wait ${s.seconds}s`;
  return s.type;
}

// Runs an action and streams its log. Shared by this page and the dashboard.
export function useRunner(onDone) {
  const [run, setRun] = useState(null); // {id, name, log, state}
  const start = async (a) => {
    if (a.confirm && !(await confirm({ title: `Run "${a.name}"?`, body: a.steps.map(stepText).join('\n'), ok: 'Run' }))) return;
    setRun({ id: a.id, name: a.name, log: '', state: 'running' });
    try {
      const res = await fetch(`/api/actions/${a.id}/run`, { method: 'POST' });
      if (!res.ok) throw new Error((await res.text()) || res.statusText);
      const reader = res.body.getReader();
      const dec = new TextDecoder();
      let log = '';
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        log += dec.decode(value, { stream: true });
        setRun(r => ({ ...r, log }));
      }
      const ok = /(^|\n)DONE/.test(log);
      setRun(r => ({ ...r, state: ok ? 'ok' : 'fail' }));
      toast(`${a.name}: ${ok ? 'done' : 'failed'}`, ok ? 'ok' : 'bad');
    } catch (e) {
      setRun(r => ({ ...r, log: (r?.log || '') + '\n' + e.message, state: 'fail' }));
    }
    onDone && onDone();
  };
  return [run, start, () => setRun(null)];
}

export function RunLog({ run, onClose }) {
  const ref = useRef();
  useEffect(() => { if (ref.current) ref.current.scrollTop = ref.current.scrollHeight; }, [run.log]);
  return html`<${Modal} title=${html`${run.name} ${run.state === 'running' ? html`<span class="spinner"></span>` : run.state === 'ok' ? html`<span class="pill ok">done</span>` : html`<span class="pill bad">failed</span>`}`}
      onClose=${onClose} foot=${html`<button class="btn" onClick=${onClose}>${run.state === 'running' ? 'Hide (keeps running)' : 'Close'}</button>`}>
    <pre class="pre" ref=${ref} style="max-height:60vh;min-height:160px">${run.log || 'Starting…'}</pre>
  <//>`;
}

function StepEditor({ step, onChange, onRemove, onUp, onDown }) {
  const set = (k, v) => onChange({ ...step, [k]: v });
  return html`<div class="step-row">
    <select class="input" value=${step.type} onChange=${e => onChange({ type: e.target.value, ...(e.target.value === 'service' ? { op: 'start', unit: '', wait: true } : e.target.value === 'wait' ? { seconds: 2 } : e.target.value === 'command' ? { cmd: '', timeout: 300 } : {}) })}>
      ${Object.entries(STEP_LABEL).map(([k, v]) => html`<option value=${k}>${v}</option>`)}
    </select>
    ${step.type === 'service' && html`
      <select class="input" value=${step.op || 'start'} onChange=${e => set('op', e.target.value)}>${['start', 'stop', 'restart'].map(o => html`<option value=${o} selected=${(step.op || 'start') === o}>${o}</option>`)}</select>
      <input class="input grow" placeholder="rudra_camera.service" value=${step.unit} onInput=${e => set('unit', e.target.value)} list="dp-units" />
      <label class="row" style="gap:4px;font-size:12px" title="Wait until it is running / stopped before the next step"><input type="checkbox" checked=${step.wait !== false} onChange=${e => set('wait', e.target.checked)} />wait</label>`}
    ${step.type === 'command' && html`<input class="input grow mono" placeholder="find ~/detections/raw -mtime +7 -delete" value=${step.cmd} onInput=${e => set('cmd', e.target.value)} />`}
    ${step.type === 'wait' && html`<input class="input" type="number" min="0" step="0.5" style="width:90px" value=${step.seconds} onInput=${e => set('seconds', e.target.value)} /><span class="faint">seconds</span>`}
    ${step.type === 'gimbal' && html`<span class="faint grow" style="font-size:12px">FOLLOW mode, siyi-pitch returns it to the flight pitch (verified)</span>`}
    <div class="row" style="gap:0"><button class="btn ghost sm" onClick=${onUp}>↑</button><button class="btn ghost sm" onClick=${onDown}>↓</button><button class="btn ghost sm" onClick=${onRemove}><${Icon} name="x"/></button></div>
  </div>`;
}

function ActionEditor({ action, units, onSave, onClose }) {
  const [a, setA] = useState(JSON.parse(JSON.stringify({ name: '', description: '', color: 'default', confirm: false, steps: [{ type: 'service', op: 'start', unit: '', wait: true }], ...action })));
  const set = (k, v) => setA(x => ({ ...x, [k]: v }));
  const setStep = (i, st) => setA(x => ({ ...x, steps: x.steps.map((s, j) => j === i ? st : s) }));
  const move = (i, d) => setA(x => { const s = [...x.steps], j = i + d; if (j < 0 || j >= s.length) return x; [s[i], s[j]] = [s[j], s[i]]; return { ...x, steps: s }; });
  return html`<${Modal} wide title=${action.id ? 'Edit quick action' : 'New quick action'} onClose=${onClose}
      foot=${html`<button class="btn" onClick=${onClose}>Cancel</button><button class="btn primary" onClick=${() => onSave(a)}>Save</button>`}>
    <div style="overflow:auto;min-height:0;display:grid;gap:12px;align-content:start">
      <datalist id="dp-units">${units.map(u => html`<option value=${u} />`)}</datalist>
      <div class="row" style="gap:10px;flex-wrap:wrap">
        <label class="field grow">Name<input class="input" value=${a.name} onInput=${e => set('name', e.target.value)} placeholder="Start mission pipeline" /></label>
        <label class="field">Colour<select class="input" value=${a.color || 'default'} onChange=${e => set('color', e.target.value)}>${Object.keys(COLORS).map(c => html`<option value=${c} selected=${(a.color || 'default') === c}>${c}</option>`)}</select></label>
      </div>
      <label class="field">Description<input class="input" value=${a.description} onInput=${e => set('description', e.target.value)} /></label>
      <label class="row" style="gap:6px"><input type="checkbox" checked=${a.confirm} onChange=${e => set('confirm', e.target.checked)} />Ask for confirmation before running</label>
      <div class="field">Steps (run in order; stops at the first failure)
        ${a.steps.map((st, i) => html`<${StepEditor} key=${i} step=${st} onChange=${s => setStep(i, s)} onRemove=${() => set('steps', a.steps.filter((_, j) => j !== i))} onUp=${() => move(i, -1)} onDown=${() => move(i, 1)} />`)}
        <div><button class="btn sm" onClick=${() => set('steps', [...a.steps, { type: 'service', op: 'start', unit: '', wait: true }])}><${Icon} name="plus"/>Add step</button></div>
      </div>
      <div class="faint" style="font-size:12px">Commands run as the panel user in its home folder (like the Terminal). Long or heavy jobs are better as a service.</div>
    </div>
  <//>`;
}

export function ActionButtons({ actions, onRun, running, compact }) {
  return html`<div class="act-grid ${compact ? 'compact' : ''}">${actions.map(a => html`
    <button class="act-btn ${COLORS[a.color] || ''}" disabled=${running && running.id === a.id && running.state === 'running'} onClick=${() => onRun(a)} title=${a.steps.map(stepText).join('\n')}>
      ${running && running.id === a.id && running.state === 'running' ? html`<span class="spinner"></span>` : html`<${Icon} name=${a.steps.some(s => s.type === 'gimbal') ? 'target' : 'bolt'} />`}
      <span class="grow" style="text-align:left;min-width:0"><b class="ellipsis" style="display:block">${a.name}</b>${!compact && a.description && html`<small class="ellipsis" style="display:block">${a.description}</small>`}</span>
    </button>`)}</div>`;
}

export function Actions() {
  const [acts, setActs] = useState(null);
  const [units, setUnits] = useState([]);
  const [edit, setEdit] = useState(null);
  const [showLog, setShowLog] = useState(false);
  const load = () => api('/actions').then(d => setActs(d.actions)).catch(e => toast(e.message, 'bad'));
  const [run, start] = useRunner(load);
  useEffect(() => { load(); api('/services').then(d => setUnits([...d.custom, ...d.system].map(s => s.name))).catch(() => {}); }, []);
  const saveAll = async (list) => {
    try { const d = await api('/actions', { method: 'PUT', body: { items: list } }); setActs(d.actions); return true; }
    catch (e) { toast(e.message, 'bad'); return false; }
  };
  const save = async (a) => { if (await saveAll(a.id ? acts.map(x => x.id === a.id ? a : x) : [...acts, a])) setEdit(null); };
  const remove = async (a) => { if (await confirm({ title: `Delete "${a.name}"?`, ok: 'Delete', danger: true })) saveAll(acts.filter(x => x.id !== a.id)); };
  const move = (a, d) => { const i = acts.indexOf(a), j = i + d; if (j < 0 || j >= acts.length) return; const n = [...acts]; [n[i], n[j]] = [n[j], n[i]]; saveAll(n); };
  const go = (a) => { setShowLog(true); start(a); };
  if (!acts) return html`<div class="page"><div class="empty"><span class="spinner"></span></div></div>`;
  return html`<div class="page" style="max-width:1100px">
    <div class="page-head"><h1>Quick actions</h1><span class="grow"></span>
      <button class="btn primary" onClick=${() => setEdit({})}><${Icon} name="plus"/>New action</button></div>
    ${acts.length === 0 && html`<div class="empty">No quick actions yet.</div>`}
    <div class="grid" style="gap:12px">${acts.map(a => html`<div class="card" key=${a.id}>
      <div class="row" style="flex-wrap:wrap;gap:10px">
        <div class="grow" style="min-width:200px"><b>${a.name}</b>${a.confirm && html` <span class="pill">asks to confirm</span>`}
          ${a.description && html`<div class="muted" style="font-size:13px">${a.description}</div>`}
          <div class="faint mono" style="font-size:12px;margin-top:4px">${a.steps.map(stepText).join('  →  ')}</div>
          ${a.last && html`<div style="font-size:12px;margin-top:4px" class=${a.last.ok ? 'ok' : 'bad'}>Last run ${fmtAgo(a.last.t)}: ${a.last.ok ? 'succeeded' : 'failed'}</div>`}
        </div>
        <button class="act-btn ${COLORS[a.color] || ''}" style="width:auto" disabled=${run && run.id === a.id && run.state === 'running'} onClick=${() => go(a)}>
          ${run && run.id === a.id && run.state === 'running' ? html`<span class="spinner"></span>` : html`<${Icon} name="play"/>`}<b>Run</b></button>
        <div class="row" style="gap:0">
          <button class="btn ghost sm" onClick=${() => move(a, -1)} title="Move up">↑</button><button class="btn ghost sm" onClick=${() => move(a, 1)} title="Move down">↓</button>
          <button class="btn ghost sm" onClick=${() => setEdit(a)} title="Edit"><${Icon} name="edit"/></button>
          <button class="btn ghost sm" onClick=${() => remove(a)} title="Delete"><${Icon} name="trash"/></button></div>
      </div></div>`)}</div>
    <div class="faint" style="font-size:12px;margin-top:12px">Actions also appear as buttons on the Dashboard.</div>
    ${edit && html`<${ActionEditor} action=${edit} units=${units} onSave=${save} onClose=${() => setEdit(null)} />`}
    ${run && showLog && html`<${RunLog} run=${run} onClose=${() => setShowLog(false)} />`}
  </div>`;
}
