/** Host side of the existing core.js protocol. No transcript or auth transport. */
const record = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const keysOnly = (value, keys) => record(value) && Object.keys(value).every(key => keys.includes(key));
export const validId = value => typeof value === 'string' && value.length > 0 && value.length <= 200 && /^[\p{L}\p{N}_.:@/-]+$/u.test(value);
export const validRole = value => value === 'operator' || value === 'reviewer';

export function exactOrigin(value) {
  const url = new URL(value);
  if (!['https:', 'http:'].includes(url.protocol) || value !== url.origin || url.username || url.password) {
    throw new TypeError('Нужен точный HTTP(S) origin без пути и завершающего слеша.');
  }
  return url.origin;
}

export function clampHeight(height) {
  return typeof height === 'number' && Number.isFinite(height) && height >= 0
    ? Math.min(1400, Math.max(360, Math.ceil(height))) : null;
}

/** A sibling iframe at the same origin is not the trusted child. Both checks matter. */
export function readChildMessage(event, childWindow, childOrigin) {
  if (!childWindow || event.source !== childWindow || event.origin !== childOrigin) return null;
  const data = event.data;
  if (!keysOnly(data, ['source', 'type', 'payload']) || data.source !== 'dalel-cqa') return null;
  const p = data.payload;
  if (!record(p)) return null;
  switch (data.type) {
    case 'ready':
      return keysOnly(p, ['role', 'mock']) && validRole(p.role) && typeof p.mock === 'boolean'
        ? { type: 'ready', payload: { role: p.role, mock: p.mock } } : null;
    case 'resize': {
      const height = clampHeight(p.height);
      return keysOnly(p, ['height']) && height !== null ? { type: 'resize', payload: { height } } : null;
    }
    case 'score.updated':
      return keysOnly(p, ['conv_id', 'total']) && validId(p.conv_id) &&
        (p.total === null || (typeof p.total === 'number' && Number.isFinite(p.total) && p.total >= 0 && p.total <= 100))
        ? { type: data.type, payload: { conv_id: p.conv_id, total: p.total } } : null;
    case 'appeal.created':
      return keysOnly(p, ['id', 'conv_id']) && validId(p.id) && validId(p.conv_id)
        ? { type: data.type, payload: { id: p.id, conv_id: p.conv_id } } : null;
    case 'decision.saved':
      return keysOnly(p, ['id', 'appeal_id']) && validId(p.id) && validId(p.appeal_id)
        ? { type: data.type, payload: { id: p.id, appeal_id: p.appeal_id } } : null;
    default: return null;
  }
}

export function createHostBridge({ eventTarget, getChildWindow, childOrigin, role = 'operator', convId = '', onEvent = () => {} }) {
  exactOrigin(childOrigin);
  if (!validRole(role) || (convId && !validId(convId))) throw new TypeError('Некорректная роль или ID диалога.');
  let ready = false;
  let disposed = false;
  const send = (type, payload) => {
    const child = getChildWindow();
    if (!ready || disposed || !child) return false;
    child.postMessage({ source: 'dalel-host', type, payload }, childOrigin);
    return true;
  };
  const sync = () => {
    send('setRole', { role });
    if (convId) send('open', { id: convId });
  };
  const receive = event => {
    if (disposed) return;
    const message = readChildMessage(event, getChildWindow(), childOrigin);
    if (!message) return;
    if (message.type === 'ready') { ready = true; sync(); }
    if (ready || message.type === 'resize') onEvent(message);
  };
  eventTarget.addEventListener('message', receive);
  return {
    get ready() { return ready; },
    reset() { ready = false; },
    setContext(next) {
      if (!validRole(next.role) || (next.convId !== '' && !validId(next.convId))) throw new TypeError('Некорректная роль или ID диалога.');
      role = next.role; convId = next.convId;
      if (ready) sync();
    },
    dispose() { disposed = true; ready = false; eventTarget.removeEventListener('message', receive); },
  };
}
