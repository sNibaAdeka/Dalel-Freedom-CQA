import { exactOrigin, validRole } from './bridge.js';

export function normalizeApiBase(value) {
  const url = new URL(value);
  const local = ['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname);
  if ((url.protocol !== 'https:' && !(url.protocol === 'http:' && local)) || url.username || url.password || url.search || url.hash) {
    throw new TypeError('API: HTTPS URL без credentials, query или fragment; HTTP разрешён только для localhost.');
  }
  return url.href.replace(/\/$/, '');
}

/** Deployment settings are trusted files. Query parameters never extend these allowlists. */
export function resolveBootstrap(search, deployment) {
  const outer = new URLSearchParams(search);
  if (outer.getAll('data').length !== 1) throw new TypeError('Ожидается один параметр data.');
  const raw = outer.get('data');
  if (!raw || raw.length > 2048) throw new TypeError('Некорректный параметр data.');
  // URLSearchParams decodes each layer once. No repeated decodeURIComponent.
  const data = new URLSearchParams(raw);
  const keys = ['parentOrigin', 'apiBase', 'role'];
  if ([...data.keys()].some(key => !keys.includes(key)) || keys.some(key => data.getAll(key).length !== 1)) {
    throw new TypeError('data должен содержать только parentOrigin, apiBase и role, по одному значению.');
  }
  const parentOrigin = exactOrigin(data.get('parentOrigin'));
  const apiBase = normalizeApiBase(data.get('apiBase'));
  const role = data.get('role');
  if (!deployment.parentOrigins?.map(exactOrigin).includes(parentOrigin)) throw new TypeError('Родительский origin не разрешён конфигурацией.');
  if (!deployment.apiBases?.map(normalizeApiBase).includes(apiBase)) throw new TypeError('API не разрешён конфигурацией.');
  if (!validRole(role) || typeof deployment.mock !== 'boolean') throw new TypeError('Некорректная конфигурация.');
  return { parentOrigin, apiBase, role, mock: deployment.mock, mockBase: null, fallbackToMock: false, offline: false };
}

export function applyBootstrap(win, deployment) {
  const config = resolveBootstrap(win.location.search, deployment);
  // Preserve shipped modules; trusted bootstrap values override pre-existing transport flags.
  win.CQA_CONFIG = { ...win.CQA_CONFIG, ...config };
  return config;
}

export function buildFrameUrl(frameUrl, { parentOrigin, apiBase, role }, deployment) {
  const url = new URL(frameUrl);
  if (!['https:', 'http:'].includes(url.protocol) || url.username || url.password) throw new TypeError('Некорректный URL iframe.');
  url.search = '';
  url.hash = '';
  url.searchParams.set('data', new URLSearchParams({ parentOrigin, apiBase, role }).toString());
  resolveBootstrap(url.search, deployment);
  return url;
}
