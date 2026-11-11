import { html, useState, useEffect, useRef } from '../../vendor/preact-htm.js';
import { api, qs } from '../api.js';
import { Icon, toast, confirm, fmtAgo } from '../ui.js';

let leafletP = null;
function loadLeaflet() {
  if (!leafletP) leafletP = new Promise((res, rej) => {
    const l = document.createElement('link'); l.rel = 'stylesheet'; l.href = 'vendor/leaflet.css'; document.head.appendChild(l);
    const s = document.createElement('script'); s.src = 'vendor/leaflet.js'; s.onload = () => res(window.L); s.onerror = rej;
    document.head.appendChild(s);
  });
  return leafletP;
}

const SAT_URL = 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}';
const LABELS_URL = 'https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}';
const confClass = (c) => c == null ? '' : c >= 0.75 ? 'hi' : c >= 0.5 ? 'mid' : 'lo';
const fmtM = (m) => m >= 1000 ? (m / 1000).toFixed(2) + ' km' : m.toFixed(m < 10 ? 2 : 1) + ' m';
const earthUrl = (t) => `https://earth.google.com/web/search/${t.lat.toFixed(7)},${t.lon.toFixed(7)}/`;
const isPhone = () => window.matchMedia('(max-width: 760px)').matches;

// ------------------------------------------------------------ zoomable photo
function ImageZoom({ t, onClose, initialView = 'det', focus = false }) {
  const [v, setV] = useState({ s: 1, x: 0, y: 0 });
  const [view, setView] = useState(t.raw_image ? initialView : 'det');
  const box = useRef();
  const imgRef = useRef();
  const g = useRef({ focusPending: focus });
  const src = view === 'raw' && t.raw_image ? t.raw_image : t.image;
  // zoom so the detected box fills about half the screen, centred
  const toTarget = () => {
    const im = imgRef.current, b = t.box;
    if (!im || !b) return;
    const dw = im.offsetWidth, dh = im.offsetHeight;
    const bw = Math.max(b[2] - b[0], 0.02), bh = Math.max(b[3] - b[1], 0.02);
    const s = Math.max(1, Math.min(8, 0.5 / Math.max(bw, bh * dh / dw * (dw / dh))));
    const cx = ((b[0] + b[2]) / 2 - 0.5) * dw, cy = ((b[1] + b[3]) / 2 - 0.5) * dh;
    setV({ s, x: -s * cx, y: -s * cy });
  };
  const clamp = (s) => Math.max(1, Math.min(8, s));
  const zoomAt = (factor, cx, cy) => setV(o => {
    const s = clamp(o.s * factor);
    const r = box.current.getBoundingClientRect();
    const px = cx - r.left - r.width / 2, py = cy - r.top - r.height / 2;
    const k = s / o.s;
    return s === 1 ? { s: 1, x: 0, y: 0 } : { s, x: px - (px - o.x) * k, y: py - (py - o.y) * k };
  });
  useEffect(() => {
    const k = (e) => { if (e.key === 'r' || e.key === 'R') setView(x => t.raw_image ? (x === 'raw' ? 'det' : 'raw') : x); if (e.key === 't' || e.key === 'T') toTarget(); if (e.key === 'Escape') onClose(); if (e.key === '+' || e.key === '=') setV(o => ({ ...o, s: clamp(o.s * 1.4) })); if (e.key === '-') setV(o => clamp(o.s / 1.4) === 1 ? { s: 1, x: 0, y: 0 } : { ...o, s: clamp(o.s / 1.4) }); };
    window.addEventListener('keydown', k);
    return () => window.removeEventListener('keydown', k);
  }, []);
  const onWheel = (e) => { e.preventDefault(); zoomAt(e.deltaY < 0 ? 1.25 : 0.8, e.clientX, e.clientY); };
  const pts = g.current.pts || (g.current.pts = new Map());
  const down = (e) => { box.current.setPointerCapture(e.pointerId); pts.set(e.pointerId, { x: e.clientX, y: e.clientY }); g.current.moved = false; };
  const move = (e) => {
    if (!pts.has(e.pointerId)) return;
    const prev = pts.get(e.pointerId);
    pts.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (pts.size === 2) {
      const [a, b] = [...pts.values()];
      const d = Math.hypot(a.x - b.x, a.y - b.y);
      if (g.current.dist) zoomAt(d / g.current.dist, (a.x + b.x) / 2, (a.y + b.y) / 2);
      g.current.dist = d;
    } else if (pts.size === 1) {
      const dx = e.clientX - prev.x, dy = e.clientY - prev.y;
      if (Math.abs(dx) + Math.abs(dy) > 2) g.current.moved = true;
      setV(o => o.s === 1 ? o : { ...o, x: o.x + dx, y: o.y + dy });
    }
  };
  const up = (e) => {
    pts.delete(e.pointerId);
    if (pts.size < 2) g.current.dist = null;
    if (pts.size === 0 && !g.current.moved) { // tap: double-tap toggles zoom
      const now = Date.now();
      if (now - (g.current.lastTap || 0) < 300) { if (v.s > 1) setV({ s: 1, x: 0, y: 0 }); else zoomAt(3, e.clientX, e.clientY); g.current.lastTap = 0; }
      else g.current.lastTap = now;
    }
  };
  return html`<div class="zoom-overlay">
    <div class="zoom-bar">
      <b class="grow ellipsis">${t.label}${t.ignored ? ' (ignored)' : ''} <span class="faint mono hide-sm" style="font-weight:400;font-size:12px">${t.lat.toFixed(7)}, ${t.lon.toFixed(7)}</span></b>
      ${t.raw_image && html`<div class="seg"><button class=${view === 'det' ? 'on' : ''} onClick=${() => setView('det')}>Detection</button><button class=${view === 'raw' ? 'on' : ''} onClick=${() => setView('raw')}>Raw</button></div>`}
      ${t.box && html`<button class="btn sm" onClick=${toTarget} title="Zoom to the target (T)"><${Icon} name="target"/><span class="hide-sm">Target</span></button>`}
      <button class="btn sm" onClick=${() => setV(o => { const s = clamp(o.s / 1.5); return s === 1 ? { s: 1, x: 0, y: 0 } : { ...o, s }; })}>−</button>
      <span class="mono num" style="width:46px;text-align:center">${Math.round(v.s * 100)}%</span>
      <button class="btn sm" onClick=${() => setV(o => ({ ...o, s: clamp(o.s * 1.5) }))}>+</button>
      <button class="btn sm" onClick=${() => setV({ s: 1, x: 0, y: 0 })}>Fit</button>
      <button class="btn sm" onClick=${onClose}><${Icon} name="x"/></button>
    </div>
    <div class="zoom-box" ref=${box} onWheel=${onWheel} onPointerDown=${down} onPointerMove=${move} onPointerUp=${up} onPointerCancel=${up}>
      <img ref=${imgRef} src=${'/api/files/raw?' + qs({ path: src })} draggable="false" alt=""
        onLoad=${() => { if (g.current.focusPending) { g.current.focusPending = false; toTarget(); } }}
        style=${{ transform: `translate(${v.x}px, ${v.y}px) scale(${v.s})`, cursor: v.s > 1 ? 'grab' : 'zoom-in' }} />
    </div>
    ${t.raw_image && html`<div class="zoom-badge">${view === 'raw' ? 'RAW frame (geo_rtsp)' : 'DETECTION (with box)'}</div>`}
    <div class="zoom-hint faint">Pinch / scroll to zoom · drag to move · double-tap to zoom${t.raw_image ? ' · R switches raw/detection at the same spot' : ''}${t.box ? ' · T zooms to the target' : ''}</div>
  </div>`;
}

// ------------------------------------------------------------ details panel
function TargetDetail({ t, busy, locked, onToggle, onZoom, onClose }) {
  const [view, setView] = useState('det');
  useEffect(() => setView('det'), [t.id, t.lat, t.lon]);
  const photo = view === 'raw' && t.raw_image ? t.raw_image : t.image;
  const rows = [
    ['Latitude', t.lat.toFixed(7)], ['Longitude', t.lon.toFixed(7)],
    ['Confidence', t.conf], ['Max confidence', t.max_conf], ['Detections', t.detections],
    ['Time', t.time], ['Frame', t.frame], ['From image centre', t.centre_px && t.centre_px + ' px'],
  ].filter(r => r[1] != null && r[1] !== '');
  const copy = () => { const c = `${t.lat.toFixed(7)}, ${t.lon.toFixed(7)}`; navigator.clipboard?.writeText(c).then(() => toast('Copied ' + c, 'ok'), () => toast(c)); };
  return html`<div class="tgt-detail card ${t.ignored ? 'is-ignored' : ''}">
    <div class="row" style="margin-bottom:8px">
      <span class="tgt-pin ${t.ignored ? 'ign' : confClass(t.conf != null ? +t.conf : null)}" style="position:static;flex:none">${t.id}</span>
      <b class="grow">${t.label}</b>
      ${t.ignored ? html`<span class="pill">ignored</span>` : html`<span class="pill ok">active</span>`}
      <button class="btn ghost sm" onClick=${onClose} title="Close"><${Icon} name="x"/></button>
    </div>
    ${t.image && t.raw_image && html`<div class="row" style="margin-bottom:6px;gap:6px">
        <div class="seg grow"><button class=${view === 'det' ? 'on' : ''} onClick=${() => setView('det')} style="flex:1">Detection</button><button class=${view === 'raw' ? 'on' : ''} onClick=${() => setView('raw')} style="flex:1">Raw</button></div>
        ${t.box && html`<button class="btn sm" onClick=${() => onZoom(view, true)} title="Open zoomed in on the target"><${Icon} name="target"/>Zoom to target</button>`}</div>`}
    ${t.image ? html`<button class="tgt-photo" onClick=${() => onZoom(view, false)} title="Open zoomable photo">
        <img src=${'/api/files/thumb?' + qs({ path: photo, w: 640 })} alt="" />
        ${t.raw_image && html`<span class="tgt-photo-tag">${view === 'raw' ? 'RAW' : 'DETECTION'}</span>`}
        <span class="tgt-photo-hint"><${Icon} name="search"/>Tap to zoom</span></button>`
      : html`<div class="faint" style="font-size:12px;margin-bottom:8px">No photo found for this target</div>`}
    <table class="tgt-table">${rows.map(r => html`<tr><td>${r[0]}</td><td>${r[1]}</td></tr>`)}</table>
    <div class="row" style="gap:6px;margin-top:10px;flex-wrap:wrap">
      ${t.ignored
        ? html`<button class="btn primary" disabled=${busy || locked} title=${locked ? 'The targets file is still being written; wait until it stops changing' : ''} onClick=${onToggle}>${busy ? html`<span class="spinner"></span>` : html`<${Icon} name="refresh"/>`}Restore target</button>`
        : html`<button class="btn danger solid" disabled=${busy || locked} title=${locked ? 'The targets file is still being written; wait until it stops changing' : ''} onClick=${onToggle}>${busy ? html`<span class="spinner"></span>` : html`<${Icon} name="eyeoff"/>`}Ignore target</button>`}
      <a class="btn" href=${earthUrl(t)} target="_blank" rel="noopener" title="Opens Google Earth at this exact point"><${Icon} name="target"/>Google Earth</a>
      <button class="btn" onClick=${copy}><${Icon} name="copy"/>Copy</button>
    </div>
    ${locked && html`<div class="warn" style="font-size:11.5px;margin-top:6px">Ignore/restore is locked while the targets file is still being written (scout running). It unlocks a minute after the file stops changing.</div>`}
    ${t.ignored && html`<div class="faint" style="font-size:11.5px;margin-top:6px">Commented out in the targets file: the drop mission will skip it.</div>`}
  </div>`;
}

// ------------------------------------------------------------ page
export function MapPage({ params = {} }) {
  const [filesList, setFilesList] = useState(null);
  const [path, setPath] = useState('');
  const [data, setData] = useState(null);
  const [labels, setLabels] = useState(true);
  const [measuring, setMeasuring] = useState(false);
  const [measure, setMeasure] = useState({ pts: 0, total: 0 });
  const [cursor, setCursor] = useState(null);
  const [ready, setReady] = useState(false);
  const [sel, setSel] = useState(null);       // selected target key
  const [zoom, setZoom] = useState(null);     // target shown full-screen
  const [busy, setBusy] = useState(false);
  const [live, setLive] = useState(true);
  const [newKeys, setNewKeys] = useState(new Set());
  const [, setTick] = useState(0);
  const [phone, setPhone] = useState(isPhone());
  const el = useRef();
  const side = useRef();
  const st = useRef({});
  const key = (t) => `${t.id}|${t.lat}|${t.lon}`;

  useEffect(() => {
    const mq = window.matchMedia('(max-width: 760px)');
    const on = () => setPhone(mq.matches);
    mq.addEventListener('change', on);
    return () => mq.removeEventListener('change', on);
  }, []);

  useEffect(() => {
    api('/map/files').then(d => { setFilesList(d.files); if (d.files[0]) setPath(d.files[0].path); }).catch(e => toast(e.message, 'bad'));
  }, []);

  useEffect(() => {
    let gone = false;
    loadLeaflet().then(L => {
      if (gone || st.current.map) return;
      const map = L.map(el.current, { zoomControl: true, maxZoom: 22, worldCopyJump: true, tap: false }).setView([20.6, 78.9], 5);
      L.control.scale({ imperial: false, maxWidth: 160 }).addTo(map);
      st.current = { L, map, markers: L.layerGroup().addTo(map), measureLayer: L.layerGroup().addTo(map) };
      st.current.base = L.tileLayer(SAT_URL, { maxNativeZoom: 19, maxZoom: 22, attribution: 'Imagery © Esri, Maxar, Earthstar Geographics' }).addTo(map);
      map.on('mousemove', e => setCursor(e.latlng));
      setReady(true);
    }).catch(() => toast('Could not load the map library', 'bad'));
    return () => { gone = true; if (st.current.map) { st.current.map.remove(); st.current = {}; } setReady(false); };
  }, []);

  useEffect(() => {
    const { L, map } = st.current;
    if (!map) return;
    if (st.current.labels) map.removeLayer(st.current.labels);
    st.current.labels = labels ? L.tileLayer(LABELS_URL, { maxNativeZoom: 19, maxZoom: 22 }).addTo(map) : null;
  }, [labels, ready]);

  const load = () => path && api('/map/targets?' + qs({ path })).then(setData).catch(e => toast(e.message, 'bad'));
  useEffect(() => { setSel(null); load(); }, [path]);

  // live updates: cheap change check every 3 s while the tab is visible; reload only on change
  const dataRef = useRef(null); dataRef.current = data;
  useEffect(() => {
    const t = setInterval(() => setTick(x => x + 1), 5000); // refresh "updated … ago" and the write lock
    return () => clearInterval(t);
  }, []);
  useEffect(() => {
    if (!live || !path) return;
    let stopped = false, inflight = false;
    const check = async () => {
      if (stopped || inflight || document.hidden || !dataRef.current) return;
      inflight = true;
      try {
        const st = await api('/map/stat?' + qs({ path }));
        const cur = dataRef.current;
        if (cur && (Math.abs(st.mtime - cur.mtime) > 1e-6)) {
          const d = await api('/map/targets?' + qs({ path }));
          if (stopped) return;
          const before = new Set(cur.targets.map(key));
          const added = d.targets.filter(t => !before.has(key(t))).map(key);
          if (added.length) {
            setNewKeys(new Set(added));
            setTimeout(() => setNewKeys(new Set()), 8000);
          }
          setSel(s0 => (s0 && d.targets.some(t => key(t) === s0)) ? s0 : null);
          setData(d);
        }
      } catch (e) { /* transient: try again next tick */ }
      inflight = false;
    };
    const t = setInterval(check, 3000);
    return () => { stopped = true; clearInterval(t); };
  }, [live, path]);

  // markers
  useEffect(() => {
    const { L, map, markers } = st.current;
    if (!map || !data) return;
    markers.clearLayers();
    st.current.byKey = {};
    data.targets.forEach(t => {
      const k = key(t);
      const cls = t.ignored ? 'ign' : confClass(t.conf != null ? +t.conf : null);
      const icon = L.divIcon({ className: '', html: `<div class="tgt-pin ${cls} ${sel === k ? 'sel' : ''} ${newKeys.has(k) ? 'new' : ''}">${t.id}</div>`, iconSize: [30, 30], iconAnchor: [15, 15] });
      const m = L.marker([t.lat, t.lon], { icon, title: t.label + (t.ignored ? ' (ignored)' : ''), zIndexOffset: t.ignored ? -100 : sel === k ? 1000 : 0 });
      m.on('click', () => focus(t));
      markers.addLayer(m);
      st.current.byKey[k] = m;
    });
  }, [data, ready, sel, newKeys]);

  const fitDone = useRef('');
  const fit = () => {
    const { L, map } = st.current;
    if (!map || !data || !data.targets.length) return;
    if (data.targets.length === 1) map.setView([data.targets[0].lat, data.targets[0].lon], 19);
    else map.fitBounds(L.latLngBounds(data.targets.map(t => [t.lat, t.lon])).pad(0.3), { maxZoom: 20 });
  };
  useEffect(() => { if (ready && data && fitDone.current !== path) { fitDone.current = path; fit(); } }, [ready, data]);
  useEffect(() => { // #/map?target=5 opens that target
    if (ready && data && params.target && !sel) { const t = data.targets.find(x => x.id === params.target); if (t) setTimeout(() => { focus(t); if (params.zoom && t.image) setZoom({ t, view: params.view || 'det', focus: params.zoom === 'target' }); }, 300); }
  }, [ready, data]);

  const focus = (t) => {
    const { map } = st.current;
    setSel(key(t));
    map.setView([t.lat, t.lon], Math.max(map.getZoom(), 19));
    if (phone) setTimeout(() => {
      // keep the (centred) pin visible at the top and bring the details into view below it
      const wrap = el.current.parentElement, main = document.querySelector('.main');
      if (!wrap || !main) return;
      const top = wrap.getBoundingClientRect().top - main.getBoundingClientRect().top + main.scrollTop;
      main.scrollTo({ top: top + wrap.clientHeight * 0.38, behavior: 'smooth' });
    }, 80);
  };

  const toggle = async (t) => {
    if (!t.ignored && !(await confirm({ title: `Ignore target ${t.id}?`, ok: 'Ignore', danger: true,
      body: `Its line in ${data.name} will be commented out, so the drop mission skips it. You can restore it at any time.` }))) return;
    setBusy(true);
    try {
      const d = await api('/map/ignore', { body: { path, id: t.id, lat: t.lat, lon: t.lon, ignored: !t.ignored, mtime: data.mtime } });
      setData(d);
      toast(`Target ${t.id} ${t.ignored ? 'restored' : 'ignored'}`, 'ok');
    } catch (e) {
      toast(e.message, 'bad');
      if (e.status === 409 || e.status === 404) load();
    }
    setBusy(false);
  };

  // measuring
  useEffect(() => {
    const { L, map, measureLayer } = st.current;
    if (!map) return;
    measureLayer.clearLayers();
    setMeasure({ pts: 0, total: 0 });
    if (!measuring) { map.getContainer().style.cursor = ''; return; }
    map.getContainer().style.cursor = 'crosshair';
    const pts = [];
    const line = L.polyline([], { color: '#22d3ee', weight: 3, dashArray: '6 6' }).addTo(measureLayer);
    const onClick = (e) => {
      pts.push(e.latlng);
      line.setLatLngs(pts);
      L.circleMarker(e.latlng, { radius: 5, color: '#22d3ee', fillColor: '#0b0f14', fillOpacity: 1, weight: 2 }).addTo(measureLayer);
      let total = 0;
      for (let i = 1; i < pts.length; i++) total += pts[i - 1].distanceTo(pts[i]);
      if (pts.length > 1) L.tooltip({ permanent: true, direction: 'top', className: 'measure-tip' }).setLatLng(e.latlng).setContent(fmtM(total)).addTo(measureLayer);
      setMeasure({ pts: pts.length, total });
    };
    map.on('click', onClick);
    const esc = (e) => { if (e.key === 'Escape') setMeasuring(false); };
    window.addEventListener('keydown', esc);
    return () => { map.off('click', onClick); window.removeEventListener('keydown', esc); };
  }, [measuring, ready]);

  const file = filesList && filesList.find(f => f.path === path);
  const selT = data && sel ? data.targets.find(t => key(t) === sel) : null;
  const nIgn = data ? data.targets.filter(t => t.ignored).length : 0;
  const writing = data && !data.own_write && Date.now() / 1000 - data.mtime < 60; // changed by something other than this panel
  const detail = selT && html`<${TargetDetail} t=${selT} busy=${busy} locked=${writing} onToggle=${() => toggle(selT)} onZoom=${(view, focus) => setZoom({ t: selT, view, focus })} onClose=${() => setSel(null)} />`;

  return html`<div class="page-full map-page">
    <div class="page-head" style="margin-bottom:10px">
      <h1>Map</h1>
      ${filesList && filesList.length > 1 && html`<select class="input" value=${path} onChange=${e => setPath(e.target.value)}>
        ${filesList.map(f => html`<option value=${f.path}>${f.name} (${f.count})</option>`)}</select>`}
      ${data && html`<span class="faint" style="font-size:12px">${data.targets.length - nIgn} active${nIgn ? ` · ${nIgn} ignored` : ''} · updated ${fmtAgo(data.mtime)}</span>`}
      <button class="live-toggle ${live ? 'on' : ''}" onClick=${() => setLive(!live)} title=${live ? 'Live: checks for changes every 3 s. Click to pause' : 'Paused. Click to resume live updates'}>
        <span class="dot ${live ? 'ok' : ''}"></span>${live ? 'Live' : 'Paused'}</button>
      <span class="grow"></span>
      <label class="row muted" style="font-size:13px;gap:5px"><input type="checkbox" checked=${labels} onChange=${() => setLabels(!labels)} />Labels</label>
      <button class="btn" onClick=${fit} title="Zoom to all targets"><${Icon} name="expand"/><span class="hide-sm">Fit</span></button>
      <button class="btn ${measuring ? 'on' : ''}" onClick=${() => setMeasuring(!measuring)} title="Measure distance"><${Icon} name="move"/><span class="hide-sm">${measuring ? 'Stop measuring' : 'Measure'}</span></button>
      ${path && html`<a class="btn" href=${'/api/map/kmz?' + qs({ path })} title="All targets as a Google Earth / Mission Planner file"><${Icon} name="download"/><span class="hide-sm">KMZ</span></a>`}
    </div>
    ${writing && html`<div class="banner warn">The targets file is being updated (scout pipeline running?). The map follows it live; ignoring/restoring targets is locked until it has stopped changing for a minute, so your changes aren't overwritten.</div>`}
    ${filesList && filesList.length === 0 && html`<div class="banner warn">No result files with coordinates. Add one (e.g. ~/targets_clustered.txt) to a data source under Flight data → Configure.</div>`}
    <div class="map-layout">
      <div class="map-wrap">
        <div ref=${el} class="map-el"></div>
        ${measuring && html`<div class="map-hint">${measure.pts < 2 ? 'Click points on the map to measure' : `Total: ${fmtM(measure.total)}`} · Esc to stop</div>`}
        ${cursor && !phone && html`<div class="map-coords mono">${cursor.lat.toFixed(7)}, ${cursor.lng.toFixed(7)}</div>`}
      </div>
      <div class="map-side" ref=${side}>
        ${detail}
        <div class="card map-list">
          <h3 style="margin:0 0 6px">Targets ${data ? `(${data.targets.length})` : ''}</h3>
          ${data && data.targets.length === 0 && html`<div class="muted">No targets in this file</div>`}
          ${data && html`<div class="list-rows">${data.targets.map(t => html`<div class="tgt-row ${sel === key(t) ? 'sel' : ''} ${t.ignored ? 'is-ignored' : ''}" onClick=${() => focus(t)}>
            <span class="tgt-pin ${t.ignored ? 'ign' : confClass(t.conf != null ? +t.conf : null)}" style="position:static;flex:none">${t.id}</span>
            <div class="grow" style="min-width:0"><div class="mono" style="font-size:12px">${t.lat.toFixed(7)}, ${t.lon.toFixed(7)}</div>
              <div class="faint" style="font-size:11.5px">${t.ignored ? 'ignored · ' : ''}${[t.conf && 'conf ' + t.conf, t.detections && t.detections + ' det.', t.time && t.time.slice(11, 19)].filter(Boolean).join(' · ')}</div></div>
            ${t.image && html`<img src=${'/api/files/thumb?' + qs({ path: t.image, w: 160 })} style="width:52px;height:39px;object-fit:cover;border-radius:4px" loading="lazy" />`}
          </div>`)}</div>`}
        </div>
      </div>
    </div>
    <div class="faint" style="font-size:11.5px;margin-top:6px">Tap a target for its photo and details. Points are plotted at the exact file coordinates; the satellite imagery itself can be a few metres off.</div>
    ${zoom && html`<${ImageZoom} t=${zoom.t} initialView=${zoom.view} focus=${zoom.focus} onClose=${() => setZoom(null)} />`}
  </div>`;
}
