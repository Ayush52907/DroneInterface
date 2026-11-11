import { html, useState, useEffect, useCallback } from '../../vendor/preact-htm.js';
import { api } from '../api.js';
import { Icon, AddPF, toast, confirm, fmtBytes, fmtTime, fmtAgo, Bar } from '../ui.js';

function LinkPill({ n }) {
  if (!n.admin_up) return html`<span class="pill">disabled</span>`;
  if (n.kind === 'can') {
    const st = n.can?.state || n.state;
    const cls = st === 'ERROR-ACTIVE' ? 'ok' : st === 'STOPPED' || st === 'BUS-OFF' ? 'bad' : 'warn';
    return html`<span class="pill ${cls}"><span class="dot ${cls}"></span>${st}</span>`;
  }
  return n.carrier ? html`<span class="pill ok"><span class="dot ok"></span>link up</span>`
    : html`<span class="pill bad"><span class="dot bad"></span>no link</span>`;
}

function signalPct(dbm) { return Math.max(0, Math.min(100, (dbm + 90) * 2)); }

function NetCard({ n }) {
  const w = n.wifi || {};
  return html`<div class="card">
    <div class="row" style="margin-bottom:10px">
      <${Icon} name=${n.kind === 'wifi' ? 'wifi' : n.kind === 'can' ? 'chip' : 'plug'} cls="ficon" />
      <b class="mono grow ellipsis">${n.name}</b>
      <${LinkPill} n=${n} />
    </div>
    <div class="kv">
      <span>Type</span><span>${n.kind === 'ethernet' ? 'Ethernet' : n.kind === 'wifi' ? 'Wi-Fi' : n.kind === 'can' ? 'CAN bus' : n.kind}${n.driver ? html` <span class="faint">(${n.driver})</span>` : ''}</span>
      ${n.kind === 'ethernet' && html`<span>Speed</span><span>${n.carrier && n.speed ? `${n.speed} Mb/s ${n.duplex || ''}` : '–'}</span>`}
      ${n.ipv4.length > 0 && html`<span>IP</span><span class="mono">${n.ipv4.join(', ')}</span>`}
      ${n.kind === 'wifi' && html`
        <span>Mode</span><span>${w.mode ? html`<span class="pill ${w.mode === 'monitor' ? 'info' : ''}">${w.mode}</span>` : '–'}${w.mode === 'monitor' ? html` <span class="faint">(WFB-ng)</span>` : ''}</span>
        ${w.ssid && html`<span>Network</span><span>${w.ssid}</span>`}
        ${w.channel && html`<span>Channel</span><span>${w.channel} (${w.freq} MHz${w.width ? `, ${w.width} MHz wide` : ''})</span>`}
        ${w.signal !== undefined && html`<span>Signal</span><span><div class="row"><span class="num" style="width:64px">${w.signal} dBm</span><div class="grow"><${Bar} pct=${signalPct(w.signal)} cls=${w.signal < -75 ? 'bad' : w.signal < -65 ? 'warn' : ''} /></div></div></span>`}
        ${w.tx_bitrate && html`<span>Bitrate</span><span>↑ ${w.tx_bitrate} · ↓ ${w.rx_bitrate || '–'}</span>`}
        ${w.txpower !== undefined && html`<span>TX power</span><span>${w.txpower} dBm</span>`}`}
      ${n.kind === 'can' && n.can && html`
        <span>Bitrate</span><span>${n.can.bitrate ? n.can.bitrate / 1000 + ' kbit/s' : 'not configured'}</span>
        <span>Error counters</span><span class="num">TX ${n.can.tx_err ?? '–'} · RX ${n.can.rx_err ?? '–'}</span>
        <span>Frames</span><span class="num">RX ${n.can.rx_packets ?? '–'} · TX ${n.can.tx_packets ?? '–'}</span>`}
      ${n.kind !== 'can' && html`<span>Traffic</span><span class="num">↓ ${fmtBytes(n.rx_bytes)} · ↑ ${fmtBytes(n.tx_bytes)}</span>`}
      ${(n.rx_errors + n.tx_errors + n.rx_dropped) > 0 && html`<span>Errors</span><span class="warn num">RX ${n.rx_errors} · TX ${n.tx_errors} · dropped ${n.rx_dropped}</span>`}
      <span>MAC</span><span class="mono faint">${n.mac}</span>
    </div>
    ${n.kind !== 'can' && html`<div class="row" style="justify-content:flex-end;margin-top:8px">
      <${AddPF} check=${{ type: 'link', iface: n.name, name: `${n.name} link up` }} /></div>`}
  </div>`;
}

function usbTree(devs) {
  const kids = {};
  devs.forEach(d => { (kids[d.parent || 'root'] = kids[d.parent || 'root'] || []).push(d); });
  const out = [];
  const walk = (id, depth) => (kids[id] || []).forEach(d => { out.push({ d, depth }); walk(d.id, depth + 1); });
  walk('root', 0);
  return out;
}

function PingTool() {
  const [host, setHost] = useState('');
  const [res, setRes] = useState(null);
  const [busy, setBusy] = useState(false);
  const go = async (e) => {
    e.preventDefault();
    if (!host) return;
    setBusy(true);
    try { setRes({ host, ...(await api('/hardware/ping', { body: { host } })) }); } catch (er) { toast(er.message, 'bad'); }
    setBusy(false);
  };
  return html`<form class="row" style="gap:6px;margin-top:10px" onSubmit=${go}>
    <input class="input grow" placeholder="Ping any host / IP…" value=${host} onInput=${e => setHost(e.target.value)} />
    <button class="btn" disabled=${busy}>${busy ? html`<span class="spinner"></span>` : 'Ping'}</button>
    ${res && html`<span class=${res.ok ? 'ok' : 'bad'} style="font-size:13px">${res.host}: ${res.ok ? res.ms + ' ms' : res.detail}</span>`}
  </form>`;
}

export function Hardware() {
  const [d, setD] = useState(null);
  const [err, setErr] = useState(null);
  const [scan, setScan] = useState({});
  const [showOther, setShowOther] = useState(false);

  const load = useCallback(() => api('/hardware').then(r => { setD(r); setErr(null); }).catch(e => setErr(e.message)), []);
  useEffect(() => { load(); const t = setInterval(load, 3000); return () => clearInterval(t); }, [load]);

  const doScan = async (bus) => {
    const ok = await confirm({ title: `Scan I²C bus ${bus}?`, ok: 'Scan',
      body: 'Probing addresses is normally harmless, but a few devices can misbehave when probed. Avoid scanning buses with flight-critical sensors while flying.' });
    if (!ok) return;
    setScan(s => ({ ...s, [bus]: { busy: true } }));
    try { const r = await api('/hardware/i2c-scan', { body: { bus } }); setScan(s => ({ ...s, [bus]: { res: r } })); }
    catch (e) { setScan(s => ({ ...s, [bus]: { res: { error: e.message } } })); }
  };

  if (!d) return html`<div class="page">${err ? html`<div class="banner">${err}</div>` : html`<div class="empty"><span class="spinner"></span></div>`}</div>`;
  const main = d.network.filter(n => ['ethernet', 'wifi', 'can'].includes(n.kind));
  const other = d.network.filter(n => !['ethernet', 'wifi', 'can'].includes(n.kind));
  const usb = usbTree(d.usb);

  return html`<div class="page">
    <div class="page-head"><h1>Hardware & Connections</h1><span class="grow"></span>
      <span class="faint" style="font-size:12px">Live · updated ${fmtTime(d.t)}</span></div>

    <div class="grid hw-top">
      <div class="card">
        <h3>Network devices</h3>
        <div class="list-rows">
          ${d.targets.length === 0 && html`<div class="muted">No devices configured. Add them under <span class="mono">network_targets</span> in config.yaml.</div>`}
          ${d.targets.map(t => html`<div>
            <span class="dot ${t.ok ? 'ok' : 'bad'}"></span>
            <div class="grow" style="min-width:0"><div>${t.name}</div><div class="faint mono" style="font-size:12px">${t.kind === 'rtsp' ? `rtsp://${t.host}:${t.port}${t.path || ''}` : t.kind === 'tcp' ? `${t.host}:${t.port}` : t.host}</div></div>
            <span class=${t.ok ? 'ok' : 'bad'} style="font-size:13px;text-align:right">${t.ok ? (t.ms !== undefined ? t.ms + ' ms' : 'ok') : t.detail}</span>
            <${AddPF} check=${t.kind === 'rtsp' ? { type: 'rtsp', host: t.host, port: t.port, path: t.path || '/', name: t.name } : t.kind === 'tcp' ? { type: 'tcp', host: t.host, port: t.port, name: t.name } : { type: 'ping', host: t.host, name: t.name + ' reachable' }} label="" />
          </div>`)}
        </div>
        <${PingTool} />
      </div>
      <div class="card">
        <h3>Connection events</h3>
        <div class="list-rows" style="max-height:260px;overflow:auto">
          ${d.events.map(e => html`<div><span class="faint num" style="font-size:12px;white-space:nowrap" title=${new Date(e.t * 1000).toLocaleString()}>${fmtTime(e.t)}</span>
            <span class="${e.level} grow">${e.msg}</span></div>`)}
        </div>
      </div>
    </div>

    <h2 class="section-title">Network interfaces</h2>
    <div class="grid g3">${main.map(n => html`<${NetCard} n=${n} key=${n.name} />`)}</div>
    ${other.length > 0 && html`<button class="btn ghost sm" style="margin-top:8px" onClick=${() => setShowOther(!showOther)}>${showOther ? '▾' : '▸'} ${other.length} other interfaces (USB device-mode, bridges, VPN)</button>
      ${showOther && html`<div class="card" style="margin-top:8px"><div class="list-rows">${other.map(n => html`<div>
        <b class="mono" style="width:140px">${n.name}</b><span class="faint grow">${n.kind === 'usb-gadget' ? 'USB device-mode (Jetson as USB network to a PC)' : n.kind}</span>
        <span class="mono faint">${n.ipv4.join(', ')}</span><${LinkPill} n=${n} /></div>`)}</div></div>`}`}

    <div class="grid g2" style="margin-top:20px">
      <div class="card">
        <h3>Serial ports</h3>
        <div class="list-rows">
          ${d.serial.length === 0 && html`<div class="muted">No serial ports</div>`}
          ${d.serial.map(s => html`<div style="align-items:flex-start">
            <div class="grow" style="min-width:0">
              <div class="row"><b class="mono">${s.path}</b><span class="pill">${s.kind}</span>${!s.access && html`<span class="pill warn" title="The panel user lacks read/write permission">no access</span>`}</div>
              ${s.usb && html`<div class="faint" style="font-size:12px">${s.usb.manufacturer || ''} ${s.usb.product || ''} · ${s.usb.vid}:${s.usb.pid} · USB port ${s.usb.id}</div>`}
              ${s.by_id && html`<div class="faint mono ellipsis" style="font-size:11px" title=${'/dev/serial/by-id/' + s.by_id}>by-id/${s.by_id}</div>`}
              <div style="font-size:13px;margin-top:3px">${s.owners.length
                ? s.owners.map(o => html`<div class="info">● in use by <b>${o.unit ? o.unit.replace('.service', '') : o.comm}</b> <span class="faint">(${o.comm}, pid ${o.pid})</span></div>`)
                : html`<span class="faint">${s.owners_hidden ? 'not used by any visible process' : 'free'}</span>`}</div>
            </div>
            <div class="row" style="gap:0"><${AddPF} check=${{ type: 'device', path: s.path, name: `${s.path} present` }} label="" />
              ${s.owners.length > 0 && html`<${AddPF} check=${{ type: 'serial_owner', path: s.path, name: `${s.path} in use (${s.owners[0].unit ? s.owners[0].unit.replace('.service', '') : s.owners[0].comm})` }} label="in use" />`}</div>
          </div>`)}
        </div>
      </div>

      <div class="card">
        <h3>USB devices</h3>
        <div class="list-rows">
          ${usb.map(({ d: u, depth }) => html`<div style=${{ paddingLeft: depth * 18 + 'px', alignItems: 'flex-start' }}>
            <${Icon} name=${u.drivers.includes('hub') || u.root_hub ? 'chip' : 'usb'} cls="ficon ${u.root_hub ? 'faint' : ''}" />
            <div class="grow" style="min-width:0">
              <div class=${u.root_hub ? 'muted' : ''}><b>${u.product || 'Unknown device'}</b> ${u.manufacturer && !u.root_hub ? html`<span class="faint">· ${u.manufacturer}</span>` : ''}</div>
              <div class="faint" style="font-size:12px">${u.root_hub ? u.speed + ' root' : html`${u.vid}:${u.pid} · port ${u.id}${u.drivers.length ? ' · ' + u.drivers.join(', ') : ''}${u.classes.length ? ' · ' + u.classes.join(', ') : ''}`}</div>
            </div>
            ${!u.root_hub && html`<span class="pill ${u.speed_mbps >= 5000 ? 'info' : ''}">${(u.speed || '').replace(/ \(.*\)/, '')}</span>
              <${AddPF} check=${{ type: 'usb', vid: u.vid, pid: u.pid, name: `${u.product || u.vid + ':' + u.pid} connected` }} label="" />`}
          </div>`)}
        </div>
      </div>

      <div class="card">
        <h3>Cameras</h3>
        <div class="list-rows">
          ${d.video.length === 0 && html`<div class="muted">No local cameras (/dev/video*). Network cameras appear under Network devices.</div>`}
          ${d.video.map(v => html`<div><b class="mono">${v.path}</b><span class="grow">${v.name}</span>
            ${v.owners.length ? html`<span class="info" style="font-size:13px">in use by ${v.owners[0].unit || v.owners[0].comm}</span>` : html`<span class="faint">free</span>`}
            <${AddPF} check=${{ type: 'device', path: v.path, name: `${v.name || v.path} present` }} label="" /></div>`)}
        </div>
      </div>

      <div class="card">
        <h3>I²C buses</h3>
        <div class="list-rows">
          ${d.i2c.map(b => html`<div style="flex-wrap:wrap">
            <b class="mono" style="width:56px">i2c-${b.bus}</b><span class="grow faint ellipsis">${b.name}</span>
            <button class="btn sm" disabled=${scan[b.bus]?.busy} onClick=${() => doScan(b.bus)}>${scan[b.bus]?.busy ? html`<span class="spinner"></span>` : 'Scan'}</button>
            ${scan[b.bus]?.res && html`<div style="width:100%;font-size:12.5px" class=${scan[b.bus].res.error ? 'bad' : ''}>
              ${scan[b.bus].res.error || (scan[b.bus].res.devices.length ? scan[b.bus].res.devices.map(x => html`<span class="pill mono" style="margin:2px">${x.addr}${x.in_use ? ' (driver)' : ''}</span>`) : 'No devices found')}</div>`}
          </div>`)}
        </div>
      </div>
    </div>
  </div>`;
}
