import { html, useState, useEffect, useContext } from '../../vendor/preact-htm.js';
import { AppCtx } from '../ctx.js';
import { api } from '../api.js';
import { Spark, Bar, fmtBytes, fmtRate, fmtDur, level } from '../ui.js';
import { ActionButtons, useRunner, RunLog } from './actions.js';

const compact = (s) => ({ t: s.t, cpu: s.cpu.total, gpu: s.gpu.load, mem: s.mem.percent, temp: s.temp_max,
  power: s.power_total, rx: s.net_total.rx, tx: s.net_total.tx });

export function Dashboard() {
  const { me, stats } = useContext(AppCtx);
  const [hist, setHist] = useState([]);

  useEffect(() => { api('/stats/history').then(d => setHist(d.history)).catch(() => {}); }, []);
  const [acts, setActs] = useState([]);
  const [showLog, setShowLog] = useState(false);
  const loadActs = () => api('/actions').then(d => setActs(d.actions)).catch(() => {});
  const [run, startRun] = useRunner(loadActs);
  useEffect(() => { loadActs(); }, []);
  useEffect(() => {
    if (!stats) return;
    setHist(h => (h.length && h[h.length - 1].t >= stats.t) ? h : [...h.slice(-299), compact(stats)]);
  }, [stats]);

  if (!stats) return html`<div class="page"><div class="empty"><span class="spinner"></span></div></div>`;
  const s = stats, sys = me.system;
  const col = (k) => hist.map(x => x[k]);
  const failed = s.failed_services || [];
  const rootDisk = s.disks.find(d => d.mount === '/') || s.disks[0];
  const ts = s.tailscale;

  return html`<div class="page">
    <div class="page-head">
      <h1>Dashboard</h1>
      <span class="muted">${sys.model}</span>
      <span class="grow"></span>
      ${s.power_mode && html`<span class="pill info" title="nvpmodel power mode">⚡ ${s.power_mode.name}</span>`}
      <span class="pill">Up ${fmtDur(s.uptime)}</span>
    </div>

    ${acts.length > 0 && html`<div style="margin-bottom:14px"><${ActionButtons} actions=${acts} running=${run} compact onRun=${(a) => { setShowLog(true); startRun(a); }} /></div>`}
    ${run && showLog && html`<${RunLog} run=${run} onClose=${() => setShowLog(false)} />`}

    ${failed.length > 0 && html`<a class="banner" href="#/services?filter=failed">
      <b>${failed.length} failed service${failed.length > 1 ? 's' : ''}:</b>
      <span class="ellipsis grow">${failed.join(', ')}</span><span>View →</span></a>`}

    <div class="grid g4">
      <div class="card stat">
        <h3>CPU</h3>
        <div class="big ${level(s.cpu.total, 75, 90)}">${s.cpu.total.toFixed(0)}<small>%</small></div>
        <div class="sub">Load ${s.load.join(' · ')}</div>
        <${Spark} data=${col('cpu')} max=${100} />
      </div>
      <div class="card stat">
        <h3>GPU</h3>
        <div class="big ${level(s.gpu.load, 75, 90)}">${s.gpu.load ?? '–'}<small>${s.gpu.load !== null ? '%' : ''}</small></div>
        <div class="sub">${s.gpu.freq_mhz ? s.gpu.freq_mhz + ' MHz' : sys.has_gpu_load ? '' : 'GPU load not available'}</div>
        <${Spark} data=${col('gpu')} max=${100} color="#a78bfa" />
      </div>
      <div class="card stat">
        <h3>Memory</h3>
        <div class="big ${level(s.mem.percent, 80, 92)}">${s.mem.percent.toFixed(0)}<small>%</small></div>
        <div class="sub">${fmtBytes(s.mem.used)} / ${fmtBytes(s.mem.total)}${s.mem.swap_total ? ` · swap ${fmtBytes(s.mem.swap_used)}` : ''}</div>
        <${Spark} data=${col('mem')} max=${100} color="#34d399" />
      </div>
      <div class="card stat">
        <h3>Temperature</h3>
        <div class="big ${level(s.temp_max, 70, 85)}">${s.temp_max ?? '–'}<small>°C</small></div>
        <div class="sub">Hottest sensor</div>
        <${Spark} data=${col('temp')} color="#fb923c" />
      </div>
      <div class="card stat">
        <h3>Power</h3>
        <div class="big">${s.power_total !== null ? (s.power_total / 1000).toFixed(1) : '–'}<small>W</small></div>
        <div class="sub">${s.power_total !== null ? 'Total board input' : 'No power monitor found'}</div>
        <${Spark} data=${col('power')} color="#facc15" />
      </div>
      <div class="card stat">
        <h3>Network</h3>
        <div class="big" style="font-size:20px">↓ ${fmtRate(s.net_total.rx)}</div>
        <div class="sub">↑ ${fmtRate(s.net_total.tx)}</div>
        <${Spark} data=${col('rx')} color="#60a5fa" />
      </div>
      <div class="card stat">
        <h3>Disk ${rootDisk ? rootDisk.mount : ''}</h3>
        ${rootDisk ? html`
          <div class="big ${level(rootDisk.percent, 85, 95)}">${rootDisk.percent.toFixed(0)}<small>%</small></div>
          <div class="sub">${fmtBytes(rootDisk.total - rootDisk.used)} free of ${fmtBytes(rootDisk.total)}</div>
          <div style="margin-top:14px"><${Bar} pct=${rootDisk.percent} cls=${level(rootDisk.percent, 85, 95)} /></div>` : html`<div class="muted">–</div>`}
      </div>
      <div class="card stat">
        <h3>Tailscale</h3>
        ${ts ? html`
          <div class="big" style="font-size:20px"><span class=${ts.online ? 'ok' : 'bad'}>${ts.online ? 'Connected' : 'Offline'}</span></div>
          <div class="sub mono ellipsis" title=${ts.dns}>${ts.ip}</div>
          <div class="sub">${ts.peers.filter(p => p.online).length} peer(s) online</div>` : html`<div class="muted">Not installed</div>`}
      </div>
    </div>

    <div class="grid g3" style="margin-top:14px">
      <div class="card">
        <h3>CPU cores</h3>
        <div class="cores">${s.cpu.cores.map((c, i) => html`<div class="core">
          <div class="row"><span>#${i}</span><span class="grow"></span><span class="num">${c.toFixed(0)}%</span></div>
          <${Bar} pct=${c} />
          <div class="faint num" style="font-size:11px;margin-top:2px">${s.cpu.freq_mhz[i] ? s.cpu.freq_mhz[i] + ' MHz' : ''}</div>
        </div>`)}</div>
      </div>

      <div class="card">
        <h3>Sensors</h3>
        <div class="list-rows">
          ${s.temps.map(t => html`<div><span class="grow">${t.name}</span><span class="num ${level(t.c, 70, 85)}">${t.c} °C</span></div>`)}
          ${s.power.map(r => html`<div><span class="grow">${r.name}</span><span class="num">${(r.mw / 1000).toFixed(2)} W</span><span class="faint num" style="width:70px;text-align:right">${(r.mv / 1000).toFixed(2)} V</span></div>`)}
          ${s.fans.map(f => html`<div><span class="grow">Fan (${f.name})</span><span class="num">${f.rpm !== null ? f.rpm + ' RPM' : ''} ${f.pct !== null ? f.pct + '%' : ''}</span></div>`)}
          ${!s.temps.length && !s.power.length && html`<div class="muted">No sensors found</div>`}
        </div>
      </div>

      <div class="card">
        <h3>System</h3>
        <div class="kv">
          <span>Host</span><span class="mono">${sys.hostname}</span>
          <span>Model</span><span>${sys.model}</span>
          <span>OS</span><span>${sys.os}</span>
          <span>Kernel</span><span class="mono">${sys.kernel}</span>
          ${sys.l4t && html`<span>L4T</span><span class="mono" style="font-size:12px">${sys.l4t.replace('# ', '').split(', GCID')[0]}</span>`}
          ${s.power_mode && html`<span>Power mode</span><span>${s.power_mode.name} (${s.power_mode.id})</span>`}
          <span>CPU cores</span><span>${sys.cpu_count}</span>
        </div>
      </div>

      <div class="card">
        <h3>Network interfaces</h3>
        <div class="list-rows">
          ${s.net.map(n => html`<div><span class="grow mono ellipsis">${n.name}</span>
            <span class="num faint">↓</span><span class="num" style="width:80px;text-align:right">${fmtRate(n.rx)}</span>
            <span class="num faint">↑</span><span class="num" style="width:80px;text-align:right">${fmtRate(n.tx)}</span></div>`)}
        </div>
      </div>

      <div class="card">
        <h3>Storage</h3>
        <div class="list-rows">
          ${s.disks.map(d => html`<div style="display:grid;gap:4px">
            <div class="row"><span class="mono grow ellipsis">${d.mount}</span><span class="faint">${d.fs}</span>
              <span class="num">${fmtBytes(d.used)} / ${fmtBytes(d.total)}</span></div>
            <${Bar} pct=${d.percent} cls=${level(d.percent, 85, 95)} /></div>`)}
        </div>
      </div>

      ${ts && html`<div class="card">
        <h3>Tailscale peers</h3>
        <div class="list-rows">
          ${ts.peers.map(p => html`<div>
            <span class="dot ${p.online ? 'ok' : ''}"></span>
            <span class="grow ellipsis">${p.name} <span class="faint">${p.os}</span></span>
            ${p.online && html`<span class="pill ${p.direct ? 'ok' : 'warn'}" title=${p.direct ? 'Direct peer-to-peer connection' : 'Traffic goes through a Tailscale relay server (slower)'}>${p.direct ? 'direct' : 'relay ' + p.relay}</span>`}
          </div>`)}
          ${!ts.peers.length && html`<div class="muted">No peers</div>`}
        </div>
      </div>`}
    </div>
  </div>`;
}
