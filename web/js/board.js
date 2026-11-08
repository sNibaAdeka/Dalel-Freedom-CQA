/* Evaluation workspace. Network access is exclusively through CQA.api. */
import { profileSettings, profileChoice, modelHint, profileGuide, runForecast, forecastText } from './model-profiles.js';
const mounts = new WeakMap();
const finishedStatuses = new Set(['complete', 'completed', 'done', 'finished', 'failed', 'error', 'cancelled', 'incomplete']);
const failedStatuses = new Set(['failed', 'error', 'cancelled', 'incomplete']);
const completedStatuses = new Set(['complete', 'completed', 'done', 'finished']);
const exportStatuses = new Set(['complete', 'completed', 'done', 'finished', 'failed', 'error']);
const finite = value => typeof value === 'number' && Number.isFinite(value);
const number = (value, digits = 2) => finite(value) ? value.toLocaleString('ru-RU', { maximumFractionDigits: digits }) : '—';
const errorText = error => error?.message || 'Не удалось получить ответ сервера.';

/** Independently testable run lifecycle. stop() stops observation, never the server run. */
export function createRunController(api, { onChange = () => {}, onProgress = () => {}, onFinish = () => {},
  schedule = setTimeout, unschedule = clearTimeout, interval = 500, restorePollLimit = 6, restoreStallLimit = 3 } = {}) {
  let epoch = 0, timer = null, destroyed = false;
  let state = { phase: 'idle', run: null, error: null, restored: false };
  let recovery = null;
  // Live API snapshots advance elapsed even while a single model call is pending.
  // Persisted running snapshots have no explicit alive flag and remain unchanged.
  const heartbeat = run => JSON.stringify([run.status, run.elapsed, run.done, run.evaluated, run.llm_calls, run.per_conv]);
  const announced = new Set();
  const emit = update => { state = { ...state, ...update }; if (!destroyed) onChange(state); };
  function clear() { if (timer !== null) unschedule(timer); timer = null; }
  const active = token => !destroyed && token === epoch;
  function accept(snapshot, token, fallback = {}) {
    if (!active(token)) return;
    if (!snapshot || typeof snapshot !== 'object') throw new Error('Сервер вернул пустое состояние прогона.');
    const run = { ...fallback, ...snapshot };
    if (typeof run.run_id !== 'string' || !run.run_id) throw new Error('Сервер не вернул номер прогона.');
    const finished = finishedStatuses.has(run.status);
    let phase = finished ? 'finished' : 'running';
    if (recovery && !finished) {
      const next = heartbeat(run);
      recovery.polls++;
      recovery.stalls = next === recovery.last ? recovery.stalls + 1 : 0;
      recovery.last = next;
      phase = recovery.stalls >= restoreStallLimit ? 'interrupted' : recovery.polls >= restorePollLimit ? 'paused' : 'restoring';
    }
    emit({ phase, run, error: null });
    if (!state.restored) onProgress(run);
    if (finished) {
      clear();
      if (!announced.has(run.run_id)) { announced.add(run.run_id); if (!state.restored) onFinish(run); }
    } else if (phase === 'running' || phase === 'restoring') timer = schedule(() => { timer = null; void poll(token); }, interval);
  }
  async function poll(token) {
    if (!active(token) || !state.run?.run_id) return;
    const previous = state.run;
    try { accept(await api.run(previous.run_id), token, previous); }
    catch (error) { if (active(token)) emit({ phase: recovery && error?.status === 404 ? 'interrupted' : 'paused', error: recovery && error?.status === 404 ? null : error }); }
  }
  return {
    get state() { return state; },
    async start(body) {
      if (destroyed || ['starting', 'running'].includes(state.phase)) return null;
      clear(); const token = ++epoch;
      recovery = null;
      emit({ phase: 'starting', run: null, error: null, restored: false });
      try {
        const response = await api.createRun(body);
        if (!active(token)) return null;
        accept(response, token, { version: body.version });
        return response;
      } catch (error) { if (active(token)) emit({ phase: 'error', error }); return null; }
    },
    watch(run, { restore = false } = {}) {
      if (destroyed) return;
      const snapshot = typeof run === 'string' ? { run_id: run } : run;
      if (!snapshot?.run_id) return;
      if (restore && finishedStatuses.has(snapshot.status)) announced.add(snapshot.run_id);
      clear(); const token = ++epoch;
      recovery = restore ? { polls: 0, stalls: 0, last: heartbeat(snapshot) } : null;
      emit({ phase: restore ? 'restoring' : 'running', run: { ...snapshot }, error: null, restored: restore });
      void poll(token);
    },
    stop() { if (destroyed) return; clear(); ++epoch; emit({ phase: state.run ? 'paused' : 'idle', error: null }); },
    reset() { if (destroyed) return; clear(); ++epoch; recovery = null; announced.clear(); emit({ phase: 'idle', run: null, error: null, restored: false }); },
    destroy() { destroyed = true; clear(); ++epoch; },
  };
}

export function latestRun(runs) {
  // API orders newest first; preserve that order when timestamps are equal/absent.
  return (Array.isArray(runs) ? runs : []).filter(r => r?.run_id && !r.preview)
    .sort((a, b) => (Date.parse(b.created_at) || 0) - (Date.parse(a.created_at) || 0))[0] || null;
}

export function runSummary(run = {}) {
  const rows = Array.isArray(run.per_conv) ? run.per_conv : [];
  const errors = new Set(rows.filter(row => rowIssue(row)).map(row => row.id));
  for (const error of Array.isArray(run.errors) ? run.errors : []) if (error?.conv_id) errors.add(error.conv_id);
  const valid = rows.filter(row => !rowIssue(row));
  return {
    rows,
    errors: errors.size,
    changed: valid.filter(row => finite(row.old_total) && finite(row.new_total) && row.old_total !== row.new_total).length,
    compared: valid.filter(row => finite(row.old_total) && finite(row.new_total)).length,
    before: histogram(rows, 'old_total'), after: histogram(valid, 'new_total'),
  };
}
function rowIssue(row) {
  if (row.status === 'incomplete' || row.score?.status === 'incomplete' || row.score?.missing?.length) return 'incomplete';
  return failedStatuses.has(row.status) || row.error ? 'error' : null;
}
function serverPlanText(run) {
  const total = run.pairs_total, evaluate = run.pairs_to_evaluate;
  if (!Number.isSafeInteger(total) || !Number.isSafeInteger(evaluate) || total < 0 || evaluate < 0 || evaluate > total) {
    return 'Сервер не передал числовой план этого прогона. Число пар недоступно.';
  }
  return `${run.is_mock ? 'Демонстрационный план' : 'План сервера'} при запуске: к оценке ${number(evaluate, 0)} пар; ` +
    `без новой оценки — ${number(total - evaluate, 0)} пар. Пара — диалог и критерий, а не вызов модели. ` +
    'Это исходный план; фактические выполнение, время и стоимость показаны ниже.';
}
function histogram(rows, key) {
  const bins = [0, 0, 0, 0, 0];
  for (const row of rows) if (finite(row[key]) && row[key] >= 0 && row[key] <= 100) bins[Math.min(4, Math.floor(row[key] / 20))]++;
  return bins;
}
export function parseImport(text) {
  let value;
  try { value = JSON.parse(text); } catch { throw new Error('Файл не является корректным JSON. Проверьте его содержимое.'); }
  if (!value || typeof value !== 'object') throw new Error('Нужен JSON с диалогом, массивом диалогов или ответом API.');
  if (Array.isArray(value) && !value.length) throw new Error('В файле нет диалогов.');
  return value;
}
export function conversationSelection(rows, group, scope, activeId) {
  return rows.filter(row => (!group || row.group_id === group) &&
    (scope !== 'active' || row.id === activeId) && (scope !== 'pending' || !finite(row.total))).map(row => row.id);
}

function node(tag, className, text) {
  const result = document.createElement(tag);
  if (className) result.className = className;
  if (text !== undefined) result.textContent = String(text);
  return result;
}
function button(text, action, className = '') {
  const result = node('button', `cqa-board__button ${className}`, text); result.type = 'button';
  result.addEventListener('click', action); return result;
}
function option(value, text) { const result = node('option', '', text); result.value = value; return result; }
function field(title, input, help) {
  const label = node('label', 'cqa-board__field'); label.append(node('span', 'cqa-board__label', title), input);
  if (help) label.append(node('small', 'cqa-board__hint', help)); return label;
}
function select(entries) { const result = node('select'); for (const [value, text] of entries) result.append(option(value, text)); return result; }

export function mount(CQA = globalThis.window?.CQA) {
  const host = document.getElementById('cqa-board');
  if (!host || !CQA?.api || !CQA?.bus) return () => {};
  if (mounts.has(host)) return mounts.get(host);
  if (!document.querySelector('link[data-cqa-board-style]')) {
    const style = node('link'); style.rel = 'stylesheet'; style.href = new URL('../css/board.css', import.meta.url).href;
    style.dataset.cqaBoardStyle = ''; document.head.append(style);
  }
  host.classList.remove('cqa-module-placeholder'); host.classList.add('cqa-board'); host.replaceChildren();
  let destroyed = false, loading = false, importing = false, preparing = false, refreshEpoch = 0, detailEpoch = 0;
  let exportRequest = null;
  const dataContext = () => JSON.stringify([CQA.config.mock !== false, CQA.config.apiBase || './api']);
  let selectedContext = dataContext(), contextEpoch = 0, wasOffline = CQA.config.offline === true;
  let rows = [], versions = [], serverConfig = null;
  let budgetController = null, guideEpoch = 0, qualityData = null, recoveryEpoch = 0;
  const cleanup = [], tiles = new Map();
  const on = (name, fn) => { const off = CQA.bus.on(name, fn); if (typeof off === 'function') cleanup.push(off); };

  const head = node('header', 'cqa-board__head'), heading = node('div');
  heading.append(node('h1', '', 'Оценка диалогов'), node('p', 'cqa-board__intro', 'Загрузите разговоры и примените выбранный чек-лист.'));
  const refreshButton = button('Обновить наборы', () => { void refresh(); void restoreRun(); void refreshSpend(); }); head.append(heading, refreshButton);
  const demo = node('p', 'cqa-board__demo', 'Демонстрация: оценки условные, модель не вызывается. Загруженные данные хранятся только в этой сессии.');
  demo.hidden = CQA.config.mock === false;
  const notice = node('p', 'cqa-board__notice'); notice.hidden = true; notice.setAttribute('role', 'status');
  const form = node('form', 'cqa-board__form');
  const budgetHost = node('div', 'cqa-board__budget');
  const group = select([['', 'Все загруженные наборы']]);
  const scope = select([['all', 'Все диалоги набора'], ['pending', 'Без итоговой оценки'], ['active', 'Открытый диалог']]);
  scope.value = CQA.state.activeConversationId ? 'active' : 'all';
  const version = select([]);
  const mode = select([['fast', 'Быстрый'], ['accurate', 'Точный (не замерен; в облачном профиле ≈8× токенов)']]);
  const location = select([['cloud', 'Облако'], ['cascade', 'Каскад: Haiku → Gemini при сомнении'], ['routing', 'Маршрутизация: звонки и рискованные чаты → Gemini'], ['local', 'Локальная модель']]);
  location.disabled = true;
  const model = node('p', 'cqa-board__hint', 'Настройки модели загружаются.');
  model.id = 'cqa-board-model-hint'; location.setAttribute('aria-describedby', model.id);
  const guide = node('div', 'cqa-board__model-guide');
  const guideDetails = node('details');
  const guideSummary = node('summary', '', 'Стоимость и качество: сохранённый замер');
  const guideModels = node('p', '', 'Справка о моделях загружается.');
  const guideComparison = node('p'), guideLimit = node('p', 'cqa-board__hint');
  guideDetails.append(guideSummary, guideComparison, guideLimit); guide.append(guideModels, guideDetails);
  const loadGuide = async () => {
    const token = ++guideEpoch;
    budgetController?.destroy(); budgetController = null;
    if (CQA.economics) budgetHost.textContent = 'Оценка бюджета загружается; расчёт ещё недоступен.';
    try {
      const payload = typeof CQA.api.modelGuide === 'function' ? await CQA.api.modelGuide() : null;
      const content = profileGuide(payload);
      if (destroyed || token !== guideEpoch) return;
      qualityData = payload;
      syncControls();
      if (payload && typeof CQA.economics?.mountBudget === 'function') {
        budgetController?.destroy();
        budgetController = CQA.economics.mountBudget(budgetHost, { quality: payload, ...budgetContext(), onSwitchCascade: () => {
          if (location.disabled) return;
          location.value = 'cascade'; mode.value = 'fast'; syncControls();
          model.textContent = 'Выбран каскад. Настройки применятся при запуске оценки.';
        } });
        syncControls();
      }
      guideDetails.hidden = !content;
      guideModels.textContent = content ? `Быстрый режим. ${content.summary}` : 'Сохранённая справка недоступна. Фактическая модель показана выше по данным сервера.';
      guideComparison.textContent = content?.comparison || ''; guideComparison.hidden = !content;
      guideLimit.textContent = content?.limitation || ''; guideLimit.hidden = !content?.limitation;
    } catch {
      if (!destroyed && token === guideEpoch) { guideDetails.hidden = true; guideModels.textContent = 'Сохранённая справка недоступна. Выбор модели доступен по настройкам сервера.'; guideComparison.hidden = true; guideLimit.hidden = true;
        budgetController?.destroy(); budgetController = null;
        if (CQA.economics) budgetHost.textContent = 'Бюджет не рассчитан: сохранённые замеры недоступны.';
      }
    }
  };
  const live = node('input'); live.type = 'checkbox'; live.checked = false; live.dataset.boardLive = '';
  const liveLabel = node('label', 'cqa-board__check'); liveLabel.append(live, node('span', '', 'Не использовать LLM-кэш для недостающих оценок'));
  const activate = node('input'); activate.type = 'checkbox'; activate.checked = scope.value !== 'active'; activate.dataset.boardActivate = '';
  const activateLabel = node('label', 'cqa-board__check'); activateLabel.append(activate, node('span', '', 'Сделать эту версию активной после расчёта'));
  const activationHint = node('p', 'cqa-board__hint'); activationHint.id = 'cqa-board-activation-hint';
  const selection = node('p', 'cqa-board__selection');
  const startButton = node('button', 'cqa-board__button cqa-board__button--primary', 'Оценить диалоги'); startButton.type = 'submit';
  startButton.setAttribute('aria-describedby', activationHint.id);
  const fields = node('div', 'cqa-board__fields');
  fields.append(field('Набор', group), field('Диалоги', scope), field('Чек-лист', version), field('Режим оценки', mode), field('Модель', location));
  const actions = node('div', 'cqa-board__actions'); actions.append(selection, startButton);
  const plan = node('div', 'cqa-board__plan');
  const planPolicy = node('p', 'cqa-board__plan-policy'); planPolicy.id = 'cqa-board-plan-policy';
  const planHint = node('p', 'cqa-board__hint cqa-board__plan-hint');
  plan.append(node('h2', '', 'Перед запуском'), planPolicy, planHint);
  live.setAttribute('aria-describedby', planPolicy.id);
  const rerun = node('details', 'cqa-board__rerun'); rerun.append(node('summary', '', 'Полный повторный расчёт'));
  const rerunHint = node('p', 'cqa-board__hint', 'Все критерии выбранных диалогов будут оценены заново, включая неизменённые. Сохранённые ответы и LLM-кэш не используются. Это может увеличить время и стоимость.');
  rerunHint.id = 'cqa-board-rerun-hint';
  const rerunButton = button('Пересчитать всё заново', event => void start(event, true));
  rerunButton.setAttribute('aria-describedby', rerunHint.id); rerun.append(rerunHint, rerunButton);
  const databaseSpend = node('p', 'cqa-board__hint', 'Всего потрачено в этой базе: нет данных.');
  form.append(fields, model, guide, liveLabel, plan, activateLabel, activationHint, budgetHost, databaseSpend, actions, rerun);

  const importer = node('details', 'cqa-board__import'); importer.append(node('summary', '', 'Загрузить новые диалоги'));
  const importForm = node('div', 'cqa-board__import-form');
  const groupId = node('input'); groupId.type = 'text'; groupId.maxLength = 120; groupId.placeholder = 'Например, DEV'; groupId.autocomplete = 'off';
  const file = node('input'); file.type = 'file'; file.accept = '.json,application/json';
  const uploadButton = button('Загрузить JSON', () => void ingest(true));
  const fetchButton = button('Получить набор с сервера', () => void ingest(false));
  const importActions = node('div', 'cqa-board__import-actions'); importActions.append(uploadButton, fetchButton);
  importForm.append(field('Идентификатор набора', groupId, 'Для JSON можно оставить пустым. Для набора организаторов укажите выданный group_id.'),
    field('Файл транскриптов', file, 'JSON: один диалог, массив или ответ API. До 20 МБ.'), importActions);
  importer.append(importForm);

  const results = node('section', 'cqa-board__results'); results.hidden = true; results.setAttribute('aria-label', 'Ход оценки');
  const runHead = node('div', 'cqa-board__run-head'), runTitle = node('h2'), runId = node('p', 'cqa-board__hint');
  const monitorButton = button('Приостановить обновление', () => controller.state.phase === 'paused' ? controller.watch(controller.state.run, { restore: controller.state.restored }) : controller.stop());
  const runIdentity = node('div'); runIdentity.append(runTitle, runId); runHead.append(runIdentity, monitorButton);
  const progress = node('progress'); progress.max = 1; progress.value = 0; progress.setAttribute('aria-label', 'Оценённые диалоги');
  const stats = node('dl', 'cqa-board__stats');
  const statNodes = {};
  for (const [key, title] of [['done', 'Обработано'], ['errors', 'С ошибками'], ['elapsed', 'Время'], ['cost_kzt', 'Стоимость'], ['from_cache', 'Ответов переиспользовано'], ['llm_calls', 'Вызовов модели']]) {
    const entry = node('div'); statNodes[key] = node('dd', '', '—'); entry.append(node('dt', '', title), statNodes[key]); stats.append(entry);
  }
  const resultNotice = node('p', 'cqa-board__result-note'); resultNotice.setAttribute('role', 'status');
  const runPlan = node('p', 'cqa-board__run-plan'); runPlan.setAttribute('role', 'status');
  const exports = node('div', 'cqa-board__exports');
  const exportJson = button('Скачать JSON', () => void downloadRun('json'));
  const exportCsv = button('Скачать CSV', () => void downloadRun('csv'));
  const exportHint = node('p', 'cqa-board__hint'); exportHint.id = 'cqa-board-export-hint';
  exportHint.setAttribute('role', 'status');
  for (const control of [exportJson, exportCsv]) control.setAttribute('aria-describedby', exportHint.id);
  exports.append(exportJson, exportCsv, exportHint);
  const plot = node('details', 'cqa-board__distribution'); plot.append(node('summary', '', 'Распределение оценок до и после'));
  const chart = node('div', 'cqa-board__chart'); plot.append(chart);
  const grid = node('div', 'cqa-board__grid');
  results.append(runHead, runPlan, progress, stats, resultNotice, exports, plot, grid);
  const empty = node('div', 'cqa-board__empty'); empty.append(node('h2', '', 'Новая оценка начнётся здесь'), node('p', '', 'После запуска появятся результаты каждого диалога, время и стоимость. Смена только весов позволяет использовать сохранённые ответы.'));
  host.append(head, demo, notice, form, importer, results, empty);

  function message(text, failed = false) {
    notice.textContent = text || ''; notice.hidden = !text; notice.dataset.tone = failed ? 'error' : 'info';
    notice.setAttribute('role', failed ? 'alert' : 'status');
  }
  function syncControls() {
    const busy = importing || preparing || ['starting', 'running'].includes(controller.state.phase);
    const ids = conversationSelection(rows, group.value, scope.value, CQA.state.activeConversationId);
    const { descendant, fresh } = selectionPolicy(ids);
    if (descendant || fresh) live.checked = false;
    planPolicy.textContent = `${fresh ? 'Новые диалоги считаются моделью сейчас, не из кэша. Недостающие ответы также запрашиваются без LLM-кэша. ' : ''}` +
      'Неизменённые критерии с совместимыми сохранёнными ответами переиспользуются. При наличии таких ответов смена только весов не вызывает модель; замена одного критерия оценивает его и ранее недостающие ответы. Смена модели, режима или промпта может потребовать дополнительные оценки.';
    planHint.textContent = forecastText(currentForecast(ids.length));
    selection.textContent = `${ids.length} ${ids.length === 1 ? 'диалог выбран' : 'диалогов выбрано'}`;
    startButton.disabled = busy || loading || !ids.length || !version.value;
    rerunButton.disabled = startButton.disabled;
    startButton.textContent = preparing || controller.state.phase === 'starting' ? 'Запускаем…' : activate.checked ? 'Оценить и применить' : `Оценить по v${version.value || '—'}`;
    const official = versions.find(item => item.active === true)?.version;
    activationHint.textContent = activate.checked ? `v${version.value || '—'} станет активной после успешного расчёта.` :
      Number(official) === Number(version.value) ? `v${official} уже активна и остаётся официальной.` :
      `v${version.value || '—'} не станет активной; ${official != null ? `v${official} остаётся официальной` : 'официальная версия не изменится'}.`;
    for (const control of [group, scope, version, mode, live, activate]) control.disabled = busy || loading;
    mode.disabled = busy || loading || ['cascade', 'routing'].includes(location.value);
    live.disabled = busy || loading || descendant || fresh;
    location.disabled = busy || loading || !serverConfig || typeof CQA.api.setConfig !== 'function';
    budgetController?.update({ ...budgetContext(), busy: busy || loading });
    for (const control of [groupId, file, uploadButton, fetchButton]) control.disabled = busy;
    refreshButton.disabled = busy || loading;
    const run = controller.state.run;
    const canExport = exportStatuses.has(run?.status) && controller.state.phase === 'finished';
    exports.hidden = CQA.config.mock === false && !canExport;
    for (const control of [exportJson, exportCsv]) control.disabled = !!exportRequest || busy || CQA.config.mock !== false || CQA.config.offline === true || !canExport;
    exportHint.textContent = CQA.config.mock !== false || CQA.config.offline === true ? 'Экспорт прогона доступен только при подключении к серверу.' :
      canExport ? `${exportRequest ? 'Готовим файл' : 'Сохранённый снимок'}: ${run.run_id}${run.version != null ? ` · чек-лист v${run.version}` : ''} · ${failedStatuses.has(run.status) || runSummary(run).errors ? 'с ошибками; нерассчитанные оценки отсутствуют' : 'завершён'}.` :
      'Экспорт появится после завершения прогона.';
  }
  function currentForecast(count) {
    return runForecast({ quality: qualityData, profile: location.value === 'configured' ? serverConfig?.profile : location.value,
      mode: mode.value, dialogues: count, criteria: versions.find(v => Number(v.version) === Number(version.value))?.criteria?.length || 8,
      votes: serverConfig?.model?.votes });
  }
  async function refreshSpend() {
    const context = dataContext(), token = contextEpoch;
    try {
      const metrics = await CQA.api.metrics?.();
      if (destroyed || token !== contextEpoch || context !== dataContext()) return;
      databaseSpend.textContent = `Всего потрачено в этой базе: накопленный расход недоступен. Стоимость сохранённых оценок активной версии: ${finite(metrics?.cost_kzt) ? `${number(metrics.cost_kzt, 2)} ₸` : 'нет данных'}. Это не стоимость этого прогона.`;
    } catch { if (!destroyed && token === contextEpoch) databaseSpend.textContent = 'Всего потрачено в этой базе: данные недоступны.'; }
  }
  async function restoreRun() {
    if (typeof CQA.api.runs !== 'function' || controller.state.phase !== 'idle') return;
    const token = ++recoveryEpoch, context = dataContext();
    try {
      const saved = await CQA.api.runs();
      if (destroyed || token !== recoveryEpoch || dataContext() !== context || controller.state.phase !== 'idle' || preparing) return;
      const latest = latestRun(saved);
      if (latest) controller.watch(latest, { restore: true });
    } catch { if (!destroyed && token === recoveryEpoch) message('Последний прогон не удалось загрузить. Обновите наборы, чтобы повторить.'); }
  }
  function budgetContext() {
    const ids = new Set(conversationSelection(rows, group.value, scope.value, CQA.state.activeConversationId));
    const selected = rows.filter(row => ids.has(row.id));
    const channels = { chat: selected.filter(row => row.channel === 'chat').length, call: selected.filter(row => row.channel === 'call').length };
    return { plan: { dialogues: ids.size, channels, mode: mode.value },
      profile: location.value === 'configured' ? serverConfig?.profile : location.value };
  }
  function selectionPolicy(ids) {
    const selected = new Set(ids);
    const chosen = versions.find(item => Number(item.version) === Number(version.value));
    return {
      descendant: chosen?.parent_version != null,
      // Null alone is not an error (e.g. all N/A). Pending/incomplete selections must bypass LLM cache;
      // the server still reuses compatible saved criterion answers even when live is true.
      fresh: rows.some(row => selected.has(row.id) && (row.status === 'pending' || row.status === 'incomplete' ||
        (!finite(row.total) && row.status !== 'no_applicable'))),
    };
  }
  async function downloadRun(format) {
    const run = controller.state.run;
    if (destroyed || exportRequest || preparing || importing || CQA.config.mock !== false || CQA.config.offline === true || controller.state.phase !== 'finished' || !exportStatuses.has(run?.status)) return;
    const request = { run, context: dataContext() };
    exportRequest = request;
    const current = () => !destroyed && exportRequest === request && controller.state.run === run &&
      controller.state.phase === 'finished' && !preparing && !importing && CQA.config.mock === false &&
      CQA.config.offline !== true && dataContext() === request.context;
    syncControls(); message('');
    try {
      const blob = await CQA.api.exportRun(run.run_id, format);
      if (!current()) return;
      const url = URL.createObjectURL(blob);
      const link = node('a'); link.href = url; link.download = `${run.run_id.replace(/[^a-zA-Z0-9_-]/g, '_')}.${format}`;
      link.hidden = true; host.append(link); link.click(); link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (error) { if (current()) message(`Не удалось скачать файл прогона ${run.run_id}: ${errorText(error)}`, true); }
    finally { if (exportRequest === request) { exportRequest = null; if (!destroyed) syncControls(); } }
  }
  function fillChoices() {
    const previousGroup = group.value;
    const groups = [...new Set(rows.map(row => row.group_id).filter(Boolean))];
    group.replaceChildren(option('', 'Все загруженные наборы'), ...groups.map(g => option(g, g)));
    group.value = groups.includes(previousGroup) ? previousGroup : '';
    const chosenVersion = Number(CQA.state.checklistVersion) || Number(version.value);
    version.replaceChildren(...versions.map(item => option(String(item.version), `v${item.version} — ${item.name || 'Чек-лист'}`)));
    if (versions.some(item => item.version === chosenVersion)) version.value = String(chosenVersion);
    syncControls();
  }
  function showModel(config) {
    model.textContent = modelHint(config);
    if (!config) return;
    const selected = profileChoice(config);
    location.replaceChildren(option('cloud', 'Облако'), option('cascade', 'Каскад: Haiku → Gemini при сомнении'), option('routing', 'Маршрутизация: звонки и рискованные чаты → Gemini'), option('local', 'Локальная модель'));
    // Preserve explicit mock/custom server profiles until the user chooses a different one.
    if (selected === 'configured') location.append(option('configured', config.profile === 'mock' ? 'Демо без модели' : `Текущий профиль: ${config.profile}`));
    location.value = selected;
    mode.value = ['cascade', 'routing'].includes(selected) ? 'fast' : config.mode || mode.value || 'fast';
  }
  async function refresh() {
    if (destroyed) return;
    const token = ++refreshEpoch; loading = true; syncControls();
    try {
      const [nextRows, nextVersions, config] = await Promise.all([
        CQA.api.conversations(), CQA.api.checklists(), typeof CQA.api.config === 'function' ? CQA.api.config() : Promise.resolve(null),
      ]);
      if (destroyed || token !== refreshEpoch) return;
      rows = Array.isArray(nextRows) ? nextRows : [];
      if (scope.value === 'active' && !rows.some(row => row.id === CQA.state.activeConversationId)) scope.value = 'all';
      versions = Array.isArray(nextVersions) ? nextVersions : [];
      serverConfig = config;
      showModel(config); fillChoices(); message(rows.length ? '' : 'Загрузите JSON или получите набор с сервера.'); return true;
    } catch (error) { if (!destroyed && token === refreshEpoch) message(errorText(error), true); return false; }
    finally { if (!destroyed && token === refreshEpoch) { loading = false; syncControls(); } }
  }
  async function ingest(fromFile) {
    if (destroyed || importing || preparing || ['starting', 'running'].includes(controller.state.phase)) return;
    let payload;
    const selectedFile = file.files?.[0];
    const id = groupId.value.trim();
    if (fromFile && !selectedFile) { message('Выберите JSON-файл с диалогами.', true); file.focus(); return; }
    if (!fromFile && !id) { message('Укажите идентификатор набора.', true); groupId.focus(); return; }
    if (selectedFile && fromFile && selectedFile.size > 20 * 1024 * 1024) { message('Файл больше 20 МБ. Разделите набор на несколько JSON-файлов.', true); return; }
    exportRequest = null; importing = true; syncControls(); message('Загружаем диалоги…');
    try {
      if (fromFile) payload = parseImport(await selectedFile.text());
      if (destroyed) return;
      const result = await CQA.api.ingest(id || 'UPLOAD', payload);
      if (destroyed) return;
      if (!finite(result?.count) || result.count < 1) throw new Error('Сервер не подтвердил загрузку диалогов.');
      const refreshed = await refresh();
      const importedGroup = result.group_id || id || 'UPLOAD';
      if (rows.some(row => row.group_id === importedGroup)) group.value = importedGroup;
      scope.value = 'all'; file.value = ''; live.checked = false;
      CQA.bus.emit('conversations:refresh', { group_id: importedGroup });
      message(refreshed ? `Загружено диалогов: ${result.count}. Новые диалоги будут оценены моделью без LLM-кэша. Выберите чек-лист и запустите оценку.` :
        `Сервер принял ${result.count} диалогов, но список не обновился. Нажмите «Обновить наборы» перед оценкой.`, !refreshed);
    } catch (error) { if (!destroyed) message(errorText(error), true); }
    finally { importing = false; if (!destroyed) syncControls(); }
  }
  async function start(event, fullRerun = false) {
    event?.preventDefault();
    if (destroyed || preparing || importing || loading || ['starting', 'running'].includes(controller.state.phase)) return;
    const ids = conversationSelection(rows, group.value, scope.value, CQA.state.activeConversationId);
    if (!ids.length || !version.value) { message('Выберите диалоги и сохранённую версию чек-листа.', true); return; }
    // live:false only permits cache use; it does not guarantee a cache hit.
    if (CQA.config.mock === false && (location.value === 'configured' ? serverConfig?.profile : location.value) !== 'mock' && ids.length > 1 &&
        !globalThis.confirm?.(`${forecastText(currentForecast(ids.length))}\n\nЗапустить расчёт для ${ids.length} диалогов?`)) return;
    ++recoveryEpoch;
    const { descendant, fresh } = selectionPolicy(ids);
    const body = { version: Number(version.value), conv_ids: ids, mode: mode.value,
      live: fullRerun || fresh || (!descendant && live.checked), force: fullRerun,
      activate: activate.checked, preview: false };
    if (group.value) body.group_id = group.value;
    const settings = { mode: body.mode, ...profileSettings(location.value, serverConfig) };
    const context = dataContext(), contextToken = contextEpoch;
    const currentContext = () => !destroyed && contextToken === contextEpoch && dataContext() === context;
    exportRequest = null; preparing = true; syncControls(); message('');
    try {
      let config = serverConfig;
      if (typeof CQA.api.setConfig === 'function') config = await CQA.api.setConfig(settings);
      if (!currentContext()) return;
      serverConfig = config; showModel(config);
      if (config?.profile) body.profile = config.profile;
      CQA.state.checklistVersion = body.version;
      await controller.start(body);
    } catch (error) { if (currentContext()) message(errorText(error), true); }
    finally { preparing = false; if (!destroyed) syncControls(); }
  }

  function renderTile(row) {
    let tile = tiles.get(row.id);
    if (!tile) {
      const root = button('', () => CQA.bus.emit('conv:open', { id: row.id, version: controller.state.run?.version }), 'cqa-board__tile'); root.dataset.convId = row.id;
      const title = node('strong', 'cqa-board__tile-id', row.id), status = node('span', 'cqa-board__tile-status');
      const scores = node('span', 'cqa-board__tile-scores'), before = node('span'), arrow = node('span', 'cqa-board__arrow', '→'), after = node('strong');
      arrow.setAttribute('aria-hidden', 'true'); scores.append(before, arrow, after);
      const error = node('span', 'cqa-board__tile-error'); error.hidden = true;
      root.append(title, status, scores, error); tile = { root, status, before, after, error }; tiles.set(row.id, tile); grid.append(root);
    }
    const completed = completedStatuses.has(row.status), issue = rowIssue(row);
    tile.root.dataset.state = issue || (completed ? 'done' : row.status === 'running' ? 'running' : 'pending');
    tile.root.dataset.change = !issue && finite(row.old_total) && finite(row.new_total) ? row.new_total > row.old_total ? 'up' : row.new_total < row.old_total ? 'down' : 'same' : 'new';
    tile.status.textContent = issue === 'incomplete' ? 'Неполная оценка' : issue ? 'Ошибка оценки' : completed ? 'Оценено' : row.status === 'running' ? 'Оценивается' : 'В очереди';
    tile.before.textContent = number(row.old_total);
    tile.after.textContent = issue ? 'Не рассчитан' : row.score?.status === 'no_applicable' ? 'Не применимо' : number(row.new_total);
    tile.error.textContent = row.error || (issue === 'incomplete' ? `Нет полного ответа${Array.isArray(row.score?.missing) && row.score.missing.length ? ` по критериям: ${row.score.missing.join(', ')}` : ''}. Откройте диалог для проверки.` : '');
    tile.error.hidden = !tile.error.textContent;
    tile.root.setAttribute('aria-label', `${row.id}. ${tile.status.textContent}. До: ${number(row.old_total)}. После: ${tile.after.textContent}. ${tile.error.textContent} Открыть диалог.`);
  }
  function renderChart(summary) {
    chart.replaceChildren();
    if (!summary.before.some(Boolean) && !summary.after.some(Boolean)) {
      chart.append(node('p', 'cqa-board__hint', 'Распределение появится после получения числовых оценок.')); return;
    }
    const max = Math.max(1, ...summary.before, ...summary.after);
    chart.append(node('p', 'cqa-board__chart-legend', 'Серый — до применения. Зелёный — после. Пропущенные оценки не учитываются.'));
    for (let i = 0; i < 5; i++) {
      const row = node('div', 'cqa-board__chart-row');
      const label = i === 4 ? '80–100' : `${i * 20}–<${(i + 1) * 20}`;
      row.append(node('span', '', label));
      const tracks = node('div', 'cqa-board__chart-tracks'); tracks.setAttribute('aria-hidden', 'true');
      for (const key of ['before', 'after']) { const bar = node('span', `cqa-board__bar cqa-board__bar--${key}`); bar.style.width = `${summary[key][i] / max * 100}%`; tracks.append(bar); }
      row.append(tracks, node('span', 'cqa-board__chart-count', `${summary.before[i]} → ${summary.after[i]}`)); chart.append(row);
    }
  }
  function renderRun(state) {
    // A new observation is a new selection, even when it returns to the same run ID.
    if (state.phase !== 'finished' || exportRequest?.run !== state.run) exportRequest = null;
    syncControls();
    if (state.error) message(state.run ? `Обновление приостановлено: ${errorText(state.error)} Прогон может продолжаться на сервере.` : `Запуск не подтверждён: ${errorText(state.error)}`, true);
    else if (state.phase === 'running' || state.phase === 'finished') message('');
    const run = state.run;
    if (!run) { results.hidden = true; empty.hidden = false; return; }
    results.hidden = false; empty.hidden = true;
    const summary = runSummary(run), terminal = state.phase === 'finished';
    runTitle.textContent = state.phase === 'interrupted' ? 'Прогон прерван (сервер перезапускался) — можно запустить заново' : state.phase === 'restoring' ? 'Проверяем сохранённый прогон' : terminal ? failedStatuses.has(run.status) ? 'Прогон завершился с ошибкой' : summary.errors ? 'Оценка завершена с ошибками' : 'Оценка завершена' : state.phase === 'paused' ? 'Обновление приостановлено' : 'Идёт оценка';
    runId.textContent = `${state.restored ? 'Сохранённый прогон' : 'Прогон'} ${run.run_id}${run.version ? ` · чек-лист v${run.version}` : ''}`;
    runPlan.textContent = serverPlanText(run);
    monitorButton.hidden = terminal || state.phase === 'interrupted'; monitorButton.textContent = state.phase === 'paused' ? 'Возобновить обновление' : 'Приостановить обновление';
    progress.max = finite(run.total) && run.total > 0 ? run.total : 1; progress.value = finite(run.done) ? Math.min(run.done, progress.max) : 0;
    statNodes.done.textContent = `${number(run.done, 0)} / ${number(run.total, 0)}`;
    statNodes.errors.textContent = number(summary.errors, 0);
    statNodes.elapsed.textContent = finite(run.elapsed) ? `${number(run.elapsed, 1)} с` : 'Не измерено';
    statNodes.cost_kzt.textContent = finite(run.cost_kzt) ? `${number(run.cost_kzt, 4)} ₸` : 'Не измерено';
    statNodes.from_cache.textContent = finite(run.from_cache) ? `${number(run.from_cache, 0)}${finite(run.cache_total) ? ` / ${number(run.cache_total, 0)}` : ''}` : 'Нет данных';
    statNodes.llm_calls.textContent = number(run.llm_calls, 0);
    resultNotice.textContent = state.phase === 'interrupted' ? 'Сервер не подтвердил живой прогон. Сохранённые результаты доступны; форма нового запуска разблокирована.' : state.phase === 'restoring' ? 'Проверяем состояние ограниченным числом запросов. Форма нового запуска доступна.' : state.phase === 'paused' ? 'Остановлено только обновление экрана. Серверный прогон не отменён.' :
      run.error ? run.error : summary.errors ? `Не удалось оценить: ${summary.errors}. Откройте диалог, проверьте ошибку и повторите запуск для нужных диалогов.` :
      terminal ? `Изменились оценки: ${summary.changed} из ${summary.compared} сравнимых. ${CQA.config.mock !== false ? 'Это демонстрационные результаты.' : ''}` : 'Результаты появляются по мере готовности.';
    if (state.restored && terminal && run.llm_calls === 0 && run.cost_kzt === 0 && CQA.config.mock === false) {
      resultNotice.textContent += ' Результаты из кэша/базы, без вызовов модели: расход этого прогона — 0 ₸. Стоимость сохранённых оценок указана отдельно.';
    }
    resultNotice.dataset.tone = run.error || summary.errors ? 'error' : 'info';
    const currentIds = new Set(summary.rows.map(row => row.id));
    for (const [id, tile] of tiles) if (!currentIds.has(id)) { tile.root.remove(); tiles.delete(id); }
    summary.rows.forEach(renderTile); renderChart(summary);
  }
  const controller = createRunController(CQA.api, {
    onChange: renderRun,
    onProgress: run => CQA.bus.emit('run:progress', run),
    onFinish: run => {
      if (run.activated === true && !failedStatuses.has(run.status) && !runSummary(run).errors && run.version != null) CQA.bus.emit('checklist:activated', { version: run.version });
      CQA.bus.emit('run:finished', run); CQA.bus.emit('run:completed', run);
      CQA.bus.emit('conversations:refresh', { run_id: run.run_id });
      void refresh(); void refreshSpend();
      const id = CQA.state.activeConversationId, token = ++detailEpoch;
      if (id && runSummary(run).rows.some(row => row.id === id && ['done', 'complete', 'completed'].includes(row.status))) {
        void CQA.api.conversation(id, run.version).then(detail => {
          if (!destroyed && token === detailEpoch && CQA.state.activeConversationId === id) CQA.bus.emit('score:updated', detail);
        }).catch(error => { if (!destroyed) message(`Оценка завершена, но карточка не обновилась: ${errorText(error)}`, true); });
      }
    },
  });
  form.addEventListener('submit', start);
  group.addEventListener('change', syncControls); scope.addEventListener('change', () => { if (scope.value === 'active') activate.checked = false; syncControls(); });
  version.addEventListener('change', syncControls);
  activate.addEventListener('change', syncControls);
  for (const control of [mode, location]) control.addEventListener('change', () => {
    if (['cascade', 'routing'].includes(location.value)) mode.value = 'fast';
    syncControls();
    model.textContent = location.value === 'cascade' ? 'Каскад работает в быстром режиме: сначала Haiku, при сомнении Gemini. Настройки применятся при запуске.'
      : location.value === 'routing' ? 'Звонки сразу на Gemini. Чаты начинает Haiku; при сомнении или C3 Да/Нет — перепроверка Gemini. Настройки применятся при запуске.'
      : 'Выбранные настройки модели будут применены при запуске.';
  });
  on('conv:open', () => { scope.value = 'active'; group.value = ''; activate.checked = false; syncControls(); });
  on('board:prepare', async request => {
    ++recoveryEpoch;
    const prepared = await refresh();
    if (destroyed || !prepared) return;
    version.value = String(request.version); group.value = request.group || '';
    scope.value = CQA.state.activeConversationId ? 'active' : 'all';
    if (scope.value === 'active') group.value = '';
    activate.checked = scope.value !== 'active'; syncControls();
    CQA.bus.emit('nav:open', 'board');
  });
  on('checklist:selected', value => { if (versions.some(v => v.version === Number(value))) { version.value = String(value); syncControls(); } });
  on('checklist:saved', () => void refresh());
  on('run:started', run => {
    if (!run?.run_id || destroyed) return;
    ++recoveryEpoch; message(''); CQA.bus.emit('nav:open', 'board'); controller.watch(run);
  });
  on('api:mode', () => {
    const context = dataContext(), offline = CQA.config.offline === true;
    const sourceChanged = context !== selectedContext || offline !== wasOffline;
    if (sourceChanged) exportRequest = null;
    if (context !== selectedContext) { selectedContext = context; ++contextEpoch; controller.reset(); }
    wasOffline = offline; demo.hidden = CQA.config.mock === false; syncControls();
    if (sourceChanged) { qualityData = null; ++recoveryEpoch; void loadGuide(); void refresh(); void restoreRun(); void refreshSpend(); }
  });
  void refresh(); void loadGuide(); void restoreRun(); void refreshSpend();
  const destroy = () => {
    budgetController?.destroy();
    destroyed = true; exportRequest = null; ++guideEpoch; ++refreshEpoch; ++detailEpoch; controller.destroy(); cleanup.forEach(off => off());
    mounts.delete(host);
  };
  mounts.set(host, destroy); return destroy;
}
