import { createContext } from '../vendor/preact-htm.js';

export const AppCtx = createContext(null);

export function parseHash() {
  const h = location.hash.replace(/^#\/?/, '');
  const [id, q = ''] = h.split('?');
  return { id: id || 'dashboard', params: Object.fromEntries(new URLSearchParams(q)) };
}
export function go(id, params = {}) {
  const q = new URLSearchParams(Object.entries(params).filter(([, v]) => v !== undefined && v !== '')).toString();
  location.hash = '#/' + id + (q ? '?' + q : '');
}
