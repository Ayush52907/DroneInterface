import { html, render, useState, useEffect } from '../vendor/preact-htm.js';
import { AppCtx, parseHash } from './ctx.js';
import { api, socket } from './api.js';
import { Icon, GlobalLayer } from './ui.js';
import { Dashboard } from './pages/dashboard.js';
import { Services } from './pages/services.js';
import { Logs } from './pages/logs.js';
import { Files } from './pages/files.js';
import { Activity } from './pages/activity.js';
import { Hardware } from './pages/hardware.js';
import { Preflight } from './pages/preflight.js';
import { Ros } from './pages/ros.js';
import { Terminal } from './pages/terminal.js';
import { Camera } from './pages/camera.js';
import { Data } from './pages/data.js';
import { Actions } from './pages/actions.js';
import { MapPage } from './pages/map.js';
import { AlertBanner, AlertBell, AlertsDrawer, useAlertNotifications } from './alerts.js';

const PAGES = [
  { id: 'dashboard', label: 'Dashboard', icon: 'dash', comp: Dashboard },
  { id: 'preflight', label: 'Preflight', icon: 'preflight', comp: Preflight },
  { id: 'actions', label: 'Quick actions', icon: 'bolt', comp: Actions },
  { id: 'services', label: 'Services', icon: 'services', comp: Services },
  { id: 'logs', label: 'Logs', icon: 'logs', comp: Logs },
  { id: 'hardware', label: 'Hardware', icon: 'chip', comp: Hardware },
  { id: 'ros', label: 'ROS 2', icon: 'ros', comp: Ros },
  { id: 'camera', label: 'Camera', icon: 'camera', comp: Camera },
  { id: 'data', label: 'Flight data', icon: 'data', comp: Data },
  { id: 'map', label: 'Map', icon: 'map', comp: MapPage },
  { id: 'files', label: 'Files', icon: 'files', comp: Files },
  { id: 'terminal', label: 'Terminal', icon: 'terminal', comp: Terminal },
  { id: 'activity', label: 'Activity', icon: 'activity', comp: Activity },
];

function App() {
  const [route, setRoute] = useState(parseHash());
  const [me, setMe] = useState(null);
  const [err, setErr] = useState(null);
  const [stats, setStats] = useState(null);
  const [conn, setConn] = useState('connecting');
  const [showAlerts, setShowAlerts] = useState(false);
  useAlertNotifications(stats && stats.alerts, me && me.system.hostname);

  useEffect(() => {
    const on = () => setRoute(parseHash());
    window.addEventListener('hashchange', on);
    return () => window.removeEventListener('hashchange', on);
  }, []);

  useEffect(() => {
    api('/me').then(setMe).catch(e => setErr(e));
  }, []);

  useEffect(() => {
    if (!me) return;
    const s = socket('/ws/stats', { onMessage: setStats, onState: setConn });
    return () => s.close();
  }, [me]);

  useEffect(() => {
    if (me) document.title = `${me.system.hostname} · Drone Panel`;
  }, [me]);

  if (err) {
    return html`<div class="boot"><div style="max-width:520px;text-align:center;display:grid;gap:10px">
      <h2 style="margin:0">${err.status === 401 ? 'Not authorized' : 'Cannot reach the drone'}</h2>
      <div class="muted">${err.message}</div>
      <div><button class="btn" onClick=${() => location.reload()}>Retry</button></div></div></div>`;
  }
  if (!me) return html`<div class="boot"><span class="spinner"></span></div>`;

  const page = PAGES.find(p => p.id === route.id) || PAGES[0];
  const Comp = page.comp;
  const failed = (stats && stats.failed_services) || [];
  const live = conn === 'open';

  const navItems = PAGES.map(p => html`
    <a href="#/${p.id}" class=${p.id === page.id ? 'active' : ''}>
      <${Icon} name=${p.icon}/><span>${p.label}</span>
      ${p.id === 'services' && failed.length > 0 && html`<span class="badge">${failed.length}</span>`}
    </a>`);

  return html`<${AppCtx.Provider} value=${{ me, stats, conn, route }}>
    <div class="shell">
      <aside class="side">
        <div style="position:relative"><img class="brand-logo" src="logo.png" alt="Logo" />
          <div style="position:absolute;right:0;top:0"><${AlertBell} summary=${stats && stats.alerts} onOpen=${() => setShowAlerts(true)} /></div></div>
        <div class="brand-host"><b class="ellipsis">${me.system.hostname}</b><small class="ellipsis" title=${me.system.model}>${me.system.model}</small></div>
        <nav class="nav">${navItems}</nav>
        <div class="side-foot">
          <div class="row"><span class="dot ${live ? 'ok' : 'bad'}"></span>${live ? 'Live' : 'Reconnecting…'}</div>
          <div class="ellipsis" title=${me.user.login}>👤 ${me.user.name}</div>
        </div>
      </aside>
      <main class="main">
        <div class="mtop"><img src="logo.png" alt="Logo" /><span class="grow"></span>
          <span class="mono" style="font-size:13px">${me.system.hostname}</span><span class="dot ${live ? 'ok' : 'bad'}" title=${live ? 'Live' : 'Reconnecting'}></span>
          <${AlertBell} summary=${stats && stats.alerts} onOpen=${() => setShowAlerts(true)} /></div>
        ${!live && conn !== 'connecting' && html`<div class="banner warn" style="margin:12px 16px 0;border-radius:8px">Connection to the drone lost. Retrying…</div>`}
        <${AlertBanner} summary=${stats && stats.alerts} onOpen=${() => setShowAlerts(true)} />
        <${Comp} key=${page.id} params=${route.params} />
      </main>
      <nav class="tabbar">${navItems}</nav>
    </div>
    ${showAlerts && html`<${AlertsDrawer} onClose=${() => setShowAlerts(false)} />`}
    <${GlobalLayer} />
  <//>`;
}

render(html`<${App} />`, document.getElementById('app'));
