/* Conversation card. All conversation text is rendered as text, never as HTML. */
import { answerState, dialogueAnswerState, createDialogueRetry, attemptScope, createAttemptStore, matchesAttempt, attemptResolved } from './provider-state.js';
const mounts = new WeakMap();
const NS = 'http://www.w3.org/2000/svg';
const roles = { operator: 'Оператор', client: 'Клиент', bot: 'Бот', system: 'Система' };
const labels = { yes: 'Да', no: 'Нет', partial: 'Частично', na: 'N/A' };
const numeric = value => typeof value === 'number' && Number.isFinite(value);
const format = (value, digits = 2) => numeric(value) ? value.toLocaleString('ru-RU', { maximumFractionDigits: digits }) : '—';
const clock = seconds => numeric(seconds) && seconds >= 0 ? `${Math.floor(seconds / 60).toString().padStart(2, '0')}:${Math.floor(seconds % 60).toString().padStart(2, '0')}` : '';

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}
function button(text, handler, className = '') {
  const node = element('button', `cqa-card__button ${className}`, text);
  node.type = 'button';
  node.addEventListener('click', handler);
  return node;
}
function icon(name) {
  const svg = document.createElementNS(NS, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('aria-hidden', 'true');
  svg.classList.add('cqa-card__icon');
  const p = document.createElementNS(NS, 'path');
  p.setAttribute('d', name === 'check' ? 'm5 12 4 4L19 6' : name === 'link' ? 'M10 14 14 10M8 16l-1 1a4 4 0 0 1-6-6l4-4a4 4 0 0 1 6 0M16 8l1-1a4 4 0 0 1 6 6l-4 4a4 4 0 0 1-6 0' : 'M12 2 22 12 12 22 2 12Z M12 7v6m0 3v.1');
  svg.append(p);
  return svg;
}

/** The cascade trace is present only for results produced by that profile. */
export function cascadeState(evaluation) {
  const trace = evaluation?.raw?.cascade;
  const routed = evaluation?.raw?.routing;
  if (!trace && routed?.channel === 'call' && typeof routed.model === 'string' && routed.model) {
    return { escalated: false, routed: true, label: 'Звонок сразу направлен сильной модели',
      detail: `Маршрутизация по каналу: ${routed.model}. Предварительный проход быстрой модели не выполнялся.` };
  }
  if (typeof trace?.escalated !== 'boolean') return null;
  const first = trace.first_answers?.[evaluation.criterion_id];
  const firstText = first == null ? 'нет ответа' : labels[first] || String(first);
  const flagged = Array.isArray(trace.flagged) ? trace.flagged.filter(value => typeof value === 'string') : [];
  const policy = Array.isArray(trace.policy) ? trace.policy.filter(value => typeof value === 'string') : [];
  const fallback = trace.escalated && /ответ первой модели: вторая не ответила/.test(evaluation.model || '');
  return {
    escalated: trace.escalated,
    label: fallback ? 'Сильная модель не ответила — сохранён первый ответ'
      : trace.escalated ? 'Перепроверено сильной моделью' : 'Хватило быстрой модели',
    detail: `Первая модель: ${firstText}. Причина: ${policy.length ? `правило маршрутизации ${policy.join(', ')}${flagged.length ? `; на проверку отмечены ${flagged.join(', ')}` : ''}` : flagged.length ? `на проверку отмечены ${flagged.join(', ')}`
      : trace.escalated ? 'не указана в результате' : 'критерии не отмечены на проверку'}.`,
  };
}

/** Strict source verification, including UTF-16 offsets used by DOM strings. */
export function verifyEvidence(evidence, messages) {
  const message = messages.find(m => m.id === evidence?.message_id);
  if (!message || !roles[message.speaker]) return { valid: false, reason: 'Не найден источник цитаты' };
  const quote = evidence.quote;
  const text = String(message.text ?? '');
  if (typeof quote !== 'string' || !quote.trim()) return { valid: false, reason: 'Пустая цитата' };
  let { start, end } = evidence;
  if (start == null && end == null) {
    start = text.indexOf(quote);
    if (start < 0 || text.indexOf(quote, start + 1) >= 0) return { valid: false, reason: 'Нужны точные границы цитаты' };
    end = start + quote.length;
  }
  const valid = Number.isInteger(start) && Number.isInteger(end) && start >= 0 && end > start && end <= text.length && text.slice(start, end) === quote;
  return { valid, message, start, end, quote, context: message.speaker !== 'operator', reason: valid ? '' : 'Цитата не совпадает с исходным текстом' };
}

/** Resolve source IDs through the transcript order; never alter unknown references. */
export function explanationReferences(text, messages = []) {
  const numbers = new Map(messages.map((message, index) => [message.id, index + 1]));
  return String(text).replace(/\[([^\[\]]+)\]/g, (original, id) => numbers.has(id) ? `#${numbers.get(id)}` : original);
}

export function mount(CQA = window.CQA) {
  const host = document.getElementById('cqa-card');
  if (!host || !CQA?.bus || !CQA?.api) return () => {};
  if (mounts.has(host)) return mounts.get(host);
  if (!document.querySelector('link[data-cqa-card-style]')) {
    const style = element('link');
    style.rel = 'stylesheet';
    style.href = new URL('../css/card.css', import.meta.url).href;
    style.dataset.cqaCardStyle = '';
    document.head.append(style);
  }
  CQA.state ||= {};
  host.classList.add('cqa-card');
  let recoveryScope = null;
  let storage;
  try { storage = window.localStorage; } catch { /* storage is optional */ }
  const attempts = createAttemptStore(storage);
  let current = null, generation = 0, selected = null, activePane = 'transcript', raf = 0;
  let reviewImpact, reviewImpactView, reviewImpactFailed = false, destroyed = false;
  let transcript, content, resultPanel, overlay, path, threadStart, threadEnd, rangeBracket, rangeStatus, live;
  let messageNodes = new Map(), textNodes = new Map(), criteriaNodes = new Map();
  const cleanupListeners = [];
  const motion = window.matchMedia('(prefers-reduced-motion: reduce)');
  const on = (event, handler) => { const off = CQA.bus.on(event, handler); if (typeof off === 'function') cleanupListeners.push(off); };
  const say = text => { if (live) live.textContent = text; };
  const isOperator = () => CQA.config?.role !== 'reviewer';
  const scopedContext = detail => ({ conv_id: detail?.id, version: detail?.checklist?.version, generation, attemptScope: recoveryScope });
  const notEvaluated = detail => dialogueAnswerState(detail).notEvaluated && !retries.get(scopedContext(detail))?.run;
  const context = () => scopedContext(current);
  const sameContext = value => !destroyed && current?.id === value.conv_id && current?.checklist?.version === value.version
    && JSON.stringify(value.attemptScope || null) === JSON.stringify(recoveryScope);
  const retries = createDialogueRetry({ ...CQA.api, conversation: (id, options) => CQA.api.conversation(id, { ...options, trace: true }) }, {
    onChange: job => {
      if (job.phase === 'forgotten') attempts.forget(job.context.attemptScope, job.referenceId);
      if (sameContext(job.context)) {
        if (job.phase === 'forgotten' || job.recovered && ['checking', 'paused', 'refresh_failed'].includes(job.phase)) render(current);
        else syncRetry();
      }
      CQA.bus.emit('card:retry-state', { conv_id: job.context.conv_id, version: job.context.version, phase: job.phase, run_id: job.run?.run_id || null });
    },
    onProgress: job => {
      if (!job.recovered && !job.pointerRecorded && matchesAttempt(job.run, job.context, job.run?.run_id)) {
        attempts.write(job.context.attemptScope, job.run.run_id); job.pointerRecorded = true;
      }
      if (!job.recovered) CQA.bus.emit('card:retry-progress', { conv_id: job.context.conv_id, version: job.context.version, run: job.run });
    },
    onComplete: (detail, job) => {
      if (attemptResolved(detail, job.run)) attempts.forget(job.context.attemptScope, job.run?.run_id);
      // A response for a previous visit or an older score update must not replace the card.
      if (sameContext(job.context) && job.context.generation === generation) render(detail);
      if (!job.recovered) CQA.bus.emit('card:retry-finished', { conv_id: job.context.conv_id, version: job.context.version, run: job.run,
        missing_count: dialogueAnswerState(detail).missingCount });
    },
  });

  function retryControl(primary = false) {
    const pinned = context();
    const control = button(notEvaluated(current) ? 'Оценить диалог' : 'Переоценить диалог', () => {
      if (!sameContext(pinned) || pinned.generation !== generation) return;
      const job = retries.get(pinned);
      if (['paused', 'refresh_failed'].includes(job?.phase)) void retries.resume(pinned);
      else void retries.start(pinned);
    }, `cqa-card__retry${primary ? ' cqa-card__retry--primary' : ''}`);
    control.dataset.cardRetry = '';
    control.setAttribute('aria-describedby', 'cqa-card-retry-scope');
    return control;
  }

  function syncRetry() {
    if (!current) return;
    const pinned = context(), job = retries.get(pinned), phase = job?.phase;
    const valid = Number.isInteger(pinned.version) && typeof CQA.api.createRun === 'function' && typeof CQA.api.run === 'function';
    const busy = ['checking', 'starting', 'running', 'refreshing'].includes(phase);
    const locked = busy || phase === 'unknown';
    const text = phase === 'checking' ? 'Проверяем прошлый запуск…' : phase === 'paused' ? 'Продолжить проверку прогона' : phase === 'refresh_failed' ? 'Обновить результат'
      : phase === 'starting' ? 'Запускаем оценку…' : phase === 'running' ? 'Оценка выполняется…'
        : phase === 'refreshing' ? 'Обновляем результат…' : phase === 'unknown' ? 'Статус запуска неизвестен' : notEvaluated(current) ? 'Оценить диалог' : 'Переоценить диалог';
    host.querySelectorAll('[data-card-retry]').forEach(node => { node.disabled = !valid || locked; node.textContent = text; node.setAttribute('aria-busy', String(busy)); });
    const status = host.querySelector('.cqa-card__retry-status');
    if (!status) return;
    const missing = dialogueAnswerState(current).missingCount;
    const messages = {
      checking: 'В этом браузере сохранён номер запуска. Проверяем его состояние на сервере.',
      forgotten: 'Сохранённый в этом браузере запуск не найден или относится к другому контексту. Его результат не применён.',
      starting: 'Отправляем запрос на новый расчёт.', running: dialogueAnswerState(current).notEvaluated ? 'Диалог оценивается по всем критериям.' : 'Повторный расчёт выполняется. Показаны предыдущие результаты.',
      refreshing: 'Прогон завершён. Получаем результаты закреплённой версии.',
      paused: 'Не удалось проверить состояние прогона. Продолжите проверку — новый расчёт не запускается.',
      refresh_failed: 'Прогон завершён, но результат не удалось загрузить. Повторите загрузку результата без нового расчёта.',
      rejected: 'Сервер отклонил запуск. Проверьте настройки и повторите. Причина сбоя провайдера не подтверждена.',
      unknown: 'Сервер не подтвердил запуск. Он мог начаться: проверьте «Табло» перед новым расчётом.',
      finished: missing ? `Прогон завершён. Критериев без ответа: ${missing}. Итог не рассчитан.`
        : dialogueAnswerState(current).incomplete ? 'Прогон завершён, но результат остаётся неполным.'
          : ['failed', 'error', 'incomplete', 'cancelled'].includes(job?.run?.status) && !attemptResolved(current, job.run)
            ? 'Сохранённый запуск завершился ошибкой. Обновление показанных оценок после этой попытки не подтверждено.' : 'Результат обновлён.',
    };
    status.textContent = !valid ? 'Повторный запуск недоступен: нужна версия чек-листа и подключённый API прогонов.'
      : phase === 'finished' && job.context.generation !== generation && dialogueAnswerState(current).incomplete ? 'Прогон завершён для предыдущего состояния карточки. Откройте диалог заново, чтобы получить его результат.' : messages[phase] || '';
    status.hidden = !status.textContent;
    status.dataset.busy = String(busy);
  }

  function showReviewImpact(detail = current) {
    if (!reviewImpact || !detail) return;
    reviewImpact.replaceChildren();
    if (dialogueAnswerState(detail).incomplete) {
      reviewImpact.removeAttribute('aria-busy');
      reviewImpact.append(element('p', 'cqa-card__empty-note', 'Приоритеты проверки по влиянию на итог появятся после получения всех ответов.'));
      return;
    }
    if (reviewImpactView) {
      reviewImpact.removeAttribute('aria-busy');
      reviewImpact.append(reviewImpactView(detail, { onFocus: criterionId => {
        // Existing evidence and appeal integrations continue to own their respective actions.
        // Keep the criterion visible on narrow cards so its evidence and appeal control are reachable.
        CQA.bus.emit('criterion:focus', { conv_id: current.id, criterion_id: criterionId, source: 'review-impact' });
      } }));
    } else {
      reviewImpact.setAttribute('aria-busy', String(!reviewImpactFailed));
      reviewImpact.append(element('p', 'cqa-review-impact-loading', reviewImpactFailed
        ? 'Приоритеты недоступны. Обновите карточку, чтобы повторить загрузку.' : 'Подготавливаем приоритеты проверки…'));
    }
  }
  if (!document.querySelector('link[data-cqa-review-impact-style]')) {
    const style = element('link'); style.rel = 'stylesheet';
    style.href = new URL('../css/review-impact.css', import.meta.url).href;
    style.dataset.cqaReviewImpactStyle = ''; document.head.append(style);
  }
  // Lazy loading preserves standalone imports of verifyEvidence (including data-URL test harnesses).
  import('./review-impact.js').then(module => {
    if (destroyed) return;
    reviewImpactView = module.createReviewImpact; showReviewImpact();
  }).catch(() => { if (!destroyed) { reviewImpactFailed = true; showReviewImpact(); } });

  function empty(title, description, retry) {
    host.replaceChildren();
    const box = element('div', 'cqa-card__empty');
    box.append(element('h2', '', title), element('p', '', description));
    if (retry) box.append(button('Повторить', retry));
    host.append(box);
    selected = null;
  }

  function setPane(pane) {
    activePane = pane;
    host.dataset.pane = pane;
    host.querySelectorAll('[data-card-pane]').forEach(node => {
      const active = node.dataset.cardPane === pane;
      node.setAttribute('aria-selected', String(active));
      node.tabIndex = active ? 0 : -1;
    });
    drawSoon();
  }

  function updateRole() {
    host.querySelectorAll('[data-card-appeal]').forEach(node => { node.hidden = !isOperator(); });
  }

  function drawSoon() {
    if (!raf) raf = requestAnimationFrame(() => { raf = 0; drawConnection(); });
  }

  function drawConnection() {
    if (!selected || !current || !overlay?.isConnected) return;
    threadStart.setAttribute('visibility', 'hidden'); threadEnd.setAttribute('visibility', 'hidden');
    const target = messageNodes.get(selected.messageId);
    const source = criteriaNodes.get(selected.criterionId);
    // A checked range belongs to the transcript even when the result pane is
    // hidden by the narrow-card layout. Only the connecting thread needs both.
    rangeBracket.hidden = true;
    if (target && transcript?.clientWidth && selected.range) {
      const end = messageNodes.get(selected.range[1]);
      if (end) {
        const base = content.getBoundingClientRect();
        const startRect = target.getBoundingClientRect(), endRect = end.getBoundingClientRect();
        rangeBracket.hidden = false;
        rangeBracket.style.top = `${startRect.top - base.top + 4}px`;
        rangeBracket.style.height = `${Math.max(20, endRect.bottom - startRect.top - 8)}px`;
      }
    }
    if (!target || !source || !transcript?.clientWidth || !resultPanel?.clientWidth) { path.setAttribute('d', ''); return; }
    const rootRect = host.getBoundingClientRect();
    const t = target.getBoundingClientRect(), s = source.getBoundingClientRect();
    const tr = transcript.getBoundingClientRect(), rr = resultPanel.getBoundingClientRect();
    const targetVisible = t.bottom > tr.top && t.top < tr.bottom;
    const sourceVisible = s.bottom > rr.top && s.top < rr.bottom;
    const sideBySide = rr.left >= tr.right - 2;
    overlay.setAttribute('viewBox', `0 0 ${rootRect.width} ${rootRect.height}`);
    if (targetVisible && sourceVisible && sideBySide) {
      const anchor = source.querySelector('.cqa-card__criterion-number')?.getBoundingClientRect() || s;
      const x1 = anchor.left + anchor.width / 2 - rootRect.left, x2 = t.right - rootRect.left;
      const y1 = Math.max(rr.top + 8, Math.min(rr.bottom - 8, anchor.top + anchor.height / 2)) - rootRect.top;
      const y2 = Math.max(tr.top + 8, Math.min(tr.bottom - 8, t.top + Math.min(t.height / 2, 35))) - rootRect.top;
      const middle = (x1 + x2) / 2;
      path.setAttribute('d', `M ${x1} ${y1} C ${middle} ${y1}, ${middle} ${y2}, ${x2} ${y2}`);
      for (const [dot, x, y] of [[threadStart, x1, y1], [threadEnd, x2, y2]]) { dot.setAttribute('visibility', 'visible'); dot.setAttribute('cx', x); dot.setAttribute('cy', y); }
    } else path.setAttribute('d', '');
  }

  function highlight(evidences) {
    for (const message of current.messages) {
      const node = textNodes.get(message.id);
      if (!node) continue;
      const text = String(message.text ?? '');
      const slices = evidences.filter(e => e.valid && e.message?.id === message.id).sort((a, b) => a.start - b.start);
      node.replaceChildren();
      let from = 0;
      for (const item of slices) {
        if (item.start < from) continue;
        node.append(document.createTextNode(text.slice(from, item.start)), element('mark', 'cqa-card__quote-mark', text.slice(item.start, item.end)));
        from = item.end;
      }
      node.append(document.createTextNode(text.slice(from)));
    }
  }

  function focusCriterion(criterionId, specificMessageId, play = false, rangeOnly = false, preserveViewport = false) {
    const evaluation = current?.evals?.find(e => e.criterion_id === criterionId);
    if (!evaluation) return;
    const evidences = (evaluation.evidence || []).map(e => verifyEvidence(e, current.messages));
    const valid = evidences.filter(e => e.valid);
    const range = evaluation.checked_range;
    const startIndex = Array.isArray(range) ? current.messages.findIndex(m => m.id === range[0]) : -1;
    const endIndex = Array.isArray(range) ? current.messages.findIndex(m => m.id === range[1]) : -1;
    const validRange = startIndex >= 0 && endIndex >= startIndex && current.messages.slice(startIndex, endIndex + 1).some(m => m.speaker === 'operator');
    const messageId = rangeOnly ? (validRange ? range[0] : null) : specificMessageId || valid[0]?.message.id || (validRange ? range[0] : null);
    host.querySelectorAll('.is-evidence-focus, .is-criterion-focus, .is-checked-range').forEach(node => node.classList.remove('is-evidence-focus', 'is-criterion-focus', 'is-checked-range'));
    criteriaNodes.get(criterionId)?.classList.add('is-criterion-focus');
    highlight(valid);
    rangeStatus.hidden = true;
    rangeBracket.hidden = true;
    if (!messageId || !messageNodes.has(messageId)) { selected = null; path.setAttribute('d', ''); say('У критерия нет проверенной цитаты или корректного участка. Нужна ручная проверка.'); return; }
    selected = { criterionId, messageId, range: (rangeOnly || !valid.length) && validRange ? range : null };
    if (selected.range) {
      for (let i = startIndex; i <= endIndex; i++) {
        const node = messageNodes.get(current.messages[i].id);
        node?.classList.add('is-checked-range');
        if (node?.tagName === 'DETAILS') node.open = true;
      }
      rangeStatus.textContent = `Проверено: реплики ${startIndex + 1}–${endIndex + 1}. Действие не найдено в указанном участке.`;
      rangeStatus.hidden = false;
    }
    const target = messageNodes.get(messageId);
    if (target.tagName === 'DETAILS') target.open = true;
    target.classList.add('is-evidence-focus');
    if (!preserveViewport) {
      setPane('transcript');
      target.scrollIntoView({ behavior: motion.matches ? 'instant' : 'smooth', block: 'center', inline: 'nearest' });
    }
    say(selected.range ? rangeStatus.textContent : `Доказательство: реплика ${current.messages.findIndex(m => m.id === messageId) + 1}, ${roles[current.messages.find(m => m.id === messageId)?.speaker] || 'участник'}.`);
    drawSoon();
    if (play && current.channel === 'call') CQA.bus.emit('audio:seek', { conv_id: current.id, message_id: messageId });
  }

  function messageRow(message, index) {
    const contextual = message.speaker === 'bot' || message.speaker === 'system';
    const row = element(contextual ? 'details' : 'article', `cqa-card__message cqa-card__message--${roles[message.speaker] ? message.speaker : 'unknown'}`);
    row.dataset.messageId = message.id;
    row.setAttribute('aria-label', `Реплика ${index + 1}. ${roles[message.speaker] || 'Участник'}`);
    const header = element(contextual ? 'summary' : 'div', 'cqa-card__message-head');
    header.append(element('strong', '', contextual ? `Контекст: ${roles[message.speaker].toLowerCase()}` : roles[message.speaker] || 'Участник'));
    const time = clock(message.rec_sec ?? message.t_sec) || String(message.ts_raw || '');
    const timestamp = element('span', 'cqa-card__time', time);
    timestamp.title = String(message.ts_raw || time);
    header.append(timestamp, element('span', 'cqa-card__message-number', `#${index + 1}`));
    const text = element('p', 'cqa-card__message-text', message.text || '');
    row.append(header, text);
    if (contextual) row.addEventListener('toggle', drawSoon);
    if (current.channel === 'call' && !contextual && numeric(message.rec_sec)) {
      row.append(button(`Слушать с ${clock(message.rec_sec)}`, () => CQA.bus.emit('audio:seek', { conv_id: current.id, message_id: message.id }), 'cqa-card__listen'));
    }
    messageNodes.set(message.id, row);
    textNodes.set(message.id, text);
    return row;
  }

  function receipt(detail) {
    const box = element('details', 'cqa-card__receipt');
    const evals = detail.evals || [];
    const measured = evals.length > 0 && evals.every(e => numeric(e.cost_kzt));
    const cost = measured ? evals.reduce((sum, e) => sum + e.cost_kzt, 0) : null;
    box.append(element('summary', '', `Чек оценки · ${cost === null ? 'стоимость не измерена' : `${format(cost, 3)} ₸`}`));
    if (detail.is_mock) box.append(element('p', 'cqa-card__mock-note', 'Демонстрационные значения. Не измерение работы модели.'));
    const latency = numeric(detail.latency_ms) ? detail.latency_ms : evals.length && evals.every(e => numeric(e.latency_ms)) ? evals.reduce((sum, e) => sum + e.latency_ms, 0) : null;
    const cached = numeric(detail.from_cache) ? detail.from_cache : evals.length && evals.every(e => typeof e.from_cache === 'boolean') ? evals.filter(e => e.from_cache).length : null;
    const entries = [
      ['Модель', [...new Set(evals.map(e => e.model).filter(Boolean))].join(', ') || 'Не указана'],
      ['Входные токены', evals.every(e => numeric(e.tokens_in)) && evals.length ? format(evals.reduce((s, e) => s + e.tokens_in, 0), 0) : 'Не измерено'],
      ['Выходные токены', evals.every(e => numeric(e.tokens_out)) && evals.length ? format(evals.reduce((s, e) => s + e.tokens_out, 0), 0) : 'Не измерено'],
      ['Время оценивания', numeric(latency) ? `${format(latency / 1000, 2)} с` : 'Не измерено'],
      ['Из кэша', numeric(cached) ? String(cached) : 'Нет данных'],
      ['Стоимость', cost === null ? 'Не измерено' : `${format(cost, 3)} ₸`],
    ];
    const dl = element('dl');
    for (const [key, value] of entries) dl.append(element('dt', '', key), element('dd', '', value));
    box.append(dl);
    return box;
  }

  function criterionRow(criterion, evaluation, line, index) {
    const id = criterion.id;
    const state = answerState(criterion, evaluation, line);
    const answer = state.answer;
    const changed = evaluation && line?.answer != null && line.answer !== evaluation.answer;
    const critical = criterion.critical === true;
    const hit = critical && answer === (criterion.critical_when ?? 'yes');
    const scaleMax = Array.isArray(criterion.scale) && criterion.scale.length ? Math.max(...criterion.scale.filter(numeric)) : null;
    const unassessed = notEvaluated(current);
    const tone = state.missing ? (unassessed ? 'pending' : 'missing') : critical ? (hit ? 'critical' : 'clear') : answer === 'na' ? 'na' : answer === 'no' || (numeric(answer) && answer <= 0) ? 'no' : answer === 'partial' || (numeric(answer) && numeric(scaleMax) && answer < scaleMax) ? 'partial' : 'yes';
    const answerText = state.missing ? (unassessed ? 'Ещё не оценён' : 'Нет ответа') : critical ? (hit ? 'Нарушение выявлено' : 'Нарушений нет') : labels[answer] || `${format(answer)}${numeric(scaleMax) ? ` из ${format(scaleMax)}` : ''}`;
    const row = element('article', `cqa-card__criterion cqa-card__criterion--${tone}`);
    row.dataset.criterionId = id;
    criteriaNodes.set(id, row);
    const heading = button('', () => CQA.bus.emit('criterion:focus', { conv_id: current.id, criterion_id: id }), 'cqa-card__criterion-heading');
    heading.append(element('span', 'cqa-card__criterion-number', index + 1), element('span', 'cqa-card__criterion-title', criterion.title || id), element('span', `cqa-card__answer cqa-card__answer--${tone}`, answerText));
    row.append(heading);
    if (state.missing && unassessed) {
      row.dataset.answerState = 'pending';
      row.append(element('p', 'cqa-card__empty-note', 'Ответ и доказательства появятся после оценки диалога.'));
      return row;
    }
    const cascade = cascadeState(evaluation);
    if (cascade) {
      const trace = element('details', 'cqa-card__cascade');
      trace.dataset.escalated = String(cascade.escalated);
      const badge = element('summary', '', cascade.label); badge.title = cascade.detail;
      trace.append(badge, element('p', '', cascade.detail)); row.append(trace);
    }
    if (state.missing) {
      row.dataset.answerState = 'missing';
      row.dataset.providerFailure = state.failure?.kind || 'unconfirmed';
      row.append(element('p', 'cqa-card__missing-title', state.failure ? 'Модель не ответила: ошибка провайдера'
        : state.invalid ? 'Получен недопустимый ответ' : 'Ответ модели не получен'));
      row.append(element('p', 'cqa-card__missing-description', state.failure?.message
        || (state.invalid ? 'Ответ не соответствует шкале критерия. Нужен повторный расчёт.' : 'Причина не указана. Оценка могла ещё не запускаться или завершиться без ответа.')));
      row.append(element('p', 'cqa-card__missing-description', 'Это пропуск оценки, а не N/A. Критерий нельзя считать выполненным.'), retryControl());
      return row;
    }
    if (changed) row.append(element('p', 'cqa-card__review-change', `Исходная оценка ИИ: ${labels[evaluation.answer] || format(evaluation.answer)}. После решения: ${labels[answer] || format(answer)}.`));
    if (!critical) {
      const meta = element('div', 'cqa-card__criterion-meta');
      meta.append(element('strong', '', answer === 'na' ? 'Исключён из расчёта' : `${format(line?.points)} / ${format(line?.max_points ?? criterion.max_points)} балла`), element('span', '', `Вес ${format(line?.weight ?? criterion.weight)} → ${format(line?.weight_effective)}%`));
      row.append(meta);
      if (numeric(line?.contribution) && answer !== 'na' && !dialogueAnswerState(current).incomplete) row.append(element('p', 'cqa-card__contribution', `Вклад в итог: ${format(line.contribution, 2)} из 100`));
    }
    if (numeric(answer) && criterion.scale_labels?.[String(answer)]) row.append(element('p', 'cqa-card__scale-label', criterion.scale_labels[String(answer)]));
    if (critical && answer != null) { const flag = element('p', hit ? 'cqa-card__critical' : 'cqa-card__flag-clear', answerText); flag.prepend(icon(hit ? 'alert' : 'check')); row.append(flag); }
    if (evaluation?.explanation) row.append(element('p', 'cqa-card__explanation', explanationReferences(evaluation.explanation, current.messages)));
    if ((evaluation?.critical_hit || (critical && evaluation?.answer === (criterion.critical_when ?? 'yes'))) && (!critical || changed)) {
      const flag = element('p', changed ? 'cqa-card__review-change' : 'cqa-card__critical', changed ? 'Грубая ошибка в исходной оценке ИИ' : 'Грубая ошибка');
      flag.prepend(icon('alert')); row.append(flag);
    }
    if (answer === 'na') row.append(element('p', 'cqa-card__na-reason', evaluation?.na_reason ? `Не применимо: ${evaluation.na_reason}` : 'Причина N/A не указана. Нужна ручная проверка.'));
    const evidences = (evaluation?.evidence || []).map(e => verifyEvidence(e, current.messages));
    for (const evidence of evidences) {
      const box = element('div', `cqa-card__evidence ${evidence.valid ? '' : 'cqa-card__evidence--invalid'}`);
      box.append(element('blockquote', '', evidence.quote || 'Цитата отсутствует'));
      if (evidence.valid) {
        const proof = element('span', 'cqa-card__verified', `${roles[evidence.message.speaker]}: цитата найдена в оригинале${evidence.context ? ' · контекст' : ''}`);
        proof.prepend(icon('check'));
        box.append(proof, button(current.channel === 'call' ? `Реплика и звук ${clock(evidence.message.rec_sec)}` : 'Показать реплику', () => focusCriterion(id, evidence.message.id, true), 'cqa-card__proof-button'));
      } else box.append(element('span', 'cqa-card__invalid', `${evidence.reason}. Проверить вручную.`));
      row.append(box);
    }
    if (evaluation?.checked_range && ['no', 'partial', 'clear'].includes(tone)) row.append(button('Показать проверенный участок', () => focusCriterion(id, undefined, false, true), 'cqa-card__range-button'));
    if (evaluation && answer !== 'na' && !evidences.some(e => e.valid) && !evaluation.checked_range) row.append(element('p', 'cqa-card__invalid', 'Проверенное доказательство не приложено. Нужна ручная проверка.'));
    if (evaluation?.suggestion) {
      const suggestion = element('details', 'cqa-card__suggestion');
      suggestion.append(element('summary', '', 'Как можно ответить'), element('p', '', evaluation.suggestion), element('small', '', 'Рекомендация. Не часть исходного диалога.'));
      row.append(suggestion);
    }
    const footer = element('div', 'cqa-card__criterion-footer');
    const confidenceLabel = evaluation?.mode === 'accurate' ? 'Совпадение оценок' : 'Уверенность модели';
    footer.append(element('span', `cqa-card__confidence ${evaluation?.needs_review ? 'cqa-card__confidence--low' : ''}`, numeric(evaluation?.confidence) ? `${confidenceLabel} ${format(evaluation.confidence * 100, 0)}%${evaluation.needs_review ? ' · проверить' : ''}` : 'Уверенность не указана'));
    if (evaluation) {
      const appeal = button('Оспорить', () => CQA.bus.emit('appeal:start', { conv_id: current.id, criterion_id: id }), 'cqa-card__appeal');
      appeal.dataset.cardAppeal = '';
      appeal.hidden = !isOperator();
      appeal.setAttribute('aria-label', `Оспорить: ${criterion.title || id}`);
      footer.append(appeal);
    }
    row.append(footer);
    return row;
  }

  function render(detail) {
    const restoreRetryFocus = current?.id === detail.id && document.activeElement?.dataset?.cardRetry !== undefined;
    const previous = current?.id === detail.id ? { selected, pane: activePane, transcript: transcript?.scrollTop || 0, results: resultPanel?.scrollTop || 0, openMessages: [...messageNodes].filter(([, node]) => node.open).map(([id]) => id) } : null;
    current = { ...detail, messages: detail.messages || [], evals: detail.evals || [] };
    selected = null;
    messageNodes = new Map(); textNodes = new Map(); criteriaNodes = new Map();
    host.replaceChildren();
    host.removeAttribute('aria-busy');
    const header = element('header', 'cqa-card__header');
    const identity = element('div');
    identity.append(element('h2', 'cqa-card__title', detail.id), element('p', 'cqa-card__subtitle', `${detail.channel === 'call' ? 'Звонок' : 'Чат'} · ${detail.language || 'Язык не указан'} · ${current.messages.length} реплик`));
    header.append(identity);
    if (detail.is_mock) header.append(element('span', 'cqa-card__demo-badge', 'Демо-оценка'));
    host.append(header);
    const tabs = element('div', 'cqa-card__mobile-tabs');
    tabs.setAttribute('role', 'tablist'); tabs.setAttribute('aria-label', 'Панель карточки');
    for (const [pane, title] of [['transcript', 'Транскрипт'], ['result', 'Результат']]) {
      const tab = button(title, () => setPane(pane));
      tab.dataset.cardPane = pane; tab.setAttribute('role', 'tab'); tab.setAttribute('aria-controls', `cqa-card-${pane}`);
      tab.addEventListener('keydown', event => {
        if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) {
          event.preventDefault();
          const next = event.key === 'Home' ? 'transcript' : event.key === 'End' ? 'result' : activePane === 'transcript' ? 'result' : 'transcript';
          setPane(next);
          tabs.querySelector(`[data-card-pane="${next}"]`)?.focus();
        }
      });
      tabs.append(tab);
    }
    host.append(tabs);
    const body = element('div', 'cqa-card__body');
    const left = element('section', 'cqa-card__transcript-panel'); left.id = 'cqa-card-transcript'; left.setAttribute('aria-label', 'Транскрипт диалога');
    const transcriptTitle = element('div', 'cqa-card__panel-heading');
    transcriptTitle.append(element('h3', '', 'Разговор'), element('span', '', 'Оригинальный текст'));
    left.append(transcriptTitle);
    if (numeric(detail.meta?.wait_sec)) left.append(element('p', 'cqa-card__handoff', `Передача оператору · ожидание ${format(detail.meta.wait_sec, 0)} с`));
    const audio = element('div', 'cqa-card__audio-slot'); audio.id = 'cqa-audio';
    if (detail.channel === 'call') audio.append(element('p', 'cqa-card__audio-fallback', 'Запись: время реплик доступно в транскрипте.'));
    left.append(audio);
    rangeStatus = element('p', 'cqa-card__range-status'); rangeStatus.hidden = true; left.append(rangeStatus);
    transcript = element('div', 'cqa-card__transcript-scroll'); transcript.tabIndex = 0; transcript.setAttribute('aria-label', 'Реплики разговора');
    content = element('div', 'cqa-card__transcript-content');
    if (!current.messages.length) content.append(element('p', 'cqa-card__empty-note', 'В этом диалоге нет реплик.'));
    current.messages.forEach((message, index) => content.append(messageRow(message, index)));
    rangeBracket = element('div', 'cqa-card__range-bracket'); rangeBracket.hidden = true; rangeBracket.setAttribute('aria-hidden', 'true'); content.append(rangeBracket);
    transcript.append(content); left.append(transcript);
    resultPanel = element('section', 'cqa-card__results'); resultPanel.id = 'cqa-card-result'; resultPanel.tabIndex = 0; resultPanel.setAttribute('aria-label', 'Результат оценки');
    const score = detail.score || {};
    const answers = dialogueAnswerState(detail);
    const unassessed = notEvaluated(detail);
    const knownJob = retries.get(scopedContext(detail)), knownRun = knownJob?.run;
    const recovering = knownJob?.recovered && ['checking', 'running', 'paused', 'refreshing', 'refresh_failed'].includes(knownJob.phase);
    const unresolvedFailure = ['failed', 'error', 'incomplete', 'cancelled'].includes(knownRun?.status) && !attemptResolved(detail, knownRun);
    const scoreBox = element('div', 'cqa-card__score-summary');
    const scoreHeading = element('div', 'cqa-card__score-heading');
    scoreHeading.append(element('h3', '', 'Итоговая оценка'), element('span', 'cqa-card__version', `Чек-лист v${detail.checklist?.version ?? score.checklist_version ?? '—'}${detail.checklist?.active === false ? ' · не активна' : ''}`));
    const digits = Number.isInteger(detail.checklist?.rounding?.total) ? Math.max(0, Math.min(6, detail.checklist.rounding.total)) : 2;
    const total = element('div', `cqa-card__total${answers.incomplete ? ' cqa-card__total--incomplete' : ''}`);
    if (answers.incomplete) total.append(element('strong', '', 'Не рассчитан'));
    else total.append(element('strong', '', format(score.total, digits)), element('span', '', '/ 100'));
    scoreBox.append(scoreHeading, total);
    if (answers.incomplete || unresolvedFailure || recovering) {
      const checkingFull = !answers.incomplete && !unresolvedFailure && recovering;
      const notice = element('div', unassessed || checkingFull ? 'cqa-card__pending-summary' : 'cqa-card__missing-summary');
      notice.setAttribute('role', 'status');
      notice.append(element('p', unassessed || checkingFull ? 'cqa-card__pending-title' : 'cqa-card__missing-title', checkingFull ? 'Проверяем сохранённый запуск' : unassessed ? 'Ещё не оценён' : !answers.incomplete && unresolvedFailure ? 'Сохранённый запуск завершился ошибкой' : `Критериев без ответа: ${answers.missingCount}${answers.missingIds.length ? ` · ${answers.missingIds.join(', ')}` : ''}`));
      notice.append(element('p', 'cqa-card__missing-description', checkingFull ? 'Показаны сохранённые оценки. Состояние известного этому браузеру запуска ещё проверяется.' : unassessed ? 'Запустите оценку, чтобы получить ответы, доказательства и итог.' : !answers.incomplete && unresolvedFailure ? 'Показаны сохранённые оценки. Их обновление после этой попытки не подтверждено.' : answers.missingCount
        ? 'Итог не рассчитан: получены не все ответы.' : 'Оценка неполная. Состав пропущенных критериев не получен.'));
      const scope = element('p', 'cqa-card__retry-scope', checkingFull ? 'Продолжение проверки запрашивает состояние уже известного запуска.' : unassessed ? `Будут оценены все критерии только этого диалога по версии v${detail.checklist?.version ?? '—'}.` : `Повтор запустит все критерии этого диалога по версии v${detail.checklist?.version ?? '—'}, включая уже оценённые. Ответы будут получены заново с текущими настройками модели. Повторить только один критерий API пока не позволяет.`);
      scope.id = 'cqa-card-retry-scope';
      notice.append(scope, retryControl(true)); scoreBox.append(notice);
    }
    const retryStatus = element('p', 'cqa-card__retry-status');
    retryStatus.setAttribute('role', 'status'); retryStatus.setAttribute('aria-live', 'polite');
    retryStatus.tabIndex = -1;
    scoreBox.append(retryStatus);
    if (score.critical) { const banner = element('div', 'cqa-card__critical', 'Отмечена грубая ошибка'); banner.prepend(icon('alert')); scoreBox.append(banner); }
    if (!answers.incomplete && score.total == null) scoreBox.append(element('p', 'cqa-card__empty-note', score.status === 'no_applicable' ? 'Итог не рассчитывается: нет применимых критериев.' : 'Итог ещё не рассчитан.'));
    const sections = element('dl', 'cqa-card__sections');
    for (const [name, value] of Object.entries(score.sections || {})) sections.append(element('dt', '', name), element('dd', '', value == null ? '—' : numeric(value) ? `${format(value, digits)} из 100` : `${format(value?.points ?? value?.score, digits)} / ${format(value?.max_points ?? value?.max, digits)}`));
    // Section results are part of the result (organizer brief), so they sit under the total, not inside «Расчёт и веса».
    if (!answers.incomplete && Object.keys(score.sections || {}).length) scoreBox.append(sections);
    const evidenceChecks = current.evals.flatMap(e => (e.evidence || []).map(item => verifyEvidence(item, current.messages)));
    if (evidenceChecks.length || !answers.incomplete) scoreBox.append(element('p', 'cqa-card__verified-summary', `Цитаты сверены ${evidenceChecks.filter(e => e.valid).length}/${evidenceChecks.length}`));
    const formula = element('details', 'cqa-card__formula');
    formula.append(element('summary', '', 'Как посчитан итог'));
    const steps = element('ol');
    for (const step of Array.isArray(score.formula_steps) ? score.formula_steps : []) steps.append(element('li', '', step));
    if (!steps.children.length) steps.append(element('li', '', 'Подробный расчёт не получен от оценщика.'));
    formula.append(steps);
    const calculation = element('details', 'cqa-card__calculation');
    calculation.append(element('summary', '', answers.incomplete ? 'Данные попытки оценки' : 'Расчёт и веса'));
    if (!answers.incomplete) {
      calculation.append(formula);
      const weights = element('div', 'cqa-card__weights-slot'); weights.id = 'cqa-weights'; calculation.append(weights);
    }
    calculation.append(receipt(current));
    const history = element('details', 'cqa-card__calculation cqa-card__history');
    const reviewHistory = element('div'); reviewHistory.id = 'cqa-review-slot';
    history.append(element('summary', '', `История решений (${(detail.decisions || []).length})`), reviewHistory);
    scoreBox.append(calculation, history); resultPanel.append(scoreBox);
    reviewImpact = element('div', 'cqa-card__review-impact-slot');
    reviewImpact.id = 'cqa-review-impact'; resultPanel.append(reviewImpact); showReviewImpact();
    let criteria = detail.checklist?.criteria || [];
    if (!criteria.length) criteria = current.evals.map(e => ({ id: e.criterion_id, title: e.title || e.criterion_id }));
    if (!criteria.length) resultPanel.append(element('p', 'cqa-card__empty-note', 'Диалог ещё не оценён. Запустите оценку, чтобы увидеть критерии и доказательства.'));
    criteria.forEach((criterion, index) => resultPanel.append(criterionRow(criterion, current.evals.find(e => e.criterion_id === criterion.id), score.lines?.find(l => l.criterion_id === criterion.id), index)));
    body.append(left, resultPanel); host.append(body);
    overlay = document.createElementNS(NS, 'svg'); overlay.classList.add('cqa-card__thread'); overlay.setAttribute('aria-hidden', 'true');
    path = document.createElementNS(NS, 'path');
    threadStart = document.createElementNS(NS, 'circle'); threadEnd = document.createElementNS(NS, 'circle');
    for (const dot of [threadStart, threadEnd]) { dot.setAttribute('r', '4'); dot.setAttribute('visibility', 'hidden'); }
    overlay.append(path, threadStart, threadEnd); host.append(overlay);
    live = element('p', 'cqa-card__live'); live.setAttribute('role', 'status'); live.setAttribute('aria-live', 'polite'); host.append(live);
    transcript.addEventListener('scroll', drawSoon, { passive: true }); resultPanel.addEventListener('scroll', drawSoon, { passive: true });
    setPane(previous?.pane || activePane);
    if (previous) {
      for (const id of previous.openMessages) if (messageNodes.has(id)) messageNodes.get(id).open = true;
      if (previous.selected) focusCriterion(previous.selected.criterionId, previous.selected.messageId, false, Boolean(previous.selected.range), true);
      transcript.scrollTop = previous.transcript; resultPanel.scrollTop = previous.results;
    }
    CQA.state.conversation = current; CQA.state.currentConversation = current;
    syncRetry();
    if (restoreRetryFocus && !retryStatus.hidden) retryStatus.focus({ preventScroll: true });
    CQA.bus.emit('card:provider-state', { conv_id: detail.id, version: detail.checklist?.version, incomplete: answers.incomplete,
      missing_count: answers.missingCount, missing_ids: answers.missingIds, provider_failure_count: answers.providerFailureCount,
      failures: answers.entries.filter(entry => entry.failure).map(entry => ({ criterion_id: entry.criterion_id, kind: entry.failure.kind })) });
    CQA.bus.emit('conv:loaded', current);
  }

  async function open(request) {
    const id = typeof request === 'string' ? request : request?.id;
    if (typeof id !== 'string' || !id) return;
    const token = ++generation;
    CQA.state.activeConversationId = id;
    current = null; recoveryScope = null;
    empty('Открываем диалог', 'Загружаем разговор и результаты оценки.');
    host.setAttribute('aria-busy', 'true');
    try {
      const rowVersion = request?.version ?? CQA.state.listScoreVersions?.[id];
      const detail = await CQA.api.conversation(id, { ...(rowVersion != null ? { version: rowVersion } : {}), trace: true });
      if (destroyed || token !== generation) return;
      if (!detail || detail.id !== id || !Array.isArray(detail.messages)) throw new Error('Invalid conversation response');
      // Read-only recovery is scoped to this browser, API endpoint and exact input content.
      // Failure to obtain a safe scope leaves the ordinary card path available.
      if (CQA.config?.mock === false && storage && typeof CQA.api.config === 'function') {
        try {
          const settings = await CQA.api.config();
          const scope = await attemptScope(detail, { ...settings, prompt_version: CQA.state.health?.prompt_version }, CQA.config.apiBase, document.baseURI);
          if (destroyed || token !== generation) return;
          recoveryScope = scope;
        } catch { /* no browser-local recovery without confirmed settings */ }
      }
      if (destroyed || token !== generation) return;
      render(detail);
      const remembered = attempts.read(recoveryScope);
      if (remembered) void retries.recover(context(), remembered);
    } catch {
      if (token !== generation) return;
      current = null;
      host.removeAttribute('aria-busy');
      empty('Диалог не открылся', 'Не удалось получить разговор. Повторите загрузку.', () => open(request));
    }
  }

  on('conv:open', open);
  on('role:changed', role => { if (typeof role === 'string') CQA.config.role = role; updateRole(); });
  on('criterion:focus', value => {
    if (!current || value?.conv_id !== current.id) return;
    if (value.source !== 'review-impact') { focusCriterion(value.criterion_id); return; }
    focusCriterion(value.criterion_id, undefined, false, false, true);
    setPane('result');
    const row = criteriaNodes.get(value.criterion_id);
    row?.scrollIntoView({ behavior: motion.matches ? 'instant' : 'smooth', block: 'start' });
    row?.querySelector('.cqa-card__criterion-heading')?.focus({ preventScroll: true });
    if (selected) messageNodes.get(selected.messageId)?.scrollIntoView({ behavior: motion.matches ? 'instant' : 'smooth', block: 'center' });
  });
  on('appeal:created', appeal => {
    if (!current || appeal?.conv_id !== current.id) return;
    if (!(current.appeals || []).some(item => item.id === appeal.id)) current.appeals = [...(current.appeals || []), appeal];
    showReviewImpact();
  });
  on('decision:saved', decision => {
    if (!current || decision?.conv_id !== current.id) return;
    if (!(current.decisions || []).some(item => item.id === decision.id)) current.decisions = [...(current.decisions || []), decision];
    // The next score:updated supplies the new baseline; don't preview against the previous score meanwhile.
    showReviewImpact({ ...current, score: null });
  });
  on('score:updated', value => {
    if (!current || !value) return;
    const version = value.checklist?.version ?? value.score?.checklist_version;
    const sameVersion = version == null || Number(version) === Number(current.checklist?.version);
    const activeDetail = value.checklist?.active === true && value.id === current.id && Array.isArray(value.messages);
    if (!sameVersion && !activeDetail) return;
    if (!sameVersion) recoveryScope = null; // A browser-local retry pointer belongs to the old version.
    if (value.id === current.id && Array.isArray(value.messages)) { generation++; render(value); }
    else if (value.conv_id === current.id && value.score) { generation++; render({ ...current, score: value.score }); }
  });
  window.addEventListener('resize', drawSoon);
  window.addEventListener('scroll', drawSoon, { passive: true, capture: true });
  const observer = typeof ResizeObserver === 'function' ? new ResizeObserver(drawSoon) : null;
  observer?.observe(host);
  empty('Выберите диалог', 'Откройте разговор из списка, чтобы проверить оценку и её доказательства.');
  const destroy = () => {
    destroyed = true;
    generation++;
    retries.destroy();
    cleanupListeners.forEach(off => off());
    window.removeEventListener('resize', drawSoon);
    window.removeEventListener('scroll', drawSoon, true);
    observer?.disconnect();
    cancelAnimationFrame(raf);
    mounts.delete(host);
  };
  mounts.set(host, destroy);
  return destroy;
}
