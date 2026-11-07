/* Independent human annotation. Intentionally imports neither core.js nor api.js. */
const SCHEMA = 1;
const own = (object, key) => Object.prototype.hasOwnProperty.call(object, key);
const record = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const digest = value => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value);

export function annotatorId(value) {
  if (typeof value !== 'string') throw new Error('Укажите имя или постоянный код разметчика.');
  const id = value.trim();
  if (!id || id.length > 80 || /[\u0000-\u001f\u007f]/u.test(id)) {
    throw new Error('Укажите имя или код: от 1 до 80 символов, без переносов строк.');
  }
  return id;
}

export function validateChecklist(checklist) {
  if (!record(checklist) || !Number.isSafeInteger(checklist.version) || checklist.version < 1 ||
      !checklist.checklist_id || !digest(checklist.checklist_hash) || !Array.isArray(checklist.criteria) ||
      !checklist.criteria.length || checklist.criterion_count !== checklist.criteria.length) {
    throw new Error('Некорректная версия чек-листа. Обновите список.');
  }
  const ids = new Set();
  for (const c of checklist.criteria) {
    if (!record(c) || typeof c.id !== 'string' || !c.id || ids.has(c.id) ||
        typeof c.hash !== 'string' || !/^[a-f0-9]{16}$/.test(c.hash) ||
        typeof c.title !== 'string' || typeof c.na_allowed !== 'boolean' ||
        !['binary', 'scale', 'numeric'].includes(c.answer_type)) {
      throw new Error('Некорректный или повторяющийся критерий. Обновите чек-лист.');
    }
    ids.add(c.id);
    if (c.answer_type !== 'binary' && (!Array.isArray(c.scale) || !c.scale.length ||
        c.scale.some(n => typeof n !== 'number' || !Number.isFinite(n) || n < 0) ||
        new Set(c.scale).size !== c.scale.length || Math.max(...c.scale) <= 0)) {
      throw new Error(`Некорректная шкала ${c.id}.`);
    }
  }
  return checklist;
}

export function allowedAnswer(criterion, answer) {
  if (answer === 'na') return criterion.na_allowed === true;
  if (criterion.answer_type === 'binary') return answer === 'yes' || answer === 'no';
  if (typeof answer !== 'number' || !Number.isFinite(answer)) return false;
  const scale = criterion.scale;
  if (!Array.isArray(scale) || !scale.length) return false;
  if (criterion.answer_type === 'scale') return scale.includes(answer);
  if (criterion.answer_type !== 'numeric') return false;
  return answer >= (scale.length > 1 ? Math.min(...scale) : 0) && answer <= Math.max(...scale);
}

function scopeOf(context) {
  const cl = validateChecklist(context.checklist), conv = context.conversation;
  if (!record(conv) || typeof conv.id !== 'string' || !conv.id || !digest(conv.transcript_hash)) {
    throw new Error('Не удалось проверить исходный диалог. Загрузите его повторно.');
  }
  return {api_scope: context.apiScope, annotator: annotatorId(context.annotator),
    conv_id: conv.id, checklist_id: cl.checklist_id, checklist_version: cl.version,
    checklist_hash: cl.checklist_hash, transcript_hash: conv.transcript_hash};
}

export function draftKey(context) {
  // Tuple encoding avoids collisions when an ID itself contains punctuation.
  const s = scopeOf(context);
  return 'dalel:blind:v1:' + JSON.stringify([s.api_scope, s.annotator, s.conv_id,
    s.checklist_id, s.checklist_version, s.checklist_hash, s.transcript_hash]);
}

export function emptyDraft(context) {
  return {schema_version: SCHEMA, scope: scopeOf(context), answers: {}};
}

function equalScope(a, b) {
  return record(a) && Object.keys(a).length === Object.keys(b).length &&
    Object.keys(b).every(key => own(a, key) && a[key] === b[key]);
}

export function restoreDraft(raw, context) {
  const value = typeof raw === 'string' ? JSON.parse(raw) : raw;
  if (!record(value) || value.schema_version !== SCHEMA || !equalScope(value.scope, scopeOf(context)) ||
      !record(value.answers)) throw new Error('Черновик относится к другим исходным данным.');
  const byId = new Map(context.checklist.criteria.map(c => [c.id, c]));
  const entries = Object.entries(value.answers).map(([id, cell]) => {
    const criterion = byId.get(id);
    if (!criterion || !record(cell) || cell.criterion_hash !== criterion.hash ||
        !own(cell, 'answer') || !allowedAnswer(criterion, cell.answer) ||
        typeof cell.annotated_at !== 'string' || !Number.isFinite(Date.parse(cell.annotated_at))) {
      throw new Error('В черновике есть устаревший или недопустимый ответ.');
    }
    return [id, {answer: cell.answer, criterion_hash: cell.criterion_hash, annotated_at: cell.annotated_at}];
  });
  return {...emptyDraft(context), answers: Object.fromEntries(entries)};
}

export function setAnswer(draft, context, criterionId, answer, now = new Date().toISOString()) {
  const next = restoreDraft(draft, context);
  const criterion = context.checklist.criteria.find(c => c.id === criterionId);
  if (!criterion) throw new Error('Критерий отсутствует в выбранной версии.');
  if (answer === undefined) {
    delete next.answers[criterionId];
    return next;
  }
  if (!allowedAnswer(criterion, answer)) throw new Error(`Недопустимый ответ для ${criterionId}.`);
  if (typeof now !== 'string' || !Number.isFinite(Date.parse(now))) throw new Error('Некорректная дата ответа.');
  Object.defineProperty(next.answers, criterionId, {value: {answer, criterion_hash: criterion.hash,
    annotated_at: now}, enumerable: true, writable: true, configurable: true});
  return next;
}

export function exportRows(draft, context, criterionIds = null) {
  const clean = restoreDraft(draft, context);
  if (criterionIds !== null && !Array.isArray(criterionIds)) throw new Error('Некорректный список критериев для экспорта.');
  const selected = criterionIds === null ? context.checklist.criteria : context.checklist.criteria.filter(c => criterionIds.includes(c.id));
  if (criterionIds !== null && (!Array.isArray(criterionIds) || !criterionIds.length ||
      new Set(criterionIds).size !== criterionIds.length || selected.length !== criterionIds.length)) {
    throw new Error('Некорректный список критериев для экспорта.');
  }
  const missing = selected.filter(c => !own(clean.answers, c.id));
  if (missing.length) throw new Error(`Ответьте на все критерии. Осталось: ${missing.length}.`);
  return selected.map(c => ({
    conv_id: clean.scope.conv_id, criterion_id: c.id, answer: clean.answers[c.id].answer,
    annotator: clean.scope.annotator, checklist_version: clean.scope.checklist_version,
    criterion_hash: c.hash, checklist_hash: clean.scope.checklist_hash,
    transcript_hash: clean.scope.transcript_hash, annotation_source: 'human',
    annotated_at: clean.answers[c.id].annotated_at,
  }));
}

export function toJSONL(draft, context) {
  return exportRows(draft, context).map(row => JSON.stringify(row)).join('\n') + '\n';
}

export function validatePriorityQueue(value, checklist = null) {
  if (!record(value) || Object.keys(value).some(k => !['schema_version', 'checklist_id', 'checklist_version', 'cells'].includes(k)) ||
      value.schema_version !== 1 || typeof value.checklist_id !== 'string' || !value.checklist_id ||
      !Number.isSafeInteger(value.checklist_version) || value.checklist_version < 1 || !Array.isArray(value.cells) ||
      !value.cells.length || value.cells.length > 1000) throw new Error('Некорректная очередь разметки.');
  const seen = new Set();
  for (const cell of value.cells) {
    if (!record(cell) || Object.keys(cell).sort().join(',') !== 'conv_id,criterion_hash,criterion_id' ||
        typeof cell.conv_id !== 'string' || !cell.conv_id || typeof cell.criterion_id !== 'string' || !cell.criterion_id ||
        !/^[a-f0-9]{16}$/.test(cell.criterion_hash)) throw new Error('Очередь должна содержать только задания без готовых ответов.');
    const key = JSON.stringify([cell.conv_id, cell.criterion_id]);
    if (seen.has(key)) throw new Error('В очереди есть повторяющееся задание.');
    seen.add(key);
  }
  if (checklist) {
    validateChecklist(checklist);
    if (value.checklist_version !== checklist.version || value.checklist_id !== checklist.checklist_id ||
        value.cells.some(cell => checklist.criteria.find(c => c.id === cell.criterion_id)?.hash !== cell.criterion_hash)) {
      throw new Error('Очередь относится к другой версии критериев. Попросите координатора обновить очередь.');
    }
  }
  return value;
}

export function createPriorityCursor(queue) {
  validatePriorityQueue(queue);
  let index = 0;
  return {get index() { return index; }, get current() { return queue.cells[index]; },
    move(delta) {
      if (!Number.isSafeInteger(delta)) throw new Error('Некорректный шаг очереди.');
      index = Math.max(0, Math.min(queue.cells.length - 1, index + delta));
      return queue.cells[index];
    }};
}

/** Export only explicit human answers; re-read blind inputs before accepting any cached draft. */
export async function exportPriorityQueue({queue, api, storage, drafts = new Map(), annotator}) {
  validatePriorityQueue(queue);
  const who = annotatorId(annotator), checklist = await api.checklist(queue.checklist_version);
  validatePriorityQueue(queue, checklist);
  const byConversation = new Map();
  for (const cell of queue.cells) {
    if (!byConversation.has(cell.conv_id)) byConversation.set(cell.conv_id, []);
    byConversation.get(cell.conv_id).push(cell.criterion_id);
  }
  const rows = [];
  for (const [convId, ids] of byConversation) {
    const conversation = await api.conversation(convId);
    if (conversation.id !== convId) throw new Error('Сервер вернул другой диалог.');
    const context = {apiScope: api.scope, annotator: who, conversation, checklist};
    const key = draftKey(context);
    // Compare previous in-memory context before looking up the fresh-hash key.
    const remembered = [...drafts.values()].find(item => item.context.apiScope === api.scope &&
      item.context.annotator === who && item.context.conversation.id === convId &&
      item.context.checklist.version === checklist.version);
    let raw = remembered?.draft ?? drafts.get(key)?.draft;
    if (raw === undefined) {
      try { raw = storage?.getItem(key); } catch { raw = null; }
    }
    if (raw === null || raw === undefined) continue;
    const draft = restoreDraft(raw, context);
    const answered = ids.filter(id => own(draft.answers, id));
    if (answered.length) rows.push(...exportRows(draft, context, answered));
  }
  if (!rows.length) throw new Error('В очереди пока нет заполненных ответов.');
  const order = new Map(queue.cells.map((c, i) => [JSON.stringify([c.conv_id, c.criterion_id]), i]));
  rows.sort((a, b) => order.get(JSON.stringify([a.conv_id, a.criterion_id])) - order.get(JSON.stringify([b.conv_id, b.criterion_id])));
  return {rows, jsonl: rows.map(row => JSON.stringify(row)).join('\n') + '\n'};
}

export function createBlindClient({apiBase = './api', baseURL, fetchImpl = globalThis.fetch} = {}) {
  const base = new URL(String(apiBase).replace(/\/+$/, '') + '/', baseURL);
  if (!['http:', 'https:'].includes(base.protocol)) throw new Error('Откройте разметку через HTTP-сервер.');
  const read = async path => {
    const response = await fetchImpl(new URL(`labeling/${path}`, base).href,
      {method: 'GET', cache: 'no-store', credentials: 'same-origin', headers: {Accept: 'application/json'}});
    if (!response.ok) throw new Error(`Не удалось загрузить данные (${response.status}). Повторите загрузку.`);
    return response.json();
  };
  return {scope: base.href, conversations: () => read('conversations'), checklists: () => read('checklists'),
    conversation: id => read(`conversations/${encodeURIComponent(id)}`),
    checklist: version => {
      if (!Number.isSafeInteger(version) || version < 1) throw new Error('Выберите версию чек-листа.');
      return read(`checklists/${version}`);
    }};
}

/** State machine kept DOM-free so race, draft, export and network boundaries are tested. */
export function createSession({api, storage, onChange = () => {}}) {
  let revision = 0;
  const state = {loading: false, exporting: false, context: null, draft: null, error: '', notice: ''};
  const emit = () => onChange(state);
  const persist = () => {
    try {
      if (!storage) throw new Error('Storage unavailable');
      storage.setItem(draftKey(state.context), JSON.stringify(state.draft));
      state.notice = 'Черновик сохранён в этом браузере.';
    } catch {
      state.notice = 'Браузер не сохраняет черновики. Скачайте ответы до закрытия страницы.';
    }
  };
  return {
    state,
    async select({annotator, convId, version}) {
      const ticket = ++revision;
      Object.assign(state, {loading: false, exporting: false, context: null, draft: null, error: '', notice: ''});
      let id;
      try { id = annotatorId(annotator); } catch (error) { state.error = error.message; emit(); return; }
      if (!convId || !Number.isSafeInteger(version) || version < 1) { emit(); return; }
      state.loading = true; emit();
      try {
        const [conversation, checklist] = await Promise.all([api.conversation(convId), api.checklist(version)]);
        if (ticket !== revision) return;
        if (conversation.id !== convId || checklist.version !== version) throw new Error('Сервер вернул другую версию данных.');
        const context = {apiScope: api.scope, annotator: id, conversation, checklist};
        const draft = emptyDraft(context);
        Object.assign(state, {context, draft});
        try {
          if (!storage) throw new Error('Storage unavailable');
          const saved = storage.getItem(draftKey(context));
          if (saved !== null) {
            state.draft = restoreDraft(saved, context);
            state.notice = 'Восстановлен ваш черновик для этой версии.';
          } else state.notice = 'Ответы сохраняются в этом браузере по мере заполнения.';
        } catch {
          state.notice = 'Черновик недоступен или устарел. Заполните ответы заново и скачайте файл.';
        }
      } catch (error) { if (ticket === revision) { state.error = error.message; state.context = null; state.draft = null; } }
      finally { if (ticket === revision) { state.loading = false; emit(); } }
    },
    answer(id, answer) {
      if (!state.context || state.loading || state.exporting) return;
      state.draft = setAnswer(state.draft, state.context, id, answer);
      state.error = ''; persist(); emit();
    },
    async download() {
      if (!state.context || state.loading || state.exporting) throw new Error('Дождитесь загрузки диалога.');
      const ticket = revision, context = state.context, draft = state.draft;
      const jsonl = toJSONL(draft, context); // completeness before any request
      state.exporting = true; state.error = ''; emit();
      try {
        const [conversation, checklist] = await Promise.all([
          api.conversation(context.conversation.id), api.checklist(context.checklist.version),
        ]);
        const fresh = {...context, conversation, checklist};
        if (ticket !== revision) throw new Error('Выбор изменился. Повторите скачивание.');
        // Revalidate hashes and every criterion; never relabel an old draft with a new version.
        restoreDraft(draft, fresh);
        state.notice = 'Файл подготовлен. Передайте его для сверки независимых разметок.';
        return {jsonl, rows: exportRows(draft, context), context};
      } catch (error) {
        if (ticket === revision) state.error = `Файл не выгружен: ${error.message}`;
        throw error;
      } finally { if (ticket === revision) { state.exporting = false; emit(); } }
    },
    destroy() { ++revision; },
  };
}

function node(doc, tag, attrs = {}, ...children) {
  const element = doc.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value !== undefined && value !== null) element.setAttribute(key, String(value));
  }
  for (const child of children.flat()) element.append(typeof child === 'string' ? doc.createTextNode(child) : child);
  return element;
}

const speakerNames = {client: 'Клиент', operator: 'Оператор', bot: 'Бот', system: 'Система'};
const answerLabel = value => value === 'yes' ? 'Да' : value === 'no' ? 'Нет' : value === 'na' ? 'Не применимо' : String(value);

/** Main must call ONLY this boot for ?label=1, before importing core.js. */
export async function boot({root = document.querySelector('.cqa-root') || document.body,
  config = window.CQA_CONFIG || {}} = {}) {
  const doc = root.ownerDocument;
  const priorityMode = new URL(doc.baseURI).searchParams.get('queue') === 'priority';
  const el = (tag, attrs, ...children) => node(doc, tag, attrs, ...children);
  root.classList.add('cqa-root', 'cqa-labeling');
  doc.title = 'Дәлел · Независимая разметка';
  if (!doc.querySelector('link[data-blind-labeling]')) {
    doc.head.append(el('link', {rel: 'stylesheet', href: new URL('../css/labeling.css', import.meta.url).href,
      'data-blind-labeling': ''}));
  }
  // Remove the scored application shell, even when it was present in static HTML.
  const annotator = el('input', {id: 'label-annotator', type: 'text', required: '', maxlength: '80',
    autocomplete: 'off', spellcheck: 'false', 'aria-describedby': 'label-annotator-help label-annotator-error'});
  const identityHelp = el('small', {id: 'label-annotator-help'}, 'Используйте один код во всех своих файлах.');
  const identityError = el('p', {id: 'label-annotator-error', class: 'label-error', hidden: ''});
  const versions = el('select', {id: 'label-version', disabled: ''}, el('option', {value: ''}, 'Загрузка…'));
  const conversations = el('select', {id: 'label-conversation', disabled: ''}, el('option', {value: ''}, 'Загрузка…'));
  const status = el('p', {class: 'label-status', role: 'status', 'aria-live': 'polite'});
  const error = el('p', {class: 'label-error', role: 'alert', hidden: ''});
  const retry = el('button', {type: 'button', class: 'label-secondary', hidden: ''}, 'Повторить загрузку');
  const workspace = el('main', {id: 'label-workspace', tabindex: '-1', class: 'label-workspace'});
  const progress = el('p', {class: 'label-progress', 'aria-live': 'polite'}, 'Выберите разметчика, диалог и версию.');
  const download = el('button', {type: 'button', class: 'label-primary', disabled: ''}, 'Скачать JSONL');
  const queueInfo = el('p', {class: 'label-queue-info', role: 'status', 'aria-live': 'polite'});
  const previous = el('button', {type: 'button', class: 'label-secondary', disabled: ''}, 'Предыдущий критерий');
  const next = el('button', {type: 'button', class: 'label-secondary', disabled: ''}, 'Следующий критерий');
  const queueNavigation = el('nav', {class: 'label-queue', 'aria-label': 'Очередь критериев', hidden: priorityMode ? null : ''},
    queueInfo, el('div', {class: 'label-queue-buttons'}, previous, next));
  root.replaceChildren(
    el('a', {href: '#label-workspace', class: 'label-skip'}, 'Перейти к разметке'),
    el('header', {class: 'label-heading'}, el('p', {class: 'label-brand'}, 'Дәлел'),
      el('h1', {}, 'Независимая разметка'), el('p', {}, 'Прочитайте диалог и ответьте по выбранному чек-листу. Ответы модели здесь не загружаются.')),
    el('section', {class: 'label-controls', 'aria-label': 'Выбор задания'},
      el('div', {}, el('label', {for: 'label-annotator'}, 'Разметчик (обязательно)'), annotator, identityHelp, identityError),
      el('div', {}, el('label', {for: 'label-version'}, 'Версия чек-листа'), versions),
      el('div', {}, el('label', {for: 'label-conversation'}, 'Диалог'), conversations)),
    el('div', {class: 'label-feedback'}, status, error, retry), queueNavigation, workspace,
    el('footer', {class: 'label-export'}, el('div', {}, progress,
      el('small', {}, priorityMode
        ? 'Скачиваются только заполненные ячейки очереди. Пропущенные ответы не добавляются. Ответы на сервер не отправляются.'
        : 'Скачивается один полностью размеченный диалог. Ответы на сервер не отправляются.')), download));

  let storage;
  try { storage = doc.defaultView.localStorage; } catch { storage = null; }
  let api;
  try { api = createBlindClient({apiBase: config.apiBase || './api', baseURL: doc.baseURI}); }
  catch (reason) { error.textContent = reason.message; error.hidden = false; return {destroy() {}}; }
  let renderedContext, renderedLoading = false, session, initialLoading = true, disposed = false;
  let priorityQueue = null, cursor = null, queueExporting = false;
  const queueDrafts = new Map();
  const fields = new Map();

  const answer = (id, value) => {
    try { session.answer(id, value); }
    catch (reason) { error.textContent = reason.message; error.hidden = false; }
  };
  const drawConversation = context => {
    fields.clear();
    const conv = context.conversation;
    const transcript = el('section', {class: 'label-transcript', 'aria-labelledby': 'label-transcript-title'},
      el('h2', {id: 'label-transcript-title'}, 'Диалог'),
      el('p', {class: 'label-meta'}, `${conv.id} · ${conv.language} · ${conv.channel === 'call' ? 'Звонок' : 'Чат'}`));
    const messages = el('ol', {class: 'label-messages'});
    for (const m of conv.messages) messages.append(el('li', {'data-speaker': m.speaker},
      el('div', {class: 'label-message-meta'}, el('strong', {}, speakerNames[m.speaker] || m.speaker),
        el('span', {}, m.ts_raw || `Реплика ${m.idx + 1}`)), el('p', {}, m.text)));
    if (!conv.messages.length) transcript.append(el('p', {}, 'В диалоге нет реплик.'));
    transcript.append(messages);
    const form = el('section', {class: 'label-answers', 'aria-labelledby': 'label-answers-title'},
      el('h2', {id: 'label-answers-title'}, 'Ваши ответы'),
      el('p', {class: 'label-meta'}, `${context.checklist.name || 'Чек-лист'} · Версия ${context.checklist.version}`));
    const selectedCriteria = priorityMode
      ? context.checklist.criteria.filter(c => c.id === cursor?.current.criterion_id) : context.checklist.criteria;
    selectedCriteria.forEach((c, index) => {
      const base = `label-c-${index}`, description = `${base}-description`, field = el('fieldset', {},
        el('legend', {}, `${c.id}. ${c.title}`), el('p', {id: description}, c.description || 'Выберите ответ по критерию.'));
      if (c.guidance?.length) field.append(el('details', {}, el('summary', {}, 'Пояснения к критерию'),
        el('ul', {}, c.guidance.map(text => el('li', {}, text)))));
      if (c.na_allowed) field.append(el('p', {class: 'label-na-rule'}, c.na_rule || 'Не применимо: только если условие критерия не относится к диалогу.'));
      const controls = [];
      if (c.answer_type === 'numeric') {
        const minimum = c.scale.length > 1 ? Math.min(...c.scale) : 0, maximum = Math.max(...c.scale);
        const input = el('input', {id: base, type: 'number', step: 'any', min: minimum, max: maximum,
          inputmode: 'decimal', 'aria-describedby': `${description} ${base}-error`});
        const validation = el('p', {id: `${base}-error`, class: 'label-error', hidden: ''});
        field.append(el('label', {for: base}, `Число от ${minimum} до ${maximum}`), input, validation);
        input.addEventListener('input', () => {
          const value = input.value === '' ? undefined : input.valueAsNumber;
          const invalid = input.validity.badInput || (value !== undefined && !allowedAnswer(c, value));
          input.setAttribute('aria-invalid', String(invalid));
          validation.hidden = !invalid;
          validation.textContent = invalid ? `Введите число от ${minimum} до ${maximum}.` : '';
          answer(c.id, invalid ? undefined : value);
        });
        controls.push({input, numeric: true});
        if (c.na_allowed) {
          const na = el('input', {type: 'checkbox', id: `${base}-na`});
          na.addEventListener('change', () => {
            input.value = ''; validation.hidden = true; input.setAttribute('aria-invalid', 'false');
            answer(c.id, na.checked ? 'na' : undefined);
          });
          field.append(el('label', {class: 'label-choice', for: `${base}-na`}, na, 'Не применимо'));
          controls.push({input: na, value: 'na'});
        }
      } else {
        const choices = c.answer_type === 'binary' ? ['yes', 'no'] : [...c.scale];
        if (c.na_allowed) choices.push('na');
        const group = el('div', {class: `label-choices ${c.answer_type === 'binary' ? 'label-binary' : ''}`});
        choices.forEach((value, j) => {
          const id = `${base}-${j}`, radio = el('input', {id, type: 'radio', name: base,
            value: JSON.stringify(value), 'aria-describedby': description});
          radio.addEventListener('change', () => { if (radio.checked) answer(c.id, value); });
          const label = el('span', {}, el('strong', {}, answerLabel(value)));
          if (c.scale_labels?.[String(value)]) label.append(el('span', {}, c.scale_labels[String(value)]));
          group.append(el('label', {for: id, class: 'label-choice'}, radio, label));
          controls.push({input: radio, value});
        });
        field.append(group);
      }
      fields.set(c.id, controls); form.append(field);
    });
    workspace.replaceChildren(transcript, form);
  };
  const render = state => {
    if (priorityMode && state.context) {
      try { validatePriorityQueue(priorityQueue, state.context.checklist); }
      catch (reason) {
        state.error = reason.message; state.context = null; state.draft = null;
      }
    }
    if (priorityMode && state.context && state.draft) {
      queueDrafts.set(draftKey(state.context), {context: state.context, draft: state.draft});
    }
    status.textContent = state.loading ? 'Загрузка исходного диалога и чек-листа…' : state.notice;
    error.textContent = state.error; error.hidden = !state.error;
    retry.hidden = !state.error || !annotator.value.trim() || initialLoading;
    workspace.setAttribute('aria-busy', String(state.loading));
    if (state.context !== renderedContext || state.loading !== renderedLoading) {
      if (state.context) drawConversation(state.context);
      else {
        fields.clear();
        workspace.replaceChildren(el('div', {class: state.loading ? 'label-skeleton' : 'label-empty'},
          el('h2', {}, state.loading ? 'Загрузка задания' : 'Начните с выбора задания'),
          el('p', {}, state.loading ? 'Подготавливаем реплики и критерии.' : 'Введите свой код, затем выберите диалог и версию чек-листа.')));
      }
      renderedContext = state.context; renderedLoading = state.loading;
    }
    const total = priorityMode ? (state.context ? 1 : 0) : state.context?.checklist.criteria.length || 0;
    const filled = priorityMode ? Number(own(state.draft?.answers || {}, cursor?.current.criterion_id)) : Object.keys(state.draft?.answers || {}).length;
    progress.textContent = total ? `Заполнено ${filled} из ${total} критериев` : 'Выберите разметчика, диалог и версию.';
    const busy = state.exporting || queueExporting;
    download.disabled = !total || (!priorityMode && filled !== total) || state.loading || busy;
    download.textContent = busy ? 'Проверка версии…' : priorityMode ? 'Скачать ответы очереди' : 'Скачать JSONL';
    annotator.disabled = busy; versions.disabled = priorityMode || initialLoading || busy;
    conversations.disabled = priorityMode || initialLoading || busy;
    if (priorityMode) {
      queueInfo.textContent = cursor ? `Задание ${cursor.index + 1} из ${priorityQueue.cells.length} · ${cursor.current.criterion_id}. Можно пропустить и вернуться.` : 'Загрузка очереди…';
      previous.disabled = !cursor || cursor.index === 0 || state.loading || busy;
      next.disabled = !cursor || cursor.index === priorityQueue.cells.length - 1 || state.loading || busy;
    }
    for (const [id, controls] of fields) {
      const value = state.draft?.answers[id]?.answer;
      for (const control of controls) {
        control.input.disabled = busy || (control.numeric && value === 'na');
        if (control.numeric) {
          if (doc.activeElement !== control.input) control.input.value = typeof value === 'number' ? String(value) : '';
        } else control.input.checked = value === control.value;
      }
    }
  };
  session = createSession({api, storage, onChange: render});
  render(session.state);
  const select = () => {
    identityError.hidden = true; annotator.setAttribute('aria-invalid', 'false');
    if (annotator.value.trim()) {
      try { annotatorId(annotator.value); }
      catch (reason) { identityError.textContent = reason.message; identityError.hidden = false; annotator.setAttribute('aria-invalid', 'true'); }
    }
    return session.select({annotator: annotator.value, convId: conversations.value, version: Number(versions.value)});
  };
  // Changing identity invalidates visible answers immediately, before blur/network completion.
  annotator.addEventListener('input', select);
  annotator.addEventListener('blur', () => {
    try { annotatorId(annotator.value); }
    catch (reason) { identityError.textContent = reason.message; identityError.hidden = false; annotator.setAttribute('aria-invalid', 'true'); }
  });
  versions.addEventListener('change', select); conversations.addEventListener('change', select);
  const moveQueue = async delta => {
    if (!cursor || queueExporting || session.state.loading) return;
    cursor.move(delta); conversations.value = cursor.current.conv_id;
    renderedContext = null; // Two adjacent tasks may share a conversation.
    await select();
    if (!disposed) workspace.focus();
  };
  previous.addEventListener('click', () => moveQueue(-1));
  next.addEventListener('click', () => moveQueue(1));
  download.addEventListener('click', async () => {
    try {
      const context = session.state.context;
      if (!context || queueExporting) return;
      let jsonl;
      if (priorityMode) {
        queueExporting = true; render(session.state);
        ({jsonl} = await exportPriorityQueue({queue: priorityQueue, api, storage, drafts: queueDrafts, annotator: context.annotator}));
      } else ({jsonl} = await session.download());
      if (disposed) return;
      const safe = value => value.replace(/[^\p{L}\p{N}_.-]+/gu, '_').slice(0, 60);
      const blob = new Blob([jsonl], {type: 'application/x-ndjson;charset=utf-8'});
      const url = URL.createObjectURL(blob);
      const a = el('a', {href: url, download: `human-${safe(context.annotator)}-${priorityMode ? 'priority' : safe(context.conversation.id)}-v${context.checklist.version}-${context.checklist.checklist_hash.slice(0, 8)}.jsonl`});
      a.hidden = true; root.append(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (reason) { session.state.error = `Файл не выгружен: ${reason.message}`; }
    finally { queueExporting = false; if (!disposed) render(session.state); }
  });
  let listsReady = false;
  const loadLists = async () => {
    initialLoading = true; versions.disabled = true; conversations.disabled = true; retry.hidden = true;
    status.textContent = 'Загрузка списка заданий…'; error.hidden = true;
    try {
      const [convs, lists] = await Promise.all([api.conversations(), api.checklists()]);
      if (disposed) return;
      conversations.replaceChildren(el('option', {value: ''}, convs.length ? 'Выберите диалог' : 'Диалогов пока нет'),
        ...convs.map(c => el('option', {value: c.id}, `${c.id} · ${c.language} · ${c.channel === 'call' ? 'Звонок' : 'Чат'}`)));
      versions.replaceChildren(el('option', {value: ''}, lists.length ? 'Выберите версию' : 'Чек-листов пока нет'),
        ...lists.map(cl => el('option', {value: cl.version}, `v${cl.version} · ${cl.name || cl.checklist_id}`)));
      if (priorityMode) {
        const response = await fetch(new URL('../data/label-priority.json', import.meta.url).href,
          {method: 'GET', cache: 'no-store', credentials: 'same-origin', headers: {Accept: 'application/json'}});
        if (!response.ok) throw new Error('Не удалось загрузить очередь. Попросите координатора проверить файл очереди.');
        priorityQueue = validatePriorityQueue(await response.json());
        if (disposed) return;
        const ids = new Set(convs.map(c => c.id));
        if (priorityQueue.cells.some(c => !ids.has(c.conv_id))) throw new Error('В очереди есть диалоги, которых нет на этом сервере. Попросите координатора загрузить DEV.');
        if (!lists.some(cl => cl.version === priorityQueue.checklist_version && cl.checklist_id === priorityQueue.checklist_id)) {
          throw new Error('На сервере нет версии чек-листа для этой очереди.');
        }
        cursor = createPriorityCursor(priorityQueue);
        versions.value = String(priorityQueue.checklist_version); conversations.value = cursor.current.conv_id;
      }
      listsReady = true;
      status.textContent = convs.length && lists.length ? 'Для каждого участника используются отдельные черновики.' : 'Добавьте диалоги и чек-лист в основном приложении, затем обновите страницу.';
    } catch (reason) { error.textContent = reason.message; error.hidden = false; retry.hidden = false; }
    finally {
      initialLoading = false; versions.disabled = priorityMode || !listsReady; conversations.disabled = priorityMode || !listsReady;
      if (priorityMode && listsReady) { render(session.state); if (annotator.value.trim()) await select(); }
    }
  };
  retry.addEventListener('click', () => listsReady ? select() : loadLists());
  await loadLists();
  return {session, destroy() { disposed = true; session.destroy(); root.replaceChildren(); }};
}
