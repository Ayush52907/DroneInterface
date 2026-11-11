import { api } from './api.js';
import { toast } from './ui.js';

// Append one check to the preflight checklist (skips exact duplicates).
export async function addPreflightCheck(check) {
  try {
    const { checks } = await api('/preflight/checks');
    const key = (c) => JSON.stringify({ ...c, id: undefined, name: undefined, severity: undefined, enabled: undefined });
    if (checks.some(c => key(c) === key(check))) { toast('Already in the preflight checklist'); return; }
    await api('/preflight/checks', { method: 'PUT', body: { checks: [...checks, { severity: 'critical', ...check }] } });
    toast(`Added to preflight: ${check.name}`, 'ok');
  } catch (e) { toast(e.message, 'bad'); }
}
