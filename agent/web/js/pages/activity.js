import { html, useState, useEffect } from '../../vendor/preact-htm.js';
import { api } from '../api.js';
import { Icon, fmtTime, toast } from '../ui.js';

export function Activity() {
  const [rows, setRows] = useState(null);
  const [q, setQ] = useState('');
  const load = () => api('/audit?limit=1000').then(d => setRows(d.entries)).catch(e => toast(e.message, 'bad'));
  useEffect(() => { load(); }, []);
  const shown = (rows || []).filter(r => !q || JSON.stringify(r).toLowerCase().includes(q.toLowerCase()));
  return html`<div class="page">
    <div class="page-head">
      <h1>Activity</h1><span class="muted">Every change made through this panel, newest first</span>
      <span class="grow"></span>
      <input class="input" placeholder="Filter…" value=${q} onInput=${e => setQ(e.target.value)} />
      <button class="btn" onClick=${load}><${Icon} name="refresh"/></button>
    </div>
    ${!rows ? html`<div class="empty"><span class="spinner"></span></div>` : html`
    <div class="table-wrap"><table class="table">
      <thead><tr><th>Time</th><th>User</th><th>Action</th><th>Target</th><th class="hide-sm">Details</th></tr></thead>
      <tbody>
        ${shown.length === 0 && html`<tr><td colspan="5"><div class="empty">No activity yet</div></td></tr>`}
        ${shown.map(r => html`<tr>
          <td class="num muted" style="white-space:nowrap">${fmtTime(r.ts, true)}</td>
          <td class="ellipsis" style="max-width:180px">${r.user}</td>
          <td><span class="pill ${r.ok ? '' : 'bad'}">${r.action}</span></td>
          <td class="mono ellipsis" style="font-size:12.5px;max-width:380px" title=${r.target}>${r.target}</td>
          <td class="hide-sm muted ellipsis" style="max-width:300px;font-size:12.5px" title=${r.detail}>${r.detail}</td>
        </tr>`)}
      </tbody>
    </table></div>`}
  </div>`;
}
