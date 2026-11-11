// In-panel alerts + browser notifications
import { html, useState, useEffect, useRef } from '../vendor/preact-htm.js';
import { api } from './api.js';
import { Icon, Drawer, toast, fmtAgo, fmtTime } from './ui.js';

let swReg = null;
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('sw.js').then(r => { swReg = r; }).catch(() => {});
}

export const notifySupported = () => 'Notification' in window;
export const notifyPermission = () => (notifySupported() ? Notification.permission : 'unsupported');

export async function enableNotifications() {
  if (!notifySupported()) { toast('This browser does not support notifications. On iPhone, add the panel to the Home Screen first.', 'bad'); return 'unsupported'; }
  const p = await Notification.requestPermission();
  if (p === 'granted') { notify('Notifications enabled', 'You will be notified about drone alerts while the panel is open in a tab.', 'test'); }
  else toast('Notifications were blocked. Allow them in the browser site settings.', 'bad');
  return p;
}

function notify(title, body, tag) {
  if (notifyPermission() !== 'granted') return;
  const opts = { body, tag, icon: 'icon-192.png', badge: 'icon-192.png', renotify: true, requireInteraction: tag !== 'test' };
  try {
    if (swReg && swReg.showNotification) swReg.showNotification(title, opts); // Android needs this
    else new Notification(title, opts);
  } catch (e) { /* ignore */ }
}

// Watches the alert summary from the stats stream; notifies on new / resolved alerts.
export function useAlertNotifications(summary, hostname) {
  const known = useRef(null);
  useEffect(() => {
    if (!summary) return;
    const cur = new Map(summary.active.map(a => [a.id, a]));
    if (known.current === null) { known.current = cur; return; } // don't notify for alerts that existed before the page opened
    if (!summary.muted) {
      for (const [id, a] of cur) if (!known.current.has(id)) notify(`${a.level === 'critical' ? '🔴' : '🟠'} ${a.title}`, `${hostname}: ${a.detail}`, a.key);
      for (const [id, a] of known.current) if (!cur.has(id)) notify(`✅ Resolved: ${a.title}`, hostname, a.key);
    }
    known.current = cur;
  }, [summary && summary.seq]);
}

export function AlertBanner({ summary, onOpen }) {
  if (!summary) return null;
  const show = summary.active.filter(a => !a.acked);
  if (!show.length) return null;
  const crit = show.some(a => a.level === 'critical');
  return html`<div class="alert-banner ${crit ? '' : 'warn'}" onClick=${onOpen}>
    <span class="alert-pulse"></span>
    <div class="grow" style="min-width:0">
      ${show.slice(0, 3).map(a => html`<div class="ellipsis"><b>${a.title}</b> <span class="hide-sm">· ${a.detail}</span></div>`)}
      ${show.length > 3 && html`<div style="font-size:12px">+${show.length - 3} more</div>`}
    </div>
    <span class="btn sm" style="background:transparent;border-color:currentColor;color:inherit">View</span>
  </div>`;
}

export function AlertBell({ summary, onOpen }) {
  const n = summary ? summary.active.length : 0;
  const crit = summary && summary.active.some(a => a.level === 'critical');
  return html`<button class="bell ${n ? (crit ? 'bad' : 'warn') : ''}" onClick=${onOpen} title="Alerts">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9M10.3 21a1.9 1.9 0 0 0 3.4 0"/></svg>
    ${n > 0 && html`<span class="bell-count">${n}</span>`}
    ${summary && summary.muted && html`<span class="bell-mute" title="Muted">z</span>`}
  </button>`;
}

export function AlertsDrawer({ onClose }) {
  const [d, setD] = useState(null);
  const [perm, setPerm] = useState(notifyPermission());
  const load = () => api('/alerts').then(setD).catch(e => toast(e.message, 'bad'));
  useEffect(() => { load(); const t = setInterval(load, 3000); return () => clearInterval(t); }, []);
  const ack = async (a) => { await api(`/alerts/${a.id}/ack`, { method: 'POST', body: {} }); load(); };
  const mute = async (m) => { await api('/alerts/mute', { body: { minutes: m } }); load(); toast(m ? `Notifications muted for ${m >= 60 ? m / 60 + ' h' : m + ' min'}` : 'Notifications on', 'ok'); };
  const muted = d && d.mute_until > Date.now() / 1000;
  const RULE = { camera_lost: 'Camera', usb_ttl: 'USB-TTL', port_conflict: 'Serial port', gimbal: 'Gimbal' };
  return html`<${Drawer} title="Alerts" onClose=${onClose}>
    <div class="card">
      <div class="row" style="flex-wrap:wrap">
        <div class="grow"><b>Browser notifications</b><div class="faint" style="font-size:12px">Pop-ups on this device while the panel is open in a tab (can be in the background).</div></div>
        ${perm === 'granted' ? html`<span class="pill ok">enabled</span>`
          : perm === 'denied' ? html`<span class="pill bad" title="Allow notifications in the browser's site settings">blocked</span>`
          : html`<button class="btn primary sm" onClick=${async () => setPerm(await enableNotifications())}>Enable</button>`}
      </div>
      <div class="row" style="margin-top:10px;flex-wrap:wrap">
        <span class="muted" style="font-size:13px">${muted ? `Muted until ${fmtTime(d.mute_until)}` : 'Mute notifications:'}</span>
        ${muted ? html`<button class="btn sm" onClick=${() => mute(0)}>Unmute</button>`
          : html`<button class="btn sm" onClick=${() => mute(30)}>30 min</button><button class="btn sm" onClick=${() => mute(60)}>1 h</button><button class="btn sm" onClick=${() => mute(240)}>4 h</button>`}
      </div>
      <div class="faint" style="font-size:12px;margin-top:6px">Muting stops pop-ups only; alerts still show in the panel.</div>
    </div>
    ${!d ? html`<div class="empty"><span class="spinner"></span></div>` : html`
      <div><h3 class="muted" style="font-size:12px;margin:0 0 8px">ACTIVE</h3>
        ${d.active.length === 0 ? html`<div class="card ok" style="color:var(--ok)">✓ No active alerts</div>` : d.active.map(a => html`<div class="alert-item ${a.level}">
          <div class="row"><b class="grow">${a.title}</b><span class="pill ${a.level === 'critical' ? 'bad' : 'warn'}">${RULE[a.rule] || a.rule}</span></div>
          <div style="font-size:13px;margin:4px 0">${a.detail}</div>
          <div class="row faint" style="font-size:12px"><span class="grow">since ${fmtTime(a.since)} (${fmtAgo(a.since)})</span>
            ${a.acked ? html`<span>acknowledged</span>` : html`<button class="btn sm" onClick=${() => ack(a)}>Acknowledge</button>`}</div>
        </div>`)}
      </div>
      <div><h3 class="muted" style="font-size:12px;margin:8px 0">HISTORY</h3>
        <div class="list-rows">${d.history.filter(h => h.resolved).slice(0, 50).map(h => html`<div style="align-items:flex-start">
          <span class="dot ${h.level === 'critical' ? 'bad' : 'warn'}" style="margin-top:6px"></span>
          <div class="grow" style="min-width:0"><div>${h.title}</div><div class="faint" style="font-size:12px">${fmtTime(h.since, true)} → resolved ${fmtTime(h.resolved)}${h.resolved_note ? ' · ' + h.resolved_note : ''}</div></div>
        </div>`)}
        ${!d.history.some(h => h.resolved) && html`<div class="muted">Nothing yet</div>`}</div>
      </div>`}
  <//>`;
}
