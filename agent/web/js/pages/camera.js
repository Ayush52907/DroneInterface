import { html, useState, useEffect, useRef } from '../../vendor/preact-htm.js';
import { api, qs } from '../api.js';
import { Icon, toast } from '../ui.js';

const BLANK = 'data:image/gif;base64,R0lGODlhAQABAAAAACw=';
const LABEL = { low: 'Low · 480p · 5 fps · ~0.6 Mbit/s', medium: 'Medium · 640p · 10 fps · ~2 Mbit/s', high: 'High · 720p · 15 fps · ~7 Mbit/s' };

function fmtLeft(s) { s = Math.max(0, Math.round(s)); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`; }

function Preview({ cam, maxMin, autostart }) {
  const [active, setActive] = useState(false);
  const [quality, setQuality] = useState('medium');
  const [nonce, setNonce] = useState(0);
  const [status, setStatus] = useState(cam);
  const [paused, setPaused] = useState('');
  const [left, setLeft] = useState(0);
  const img = useRef();
  const stage = useRef();

  const stopImg = () => { if (img.current) img.current.src = BLANK; }; // closes the HTTP stream
  const start = () => { setPaused(''); setNonce(n => n + 1); setActive(true); setLeft(maxMin * 60); };
  const stop = (why = '') => { stopImg(); setActive(false); setPaused(why); };

  // countdown + auto stop
  useEffect(() => {
    if (!active) return;
    const t = setInterval(() => setLeft(l => {
      if (l <= 1) { stop(`Preview stopped after ${maxMin} min to save bandwidth.`); return 0; }
      return l - 1;
    }), 1000);
    return () => clearInterval(t);
  }, [active, nonce]);

  // stats while active
  useEffect(() => {
    if (!active) return;
    const poll = () => api('/camera').then(d => setStatus(d.cameras[cam.idx])).catch(() => {});
    poll();
    const t = setInterval(poll, 2000);
    return () => clearInterval(t);
  }, [active]);

  // stop when the tab is hidden or the page is left
  useEffect(() => {
    const vis = () => { if (document.hidden && active) stop('Preview paused because the tab was hidden.'); };
    document.addEventListener('visibilitychange', vis);
    return () => document.removeEventListener('visibilitychange', vis);
  }, [active]);
  useEffect(() => { if (autostart) start(); return () => stopImg(); }, []);

  const changeQuality = (q) => { setQuality(q); if (active) { stopImg(); setNonce(n => n + 1); setLeft(maxMin * 60); } };
  const fullscreen = () => { const el = stage.current; (el.requestFullscreen || el.webkitRequestFullscreen || (() => {})).call(el); };
  const src = `/api/camera/${cam.idx}/stream?` + qs({ quality, n: nonce });
  const err = status.error && !status.running;

  return html`<div class="card cam-card">
    <div class="row" style="margin-bottom:10px;flex-wrap:wrap">
      <${Icon} name="camera" cls="ficon" /><b>${cam.name}</b><span class="faint mono hide-sm" style="font-size:12px">${cam.url}</span>
      <span class="grow"></span>
      <select class="input" value=${quality} onChange=${e => changeQuality(e.target.value)}>
        ${Object.keys(LABEL).map(k => html`<option value=${k}>${LABEL[k]}</option>`)}</select>
    </div>
    <div class="cam-stage" ref=${stage}>
      ${active ? html`<img ref=${img} src=${src} alt="Live preview" onError=${() => setTimeout(() => api('/camera').then(d => setStatus(d.cameras[cam.idx])), 500)} />
        <div class="cam-overlay">
          <span class="pill bad" style="background:#b91c1c;color:#fff;border-color:#b91c1c">● LIVE</span>
          <span class="pill">${status.fps || 0} fps</span><span class="pill hide-sm">${((status.kbps || 0) / 1000).toFixed(1)} Mbit/s</span>
          ${status.viewers > 1 && html`<span class="pill">${status.viewers} watching</span>`}
          <span class="grow"></span><span class="pill" title="Stops automatically">⏱ ${fmtLeft(left)}</span>
        </div>
        ${err && html`<div class="cam-msg bad">${status.error}</div>`}
        ${!err && !status.fps && html`<div class="cam-msg"><span class="spinner"></span> Connecting to camera…</div>`}`
      : html`<div class="cam-msg">
          ${paused && html`<div class="warn" style="margin-bottom:10px">${paused}</div>`}
          <button class="btn primary" style="padding:12px 22px;font-size:15px" onClick=${start}><${Icon} name="play"/>${paused ? 'Resume preview' : 'Start preview'}</button>
          <div class="faint" style="font-size:12px;margin-top:10px;max-width:420px">The preview is off until you start it. It stops on its own after ${maxMin} min, when you leave this page or hide the tab, so the Jetson and the link aren't loaded when nobody is watching.</div>
          ${err && html`<div class="bad" style="margin-top:10px">${status.error}</div>`}
        </div>`}
    </div>
    <div class="row" style="margin-top:10px;flex-wrap:wrap">
      ${active && html`<button class="btn danger" onClick=${() => stop()}><${Icon} name="stop"/>Stop</button>
        <a class="btn" href=${`/api/camera/${cam.idx}/snapshot`} onClick=${(e) => { if (!status.fps) { e.preventDefault(); toast('No frame yet', 'bad'); } }}><${Icon} name="download"/>Snapshot</a>
        <button class="btn" onClick=${fullscreen}><${Icon} name="expand"/><span class="hide-sm">Full screen</span></button>
        <button class="btn ghost" onClick=${() => setLeft(maxMin * 60)} title="Restart the auto-stop timer">+${maxMin} min</button>`}
      <span class="grow"></span>
      <span class="faint" style="font-size:12px">${status.hw ? 'Hardware decoding on Jetson · ' : ''}${status.width}×${status.height}</span>
    </div>
  </div>`;
}

function HoldButton({ onStart, onStop, children, cls = 'btn', title }) {
  const t = useRef(null);
  const down = (e) => { e.preventDefault(); onStart(); clearInterval(t.current); t.current = setInterval(onStart, 250); };
  const up = () => { if (t.current) { clearInterval(t.current); t.current = null; onStop(); } };
  useEffect(() => () => up(), []);
  return html`<button class=${cls} title=${title} onPointerDown=${down} onPointerUp=${up} onPointerLeave=${up} onPointerCancel=${up} onContextMenu=${e => e.preventDefault()}>${children}</button>`;
}

function GimbalPanel() {
  const [g, setG] = useState(null);
  const [busy, setBusy] = useState('');
  const [speed, setSpeed] = useState(40);
  const [steps, setSteps] = useState(null);
  const load = () => api('/gimbal').then(setG).catch(() => {});
  useEffect(() => { load(); const t = setInterval(load, 1000); return () => clearInterval(t); }, []);
  const flight = async () => {
    setBusy('flight'); setSteps(null);
    try { const r = await api('/gimbal/flight', { method: 'POST', body: {} }); setG(r.status); setSteps(r.steps); toast(r.detail, r.ok ? 'ok' : 'bad'); }
    catch (e) { toast(e.message, 'bad'); }
    setBusy('');
  };
  const cmd = (action) => api('/gimbal/cmd', { body: { action } }).then(load).catch(e => toast(e.message, 'bad'));
  const move = (yaw, pitch) => api('/gimbal/move', { body: { yaw: yaw * speed, pitch: pitch * speed } }).catch(() => {});
  const stop = () => api('/gimbal/stop', { method: 'POST', body: {} }).catch(() => {});
  if (!g) return html`<div class="card"><span class="spinner"></span></div>`;
  const a = g.attitude;
  const svcOk = g.service.active === 'active';
  const pos = g.in_position;
  return html`<div class="card gimbal">
    <div class="row" style="flex-wrap:wrap;margin-bottom:12px">
      <${Icon} name="target" cls="ficon" /><b>Gimbal</b>
      <span class="pill ${g.online ? 'ok' : 'bad'}"><span class="dot ${g.online ? 'ok' : 'bad'}"></span>${g.online ? 'connected' : 'not responding'}</span>
      <span class="pill ${svcOk ? 'ok' : 'bad'}" title="This service holds the flight position">${g.service.name.replace('.service', '')}: ${g.service.active}</span>
      ${g.mode && html`<span class="pill ${g.mode === 'FOLLOW' ? 'info' : 'warn'}">${g.mode} mode</span>`}
      ${g.manual && !g.manual_ready && g.online && html`<span class="pill warn"><span class="spinner" style="width:10px;height:10px"></span>switching to manual… (2–3 s)</span>`}
      ${g.recording && html`<span class="pill bad">● REC</span>`}
      ${g.sd_card === 'missing' && html`<span class="pill warn" title="Photos and video are saved on the camera's SD card">no SD card in camera</span>`}
      ${g.sd_card === 'error' && html`<span class="pill bad">camera SD card error</span>`}
      <span class="grow"></span>
      ${a && html`<span class="mono num" style="font-size:13px">pitch <b>${a.pitch.toFixed(1)}°</b> · yaw ${a.yaw.toFixed(1)}° · roll ${a.roll.toFixed(1)}°</span>`}
    </div>

    <div class="flightpos ${pos ? 'ok' : pos === false ? 'warn' : ''}">
      <div class="grow">
        <div style="font-weight:700;font-size:15px">${pos ? '✓ In flight position' : pos === false ? '⚠ Not in flight position' : 'Flight position unknown'}</div>
        <div class="faint" style="font-size:12px">Target pitch ${g.target_pitch}° ±${g.tolerance}°, yaw 0° ±${g.yaw_tolerance}°, in FOLLOW mode · from ${g.target_source.replace(/^\/home\/[^/]+/, '~')}</div>
      </div>
      <button class="btn primary" style="padding:12px 18px;font-size:15px" disabled=${!!busy || !!g.busy} onClick=${flight}>
        ${busy === 'flight' || g.busy ? html`<span class="spinner"></span>` : html`<${Icon} name="target"/>`}Flight position (${g.target_pitch}°)</button>
    </div>
    ${(g.manual || g.mode === 'LOCK') && g.online && html`<div class="banner warn" style="margin:10px 0 0">Manual control: ${g.service.name.replace('.service', '')} is paused (gimbal in ${g.mode} mode). Press <b>Flight position</b> before flying.</div>`}
    ${steps && steps.length > 0 && html`<pre class="pre" style="margin-top:8px;max-height:120px;font-size:11.5px">${steps.join('\n')}</pre>`}

    <details class="manual" style="margin-top:12px">
      <summary>Manual controls</summary>
      <div class="row" style="flex-wrap:wrap;gap:20px;align-items:flex-start;margin-top:12px">
        <div class="dpad">
          <span></span><${HoldButton} title="Tilt up" onStart=${() => move(0, 1)} onStop=${stop}>▲<//><span></span>
          <${HoldButton} title="Pan left" onStart=${() => move(-1, 0)} onStop=${stop}>◀<//>
          <button class="btn" title="Center" onClick=${() => cmd('center')}>⊙</button>
          <${HoldButton} title="Pan right" onStart=${() => move(1, 0)} onStop=${stop}>▶<//>
          <span></span><${HoldButton} title="Tilt down" onStart=${() => move(0, -1)} onStop=${stop}>▼<//><span></span>
        </div>
        <div style="display:grid;gap:10px;min-width:220px">
          <label class="field">Speed: ${speed}%<input type="range" min="10" max="100" step="10" value=${speed} onInput=${e => setSpeed(+e.target.value)} /></label>
          <div class="row" style="gap:6px;flex-wrap:wrap">
            <${HoldButton} title="Zoom in (hold)" onStart=${() => cmd('zoom_in')} onStop=${() => cmd('zoom_stop')}><${Icon} name="zoomin"/>Zoom<//>
            <${HoldButton} title="Zoom out (hold)" onStart=${() => cmd('zoom_out')} onStop=${() => cmd('zoom_stop')}><${Icon} name="zoomout"/>Zoom<//>
          </div>
          <div class="row" style="gap:6px;flex-wrap:wrap">
            <button class="btn" disabled=${g.sd_card && g.sd_card !== 'ok'} title=${g.sd_card === 'ok' ? '' : 'Insert an SD card in the camera'} onClick=${() => cmd('photo')}><${Icon} name="camera"/>Photo</button>
            <button class="btn ${g.recording ? 'danger' : ''}" disabled=${g.sd_card && g.sd_card !== 'ok' && !g.recording} title=${g.sd_card === 'ok' ? '' : 'Insert an SD card in the camera'} onClick=${() => cmd('record')}><${Icon} name="record"/>${g.recording ? 'Stop recording' : 'Record'}</button>
          </div>
          <div class="faint" style="font-size:12px">Hold an arrow to move. The first press switches the gimbal to LOCK mode and waits for siyi-pitch to pause (2–3 s); after that the arrows respond immediately. Flight position hands control back to siyi-pitch. Photos and video are saved on the camera's SD card${g.sd_card === 'missing' ? ', and there is none in the camera right now' : ''}.</div>
        </div>
      </div>
    </details>
  </div>`;
}

export function Camera({ params }) {
  const [d, setD] = useState(null);
  useEffect(() => { api('/camera').then(setD).catch(e => toast(e.message, 'bad')); }, []);
  if (!d) return html`<div class="page"><div class="empty"><span class="spinner"></span></div></div>`;
  return html`<div class="page" style="max-width:1200px">
    <div class="page-head"><h1>Camera</h1><span class="muted">On-demand live preview</span></div>
    ${d.cameras.length === 0 && html`<div class="banner warn">No cameras configured. Add them under <span class="mono">cameras</span> in config.yaml.</div>`}
    <div class="grid" style="gap:16px">${d.cameras.map(c => html`<${Preview} key=${c.idx} cam=${c} maxMin=${d.max_minutes} autostart=${params.start === '1' && c.idx === 0} />`)}
      ${d.gimbal && html`<${GimbalPanel} />`}</div>
  </div>`;
}
