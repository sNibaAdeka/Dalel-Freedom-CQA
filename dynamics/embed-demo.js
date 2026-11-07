import { createHostBridge, validId } from './bridge.js';
import { buildFrameUrl } from './bootstrap.js';
import { deployment } from './deployment.js';

const byId = id => document.getElementById(id);
const frame = byId('cqa-frame');
const form = byId('host-form');
const status = byId('connection-state');
const error = byId('connection-error');
const apiSelect = byId('api-base');
const roleSelect = byId('host-role');
const convInput = byId('conv-id');
const workspace = byId('embed-workspace');
const loading = byId('frame-loading');
let bridge;
let timeout;
let currentApi;
let count = 0;

function logEvent({ type, payload }) {
  if (type === 'resize') { frame.style.height = `${payload.height}px`; return; }
  if (type === 'ready') {
    clearTimeout(timeout);
    loading.hidden = true;
    workspace.setAttribute('aria-busy', 'false');
    error.hidden = true;
    status.textContent = payload.mock ? 'Подключено · условные демо-данные' : 'Подключено · серверный режим';
  }
  const row = document.createElement('li');
  const time = document.createElement('time');
  time.dateTime = new Date().toISOString();
  time.textContent = new Date().toLocaleTimeString('ru-RU', { hour12: false });
  const title = document.createElement('strong'); title.textContent = type;
  const detail = document.createElement('span');
  detail.textContent = Object.entries(payload).map(([key, value]) => `${key}: ${value ?? '—'}`).join(' · ');
  row.append(time, title, detail);
  const log = byId('event-log'); log.querySelector('.empty')?.remove(); log.prepend(row);
  while (log.children.length > 12) log.lastElementChild.remove();
  byId('event-count').textContent = `Получено: ${++count}`;
}

function showError(message) {
  clearTimeout(timeout);
  loading.hidden = true;
  workspace.setAttribute('aria-busy', 'false');
  status.textContent = 'Подключение не подтверждено';
  error.textContent = message; error.hidden = false;
}

function applyContext() {
  const convId = convInput.value.trim();
  const valid = !convId || validId(convId);
  convInput.setAttribute('aria-invalid', String(!valid));
  byId('conv-error').hidden = valid;
  if (!valid) {
    byId('conv-error').textContent = 'Используйте ID до 200 символов: буквы, цифры, _, ., :, @, / или дефис.';
    convInput.focus(); return;
  }
  try {
    const url = buildFrameUrl(new URL(deployment.framePath, location.href), {
      apiBase: apiSelect.value, role: roleSelect.value, parentOrigin: location.origin,
    }, deployment);
    if (bridge?.ready && currentApi === apiSelect.value) {
      bridge.setContext({ role: roleSelect.value, convId });
      status.textContent = convId ? 'Команда открытия диалога передана' : 'Роль обновлена; текущий диалог сохранён';
      return;
    }
    bridge?.dispose();
    clearTimeout(timeout);
    error.hidden = true; loading.hidden = false;
    workspace.setAttribute('aria-busy', 'true');
    status.textContent = 'Ожидаем приложение…';
    currentApi = apiSelect.value;
    bridge = createHostBridge({ eventTarget: window, getChildWindow: () => frame.contentWindow,
      childOrigin: url.origin, role: roleSelect.value, convId, onEvent: logEvent });
    frame.src = url.href;
    timeout = setTimeout(() => showError('Приложение не прислало ready за 15 секунд. Проверьте bootstrap, parentOrigin и доступность страницы, затем нажмите «Применить».'), 15000);
  } catch (failure) { showError(failure.message); }
}

try {
  for (const base of deployment.apiBases) { const option = document.createElement('option'); option.value = base; option.textContent = base; apiSelect.append(option); }
  if (deployment.apiBases.includes(`${location.origin}/api`)) apiSelect.value = `${location.origin}/api`;
  roleSelect.value = deployment.defaultRole;
  convInput.value = deployment.defaultConvId;
  form.addEventListener('submit', event => { event.preventDefault(); applyContext(); });
  window.addEventListener('pagehide', () => { clearTimeout(timeout); bridge?.dispose(); });
  window.addEventListener('pageshow', event => { if (event.persisted) applyContext(); });
  applyContext();
} catch { showError('Не удалось прочитать конфигурацию стенда. Проверьте deployment.js.'); }
