// HTTP + WebSocket helpers
export async function api(path, opts = {}) {
  const init = { method: opts.method || (opts.body !== undefined ? 'POST' : 'GET'), headers: {} };
  if (opts.body !== undefined) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(opts.body);
  }
  const res = await fetch('/api' + path, init);
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = { detail: text }; }
  if (!res.ok) {
    const err = new Error(errText(data, res));
    err.status = res.status;
    err.data = data;
    throw err;
  }
  return data;
}

function errText(data, res) {
  const d = data && data.detail;
  if (!d) return `${res.status} ${res.statusText}`;
  if (typeof d === 'string') return d;
  if (Array.isArray(d)) return d.map(x => x.msg || JSON.stringify(x)).join('; ');
  if (d.conflicts) return 'Already exists: ' + d.conflicts.join(', ');
  return JSON.stringify(d);
}

export const qs = (o) => Object.entries(o)
  .filter(([, v]) => v !== undefined && v !== null && v !== '')
  .map(([k, v]) => encodeURIComponent(k) + '=' + encodeURIComponent(v)).join('&');

// Reconnecting WebSocket. onMessage(obj), onState('open'|'closed')
export function socket(path, { onMessage, onState, reconnect = true } = {}) {
  let ws, closed = false, delay = 500, timer;
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const open = () => {
    ws = new WebSocket(`${proto}//${location.host}${path}`);
    ws.onopen = () => { delay = 500; onState && onState('open'); };
    ws.onmessage = (e) => { try { onMessage && onMessage(JSON.parse(e.data)); } catch (err) { console.error(err); } };
    ws.onclose = (e) => {
      onState && onState('closed', e.code);
      if (!closed && reconnect && e.code !== 4401) {
        timer = setTimeout(open, delay);
        delay = Math.min(delay * 2, 8000);
      }
    };
  };
  open();
  return {
    close() { closed = true; clearTimeout(timer); ws && ws.close(); },
    send(s) { ws && ws.readyState === 1 && ws.send(s); },
  };
}

export const rawUrl = (p) => '/api/files/raw?' + qs({ path: p });
export const downloadUrl = (p) => '/api/files/download?' + qs({ path: p });
