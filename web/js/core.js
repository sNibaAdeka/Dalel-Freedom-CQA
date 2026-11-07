import { mount as mountApi } from './api.js';
import { keyboardAction, modeCopy, runFailureNotice } from './shell-state.js';
import { mount as mountGlass } from './glass.js';
import * as economics from './bank-economics.js';

const TAB_NAMES = ['dialogues', 'editor', 'review', 'board', 'quality'];
// Register only modules shipped in the assembled build. An absent module is never requested.
const MODULE_PATHS = Object.freeze({ list: './list.js', card: './card.js', editor: './editor.js', review: './review.js', board: './board.js', quality: './quality.js', audio: './audio.js', weights: './weights.js', dna: './dna.js' });
let bootPromise;

export function createBus() {
  const listeners = new Map();
  return {
    on(name, callback) {
      if (typeof callback !== 'function') throw new TypeError('Event listener must be a function');
      if (!listeners.has(name)) listeners.set(name, new Set());
      listeners.get(name).add(callback);
      return () => listeners.get(name)?.delete(callback);
    },
    emit(name, payload) {
      for (const callback of [...(listeners.get(name) || [])]) {
        try {
          const result = callback(payload);
          if (result && typeof result.catch === 'function') result.catch(error => { if (name !== 'app:error') this.emit('app:error', error); });
        } catch (error) { if (name !== 'app:error') this.emit('app:error', error); }
      }
    },
  };
}

export function normalizeParentOrigin(value) {
  if (!value || value === '*') return null;
  try { const url = new URL(value); return ['https:', 'http:'].includes(url.protocol) && url.origin === value ? url.origin : null; } catch { return null; }
}

export function isEditable(target) {
  return !!target?.closest?.('input, textarea, select, [contenteditable]:not([contenteditable="false"])');
}

export function boot() {
  if (bootPromise) return bootPromise;
  bootPromise = initialize();
  return bootPromise;
}

/** Page opened without ?mode: use the server when its health endpoint answers within 1.5 s, else the demo data. */
async function serverAnswers(apiBase) {
  if (typeof fetch !== 'function' || typeof location === 'undefined' || location.protocol === 'file:') return false;
  const ctrl = typeof AbortController === 'function' ? new AbortController() : null;
  const timer = ctrl ? setTimeout(() => ctrl.abort(), 1500) : null;
  try {
    const res = await fetch(`${String(apiBase || './api').replace(/\/$/, '')}/health`, { signal: ctrl?.signal, cache: 'no-store' });
    return res.ok;
  } catch { return false; } finally { if (timer) clearTimeout(timer); }
}

async function initialize() {
  const root = document.querySelector('.cqa-root');
  if (!root) throw new Error('Missing .cqa-root');
  const supplied = window.CQA_CONFIG || {};
  const config = {
    apiBase: './api', role: 'operator', locale: 'ru', projector: false, offline: false,
    mock: true, mockBase: null, modules: [], parentOrigin: null, ...supplied,
  };
  if (config.mock === undefined) config.mock = !(await serverAnswers(config.apiBase));
  config.role = config.role === 'reviewer' ? 'reviewer' : 'operator';
  config.parentOrigin = normalizeParentOrigin(config.parentOrigin);
  config.modules = [...new Set(Array.isArray(config.modules) ? config.modules.filter(name => Object.hasOwn(MODULE_PATHS, name)) : [])];
  const CQA = window.CQA = { config, api: {}, state: { tab: 'dialogues', role: config.role, activeConversationId: null, checklistVersion: 1, modules: {}, ready: false }, bus: createBus() };
  CQA.economics = economics;
  mountApi(CQA);
  try { mountGlass(root, { bus: CQA.bus }); } catch { /* appearance layer is optional */ }
  const byId = name => document.getElementById(name);
  let toastTimer;
  function announce(message) { byId('cqa-announcer').textContent = message; }
  function toast(message) {
    clearTimeout(toastTimer); const node = byId('cqa-toast'); node.textContent = String(message); node.hidden = false;
    toastTimer = setTimeout(() => { node.hidden = true; }, 5000);
  }
  CQA.toast = toast;
  CQA.announce = announce;
  CQA.bus.on('app:error', error => toast(error?.message || 'Не удалось выполнить действие. Повторите попытку.'));

  function postToParent(type, payload = {}) {
    if (window.parent === window || !config.parentOrigin) return;
    // Only explicit origin and selected fields leave the iframe; never send transcripts.
    window.parent.postMessage({ source: 'dalel-cqa', type, payload }, config.parentOrigin);
  }
  function setTab(tab, { focus = false, updateHash = true } = {}) {
    if (!TAB_NAMES.includes(tab)) return;
    CQA.state.tab = tab;
    root.querySelectorAll('[data-tab]').forEach(button => { const active = button.dataset.tab === tab; button.setAttribute('aria-selected', String(active)); button.tabIndex = active ? 0 : -1; if (active && focus) button.focus(); });
    TAB_NAMES.forEach(name => { byId(`cqa-panel-${name}`).hidden = name !== tab; });
    if (updateHash) history.replaceState(null, '', `#${tab}`);
    CQA.bus.emit('tab:changed', tab);
    announce(`${byId(`cqa-tab-${tab}`).textContent.trim()}: раздел открыт`);
  }
  CQA.setTab = setTab;
  function setRole(role) {
    if (!['operator', 'reviewer'].includes(role)) return;
    config.role = role; CQA.state.role = role;
    root.dataset.role = role;
    root.querySelectorAll('[data-role]').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.role === role)));
    CQA.bus.emit('role:changed', role);
    announce(role === 'operator' ? 'Роль: оператор' : 'Роль: проверяющий');
  }
  CQA.setRole = setRole;
  function setProjector(enabled) {
    config.projector = !!enabled; root.classList.toggle('projector', config.projector);
    if (config.projector) root.querySelectorAll('details.cqa-card__receipt').forEach(receipt => { receipt.open = false; });
    byId('cqa-projector-toggle').setAttribute('aria-pressed', String(config.projector));
    CQA.bus.emit('projector:changed', config.projector);
    announce(config.projector ? 'Режим проектора включён' : 'Режим проектора выключен');
  }
  CQA.setProjector = setProjector;
  function showMode() {
    const copy = modeCopy(config, CQA.state.health, CQA.state.serverUnavailable);
    root.dataset.mode = copy.mode;
    byId('cqa-mode-title').textContent = copy.title;
    byId('cqa-mode-description').textContent = copy.description;
    const link = byId('cqa-mode-switch');
    if (link) {
      const url = new URL(location.href); url.searchParams.set('mode', config.mock !== false ? 'live' : 'demo');
      url.searchParams.delete('fixtures');
      link.href = url.href;
      link.textContent = config.mock !== false ? 'Подключить сервер' : 'Открыть демо';
    }
  }
  async function refreshHealth() {
    if (config.mock !== false || config.offline) return showMode();
    try { CQA.state.health = await CQA.api.health(); CQA.state.serverUnavailable = false; }
    catch { CQA.state.health = null; CQA.state.serverUnavailable = true; }
    showMode();
  }
  CQA.bus.on('api:mode', showMode);
  let failedRun = null;
  function showRunFailure(run) {
    const banner = byId('cqa-run-warning');
    if (!banner) return;
    const notice = runFailureNotice(run);
    failedRun = notice ? run : null;
    banner.hidden = !notice;
    if (notice) {
      byId('cqa-run-warning-title').textContent = notice.title;
      byId('cqa-run-warning-copy').textContent = notice.description;
    }
  }
  CQA.bus.on('run:progress', showRunFailure);
  CQA.bus.on('run:completed', showRunFailure);
  CQA.bus.on('card:retry-progress', payload => CQA.bus.emit('run:progress', payload.run));
  CQA.bus.on('card:retry-finished', payload => CQA.bus.emit('run:completed', payload.run));
  byId('cqa-run-warning-open')?.addEventListener('click', () => {
    setTab('board', { focus: true });
    if (failedRun?.run_id) CQA.bus.emit('run:started', failedRun);
  });
  CQA.bus.on('api:fallback', () => toast('Сервер недоступен. Включена демонстрация с условными оценками.'));
  CQA.bus.on('conv:open', id => { CQA.state.activeConversationId = id; setTab('dialogues'); });
  CQA.bus.on('nav:open', tab => setTab(tab));
  CQA.bus.on('appeal:start', () => {
    if (CQA.state.modules.review) return;
    setTab('review');
    toast('Форма апелляции ещё не подключена. Обращение не сохранено.');
  });
  CQA.bus.on('score:updated', data => postToParent('score.updated', { conv_id: data?.conv_id || data?.id, total: data?.score?.total }));
  CQA.bus.on('appeal:created', data => postToParent('appeal.created', { id: data?.id, conv_id: data?.conv_id }));
  CQA.bus.on('decision:saved', data => postToParent('decision.saved', { id: data?.id || data?.decision?.id, appeal_id: data?.appeal_id || data?.decision?.appeal_id }));
  root.querySelectorAll('[data-tab]').forEach(button => button.addEventListener('click', () => setTab(button.dataset.tab)));
  root.querySelectorAll('[data-role]').forEach(button => button.addEventListener('click', () => setRole(button.dataset.role)));
  byId('cqa-projector-toggle').addEventListener('click', () => setProjector(!config.projector));
  root.querySelector('.cqa-brand').addEventListener('click', event => { event.preventDefault(); setTab('dialogues'); });
  root.querySelector('[role="tablist"]').addEventListener('keydown', event => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault(); const index = TAB_NAMES.indexOf(CQA.state.tab);
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? TAB_NAMES.length - 1 : (index + (event.key === 'ArrowRight' ? 1 : TAB_NAMES.length - 1)) % TAB_NAMES.length;
    setTab(TAB_NAMES[next], { focus: true });
  });
  document.addEventListener('keydown', event => {
    if (isEditable(event.target)) return;
    const action = keyboardAction(event, config.demo);
    if (!action) return;
    event.preventDefault();
    if (action.type === 'projector') setProjector(!config.projector);
    else if (action.type === 'tab') setTab(action.tab, { focus: true });
    else if (action.type === 'conversation') CQA.bus.emit('conv:open', action.id);
    else if (action.type === 'dismiss') byId('cqa-toast').hidden = true;
  });
  window.addEventListener('hashchange', () => setTab(location.hash.slice(1), { updateHash: false }));
  window.addEventListener('message', event => {
    if (!config.parentOrigin || event.origin !== config.parentOrigin || event.source !== window.parent) return;
    const data = event.data;
    if (!data || typeof data !== 'object' || data.source !== 'dalel-host') return;
    if (data.type === 'open' && typeof data.payload?.id === 'string' && data.payload.id.length <= 200) CQA.bus.emit('conv:open', data.payload.id);
    if (data.type === 'setRole' && ['operator', 'reviewer'].includes(data.payload?.role)) setRole(data.payload.role);
  });
  if (typeof ResizeObserver !== 'undefined') {
    let frame = 0;
    new ResizeObserver(() => { cancelAnimationFrame(frame); frame = requestAnimationFrame(() => postToParent('resize', { height: root.scrollHeight })); }).observe(root);
  }

  async function refreshChecklists(preferred = CQA.state.checklistVersion) {
    const versions = await CQA.api.checklists();
    CQA.state.checklists = versions;
    const select = byId('cqa-version-select'); select.replaceChildren();
    for (const version of versions) { const option = document.createElement('option'); option.value = version.version; option.textContent = `v${version.version} · ${version.name || 'Чек-лист'}`; select.append(option); }
    if (versions.some(item => String(item.version) === String(preferred))) select.value = preferred;
    CQA.state.checklistVersion = Number(select.value) || null;
    if (!versions.length) { const option = document.createElement('option'); option.textContent = 'Нет сохранённых версий'; select.append(option); }
    renderChecklistPreview();
    CQA.bus.emit('checklists:updated', versions);
  }
  function renderChecklistPreview() {
    const container = byId('cqa-shell-checklist'); if (!container) return;
    container.replaceChildren(); const current = CQA.state.checklists?.find(item => item.version === CQA.state.checklistVersion);
    if (!current) return;
    const list = document.createElement('ul'); list.className = 'cqa-shell-criteria';
    for (const item of current.criteria || []) { const row = document.createElement('li'); const title = document.createElement('strong'); title.textContent = item.title; const weight = document.createElement('span'); weight.textContent = `Вес ${item.weight}`; row.append(title, weight); list.append(row); }
    container.append(list);
  }
  byId('cqa-version-select').addEventListener('change', event => {
    CQA.state.checklistVersion = Number(event.target.value); renderChecklistPreview();
    CQA.bus.emit('checklist:selected', CQA.state.checklistVersion);
    toast('Выбрана версия чек-листа. Сохранённые оценки пока не пересчитывались.');
  });
  CQA.bus.on('checklist:saved', data => refreshChecklists(data?.version).catch(error => CQA.bus.emit('app:error', error)));
  CQA.bus.on('checklist:activated', data => {
    CQA.state.activeChecklistVersion = Number(data?.version);
    refreshChecklists(data?.version).catch(error => CQA.bus.emit('app:error', error));
  });
  CQA.bus.on('run:completed', refreshHealth);

  setRole(config.role); setProjector(config.projector); showMode(); setTab(TAB_NAMES.includes(location.hash.slice(1)) ? location.hash.slice(1) : 'dialogues', { updateHash: false });
  // Dynamic whitelist lets each worktree ship independently without missing-file requests.
  for (const name of config.modules) {
    try {
      const module = await import(MODULE_PATHS[name]);
      if (typeof module.mount !== 'function') throw new Error(`Модуль ${name} не содержит mount().`);
      await module.mount(CQA); CQA.state.modules[name] = true;
    } catch (error) {
      CQA.state.modules[name] = false;
      toast(`Не удалось подключить раздел: ${name}. ${error.message}`);
      if (['editor', 'review', 'board'].includes(name)) {
        const container = byId(`cqa-${name}`);
        if (container) { const message = document.createElement('p'); message.className = 'cqa-module-placeholder'; message.textContent = 'Раздел не загрузился. Обновите страницу; если ошибка повторится, проверьте файлы приложения.'; container.replaceChildren(message); }
      }
    }
  }
  if (!CQA.state.modules.list) installShellList(CQA, root);
  if (!CQA.state.modules.card) installShellCard(CQA, root);
  try { await refreshChecklists(); } catch (error) { byId('cqa-version-select').replaceChildren(new Option('Недоступен', '')); toast(error.message); }
  await refreshHealth();
  CQA.state.ready = true;
  CQA.bus.emit('app:ready', CQA);
  postToParent('ready', { role: config.role, mock: config.mock !== false });
  return CQA;
}

function installShellList(CQA, root) {
  CQA.bus.on('app:ready', async () => {
    const container = root.querySelector('#cqa-shell-list'); if (!container) return;
    const rows = await CQA.api.conversations(); container.replaceChildren(); root.querySelector('#cqa-shell-count').textContent = rows.length;
    for (const row of rows) {
      const button = document.createElement('button'); button.type = 'button'; button.className = 'cqa-shell-row'; button.dataset.id = row.id; button.setAttribute('aria-pressed', 'false');
      const title = document.createElement('strong'); title.textContent = row.id;
      const score = document.createElement('b'); score.textContent = row.total == null ? '—' : row.total;
      const meta = document.createElement('span'); meta.textContent = `${row.channel === 'call' ? 'Звонок' : 'Чат'} · ${row.language}${row.needs_review ? ' · На проверку' : ''}`;
      button.append(title, score, meta); button.addEventListener('click', () => CQA.bus.emit('conv:open', row.id)); container.append(button);
    }
    if (!rows.length) container.textContent = 'Диалогов пока нет.';
  });
  CQA.bus.on('conv:open', id => root.querySelectorAll('.cqa-shell-row').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.id === id))));
}

function installShellCard(CQA, root) {
  let request = 0;
  CQA.bus.on('conv:open', async id => {
    const token = ++request; const detail = await CQA.api.conversation(id); if (token !== request) return;
    CQA.state.conversation = detail; CQA.state.currentConversation = detail;
    const container = root.querySelector('#cqa-card'); container.replaceChildren();
    const section = document.createElement('section'); section.className = 'cqa-preview';
    const header = document.createElement('header'); const heading = document.createElement('h2'); heading.textContent = detail.id;
    const score = document.createElement('div'); score.className = 'cqa-preview-score'; score.textContent = detail.score?.total ?? '—'; const suffix = document.createElement('small'); suffix.textContent = ' / 100'; score.append(suffix); header.append(heading, score);
    const intro = document.createElement('p'); intro.className = 'cqa-empty-copy'; intro.textContent = 'Предпросмотр транскрипта. Модуль подробной оценки ещё не подключён.';
    section.append(header, intro);
    const labels = { operator: 'Оператор', client: 'Клиент', bot: 'Бот · контекст', system: 'Система · контекст' };
    for (const message of detail.messages) { const node = document.createElement('article'); node.className = 'cqa-preview-message'; node.dataset.speaker = message.speaker; const label = document.createElement('strong'); label.textContent = labels[message.speaker] || message.speaker; const time = document.createElement('time'); time.textContent = message.ts_raw; const text = document.createElement('p'); text.textContent = message.text; node.append(label, time, text); section.append(node); }
    container.append(section); CQA.bus.emit('conv:loaded', detail);
  });
}
