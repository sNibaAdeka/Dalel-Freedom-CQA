/* T24 · Appeal (operator), decision (reviewer), three-layer score history and journal.
 * Contract: TASKS 1.2 (Appeal, Decision, AuditEvent), 1.3 (/appeals, /appeals/{id}/dossier, /decisions), 1.5 (bus).
 * Client-side recalculation mirrors cqa/engine.py: the last decision per criterion (by created_at, then list order) wins,
 * confirm keeps new_answer ?? old_answer ?? AI answer, decisions and evaluations for an outdated criterion_hash do not
 * apply, a weighted criterion without an answer means no total. The AI evaluation is never modified. Totals after a
 * decision come from the API response or from the checklist formula, never from constants.
 * Conversation and user text is rendered with textContent only. Pure helpers are exported for node tests. */

const NS = 'http://www.w3.org/2000/svg';
const mounts = new WeakMap();
const ROLE_LABELS = { operator: 'Оператор', client: 'Клиент', bot: 'Бот', system: 'Система' };
const ANSWER_LABELS = { yes: 'Да', no: 'Нет', partial: 'Частично', na: 'N/A' };
const KIND_ORDER = { ai: 0, appeal: 1, dossier: 2, decision: 3, recalc: 4, other: 5 };
const KIND_TITLES = { ai: 'Оценка ИИ', appeal: 'Апелляция', dossier: 'Досье ИИ', decision: 'Решение', recalc: 'Пересчёт', other: 'Событие' };
const KIND_ICONS = { ai: 'sparkles', appeal: 'message', dossier: 'file', decision: 'gavel', recalc: 'refresh', other: 'dot' };
const LAST_KEY = '9999-12-31T23:59:59.999Z'; // sorts after any ISO timestamp: a previewed decision is always the latest
export const COMMENT_LIMIT = 2000;
export const REASON_LIMIT = 1000;
const ICONS = {
  close: ['M18 6 6 18', 'M6 6l12 12'],
  arrow: ['M5 12h14', 'm13 6 6 6-6 6'],
  refresh: ['M21 12a9 9 0 1 1-2.64-6.36L21 8', 'M21 3v5h-5'],
  check: ['m5 12 4 4L19 6'],
  alert: ['M12 8v5', 'M12 16.5v.01', 'M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z'],
  clock: ['M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20z', 'M12 6v6l4 2'],
  user: ['M20 21a8 8 0 0 0-16 0', 'M12 13a5 5 0 1 0 0-10 5 5 0 0 0 0 10z'],
  sparkles: ['M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9z', 'M19 17v4', 'M17 19h4'],
  message: ['M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z'],
  file: ['M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z', 'M14 3v6h6', 'M8 13h8', 'M8 17h5'],
  gavel: ['m14 13-7.5 7.5a2.12 2.12 0 0 1-3-3L11 10', 'm16 16 6-6', 'm8 8 6-6', 'm9 7 8 8', 'm21 11-8-8'],
  pencil: ['M12 20h9', 'M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4z'],
  flag: ['M4 22V4', 'M4 4h12l-2 4 2 4H4'],
  open: ['M7 17 17 7', 'M8 7h9v9'],
  dot: ['M12 13a1 1 0 1 0 0-2 1 1 0 0 0 0 2z'],
};

// ---------------------------------------------------------------- formatting

const isNum = value => typeof value === 'number' && Number.isFinite(value);
const numberFormat = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 2 });
export function formatNumber(value) { return isNum(value) ? numberFormat.format(value) : '—'; }
export function plural(count, [one, few, many]) {
  const mod10 = Math.abs(count) % 10, mod100 = Math.abs(count) % 100;
  if (mod10 === 1 && mod100 !== 11) return one;
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return few;
  return many;
}
export function excerpt(text, limit = 160) {
  const clean = String(text ?? '').replace(/\s+/g, ' ').trim();
  return clean.length > limit ? `${clean.slice(0, limit - 1).trimEnd()}…` : clean;
}
function validDate(ts) {
  if (ts === null || ts === undefined || ts === '') return null;
  const date = new Date(ts);
  return Number.isNaN(date.getTime()) ? null : date;
}
export function formatTime(ts, now = new Date()) {
  const date = validDate(ts);
  if (!date) return '';
  const time = date.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
  return date.toDateString() === now.toDateString() ? time : `${date.toLocaleDateString('ru-RU', { day: '2-digit', month: '2-digit' })} ${time}`;
}
export function relativeTime(ts, now = Date.now()) {
  const date = validDate(ts);
  if (!date) return '';
  const minutes = Math.round((now - date.getTime()) / 60000);
  if (minutes < 1) return 'только что';
  if (minutes < 60) return `${minutes} мин назад`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} ч назад`;
  return formatTime(ts, new Date(now));
}
function clock(seconds) {
  return isNum(seconds) && seconds >= 0 ? `${String(Math.floor(seconds / 60)).padStart(2, '0')}:${String(Math.floor(seconds % 60)).padStart(2, '0')}` : '';
}
function messageTime(message) { return clock(message?.rec_sec ?? message?.t_sec) || String(message?.ts_raw || ''); }
function messageOf(error) { return error?.message ? String(error.message) : 'неизвестная ошибка'; }

// ---------------------------------------------------------------- criteria and answers

/** Flag-only criterion (v1 C8): critical with weight 0; never part of the total. */
export function isFlagOnly(criterion) { return Boolean(criterion?.critical) && !(Number(criterion?.weight) > 0); }
/** Answer that raises the gross-error flag: critical_when, legacy critical_rule 'no', otherwise «yes» (v1 C8). */
export function violationAnswer(criterion) {
  if (criterion?.critical_when === 'yes' || criterion?.critical_when === 'no') return criterion.critical_when;
  return criterion?.critical_rule === 'no' ? 'no' : 'yes';
}

function scaleSteps(criterion) {
  return Array.isArray(criterion?.scale) ? [...new Set(criterion.scale.map(Number).filter(Number.isFinite))].sort((a, b) => a - b) : [];
}

/** Allowed numeric range: explicit min/max, else the scale bounds, else 0..max_points. */
export function numericRange(criterion) {
  const steps = scaleSteps(criterion);
  const first = (...values) => values.find(isNum);
  const min = first(criterion?.min, criterion?.range?.[0], steps.length ? steps[0] : undefined, 0);
  const max = first(criterion?.max, criterion?.range?.[1], steps.length > 1 ? steps.at(-1) : undefined, Number(criterion?.max_points) > 0 ? Number(criterion.max_points) : undefined);
  const step = isNum(criterion?.step) && criterion.step > 0 ? criterion.step : null;
  return { min, max: isNum(max) ? max : null, step };
}

function answerMax(criterion) {
  if (criterion?.answer_type === 'numeric') { const { max } = numericRange(criterion); return isNum(max) && max > 0 ? max : null; }
  const steps = scaleSteps(criterion);
  if (steps.length) return steps.at(-1) > 0 ? steps.at(-1) : null;
  return Number(criterion?.max_points) > 0 ? Number(criterion.max_points) : null;
}

/** Discrete answers a reviewer may choose: binary → yes/no, scale → its steps; N/A only when the checklist allows it. */
export function answerOptions(criterion) {
  const type = criterion?.answer_type;
  const options = type === 'scale' ? scaleSteps(criterion) : type === 'numeric' ? [] : ['yes', 'no'];
  if (criterion?.na_allowed) options.push('na');
  return options;
}

export function normalizeAnswer(criterion, answer) {
  if (answer === null || answer === undefined) return null;
  if (typeof answer === 'string') {
    const text = answer.trim().toLowerCase();
    if (['yes', 'no', 'partial', 'na'].includes(text)) return text;
    if (text === 'n/a') return 'na';
    const numeric = Number(text.replace(',', '.'));
    if (text !== '' && Number.isFinite(numeric) && ['scale', 'numeric'].includes(criterion?.answer_type)) return numeric;
    return answer;
  }
  return isNum(answer) ? answer : null;
}

export function sameAnswer(a, b) { return a === b || (isNum(a) && isNum(b) && Math.abs(a - b) < 1e-9); }

/** Strict validation of a new answer by criterion type. Returns {ok, value} or {ok:false, error}. */
export function validateAnswer(criterion, raw) {
  if (raw === null || raw === undefined || raw === '') return { ok: false, error: 'Выберите новый ответ.' };
  const value = normalizeAnswer(criterion, raw);
  const type = criterion?.answer_type || 'binary';
  if (value === 'na') return criterion?.na_allowed ? { ok: true, value } : { ok: false, error: 'N/A для этого критерия чек-лист не допускает.' };
  if (type === 'scale') {
    const steps = scaleSteps(criterion);
    const match = isNum(value) ? steps.find(step => Math.abs(step - value) < 1e-9) : undefined;
    return match !== undefined ? { ok: true, value: match } : { ok: false, error: `Выберите значение шкалы: ${steps.map(formatNumber).join(', ')}.` };
  }
  if (type === 'numeric') {
    const { min, max, step } = numericRange(criterion);
    const inRange = isNum(value) && value >= min - 1e-9 && (max === null || value <= max + 1e-9);
    const onStep = !step || (isNum(value) && Math.abs((value - min) / step - Math.round((value - min) / step)) < 1e-9);
    if (inRange && onStep) return { ok: true, value };
    const bounds = max === null ? `не меньше ${formatNumber(min)}` : `от ${formatNumber(min)} до ${formatNumber(max)}`;
    return { ok: false, error: `Введите число ${bounds}${step ? ` с шагом ${formatNumber(step)}` : ''}.` };
  }
  return value === 'yes' || value === 'no' ? { ok: true, value } : { ok: false, error: 'Для этого критерия допустимы только ответы «Да» и «Нет».' };
}

export function answerLabel(criterion, answer) {
  if (answer === null || answer === undefined) return 'нет ответа';
  if (answer === 'na') return 'N/A';
  if (isFlagOnly(criterion) && (answer === 'yes' || answer === 'no')) return `${ANSWER_LABELS[answer]} — ${answer === violationAnswer(criterion) ? 'нарушение есть' : 'нарушения нет'}`;
  if (isNum(answer)) { const max = answerMax(criterion); return max ? `${formatNumber(answer)} из ${formatNumber(max)}` : formatNumber(answer); }
  return ANSWER_LABELS[answer] || String(answer);
}

function answerTone(criterion, answer) {
  if (answer === null || answer === undefined) return 'pending';
  if (answer === 'na') return 'na';
  if (isFlagOnly(criterion)) return answer === violationAnswer(criterion) ? 'critical' : 'yes';
  if (answer === 'yes') return 'yes';
  if (answer === 'no') return 'no';
  if (answer === 'partial') return 'partial';
  if (isNum(answer)) { const max = answerMax(criterion); return max && answer >= max ? 'yes' : answer > 0 ? 'partial' : 'no'; }
  return 'pending';
}

// ---------------------------------------------------------------- request bodies

/** POST /appeals body. Comment is required; message IDs are unique and ordered as in the transcript. */
export function buildAppealBody(input, messages = []) {
  const errors = {};
  const convId = typeof input?.conv_id === 'string' ? input.conv_id : '';
  const criterionId = typeof input?.criterion_id === 'string' ? input.criterion_id : '';
  if (!convId) errors.conv_id = 'Не выбран диалог.';
  if (!criterionId) errors.criterion_id = 'Выберите критерий.';
  if (input?.checklist_version != null && (!Number.isInteger(input.checklist_version) || input.checklist_version < 1)) errors.checklist_version = 'Не выбрана версия чек-листа.';
  const comment = String(input?.comment ?? '').trim();
  if (!comment) errors.comment = 'Напишите комментарий: что именно оценено неверно и почему.';
  else if (comment.length > COMMENT_LIMIT) errors.comment = `Комментарий длиннее ${COMMENT_LIMIT} символов. Сократите его.`;
  const order = new Map((messages || []).map((message, index) => [message.id, index]));
  const ids = [...new Set((input?.message_ids || []).filter(id => typeof id === 'string' && id))]
    .filter(id => !order.size || order.has(id))
    .sort((a, b) => (order.get(a) ?? 0) - (order.get(b) ?? 0));
  if (Object.keys(errors).length) return { ok: false, errors };
  return { ok: true, body: { conv_id: convId, criterion_id: criterionId, comment, message_ids: ids,
    ...(input?.checklist_version != null ? { checklist_version: input.checklist_version } : {}) } };
}

/** POST /decisions body. Reason is required for both actions; new_answer only for «change» and strictly by type. */
export function buildDecisionBody(input, criterion, currentAnswer) {
  const errors = {};
  const action = input?.action;
  if (!input?.appeal_id) errors.appeal_id = 'Апелляция не выбрана.';
  if (action !== 'confirm' && action !== 'change') errors.action = 'Выберите решение: подтвердить или изменить оценку.';
  const reason = String(input?.reason ?? '').trim();
  if (!reason) errors.reason = 'Укажите причину решения: она попадёт в карточку и журнал.';
  else if (reason.length > REASON_LIMIT) errors.reason = `Причина длиннее ${REASON_LIMIT} символов. Сократите её.`;
  let newAnswer;
  if (action === 'change') {
    const check = validateAnswer(criterion, input?.answer ?? input?.new_answer);
    if (!check.ok) errors.answer = check.error;
    else if (sameAnswer(check.value, normalizeAnswer(criterion, currentAnswer))) errors.answer = 'Новый ответ совпадает с текущим. Выберите другой ответ или подтвердите оценку.';
    else newAnswer = check.value;
  }
  if (Object.keys(errors).length) return { ok: false, errors };
  const body = { appeal_id: input.appeal_id, action, reason };
  if (action === 'change') body.new_answer = newAnswer;
  return { ok: true, body };
}

// ---------------------------------------------------------------- scoring (checklist formula, PDF v1)

/** Half-up rounding that is stable against binary noise (85.385 → 85.39). */
export function roundHalfUp(value, digits = 2) {
  if (!isNum(value)) return null;
  const factor = 10 ** digits;
  const scaled = Number((Math.abs(value) * factor).toFixed(8));
  return (Math.sign(value) || 1) * Math.floor(scaled + 0.5) / factor;
}

function fractionFor(criterion, answer) {
  if (answer === 'yes') return 1;
  if (answer === 'no') return 0;
  if (answer === 'partial') return 0.5;
  if (isNum(answer)) { const max = answerMax(criterion); return max ? answer / max : null; }
  return null;
}

/**
 * Total by the checklist formula: «Да» = full weight, «Нет» = 0, scale = value / max(scale) × weight; N/A is excluded and
 * weights of applicable criteria are stretched to 100; flag-only criteria never count; rounding from the checklist
 * (default 2, half up). No applicable weighted criteria, or a weighted criterion without an answer → total null.
 */
export function scoreAnswers(checklist, answers) {
  const criteria = Array.isArray(checklist?.criteria) ? checklist.criteria : [];
  const read = id => (answers instanceof Map ? answers.get(id) : answers?.[id]);
  const digits = Number.isInteger(checklist?.rounding?.total) ? checklist.rounding.total : 2;
  const lines = criteria.map(criterion => {
    const answer = normalizeAnswer(criterion, read(criterion.id));
    const weight = Number(criterion.weight) || 0;
    const fraction = answer === 'na' ? null : fractionFor(criterion, answer);
    const maxPoints = criterion.max_points !== null && criterion.max_points !== undefined && Number.isFinite(Number(criterion.max_points)) ? Number(criterion.max_points) : weight;
    return {
      criterion_id: criterion.id, answer, weight, scored: weight > 0 && !isFlagOnly(criterion), is_na: answer === 'na', missing: answer === null,
      fraction, points: fraction === null ? null : fraction * maxPoints, max_points: maxPoints, weight_effective: 0, contribution: 0,
    };
  });
  const incomplete = lines.some(line => line.scored && line.missing);
  const applicable = lines.reduce((sum, line) => sum + (line.scored && line.fraction !== null ? line.weight : 0), 0);
  let raw = 0;
  for (const line of lines) {
    if (!line.scored || line.fraction === null || !applicable) continue;
    line.weight_effective = line.weight / applicable * 100;
    line.contribution = line.fraction * line.weight_effective;
    raw += line.contribution;
  }
  const computable = applicable > 0 && !incomplete;
  return { total: computable ? roundHalfUp(raw, digits) : null, total_raw: computable ? raw : null, applicable_weight: applicable, digits, lines, incomplete };
}

// ---------------------------------------------------------------- conversation detail helpers

const timeOf = ts => validDate(ts)?.getTime() ?? null;
function sortByTime(items, key = 'created_at') {
  return items.map((item, index) => ({ item, index })).sort((a, b) => {
    const ta = timeOf(a.item?.[key]), tb = timeOf(b.item?.[key]);
    if (ta !== null && tb !== null && ta !== tb) return ta - tb;
    if (ta === null && tb !== null) return 1;
    if (tb === null && ta !== null) return -1;
    return a.index - b.index;
  }).map(entry => entry.item);
}
/** Engine order for decisions: created_at as a string (missing first), then list order. */
function engineOrder(items) {
  return items.map((item, index) => ({ item, index, key: String(item?.created_at || '') }))
    .sort((a, b) => (a.key < b.key ? -1 : a.key > b.key ? 1 : a.index - b.index)).map(entry => entry.item);
}
const staleHash = (itemHash, criterion) => Boolean(itemHash && criterion?.hash && itemHash !== criterion.hash);

export function criteriaOf(detail, fallbackChecklist) {
  const own = detail?.checklist?.criteria;
  if (Array.isArray(own) && own.length) return own;
  if (Array.isArray(fallbackChecklist?.criteria) && fallbackChecklist.criteria.length) return fallbackChecklist.criteria;
  return (detail?.evals || []).map(item => ({ id: item.criterion_id, title: item.title || item.criterion_id }));
}
export function criterionOf(detail, criterionId, fallbackChecklist) {
  return criteriaOf(detail, fallbackChecklist).find(item => item.id === criterionId) || { id: criterionId, title: criterionId };
}
/** Model evaluation of a criterion: the last one for the current wording (an outdated criterion_hash counts as missing). */
export function evaluationOf(detail, criterionId, fallbackChecklist) {
  const criterion = criterionOf(detail, criterionId, fallbackChecklist);
  let found = null;
  for (const item of detail?.evals || []) if (item?.criterion_id === criterionId && !staleHash(item.criterion_hash, criterion)) found = item;
  return found;
}
/** An appeal owns the original model answer, even after the same wording is evaluated again. */
export function appealEvaluation(detail, appeal, fallbackChecklist) {
  const snapshot = appeal?.evaluation;
  if (snapshot == null) return evaluationOf(detail, appeal?.criterion_id, fallbackChecklist);
  const criterion = criterionOf(detail, appeal.criterion_id, fallbackChecklist);
  if (snapshot.criterion_id !== appeal.criterion_id || staleHash(snapshot.criterion_hash, criterion) ||
      (appeal.criterion_hash && snapshot.criterion_hash !== appeal.criterion_hash)) return null;
  return snapshot;
}
export function appealAssessment(detail, appeal, fallbackChecklist) {
  const criterion = criterionOf(detail, appeal.criterion_id, fallbackChecklist);
  const evaluation = appealEvaluation(detail, appeal, fallbackChecklist);
  const answer = evaluation ? normalizeAnswer(criterion, evaluation.answer) : null;
  const maxPoints = Number(criterion.max_points ?? criterion.weight) || 0;
  const fraction = fractionFor(criterion, answer);
  // The snapshot contains one criterion, not the historical answers to every other criterion.
  // Show its original points and base weight without inventing a historical N/A denominator.
  return { evaluation, answer, line: { points: fraction == null ? null : fraction * maxPoints,
    max_points: maxPoints, weight_effective: Number(criterion.weight) || 0 } };
}
export function aiAnswerFor(detail, criterionId, fallbackChecklist) {
  const evaluation = evaluationOf(detail, criterionId, fallbackChecklist);
  return evaluation ? normalizeAnswer(criterionOf(detail, criterionId, fallbackChecklist), evaluation.answer) : null;
}
export function appealsFor(detail, criterionId) { return (detail?.appeals || []).filter(item => item?.criterion_id === criterionId); }

/** Decisions grouped by criterion in engine order; each has criterion_id and `stale` (taken for an older wording). */
export function decisionsByCriterion(detail, fallbackChecklist) {
  const appeals = new Map((detail?.appeals || []).map(item => [item.id, item]));
  const grouped = new Map();
  for (const decision of engineOrder(detail?.decisions || [])) {
    const id = decision?.criterion_id || appeals.get(decision?.appeal_id)?.criterion_id;
    if (!id) continue;
    const stale = staleHash(decision.criterion_hash, criterionOf(detail, id, fallbackChecklist));
    if (!grouped.has(id)) grouped.set(id, []);
    grouped.get(id).push({ ...decision, criterion_id: id, stale });
  }
  return grouped;
}

function answerAfter(criterion, decision, aiAnswer) {
  if (decision.action === 'change') return normalizeAnswer(criterion, decision.new_answer);
  return normalizeAnswer(criterion, decision.new_answer) ?? normalizeAnswer(criterion, decision.old_answer) ?? aiAnswer;
}

/** Final answer = the last applicable decision (engine rules), otherwise the AI answer. Input is not mutated. */
export function finalAnswerFor(detail, criterionId, fallbackChecklist) {
  const criterion = criterionOf(detail, criterionId, fallbackChecklist);
  const ai = aiAnswerFor(detail, criterionId, fallbackChecklist);
  const last = (decisionsByCriterion(detail, fallbackChecklist).get(criterionId) || []).filter(item => !item.stale).at(-1);
  return last ? answerAfter(criterion, last, ai) : ai;
}

/** Confirmation belongs to the appeal snapshot, even after another run replaces the live evaluation. */
export function appealConfirmAnswer(detail, appeal, fallbackChecklist) {
  return appeal.current_answer ?? appeal.original_answer ?? finalAnswerFor(detail, appeal.criterion_id, fallbackChecklist);
}

export function openAppealFor(detail, criterionId) {
  const decided = new Set((detail?.decisions || []).map(item => item?.appeal_id));
  return appealsFor(detail, criterionId).find(item => (item.status || 'open') === 'open' && !decided.has(item.id)) || null;
}

function checklistOf(detail, fallbackChecklist) {
  if (Array.isArray(detail?.checklist?.criteria) && detail.checklist.criteria.length) return detail.checklist;
  if (Array.isArray(fallbackChecklist?.criteria) && fallbackChecklist.criteria.length) return fallbackChecklist;
  return null;
}
function answersMap(detail, criteria, fallbackChecklist, pick) {
  return new Map(criteria.map(item => [item.id, pick(detail, item.id, fallbackChecklist)]));
}

export function classifyAuditEvent(event) {
  const test = text => {
    const value = String(text || '').toLowerCase();
    if (!value) return null;
    if (/recalc|rescor|score[._:-]?(updated|recalculated|changed)|пересч/.test(value)) return 'recalc';
    if (/decision|решени/.test(value)) return 'decision';
    if (/dossier|досье/.test(value)) return 'dossier';
    if (/appeal|апелляц/.test(value)) return 'appeal';
    if (/eval|judge|assess|run|score|оцен/.test(value)) return 'ai';
    return null;
  };
  return test(event?.event) || test(event?.entity) || 'other';
}

/**
 * Three layers of the score: AI total (recomputed from the untouched AI answers, or taken from the AI audit event),
 * decisions per disputed criterion, and the final total (authoritative value from the API).
 */
export function buildHistory(detail, fallbackChecklist) {
  const checklist = checklistOf(detail, fallbackChecklist);
  const criteria = criteriaOf(detail, fallbackChecklist);
  const grouped = decisionsByCriterion(detail, fallbackChecklist);
  const ai = answersMap(detail, criteria, fallbackChecklist, aiAnswerFor);
  const final = answersMap(detail, criteria, fallbackChecklist, finalAnswerFor);
  const aiScore = checklist ? scoreAnswers(checklist, ai) : null;
  const finalScore = checklist ? scoreAnswers(checklist, final) : null;
  const decided = [...grouped.values()].some(list => list.some(item => !item.stale));
  const apiTotal = detail?.score && Object.hasOwn(detail.score, 'total') ? detail.score.total : undefined;
  const auditAi = [...(detail?.audit || [])].reverse().find(event => classifyAuditEvent(event) === 'ai' && (isNum(event?.payload?.total) || isNum(event?.payload?.ai_total)));
  let aiTotal = aiScore ? aiScore.total : null, aiSource = aiScore ? 'recalc' : 'none';
  if (auditAi) { aiTotal = isNum(auditAi.payload.ai_total) ? auditAi.payload.ai_total : auditAi.payload.total; aiSource = 'audit'; }
  else if (!decided && apiTotal !== undefined) { aiTotal = apiTotal; aiSource = 'api'; }
  const finalTotal = apiTotal !== undefined ? apiTotal : finalScore ? finalScore.total : null;
  const verified = finalScore && apiTotal !== undefined ? finalScore.total === apiTotal || (isNum(finalScore.total) && isNum(apiTotal) && Math.abs(finalScore.total - apiTotal) < 1e-9) : null;
  const disputed = criteria.filter(criterion => appealsFor(detail, criterion.id).length || grouped.has(criterion.id)).map(criterion => {
    const decisions = grouped.get(criterion.id) || [];
    return {
      criterion, ai: ai.get(criterion.id), final: final.get(criterion.id), appeals: sortByTime(appealsFor(detail, criterion.id)), decisions,
      applied: decisions.filter(item => !item.stale), open: Boolean(openAppealFor(detail, criterion.id)),
      aiLine: aiScore?.lines.find(line => line.criterion_id === criterion.id) || null,
      finalLine: finalScore?.lines.find(line => line.criterion_id === criterion.id) || null,
    };
  });
  const evals = detail?.evals || [];
  return {
    checklist, criteria, aiScore, finalScore, aiTotal, aiSource, finalTotal, verified, decided, disputed,
    critical: { ai: evals.some(item => item?.critical_hit), final: Boolean(detail?.score?.critical) },
    journal: buildJournal(detail, { checklist, criteria, aiTotal, fallbackChecklist }),
  };
}

/** Journal from detail.audit + detail.appeals + detail.decisions: AI evaluation, appeal, decision, recalculation. */
export function buildJournal(detail, context = {}) {
  const fallback = context.fallbackChecklist;
  const criteria = context.criteria || criteriaOf(detail, fallback);
  const checklist = context.checklist === undefined ? checklistOf(detail, fallback) : context.checklist;
  const criterionById = id => criteria.find(item => item.id === id) || { id, title: id };
  const label = id => { const item = criterionById(id); return item.title && item.title !== id ? `${id} · ${item.title}` : String(id || 'критерий'); };
  const appeals = detail?.appeals || [];
  const appealById = new Map(appeals.map(item => [item.id, item]));
  const ordered = engineOrder(detail?.decisions || []);
  const decisionIds = new Set(ordered.map(item => item.id));
  const auditTs = new Map();
  const recalcEvents = [];
  const entries = [];
  let order = 0;
  const push = entry => entries.push({ ...entry, order: order++ });
  (Array.isArray(detail?.audit) ? detail.audit : []).forEach(event => {
    const kind = classifyAuditEvent(event);
    const payload = event?.payload && typeof event.payload === 'object' ? event.payload : {};
    if (kind === 'appeal' && appealById.has(event.entity_id)) { auditTs.set(`appeal:${event.entity_id}`, event.ts); return; }
    if (kind === 'decision' && decisionIds.has(event.entity_id)) { auditTs.set(`decision:${event.entity_id}`, event.ts); return; }
    if (kind === 'recalc') { recalcEvents.push({ event, payload, used: false }); return; }
    if (kind === 'ai') {
      const total = isNum(payload.ai_total) ? payload.ai_total : isNum(payload.total) ? payload.total : null;
      push({ kind, ts: event.ts || null, title: KIND_TITLES.ai, text: [total !== null ? `итог ${formatNumber(total)}` : '', payload.model ? `модель ${payload.model}` : ''].filter(Boolean).join(', ') || String(event.event || 'оценка выполнена') });
      return;
    }
    if (kind === 'dossier') { push({ kind, ts: event.ts || null, title: KIND_TITLES.dossier, text: 'доводы «за» и «против» готовы, решает человек' }); return; }
    push({ kind: kind === 'appeal' || kind === 'decision' ? 'other' : kind, ts: event.ts || null, title: KIND_TITLES.other, text: String(event.event || event.entity || 'событие') });
  });
  if (!entries.some(entry => entry.kind === 'ai') && (detail?.evals || []).length) {
    const models = [...new Set((detail.evals || []).map(item => item?.model).filter(Boolean))].join(', ');
    const stamps = (detail.evals || []).map(item => item?.created_at || item?.evaluated_at).filter(value => validDate(value));
    const ts = stamps.length ? sortByTime(stamps.map(value => ({ created_at: value })))[0].created_at : null;
    push({ kind: 'ai', ts, title: KIND_TITLES.ai, text: [`итог ${formatNumber(context.aiTotal ?? null)}`, models ? `модель ${models}` : ''].filter(Boolean).join(', ') });
  }
  for (const appeal of appeals) {
    push({ kind: 'appeal', ts: appeal.created_at || auditTs.get(`appeal:${appeal.id}`) || null, title: KIND_TITLES.appeal, criterion_id: appeal.criterion_id, text: `${label(appeal.criterion_id)}: «${excerpt(appeal.comment, 140)}»` });
  }
  // Totals step by step with the engine rules: decisions 1..k applied, last per criterion wins.
  const totalAfter = count => {
    if (!checklist) return null;
    const partial = { ...detail, decisions: ordered.slice(0, count) };
    return scoreAnswers(checklist, answersMap(partial, criteria, fallback, finalAnswerFor)).total;
  };
  ordered.forEach((decision, index) => {
    const criterionId = decision.criterion_id || appealById.get(decision.appeal_id)?.criterion_id;
    const criterion = criterionById(criterionId);
    const previous = finalAnswerFor({ ...detail, decisions: ordered.slice(0, index) }, criterionId, fallback);
    const oldAnswer = normalizeAnswer(criterion, decision.old_answer) ?? previous;
    const stale = staleHash(decision.criterion_hash, criterion);
    const next = answerAfter(criterion, decision, aiAnswerFor(detail, criterionId, fallback));
    const ts = decision.created_at || auditTs.get(`decision:${decision.id}`) || null;
    const reason = decision.reason ? ` Причина: «${excerpt(decision.reason, 140)}»` : '';
    const what = stale
      ? `${label(criterionId)}: решение принято по прежней формулировке критерия и не применяется.${reason}`
      : decision.action === 'change'
        ? `${label(criterionId)}: изменено ${answerLabel(criterion, oldAnswer)} → ${answerLabel(criterion, next)}.${reason}`
        : `${label(criterionId)}: оценка подтверждена (${answerLabel(criterion, next)}).${reason}`;
    push({ kind: 'decision', ts, title: KIND_TITLES.decision, criterion_id: criterionId, text: what });
    const match = recalcEvents.find(item => !item.used && (item.payload.decision_id === decision.id || item.event?.entity_id === decision.id || (decision.appeal_id && item.payload.appeal_id === decision.appeal_id)))
      || recalcEvents.find(item => !item.used && timeOf(item.event?.ts) !== null && timeOf(ts) !== null && timeOf(item.event.ts) >= timeOf(ts));
    if (match) match.used = true;
    const from = [match?.payload.old_total, match?.payload.from, decision.old_total, totalAfter(index)].find(value => value === null || isNum(value));
    const to = [match?.payload.new_total, match?.payload.to, decision.new_total, totalAfter(index + 1)].find(value => value === null || isNum(value));
    push({ kind: 'recalc', ts: match?.event?.ts || ts, title: KIND_TITLES.recalc, criterion_id: criterionId, text: `итог ${formatNumber(from ?? null)} → ${formatNumber(to ?? null)}` });
  });
  for (const item of recalcEvents.filter(entry => !entry.used)) {
    const from = isNum(item.payload.old_total) ? item.payload.old_total : null;
    const to = isNum(item.payload.new_total) ? item.payload.new_total : null;
    push({ kind: 'recalc', ts: item.event?.ts || null, title: KIND_TITLES.recalc, text: from !== null || to !== null ? `итог ${formatNumber(from)} → ${formatNumber(to)}` : String(item.event?.event || 'итог пересчитан') });
  }
  return entries.sort((a, b) => {
    const ta = timeOf(a.ts), tb = timeOf(b.ts);
    const ka = ta ?? (a.kind === 'ai' ? -Infinity : Infinity), kb = tb ?? (b.kind === 'ai' ? -Infinity : Infinity);
    if (ka !== kb) return ka - kb;
    return (KIND_ORDER[a.kind] ?? 9) - (KIND_ORDER[b.kind] ?? 9) || a.order - b.order;
  }).map(({ order: _order, ...entry }) => entry);
}

/** Detail with one more (hypothetical) latest decision; the source detail is not mutated. */
export function withDecision(detail, decision) {
  return { ...detail, decisions: [...(detail?.decisions || []), { id: '__preview__', created_at: LAST_KEY, ...decision }] };
}

/** Preview of «Итог: X → Y» before saving: both ends recomputed with the checklist formula. */
export function previewDecision(detail, criterionId, newAnswer, fallbackChecklist) {
  const before = buildHistory(detail, fallbackChecklist);
  const after = buildHistory(withDecision(detail, { criterion_id: criterionId, action: 'change', new_answer: newAnswer }), fallbackChecklist);
  return { from: before.finalScore ? before.finalScore.total : before.finalTotal, to: after.finalScore ? after.finalScore.total : null };
}

/** Transcript rows around the focus messages (±context), with gap markers between windows. */
export function fragmentRows(messages, focusIds, context = 2) {
  const list = Array.isArray(messages) ? messages : [];
  const index = new Map(list.map((message, i) => [message.id, i]));
  const centers = [...new Set(focusIds || [])].map(id => index.get(id)).filter(value => value !== undefined);
  if (!centers.length) return [];
  const keep = new Set();
  for (const center of centers) for (let i = Math.max(0, center - context); i <= Math.min(list.length - 1, center + context); i++) keep.add(i);
  const rows = [];
  let previous = -1;
  for (const i of [...keep].sort((a, b) => a - b)) {
    if (i - previous > 1) rows.push({ gap: i - previous - 1 });
    rows.push({ index: i, message: list[i] });
    previous = i;
  }
  if (previous < list.length - 1) rows.push({ gap: list.length - 1 - previous });
  return rows;
}

/** Exact-slice quote check. Any role may be evidence (PDF p.3: a message or a system event); text must match exactly. */
export function checkQuote(evidence, messages) {
  const message = (messages || []).find(item => item.id === evidence?.message_id);
  if (!message) return { valid: false, reason: 'Реплика не найдена' };
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
  return { valid, start, end, message, reason: valid ? '' : 'Цитата не совпадает с текстом реплики' };
}

/** Odometer columns: digits of the target with start digits aligned by the decimal separator. */
export function odometerColumns(from, to) {
  const target = formatNumber(to);
  if (!isNum(to)) return [{ char: target }];
  const start = isNum(from) ? formatNumber(from) : '';
  const [targetInt, targetFrac = ''] = target.split(',');
  const [startInt = '', startFrac = ''] = start.split(',');
  const intStart = startInt.length >= targetInt.length ? startInt.slice(-targetInt.length) : startInt.padStart(targetInt.length, ' ');
  const fracStart = startFrac.padEnd(targetFrac.length, ' ').slice(0, targetFrac.length);
  const columns = [];
  const push = (char, startChar) => columns.push(/\d/.test(char) ? { digit: Number(char), from: /\d/.test(startChar) ? Number(startChar) : 0 } : { char });
  [...targetInt].forEach((char, i) => push(char, intStart[i]));
  if (target.includes(',')) { columns.push({ char: ',' }); [...targetFrac].forEach((char, i) => push(char, fracStart[i])); }
  return columns;
}

export function normalizeDossier(raw) {
  const list = value => (Array.isArray(value) ? value : value ? [value] : []).map(item => {
    if (typeof item === 'string') return { text: item, quotes: [] };
    const sources = Array.isArray(item?.quotes) ? item.quotes : item?.quote ? [item] : [];
    const quotes = sources.map(source => {
      const q = typeof source === 'string' ? { quote: source } : source || {};
      return { quote: typeof q.quote === 'string' ? q.quote : '', message_id: q.message_id || item?.message_id || item?.message_ids?.[0] || null, start: q.start, end: q.end };
    }).filter(q => q.quote.trim());
    return { text: String(item?.text || item?.argument || item?.summary || ''), quotes };
  }).filter(item => item.text || item.quotes.length);
  const rec = raw?.recommendation;
  const recommendation = typeof rec === 'string' ? (rec.trim() ? { text: rec.trim() } : null)
    : rec && typeof rec === 'object' ? { text: String(rec.text || rec.summary || ''), action: rec.action || null, new_answer: rec.new_answer, confidence: isNum(rec.confidence) ? rec.confidence : null } : null;
  return {
    pro: list(raw?.arguments_for ?? raw?.for ?? raw?.pro), contra: list(raw?.arguments_against ?? raw?.against ?? raw?.contra), recommendation,
    pending: raw?.status === 'pending' || raw?.status === 'processing', is_mock: Boolean(raw?.is_mock),
  };
}

/** Hotkeys are matched by physical key (event.code) so they work in the Russian layout too. */
export const HOTKEYS = Object.freeze({ confirm: 'KeyY', change: 'KeyN', next: 'KeyJ', previous: 'KeyK', appeal: 'KeyA' });
export function isTextEntry(target) {
  if (!target || typeof target !== 'object') return false;
  const tag = String(target.tagName || '').toLowerCase();
  if (tag === 'textarea' || tag === 'select') return true;
  if (target.isContentEditable) return true;
  if (tag !== 'input') return false;
  return !['checkbox', 'radio', 'button', 'submit', 'reset', 'range', 'color', 'file'].includes(String(target.type || 'text').toLowerCase());
}

// ---------------------------------------------------------------- async coordination

/** Monotonic token guard: only the latest started request may update the UI. */
export function createLatest() {
  let current = 0;
  return { next: () => ++current, isCurrent: token => token === current, invalidate: () => { current++; } };
}

/** Loads conversation and dossier for one appeal; responses for a previously selected appeal are ignored. */
export function createCaseLoader(api, handlers = {}) {
  const guard = createLatest();
  return {
    load(appeal) {
      const token = guard.next();
      const live = () => guard.isCurrent(token);
      const version = Number(appeal.checklist_version);
      const pinned = Number.isInteger(version) && version > 0;
      const detailRequest = Promise.resolve().then(() => api.conversation(appeal.conv_id, pinned ? { version } : undefined));
      const dossierRequest = Promise.resolve().then(() => api.dossier(appeal.id));
      dossierRequest.catch(() => {});
      return (async () => {
        let detail;
        try { detail = await detailRequest; } catch (error) { if (!live()) return 'stale'; handlers.onError?.(appeal, error); return 'error'; }
        if (!live()) return 'stale';
        if (!detail || detail.id !== appeal.conv_id) { handlers.onError?.(appeal, new Error('Диалог апелляции не найден.')); return 'error'; }
        if (pinned && Number(detail.checklist?.version) !== version) {
          handlers.onError?.(appeal, new Error('Сервер не вернул версию чек-листа, по которой подана апелляция. Обновите данные перед решением.'));
          return 'error';
        }
        handlers.onDetail?.(appeal, detail);
        try {
          const dossier = await dossierRequest;
          if (!live()) return 'stale';
          handlers.onDossier?.(appeal, dossier);
        } catch (error) { if (!live()) return 'stale'; handlers.onDossierError?.(appeal, error); }
        return 'done';
      })();
    },
    cancel: () => guard.invalidate(),
  };
}

/** Save an appeal. Success only when the API confirms with an ID; failures propagate and emit nothing. */
export async function submitAppeal({ api, bus }, body) {
  const appeal = await api.createAppeal(body);
  if (!appeal || typeof appeal !== 'object' || !appeal.id) throw new Error('Сервер не подтвердил сохранение апелляции.');
  const payload = { ...body, ...appeal, conv_id: appeal.conv_id || body.conv_id, criterion_id: appeal.criterion_id || body.criterion_id, status: appeal.status || 'open' };
  bus?.emit('appeal:created', payload);
  return payload;
}

/** X → Y after a decision: from the API response, otherwise recalculated from the conversation with the checklist formula. */
export function resolveDecisionTotals(response, before, body, criterionId, fallbackChecklist) {
  const pick = key => (response && Object.hasOwn(response, key) && (response[key] === null || isNum(response[key])) ? response[key] : undefined);
  let from = pick('old_total');
  let to = pick('new_total');
  const source = from !== undefined && to !== undefined ? 'api' : 'recalc';
  if (to === undefined && response?.score && Object.hasOwn(response.score, 'total') && (response.score.total === null || isNum(response.score.total))) to = response.score.total;
  if (from === undefined && before) from = buildHistory(before, fallbackChecklist).finalTotal;
  if (to === undefined && before) {
    const projected = withDecision(before, { appeal_id: body.appeal_id, criterion_id: criterionId, action: body.action, new_answer: body.action === 'change' ? body.new_answer : undefined, old_answer: finalAnswerFor(before, criterionId, fallbackChecklist) });
    to = buildHistory(projected, fallbackChecklist).finalScore?.total ?? null;
  }
  return { from: from ?? null, to: to ?? null, source };
}

/** Save a decision, then emit decision:saved and score:updated (fresh detail, or {conv_id, score}). Failures emit nothing. */
export async function submitDecision({ api, bus }, body, context = {}) {
  const response = await api.createDecision(body);
  if (!response || typeof response !== 'object') throw new Error('Сервер не подтвердил сохранение решения.');
  const totals = resolveDecisionTotals(response, context.before, body, context.criterion_id, context.checklist);
  let fresh = null;
  try { fresh = await api.conversation(context.conv_id, { trace: true }); } catch { fresh = null; }
  if (fresh && fresh.id !== context.conv_id) fresh = null;
  const decision = response.decision && typeof response.decision === 'object' ? response.decision : {};
  const payload = { ...decision, appeal_id: decision.appeal_id || body.appeal_id, action: decision.action || body.action, reason: decision.reason || body.reason, conv_id: context.conv_id, criterion_id: context.criterion_id, old_total: totals.from, new_total: totals.to };
  if (!Object.hasOwn(payload, 'new_answer') && body.action === 'change') payload.new_answer = body.new_answer;
  bus?.emit('decision:saved', payload);
  if (fresh) bus?.emit('score:updated', fresh);
  else if (response.score && typeof response.score === 'object') bus?.emit('score:updated', { conv_id: context.conv_id, score: response.score });
  return { decision: payload, from: totals.from, to: totals.to, source: totals.source, fresh };
}

// ---------------------------------------------------------------- DOM helpers

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}
function icon(name) {
  const svg = document.createElementNS(NS, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('focusable', 'false');
  svg.classList.add('cqa-review-icon');
  for (const d of ICONS[name] || ICONS.dot) { const path = document.createElementNS(NS, 'path'); path.setAttribute('d', d); svg.append(path); }
  return svg;
}
function button(text, className, onClick) {
  const node = el('button', `cqa-review-button ${className || ''}`.trim(), text);
  node.type = 'button';
  if (onClick) node.addEventListener('click', onClick);
  return node;
}
function kbd(key) { const node = el('kbd', 'cqa-review-kbd', key); node.setAttribute('aria-hidden', 'true'); return node; }
function chip(text, tone, iconName) {
  const node = el('span', `cqa-review-chip cqa-review-chip--${tone}`);
  if (iconName) node.append(icon(iconName));
  node.append(el('span', '', text));
  return node;
}
function srOnly(text) { return el('span', 'cqa-review-sr', text); }
let uid = 0;
const nextId = prefix => `${prefix}-${++uid}`;
function attachStyle() {
  if (document.querySelector('link[data-cqa-review-style]')) return;
  const link = el('link');
  link.rel = 'stylesheet';
  link.href = new URL('../css/review.css', import.meta.url).href;
  link.dataset.cqaReviewStyle = '';
  document.head.append(link);
}
function makeField(labelText, control, { hint, required } = {}) {
  const wrap = el('div', 'cqa-review-field');
  if (!control.id) control.id = nextId('cqa-review-control');
  const label = el('label', 'cqa-review-label', labelText);
  label.htmlFor = control.id;
  if (required) {
    const mark = el('span', 'cqa-review-required', ' *');
    mark.setAttribute('aria-hidden', 'true');
    label.append(mark, srOnly(' (обязательно)'));
    control.required = true;
  }
  wrap.append(label);
  const describedBy = [];
  if (hint) { const hintNode = el('p', 'cqa-review-hint', hint); hintNode.id = `${control.id}-hint`; describedBy.push(hintNode.id); wrap.append(hintNode); }
  wrap.append(control);
  const error = el('p', 'cqa-review-error');
  error.id = `${control.id}-error`;
  error.hidden = true;
  describedBy.push(error.id);
  control.setAttribute('aria-describedby', describedBy.join(' '));
  wrap.append(error);
  const setError = message => {
    error.replaceChildren();
    error.hidden = !message;
    if (message) { error.append(icon('alert'), el('span', '', message)); control.setAttribute('aria-invalid', 'true'); } else control.removeAttribute('aria-invalid');
  };
  return { wrap, control, setError };
}
function appendHighlighted(node, text, slices) {
  let from = 0;
  for (const slice of slices) {
    if (slice.start < from) continue;
    node.append(document.createTextNode(text.slice(from, slice.start)), el('mark', 'cqa-review-mark', text.slice(slice.start, slice.end)));
    from = slice.end;
  }
  node.append(document.createTextNode(text.slice(from)));
}
const cssEscape = value => (typeof CSS !== 'undefined' && typeof CSS.escape === 'function' ? CSS.escape(value) : String(value).replace(/["\\\]]/g, '\\$&'));

// ---------------------------------------------------------------- module

export function mount(CQA = globalThis.window?.CQA) {
  if (!CQA?.bus || !CQA?.api || typeof document === 'undefined') return () => {};
  if (mounts.has(CQA)) return mounts.get(CQA);
  attachStyle();
  CQA.state ||= {};
  const root = document.querySelector('.cqa-root') || document.body;
  const motionQuery = typeof window.matchMedia === 'function' ? window.matchMedia('(prefers-reduced-motion: reduce)') : null;
  const reduced = () => Boolean(motionQuery?.matches);
  const role = () => (CQA.config?.role === 'reviewer' ? 'reviewer' : 'operator');
  const state = {
    queue: [], queueStatus: 'idle', queueError: '', queueLoaded: false, selectedId: null, view: null, focusCase: false,
    titles: new Map(), drafts: new Map(), decisionDrafts: new Map(), pendingRoll: null, decisionBusy: false, appealBusy: false, focusedCriterion: null,
  };
  const guards = { queue: createLatest(), history: createLatest(), form: createLatest() };
  const offs = [];
  let decisionControl = null;
  const on = (name, handler) => { const off = CQA.bus.on(name, handler); if (typeof off === 'function') offs.push(off); };
  const checklistFromState = () => {
    const versions = Array.isArray(CQA.state?.checklists) ? CQA.state.checklists : [];
    return versions.find(item => String(item.version) === String(CQA.state?.checklistVersion)) || versions.at(-1) || null;
  };
  const say = message => {
    if (typeof CQA.announce === 'function') CQA.announce(message);
    else if (panel?.live) panel.live.textContent = message;
  };
  const notify = message => { if (typeof CQA.toast === 'function') CQA.toast(message); else say(message); };
  const fail = error => notify(`Не удалось выполнить действие: ${messageOf(error)}`);
  function harvestTitles(detail) {
    for (const item of [...(checklistFromState()?.criteria || []), ...(detail?.checklist?.criteria || [])]) if (item?.id && item.title) state.titles.set(item.id, item.title);
  }
  const criterionTitle = id => (state.titles.get(id) ? `${id} · ${state.titles.get(id)}` : String(id || 'Критерий'));

  // ------------------------------------------------ shared assessment views
  function pointsText(criterion, line, answer) {
    if (isFlagOnly(criterion)) return 'без баллов, отдельный флаг';
    if (answer === 'na') return 'исключён из итога';
    if (!line || line.points === null) return '—';
    return `${formatNumber(line.points)} из ${formatNumber(line.max_points)}`;
  }
  function weightText(criterion, line, answer) {
    const weight = Number(criterion.weight) || 0;
    if (isFlagOnly(criterion)) return '0, только флаг';
    if (answer === 'na') return `${formatNumber(weight)}% перераспределён`;
    if (line && Math.abs(line.weight_effective - weight) > 0.005) return `${formatNumber(weight)}% → ${formatNumber(line.weight_effective)}% с учётом N/A`;
    return `${formatNumber(weight)}%`;
  }
  function evidenceList(detail, evaluation) {
    const items = (evaluation?.evidence || []).filter(item => item && (item.quote || item.message_id));
    if (!items.length) return null;
    const list = el('ul', 'cqa-review-evidence');
    for (const evidence of items) {
      const index = (detail.messages || []).findIndex(item => item.id === evidence.message_id);
      const message = detail.messages?.[index];
      const check = checkQuote(evidence, detail.messages);
      const li = el('li');
      li.append(el('blockquote', '', evidence.quote || message?.text || ''));
      const meta = el('p', 'cqa-review-evidence__meta');
      meta.append(el('span', 'cqa-review-mono', index >= 0 ? `#${index + 1}` : '#?'), el('span', '', index >= 0 ? `${ROLE_LABELS[message.speaker] || 'Участник'}${messageTime(message) ? `, ${messageTime(message)}` : ''}` : 'реплика не найдена'));
      const verdict = el('span', check.valid ? 'cqa-review-evidence__ok' : 'cqa-review-evidence__bad');
      verdict.append(icon(check.valid ? 'check' : 'alert'), el('span', '', check.valid ? 'Цитата найдена в оригинале' : `${check.reason}. Проверьте вручную`));
      meta.append(verdict);
      li.append(meta);
      list.append(li);
    }
    return list;
  }
  function assessmentView(detail, criterion, history, appeal = null) {
    const checklist = checklistFromState();
    const original = appeal ? appealAssessment(detail, appeal, checklist) : null;
    const evaluation = (original ? original.evaluation : evaluationOf(detail, criterion.id, checklist)) || {};
    const ai = original ? original.answer : aiAnswerFor(detail, criterion.id, checklist);
    const final = finalAnswerFor(detail, criterion.id, checklist);
    const line = original ? original.line : history.aiScore?.lines.find(item => item.criterion_id === criterion.id) || null;
    const wrap = el('div', 'cqa-review-assessment');
    const facts = el('dl', 'cqa-review-facts');
    const fact = (term, value, tone) => {
      const row = el('div', `cqa-review-fact${tone ? ` cqa-review-fact--${tone}` : ''}`);
      row.append(el('dt', '', term), el('dd', '', value));
      facts.append(row);
    };
    fact('Ответ ИИ', answerLabel(criterion, ai), answerTone(criterion, ai));
    fact('Баллы', pointsText(criterion, line, ai));
    fact('Вес', weightText(criterion, line, ai));
    if (isNum(evaluation.confidence)) fact('Уверенность', `${Math.round(evaluation.confidence * 100)}%${evaluation.needs_review ? ', низкая' : ''}`);
    wrap.append(facts);
    if (!sameAnswer(final, ai)) {
      const current = el('p', 'cqa-review-assessment__final');
      current.append(icon('gavel'), el('span', '', `${appeal ? 'Текущий ответ' : 'После решения проверяющего сейчас'} ${answerLabel(criterion, final)}. Исходная оценка ИИ сохранена.`));
      wrap.append(current);
    }
    if (evaluation.explanation) wrap.append(el('p', 'cqa-review-assessment__explanation', evaluation.explanation));
    if (ai === 'na') wrap.append(el('p', 'cqa-review-assessment__na', evaluation.na_reason ? `Причина N/A: ${evaluation.na_reason}` : 'Причина N/A не указана.'));
    const evidence = evidenceList(detail, evaluation);
    if (evidence) wrap.append(evidence);
    else if (Array.isArray(evaluation.checked_range) && evaluation.checked_range.length) {
      const index = id => (detail.messages || []).findIndex(item => item.id === id) + 1;
      wrap.append(el('p', 'cqa-review-hint', `Цитаты нет: ИИ проверил реплики #${index(evaluation.checked_range[0])}–#${index(evaluation.checked_range.at(-1))} и не нашёл действия.`));
    } else if (ai !== 'na') wrap.append(el('p', 'cqa-review-hint', 'ИИ не приложил цитату. Проверьте диалог вручную.'));
    return wrap;
  }

  // ------------------------------------------------ appeal drawer (operator)
  let ui = null;
  let appealState = null;
  let closeTimer = 0;
  function buildDialog() {
    const dialog = el('dialog', 'cqa-review-dialog');
    const titleId = nextId('cqa-appeal-title'), subtitleId = nextId('cqa-appeal-subtitle');
    dialog.setAttribute('aria-labelledby', titleId);
    dialog.setAttribute('aria-describedby', subtitleId);
    const form = el('form', 'cqa-review-dialog__form');
    form.noValidate = true;
    const header = el('header', 'cqa-review-dialog__header');
    const heading = el('div', 'cqa-review-dialog__heading');
    const title = el('h2', 'cqa-review-dialog__title', 'Оспорить оценку');
    title.id = titleId;
    const subtitle = el('p', 'cqa-review-dialog__subtitle');
    subtitle.id = subtitleId;
    heading.append(title, subtitle);
    const close = button('', 'cqa-review-icon-button', () => closeAppeal());
    close.append(icon('close'));
    close.setAttribute('aria-label', 'Закрыть форму апелляции');
    close.setAttribute('aria-keyshortcuts', 'Escape');
    header.append(heading, close);
    const body = el('div', 'cqa-review-dialog__body');
    const select = el('select', 'cqa-review-select');
    const criterionField = makeField('Критерий', select);
    const current = el('section', 'cqa-review-current');
    const currentTitle = el('h3', 'cqa-review-current__title', 'Текущая оценка ИИ');
    currentTitle.id = nextId('cqa-appeal-current');
    current.setAttribute('aria-labelledby', currentTitle.id);
    const currentBody = el('div');
    current.append(currentTitle, currentBody);
    const duplicate = el('p', 'cqa-review-notice');
    duplicate.hidden = true;
    const comment = el('textarea', 'cqa-review-textarea');
    comment.rows = 4;
    comment.maxLength = COMMENT_LIMIT;
    comment.autofocus = true;
    const commentField = makeField('Ваш комментарий', comment, { hint: 'Что именно оценено неверно и почему. Проверяющий увидит этот текст рядом со спорной оценкой.', required: true });
    const messages = el('fieldset', 'cqa-review-messages');
    const legend = el('legend', 'cqa-review-label');
    const legendCount = el('span', 'cqa-review-messages__count');
    legend.append(el('span', '', 'Реплики-доказательства'), legendCount);
    const messagesHint = el('p', 'cqa-review-hint', 'Отмечены реплики, на которые сослался ИИ. Добавьте те, что подтверждают вашу позицию.');
    messagesHint.id = nextId('cqa-appeal-messages-hint');
    messages.setAttribute('aria-describedby', messagesHint.id);
    const list = el('ul', 'cqa-review-message-list');
    messages.append(legend, messagesHint, list);
    const formError = el('p', 'cqa-review-form-error');
    formError.setAttribute('role', 'alert');
    formError.hidden = true;
    body.append(criterionField.wrap, current, duplicate, commentField.wrap, messages, formError);
    const footer = el('footer', 'cqa-review-dialog__footer');
    const escHint = el('span', 'cqa-review-dialog__hint');
    escHint.append(kbd('Esc'), el('span', '', 'закрыть без отправки'));
    const cancel = button('Отмена', '', () => closeAppeal());
    const submit = el('button', 'cqa-review-button cqa-review-button--primary', 'Подать апелляцию');
    submit.type = 'submit';
    footer.append(escHint, cancel, submit);
    form.append(header, body, footer);
    dialog.append(form);
    root.append(dialog);
    select.addEventListener('change', () => {
      saveDraft();
      appealState.criterionId = select.value;
      criterionField.setError('');
      renderCriterionPart();
    });
    list.addEventListener('change', () => { if (appealState) appealState.touched = true; updateMessageCount(); });
    comment.addEventListener('input', () => { if (comment.value.trim()) commentField.setError(''); });
    form.addEventListener('submit', event => { event.preventDefault(); submitAppealForm().catch(fail); });
    dialog.addEventListener('cancel', event => { event.preventDefault(); closeAppeal(); });
    dialog.addEventListener('close', () => { clearTimeout(closeTimer); dialog.classList.remove('is-closing'); });
    dialog.addEventListener('keydown', event => {
      if (event.key === 'Tab') trapTab(event, dialog);
      else if (event.key === 'Escape' && typeof dialog.showModal !== 'function') { event.preventDefault(); closeAppeal(); }
      // Global single-key shortcuts (P, 1–4, J/K) must not act behind a modal form.
      if (!event.ctrlKey && !event.metaKey && !event.altKey && event.key.length === 1) event.stopPropagation();
    });
    return { dialog, form, title, subtitle, select, criterionField, currentBody, duplicate, comment, commentField, list, legendCount, formError, submit, cancel };
  }
  function trapTab(event, container) {
    const focusable = [...container.querySelectorAll('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])')]
      .filter(node => !node.disabled && node.getClientRects().length > 0);
    if (!focusable.length) return;
    const first = focusable[0], last = focusable.at(-1);
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  }
  const draftKey = (convId, criterionId) => `${convId}|${criterionId}`;
  function checkedMessageIds() { return [...(ui?.list.querySelectorAll('input[type="checkbox"]:checked') || [])].map(node => node.value); }
  function saveDraft() {
    if (!ui || !appealState?.criterionId) return;
    const comment = ui.comment.value;
    if (comment.trim() || appealState.touched) state.drafts.set(draftKey(appealState.detail.id, appealState.criterionId), { comment, messageIds: checkedMessageIds() });
  }
  function updateMessageCount() {
    if (!ui) return;
    const count = checkedMessageIds().length;
    ui.legendCount.textContent = count ? `, отмечено ${count}` : ', ничего не отмечено';
  }
  function renderCriterionPart({ keepComment = true } = {}) {
    const { detail } = appealState;
    const checklist = checklistFromState();
    const criterion = criterionOf(detail, appealState.criterionId, checklist);
    const history = buildHistory(detail, checklist);
    ui.currentBody.replaceChildren(assessmentView(detail, criterion, history));
    const open = openAppealFor(detail, criterion.id);
    ui.duplicate.replaceChildren();
    ui.duplicate.hidden = !open;
    if (open) ui.duplicate.append(icon('clock'), el('span', '', `По этому критерию уже есть апелляция на проверке${open.created_at ? ` (подана в ${formatTime(open.created_at)})` : ''}. Новую можно подать после решения проверяющего.`));
    const draft = state.drafts.get(draftKey(detail.id, criterion.id));
    if (draft) ui.comment.value = draft.comment;
    else if (!keepComment) ui.comment.value = '';
    const evidenceIds = new Set((evaluationOf(detail, criterion.id, checklist)?.evidence || []).map(item => item?.message_id).filter(Boolean));
    const checked = new Set(draft ? draft.messageIds : evidenceIds);
    ui.list.replaceChildren();
    (detail.messages || []).forEach((message, index) => {
      const li = el('li');
      const speaker = ROLE_LABELS[message.speaker] ? message.speaker : 'unknown';
      const label = el('label', `cqa-review-message-option cqa-review-message-option--${speaker}`);
      const box = el('input');
      box.type = 'checkbox';
      box.value = message.id;
      box.checked = checked.has(message.id);
      const meta = el('span', 'cqa-review-message-option__meta');
      meta.append(el('span', 'cqa-review-mono', `#${index + 1}`), el('strong', '', `${ROLE_LABELS[message.speaker] || 'Участник'}${speaker === 'bot' || speaker === 'system' ? ', контекст' : ''}`));
      if (messageTime(message)) meta.append(el('span', 'cqa-review-mono cqa-review-message-option__time', messageTime(message)));
      if (evidenceIds.has(message.id)) meta.append(chip('Доказательство ИИ', 'ai', 'sparkles'));
      label.append(box, meta, el('span', 'cqa-review-message-option__text', message.text || ''));
      li.append(label);
      ui.list.append(li);
    });
    if (!(detail.messages || []).length) ui.list.append(el('li', 'cqa-review-hint', 'В диалоге нет реплик.'));
    appealState.touched = Boolean(draft);
    updateMessageCount();
  }
  function setAppealBusy(busy) {
    if (!ui) return;
    ui.submit.disabled = busy;
    ui.submit.setAttribute('aria-busy', String(busy));
    ui.submit.textContent = busy ? 'Отправляем…' : 'Подать апелляцию';
  }
  async function openAppeal(request, opener) {
    if (role() !== 'operator') { notify('Апелляцию подаёт оператор. Переключите роль на «Оператор».'); return; }
    const convId = request?.conv_id;
    if (typeof convId !== 'string' || !convId) return;
    const token = guards.form.next();
    let detail = CQA.state?.conversation?.id === convId ? CQA.state.conversation : null;
    if (!detail) {
      try { detail = await CQA.api.conversation(convId); }
      catch (error) { if (guards.form.isCurrent(token)) notify(`Форма апелляции не открылась: ${messageOf(error)}`); return; }
      if (!guards.form.isCurrent(token)) return;
      if (!detail || detail.id !== convId) { notify('Форма апелляции не открылась: диалог не найден.'); return; }
    }
    if (role() !== 'operator') return;
    harvestTitles(detail);
    const checklist = checklistFromState();
    const criteria = criteriaOf(detail, checklist).filter(item => evaluationOf(detail, item.id, checklist));
    if (!criteria.length) { notify('В этом диалоге нет оценённых критериев: оспаривать нечего.'); return; }
    if (ui?.dialog.open && appealState) saveDraft();
    ui ||= buildDialog();
    const criterionId = criteria.some(item => item.id === request.criterion_id) ? request.criterion_id : criteria[0].id;
    appealState = { detail, criterionId, opener: opener && opener !== document.body ? opener : appealState?.opener || null, touched: false };
    ui.select.replaceChildren(...criteria.map(item => new Option(`${item.id} · ${item.title || item.id}: ${answerLabel(item, finalAnswerFor(detail, item.id, checklist))}`, item.id)));
    ui.select.value = criterionId;
    ui.subtitle.textContent = `Диалог ${detail.id}, итог ${formatNumber(detail.score?.total)}`;
    ui.formError.hidden = true;
    ui.criterionField.setError('');
    ui.commentField.setError('');
    ui.comment.value = '';
    renderCriterionPart({ keepComment: false });
    setAppealBusy(false);
    clearTimeout(closeTimer);
    ui.dialog.classList.remove('is-closing');
    if (!ui.dialog.open) {
      if (typeof ui.dialog.showModal === 'function') ui.dialog.showModal(); else ui.dialog.setAttribute('open', '');
    }
    requestAnimationFrame(() => ui?.comment.focus());
  }
  function appealButtonFor(criterionId) {
    const card = document.getElementById('cqa-card');
    return card?.querySelector(`.cqa-card__criterion[data-criterion-id="${cssEscape(criterionId || '')}"] [data-card-appeal]`) || null;
  }
  function restoreFocus() {
    const opener = appealState?.opener;
    if (opener?.isConnected && typeof opener.focus === 'function') { opener.focus(); return; }
    const card = document.getElementById('cqa-card');
    const fallback = appealButtonFor(appealState?.criterionId) || card?.querySelector(`.cqa-card__criterion[data-criterion-id="${cssEscape(appealState?.criterionId || '')}"] .cqa-card__criterion-heading`);
    fallback?.focus?.();
  }
  function closeAppeal({ keepDraft = true } = {}) {
    if (!ui?.dialog.open) return;
    if (keepDraft) saveDraft();
    const finish = () => {
      clearTimeout(closeTimer);
      ui.dialog.classList.remove('is-closing');
      if (ui.dialog.open) { if (typeof ui.dialog.close === 'function') ui.dialog.close(); else ui.dialog.removeAttribute('open'); }
      restoreFocus();
    };
    if (reduced()) { finish(); return; }
    ui.dialog.classList.add('is-closing');
    clearTimeout(closeTimer);
    closeTimer = setTimeout(finish, 170);
  }
  async function submitAppealForm() {
    if (!ui || !appealState || state.appealBusy) return;
    ui.formError.hidden = true;
    ui.criterionField.setError('');
    ui.commentField.setError('');
    const { detail } = appealState;
    const criterionId = ui.select.value;
    const built = buildAppealBody({ conv_id: detail.id, criterion_id: criterionId, checklist_version: detail.checklist?.version,
      comment: ui.comment.value, message_ids: checkedMessageIds() }, detail.messages);
    const errors = built.ok ? {} : { ...built.errors };
    if (openAppealFor(detail, criterionId)) errors.criterion_id = 'По этому критерию апелляция уже на проверке. Дождитесь решения или выберите другой критерий.';
    if (role() !== 'operator') errors.role = 'Апелляцию подаёт оператор. Переключите роль на «Оператор».';
    if (Object.keys(errors).length) {
      if (errors.criterion_id) ui.criterionField.setError(errors.criterion_id);
      if (errors.comment) ui.commentField.setError(errors.comment);
      if (errors.role || errors.conv_id || errors.checklist_version) { ui.formError.textContent = errors.role || errors.conv_id || errors.checklist_version; ui.formError.hidden = false; }
      (errors.criterion_id ? ui.select : errors.comment ? ui.comment : ui.submit).focus();
      return;
    }
    state.appealBusy = true;
    setAppealBusy(true);
    try {
      const appeal = await submitAppeal({ api: CQA.api, bus: CQA.bus }, built.body);
      state.drafts.delete(draftKey(detail.id, criterionId));
      appealState.criterionId = criterionId;
      closeAppeal({ keepDraft: false });
      notify(`Апелляция подана: ${criterionTitle(criterionId)}. Передана проверяющему.`);
      refreshAfterAppeal(detail.id, appeal).catch(() => {});
    } catch (error) {
      state.appealBusy = false;
      setAppealBusy(false); // re-enable before focusing: a disabled button cannot take focus
      ui.formError.textContent = `Апелляция не подана: ${messageOf(error)}`;
      ui.formError.hidden = false;
      ui.submit.focus();
    } finally {
      state.appealBusy = false;
      setAppealBusy(false);
    }
  }
  async function refreshAfterAppeal(convId, appeal) {
    const token = guards.history.next();
    let fresh = null;
    try { fresh = await CQA.api.conversation(convId); } catch { fresh = null; }
    if (!guards.history.isCurrent(token)) return;
    const current = CQA.state?.conversation;
    if (!current || current.id !== convId) return;
    // Shared conversation state gets the new appeal so later partial card renders keep it; score and evals stay as they are.
    if (fresh && fresh.id === convId) { current.appeals = fresh.appeals || []; current.decisions = fresh.decisions || []; current.audit = fresh.audit || []; }
    else if (!(current.appeals || []).some(item => item.id === appeal.id)) current.appeals = [...(current.appeals || []), appeal];
    renderHistory(current);
    decorateCard(current);
  }

  // ------------------------------------------------ history block in the card
  function historyTarget() {
    const slot = document.getElementById('cqa-review-slot');
    if (slot) return { slot };
    const card = document.getElementById('cqa-card');
    const results = card && (card.querySelector('#cqa-card-result') || card.querySelector('.cqa-card__results'));
    if (!results) return null;
    return { parent: results, anchor: [...results.children].find(node => node.classList.contains('cqa-card__score-summary')) || null };
  }
  function journalView(entries) {
    const wrap = el('section', 'cqa-review-journal');
    const head = el('div', 'cqa-review-journal__head');
    const title = el('h4', 'cqa-review-journal__title', 'Журнал');
    title.id = nextId('cqa-review-journal');
    wrap.setAttribute('aria-labelledby', title.id);
    head.append(title, el('span', 'cqa-review-journal__count', `${entries.length} ${plural(entries.length, ['событие', 'события', 'событий'])}`));
    const list = el('ol', 'cqa-review-timeline');
    for (const entry of entries) {
      const item = el('li', `cqa-review-timeline__item cqa-review-timeline__item--${entry.kind}`);
      const node = el('span', 'cqa-review-timeline__node');
      node.append(icon(KIND_ICONS[entry.kind] || 'dot'));
      const content = el('div', 'cqa-review-timeline__content');
      const meta = el('div', 'cqa-review-timeline__meta');
      const badge = el('span', `cqa-review-badge cqa-review-badge--${entry.kind}`, entry.title);
      const date = validDate(entry.ts);
      const time = el('time', 'cqa-review-mono cqa-review-timeline__time', date ? formatTime(entry.ts) : 'время не записано');
      if (date) time.dateTime = date.toISOString();
      meta.append(badge, time);
      content.append(meta, el('p', 'cqa-review-timeline__text', entry.text));
      item.append(node, content);
      list.append(item);
    }
    wrap.append(head, list);
    return wrap;
  }
  function layerCell(kind, labelText, value, extra) {
    const cell = el('div', `cqa-review-layer-cell cqa-review-layer-cell--${kind}`);
    cell.append(el('span', 'cqa-review-layer-cell__label', labelText), el('strong', 'cqa-review-layer-cell__value', value));
    if (extra) cell.append(el('span', 'cqa-review-layer-cell__extra', extra));
    return cell;
  }
  function chainView(entry) {
    const { criterion } = entry;
    const item = el('li', 'cqa-review-chain');
    const head = el('div', 'cqa-review-chain__head');
    head.append(el('strong', 'cqa-review-chain__title', `${criterion.id} · ${criterion.title || criterion.id}`));
    head.append(entry.open ? chip('На проверке', 'disputed', 'clock') : chip(entry.applied.length ? 'Решено' : 'Без решения', 'reviewed', 'check'));
    // Points are shown only when they add information (binary answers, or scales whose max differs from max_points).
    const points = (line, answer) => (isFlagOnly(criterion) || answer === 'na' || !line || line.points === null || (isNum(answer) && answerMax(criterion) === line.max_points) ? '' : `${formatNumber(line.points)} из ${formatNumber(line.max_points)} б.`);
    const last = entry.applied.at(-1);
    const row = el('div', 'cqa-review-layers-row');
    const arrow = () => { const svg = icon('arrow'); svg.classList.add('cqa-review-layers-row__arrow'); return svg; };
    const decisionCell = entry.open && !last
      ? layerCell('pending', 'Решение', 'Ждёт проверяющего')
      : last
        ? layerCell('decision', `Решение${formatTime(last.created_at) ? `, ${formatTime(last.created_at)}` : ''}`, last.action === 'change' ? `Изменено на ${answerLabel(criterion, normalizeAnswer(criterion, last.new_answer))}` : 'Оценка подтверждена')
        : layerCell('pending', 'Решение', 'Нет решения');
    row.append(layerCell('ai', 'Оценка ИИ', answerLabel(criterion, entry.ai), points(entry.aiLine, entry.ai)), arrow(), decisionCell, arrow(), layerCell('final', 'Финал', answerLabel(criterion, entry.final), points(entry.finalLine, entry.final)));
    item.append(head, row);
    if (last?.reason) item.append(el('p', 'cqa-review-chain__reason', `Причина: «${last.reason}»`));
    if (entry.open && last) item.append(el('p', 'cqa-review-chain__note', 'Новая апелляция по этому критерию ждёт проверяющего.'));
    if (entry.applied.length > 1) item.append(el('p', 'cqa-review-chain__note', `Решений по критерию: ${entry.applied.length}. Действует последнее, остальные в журнале.`));
    const stale = entry.decisions.length - entry.applied.length;
    if (stale > 0) item.append(el('p', 'cqa-review-chain__note', `${stale} ${plural(stale, ['решение принято', 'решения приняты', 'решений принято'])} по прежней формулировке критерия и не применяется.`));
    return item;
  }
  function historyView(detail) {
    const history = buildHistory(detail, checklistFromState());
    const section = el('section', 'cqa-review-history');
    section.dataset.cqaReviewHistory = detail.id;
    const heading = el('h3', 'cqa-review-history__title', 'История оценки');
    heading.id = nextId('cqa-review-history');
    section.setAttribute('aria-labelledby', heading.id);
    const head = el('div', 'cqa-review-history__head');
    head.append(heading);
    if (history.verified === true) {
      const ok = el('span', 'cqa-review-verified');
      ok.append(icon('check'), el('span', '', 'Итог сверен с формулой чек-листа'));
      head.append(ok);
    } else if (history.verified === false) {
      const warn = el('span', 'cqa-review-verified cqa-review-verified--warn');
      warn.append(icon('alert'), el('span', '', `Итог сервера ${formatNumber(history.finalTotal)} не совпал с пересчётом ${formatNumber(history.finalScore?.total)}`));
      head.append(warn);
    }
    const layers = el('p', 'cqa-review-layers');
    const aiLayer = el('span', 'cqa-review-layer');
    aiLayer.append(el('span', 'cqa-review-layer__label', 'Итог ИИ'), el('strong', 'cqa-review-layer__value', formatNumber(history.aiTotal)));
    const finalLayer = el('span', 'cqa-review-layer cqa-review-layer--final');
    const finalValue = el('strong', 'cqa-review-layer__value', formatNumber(history.finalTotal));
    finalValue.dataset.reviewFinal = '';
    finalLayer.append(el('span', 'cqa-review-layer__label', 'Финал'), finalValue);
    const arrow = icon('arrow');
    arrow.classList.add('cqa-review-layers__arrow');
    layers.append(aiLayer, arrow, srOnly(' → '), finalLayer);
    section.append(head, layers);
    const open = history.disputed.some(item => item.open);
    if (!history.decided) section.append(el('p', 'cqa-review-history__note', open ? 'Апелляция на проверке: пока финал равен оценке ИИ.' : 'Решений проверяющего нет: финал равен оценке ИИ. Исходная оценка ИИ хранится всегда.'));
    if (history.critical.ai !== history.critical.final) section.append(el('p', 'cqa-review-history__note', `Грубая ошибка: у ИИ ${history.critical.ai ? 'отмечена' : 'не отмечена'}, в финале ${history.critical.final ? 'отмечена' : 'не отмечена'}.`));
    if (history.disputed.length) {
      const chains = el('ul', 'cqa-review-chains');
      history.disputed.forEach(item => chains.append(chainView(item)));
      section.append(chains);
    }
    section.append(journalView(history.journal));
    return section;
  }
  function renderHistory(detail) {
    if (!detail || typeof detail !== 'object' || !detail.id) return;
    const target = historyTarget();
    document.querySelectorAll('[data-cqa-review-history]').forEach(node => { if (!target?.slot?.contains(node)) node.remove(); });
    if (!target) return;
    const block = historyView(detail);
    if (target.slot) {
      target.slot.replaceChildren(block);
      const summary = target.slot.closest('.cqa-card__history')?.querySelector('summary');
      if (summary) summary.textContent = `История решений (${(detail.decisions || []).length})`;
    }
    else if (target.anchor) target.anchor.after(block);
    else target.parent.prepend(block);
    playPendingRoll(block, detail);
  }
  function playPendingRoll(block, detail) {
    const roll = state.pendingRoll;
    if (!roll || roll.convId !== detail.id) return;
    if (!block.getClientRects().length) return; // hidden tab: play when the card is visible
    state.pendingRoll = null;
    const value = block.querySelector('[data-review-final]');
    if (!value || !isNum(roll.to) || detail.score?.total !== roll.to) return;
    value.replaceChildren(odometer(roll.from, roll.to, true), srOnly(formatNumber(roll.to)));
  }
  function decorateCard(detail) {
    const card = document.getElementById('cqa-card');
    if (!card || !detail) return;
    card.querySelectorAll('[data-cqa-review-chip]').forEach(node => node.remove());
    const grouped = decisionsByCriterion(detail, checklistFromState());
    for (const row of card.querySelectorAll('.cqa-card__criterion[data-criterion-id]')) {
      if (row.querySelector('[data-appeal-status]')) continue; // the card renders its own status chip
      const id = row.dataset.criterionId;
      const open = openAppealFor(detail, id);
      const applied = (grouped.get(id) || []).filter(item => !item.stale);
      if (!open && !applied.length) continue;
      const line = el('p', 'cqa-review-card-status');
      line.dataset.cqaReviewChip = '';
      if (open) line.append(chip('На проверке', 'disputed', 'clock'), el('span', 'cqa-review-card-status__text', `апелляция оператора${open.created_at ? `, ${formatTime(open.created_at)}` : ''}`));
      else line.append(chip(applied.at(-1).action === 'change' ? 'Изменено проверяющим' : 'Подтверждено проверяющим', 'reviewed', 'check'));
      const heading = row.querySelector('.cqa-card__criterion-heading');
      if (heading) heading.after(line); else row.prepend(line);
    }
  }

  // ------------------------------------------------ odometer
  function odometer(from, to, animate) {
    const wrap = el('span', 'cqa-review-odometer');
    wrap.setAttribute('aria-hidden', 'true');
    const motion = animate && !reduced() && isNum(from) && isNum(to) && from !== to;
    const reels = [];
    for (const column of odometerColumns(from, to)) {
      if (column.char !== undefined) { wrap.append(el('span', 'cqa-review-odometer__char', column.char)); continue; }
      const slot = el('span', 'cqa-review-odometer__slot');
      const reel = el('span', 'cqa-review-odometer__reel');
      for (let digit = 0; digit <= 9; digit++) reel.append(el('span', '', digit));
      reel.style.setProperty('--cqa-digit', String(motion ? column.from : column.digit));
      slot.append(reel);
      wrap.append(slot);
      reels.push({ reel, column });
    }
    if (motion) {
      requestAnimationFrame(() => requestAnimationFrame(() => {
        reels.forEach(({ reel, column }, index) => {
          reel.style.setProperty('--cqa-delay', `${(reels.length - 1 - index) * 30}ms`);
          reel.classList.add('is-rolling');
          reel.style.setProperty('--cqa-digit', String(column.digit));
        });
      }));
    }
    return wrap;
  }

  // ------------------------------------------------ reviewer panel
  const host = document.getElementById('cqa-review');
  let panel = null;
  function buildPanel() {
    const section = el('section', 'cqa-review');
    const title = el('h1', 'cqa-review__title');
    title.id = nextId('cqa-review-title');
    section.setAttribute('aria-labelledby', title.id);
    const count = el('span', 'cqa-review__count');
    title.append(el('span', '', 'На проверке'), count);
    const head = el('header', 'cqa-review__head');
    const titles = el('div', 'cqa-review__titles');
    titles.append(title, el('p', 'cqa-review__lead', 'Апелляции операторов. Проверяющий подтверждает оценку ИИ или меняет её с обязательной причиной, итог пересчитывается по формуле чек-листа.'));
    const refresh = button('Обновить', '', () => loadQueue({ announce: true }).catch(fail));
    refresh.prepend(icon('refresh'));
    head.append(titles, refresh);
    const roleNote = el('p', 'cqa-review__role-note');
    roleNote.append(icon('user'), el('span', '', 'Сейчас роль «Оператор»: очередь видна, а решения принимает проверяющий. Переключите роль в шапке.'));
    const layout = el('div', 'cqa-review__layout');
    const queue = el('nav', 'cqa-review__queue');
    queue.setAttribute('aria-label', 'Очередь апелляций');
    const queueStatus = el('div', 'cqa-review-queue-status');
    const queueList = el('ul', 'cqa-review-queue');
    const queueKeys = el('p', 'cqa-review-keys');
    queueKeys.append(kbd('J'), kbd('K'), el('span', '', 'следующая и предыдущая'));
    queue.append(queueStatus, queueList, queueKeys);
    const detailHost = el('div', 'cqa-review__detail');
    layout.append(queue, detailHost);
    const live = el('p', 'cqa-review-sr');
    live.setAttribute('role', 'status');
    live.setAttribute('aria-live', 'polite');
    section.append(head, roleNote, layout, live);
    return { section, count, roleNote, queueStatus, queueList, queueKeys, detailHost, live, dossierHost: null, decisionHost: null };
  }
  function panelVisible() { return Boolean(panel?.section.isConnected && panel.section.getClientRects().length); }
  function updateTabBadge(count) {
    const tab = document.getElementById('cqa-tab-review');
    if (!tab) return;
    let badge = tab.querySelector('[data-review-count]');
    if (!badge) {
      badge = el('span', 'cqa-review-tab-count');
      badge.dataset.reviewCount = '';
      tab.append(document.createTextNode(' '), badge);
    }
    badge.textContent = count === null ? '' : String(count);
    badge.hidden = !count;
  }
  function renderQueue() {
    if (!panel) { updateTabBadge(state.queueLoaded ? state.queue.length : null); return; }
    const ready = state.queueLoaded;
    panel.count.textContent = ready ? ` (${state.queue.length})` : '';
    updateTabBadge(ready ? state.queue.length : null);
    panel.queueStatus.replaceChildren();
    panel.queueKeys.hidden = state.queue.length < 2;
    if (state.queueStatus === 'loading' && !state.queueLoaded) {
      const skeleton = el('div', 'cqa-review-skeleton');
      skeleton.setAttribute('aria-hidden', 'true');
      for (let i = 0; i < 3; i++) skeleton.append(el('span', 'cqa-review-skeleton__card'));
      panel.queueStatus.append(skeleton, srOnly('Загружаем очередь апелляций'));
    } else if (state.queueStatus === 'error') {
      const box = el('div', 'cqa-review-queue-error');
      box.append(el('p', 'cqa-review-error', `Очередь не загрузилась: ${state.queueError}`), button('Повторить', '', () => loadQueue().catch(fail)));
      panel.queueStatus.append(box);
    } else if (state.queueLoaded && !state.queue.length) {
      panel.queueStatus.append(el('p', 'cqa-review-empty', 'Открытых апелляций нет. Когда оператор оспорит оценку в карточке диалога, обращение появится здесь.'));
    }
    if (state.view?.result) renderResult(false);
    panel.queueList.replaceChildren(...state.queue.map(appeal => {
      const item = el('li');
      const node = el('button', 'cqa-review-item');
      node.type = 'button';
      node.dataset.appealId = appeal.id;
      if (state.selectedId === appeal.id) node.setAttribute('aria-current', 'true');
      const top = el('span', 'cqa-review-item__top');
      top.append(el('strong', 'cqa-review-mono', appeal.conv_id), el('span', 'cqa-review-item__time', relativeTime(appeal.created_at)));
      node.append(top, el('span', 'cqa-review-item__criterion', criterionTitle(appeal.criterion_id)), el('span', 'cqa-review-item__comment', excerpt(appeal.comment, 110)));
      node.addEventListener('click', () => selectAppeal(appeal, { focus: true }));
      item.append(node);
      return item;
    }));
  }
  async function loadQueue({ announce = false, autoselect = false } = {}) {
    const token = guards.queue.next();
    state.queueStatus = 'loading';
    renderQueue();
    let rows;
    try { rows = await CQA.api.appeals({ status: 'open' }); }
    catch (error) {
      if (!guards.queue.isCurrent(token)) return;
      state.queueStatus = 'error';
      state.queueError = messageOf(error);
      renderQueue();
      return;
    }
    if (!guards.queue.isCurrent(token)) return;
    const list = Array.isArray(rows) ? rows : Array.isArray(rows?.appeals) ? rows.appeals : Array.isArray(rows?.items) ? rows.items : [];
    state.queue = sortByTime(list.filter(item => item?.id && (item.status || 'open') === 'open')).reverse();
    state.queueStatus = 'ready';
    state.queueLoaded = true;
    harvestTitles();
    renderQueue();
    if (announce) say(`Очередь обновлена: ${state.queue.length} ${plural(state.queue.length, ['апелляция', 'апелляции', 'апелляций'])}.`);
    if ((autoselect || panelVisible()) && !state.selectedId && state.queue.length && panel) selectAppeal(state.queue[0], { focus: false });
  }
  const caseLoader = createCaseLoader(CQA.api, {
    onDetail(appeal, detail) {
      harvestTitles(detail);
      state.view = { status: 'ready', appeal, detail, dossier: { status: 'loading' }, result: null };
      renderCase();
    },
    onDossier(appeal, dossier) {
      if (state.view?.appeal?.id !== appeal.id) return;
      state.view.dossier = { status: 'ready', data: dossier };
      renderDossier();
    },
    onDossierError(appeal, error) {
      if (state.view?.appeal?.id !== appeal.id) return;
      state.view.dossier = { status: 'error', error: messageOf(error) };
      renderDossier();
    },
    onError(appeal, error) {
      state.view = { status: 'error', appeal, error: messageOf(error) };
      renderCase();
    },
  });
  function selectAppeal(appeal, { focus = false } = {}) {
    if (!panel || !appeal?.id) return;
    state.selectedId = appeal.id;
    state.focusCase = focus;
    state.view = { status: 'loading', appeal };
    decisionControl = null;
    renderQueue();
    renderCase();
    caseLoader.load(appeal).catch(fail);
  }
  function moveSelection(step) {
    if (!state.queue.length) return;
    const index = state.queue.findIndex(item => item.id === state.selectedId);
    const next = index < 0 ? (step > 0 ? 0 : state.queue.length - 1) : Math.max(0, Math.min(state.queue.length - 1, index + step));
    if (index === next && index >= 0) return;
    selectAppeal(state.queue[next], { focus: true });
    panel.queueList.querySelector(`[data-appeal-id="${cssEscape(state.queue[next].id)}"]`)?.scrollIntoView({ block: 'nearest' });
  }
  function block(titleText, iconName, content, modifier) {
    const section = el('section', `cqa-review-block${modifier ? ` cqa-review-block--${modifier}` : ''}`);
    const heading = el('h3', 'cqa-review-block__title');
    heading.id = nextId('cqa-review-block');
    heading.append(icon(iconName), el('span', '', titleText));
    section.setAttribute('aria-labelledby', heading.id);
    section.append(heading, content);
    return section;
  }
  function fragmentView(detail, appeal, criterion) {
    const evaluation = appealEvaluation(detail, appeal, checklistFromState());
    const marked = new Set((appeal.message_ids || []).filter(Boolean));
    const evidenceIds = new Set((evaluation?.evidence || []).map(item => item?.message_id).filter(Boolean));
    let focus = [...marked, ...evidenceIds];
    let note = '';
    if (!focus.length && Array.isArray(evaluation?.checked_range)) { focus = evaluation.checked_range.filter(Boolean); note = 'Оператор не отметил реплики, у ИИ нет цитаты: показан проверенный участок.'; }
    let rows = fragmentRows(detail.messages, focus, 2);
    const messages = detail.messages || [];
    if (!rows.length) {
      rows = messages.slice(0, 6).map((message, index) => ({ index, message }));
      if (messages.length > 6) rows.push({ gap: messages.length - 6 });
      note = 'Реплики не отмечены: показано начало диалога.';
    }
    const wrap = el('div', 'cqa-review-fragment');
    if (note) wrap.append(el('p', 'cqa-review-hint', note));
    const list = el('ul', 'cqa-review-fragment__list');
    for (const row of rows) {
      if (row.gap) { list.append(el('li', 'cqa-review-fragment__gap', `скрыто ${row.gap} ${plural(row.gap, ['реплика', 'реплики', 'реплик'])}`)); continue; }
      const message = row.message;
      const speaker = ROLE_LABELS[message.speaker] ? message.speaker : 'unknown';
      const isMarked = marked.has(message.id), isEvidence = evidenceIds.has(message.id);
      const item = el('li', `cqa-review-line cqa-review-line--${speaker}${isMarked ? ' is-marked' : ''}${isEvidence ? ' is-evidence' : ''}`);
      const head = el('div', 'cqa-review-line__head');
      head.append(el('span', 'cqa-review-mono cqa-review-line__number', `#${row.index + 1}`), el('strong', '', `${ROLE_LABELS[message.speaker] || 'Участник'}${speaker === 'bot' || speaker === 'system' ? ', контекст' : ''}`));
      if (messageTime(message)) head.append(el('span', 'cqa-review-mono cqa-review-line__time', messageTime(message)));
      if (isMarked || isEvidence) {
        const tags = el('span', 'cqa-review-line__tags');
        if (isMarked) tags.append(chip('Отмечено оператором', 'disputed', 'user'));
        if (isEvidence) tags.append(chip('Доказательство ИИ', 'ai', 'sparkles'));
        head.append(tags);
      }
      const text = el('p', 'cqa-review-line__text');
      const slices = (evaluation?.evidence || []).filter(item => item?.message_id === message.id).map(item => checkQuote(item, messages)).filter(item => item.valid).sort((a, b) => a.start - b.start);
      appendHighlighted(text, String(message.text ?? ''), slices);
      item.append(head, text);
      list.append(item);
    }
    wrap.append(list);
    return wrap;
  }
  function renderCase() {
    if (!panel) return;
    const target = panel.detailHost;
    target.replaceChildren();
    panel.dossierHost = null;
    panel.decisionHost = null;
    decisionControl = null;
    const view = state.view;
    if (!view) {
      const empty = el('div', 'cqa-review-placeholder');
      empty.append(icon('gavel'), el('p', '', state.queue.length ? 'Выберите апелляцию в очереди.' : 'Здесь появятся спорная оценка, комментарий оператора, фрагмент диалога и досье ИИ.'));
      target.append(empty);
      return;
    }
    if (view.status === 'loading') {
      target.setAttribute('aria-busy', 'true');
      const skeleton = el('div', 'cqa-review-skeleton cqa-review-skeleton--case');
      skeleton.setAttribute('aria-hidden', 'true');
      for (let i = 0; i < 6; i++) skeleton.append(el('span', 'cqa-review-skeleton__line'));
      target.append(skeleton, srOnly('Загружаем апелляцию'));
      return;
    }
    target.removeAttribute('aria-busy');
    if (view.status === 'error') {
      const box = el('div', 'cqa-review-queue-error');
      box.append(el('p', 'cqa-review-error', `Апелляция не открылась: ${view.error}`), button('Повторить', '', () => selectAppeal(view.appeal, { focus: true })));
      target.append(box);
      return;
    }
    const { appeal, detail } = view;
    const checklist = checklistFromState();
    const criterion = criterionOf(detail, appeal.criterion_id, checklist);
    const history = buildHistory(detail, checklist);
    const article = el('article', 'cqa-review-case');
    const header = el('header', 'cqa-review-case__header');
    const meta = el('p', 'cqa-review-case__meta');
    meta.append(el('span', 'cqa-review-mono', detail.id), el('span', '', `${detail.channel === 'call' ? 'Звонок' : 'Чат'}, ${detail.language || 'язык не указан'}`));
    if (appeal.created_at) meta.append(el('span', '', `апелляция подана в ${formatTime(appeal.created_at)}`));
    const title = el('h2', 'cqa-review-case__title', `${criterion.id} · ${criterion.title || criterion.id}`);
    title.id = nextId('cqa-review-case');
    title.tabIndex = -1;
    article.setAttribute('aria-labelledby', title.id);
    const openButton = button('Открыть диалог', 'cqa-review-case__open', () => CQA.bus.emit('conv:open', detail.id));
    openButton.append(icon('open'));
    const titleRow = el('div', 'cqa-review-case__title-row');
    titleRow.append(title, openButton);
    header.append(meta, titleRow);
    const grid = el('div', 'cqa-review-case__grid');
    const main = el('div', 'cqa-review-case__main');
    const aside = el('div', 'cqa-review-case__aside');
    const comment = el('p', 'cqa-review-comment', appeal.comment || 'Комментарий не указан.');
    main.append(block('Спорная оценка ИИ', 'sparkles', assessmentView(detail, criterion, history, appeal)), block('Комментарий оператора', 'message', comment, 'comment'), block('Фрагмент диалога', 'message', fragmentView(detail, appeal, criterion)));
    panel.dossierHost = el('div', 'cqa-review-dossier');
    aside.append(block('Досье ИИ', 'file', panel.dossierHost, 'dossier'));
    panel.decisionHost = el('div', 'cqa-review-decision-host');
    grid.append(main, aside);
    article.append(header, grid, panel.decisionHost);
    target.append(article);
    renderDossier();
    if (view.result) renderResult(false); else renderDecision();
    if (state.focusCase) { state.focusCase = false; title.focus(); }
  }
  function argumentsView(titleText, items, tone, detail) {
    const wrap = el('div', `cqa-review-arguments cqa-review-arguments--${tone}`);
    const heading = el('h4', 'cqa-review-arguments__title');
    heading.append(icon(tone === 'pro' ? 'check' : 'alert'), el('span', '', titleText));
    wrap.append(heading);
    if (!items.length) { wrap.append(el('p', 'cqa-review-hint', 'ИИ не привёл доводов.')); return wrap; }
    const list = el('ul');
    for (const item of items) {
      const li = el('li');
      if (item.text) li.append(el('p', '', item.text));
      for (const quote of item.quotes) {
        li.append(el('blockquote', '', quote.quote));
        const checked = checkQuote(quote, detail.messages);
        if (!checked.valid) li.append(el('p', 'cqa-review-error', `Цитата не подтверждена: ${checked.reason}.`));
        else {
          const index = detail.messages.findIndex(message => message.id === quote.message_id);
          li.append(el('p', 'cqa-review-hint', `Текст сверён · #${index + 1}, ${ROLE_LABELS[checked.message.speaker] || 'участник'}`));
        }
      }
      list.append(li);
    }
    wrap.append(list);
    return wrap;
  }
  function renderDossier() {
    const target = panel?.dossierHost;
    const view = state.view;
    if (!target || !view?.detail) return;
    target.replaceChildren();
    const human = el('p', 'cqa-review-human');
    human.append(icon('user'), el('span', '', 'Решает человек. Досье — подсказка ИИ, а не решение.'));
    target.append(human);
    const dossier = view.dossier || { status: 'loading' };
    if (dossier.status === 'loading') {
      target.setAttribute('aria-busy', 'true');
      const skeleton = el('div', 'cqa-review-skeleton cqa-review-skeleton--dossier');
      skeleton.setAttribute('aria-hidden', 'true');
      for (let i = 0; i < 6; i++) skeleton.append(el('span', 'cqa-review-skeleton__line'));
      target.append(skeleton, srOnly('Готовим досье'));
      return;
    }
    target.removeAttribute('aria-busy');
    const retry = () => {
      const appeal = view.appeal;
      view.dossier = { status: 'loading' };
      renderDossier();
      Promise.resolve().then(() => CQA.api.dossier(appeal.id)).then(data => {
        if (state.view?.appeal?.id !== appeal.id) return;
        state.view.dossier = { status: 'ready', data };
        renderDossier();
      }, error => {
        if (state.view?.appeal?.id !== appeal.id) return;
        state.view.dossier = { status: 'error', error: messageOf(error) };
        renderDossier();
      });
    };
    if (dossier.status === 'error') { target.append(el('p', 'cqa-review-error', `Досье не загрузилось: ${dossier.error}. Решение можно принять и без него.`), button('Повторить', '', retry)); return; }
    const data = normalizeDossier(dossier.data);
    if (data.pending) { target.append(el('p', 'cqa-review-hint', 'Досье ещё готовится. Решение можно принять и без него.'), button('Обновить досье', '', retry)); return; }
    if (data.is_mock) target.append(el('p', 'cqa-review-mock-note', 'Демо-досье: синтетические доводы, не ответ модели.'));
    const columns = el('div', 'cqa-review-arguments-grid');
    columns.append(argumentsView('В пользу апелляции', data.pro, 'pro', view.detail), argumentsView('В пользу исходной оценки', data.contra, 'contra', view.detail));
    target.append(columns);
    if (data.recommendation) {
      const criterion = criterionOf(view.detail, view.appeal.criterion_id, checklistFromState());
      const rec = data.recommendation;
      const action = rec.action === 'change' && rec.new_answer !== undefined && rec.new_answer !== null ? `изменить на ${answerLabel(criterion, normalizeAnswer(criterion, rec.new_answer))}` : rec.action === 'confirm' ? 'подтвердить оценку' : '';
      const box = el('div', 'cqa-review-recommendation');
      box.append(el('strong', '', `Рекомендация ИИ${action ? `: ${action}` : ''}${isNum(rec.confidence) ? `, уверенность ${Math.round(rec.confidence * 100)}%` : ''}`));
      if (rec.text) box.append(el('p', '', rec.text));
      target.append(box);
    }
  }
  function answerControl(criterion, current, draft) {
    const wrap = el('fieldset', 'cqa-review-choice');
    const legend = el('legend', 'cqa-review-label', 'Новый ответ для «Изменить»');
    const hint = el('p', 'cqa-review-hint');
    hint.id = nextId('cqa-review-answer-hint');
    const error = el('p', 'cqa-review-error');
    error.id = nextId('cqa-review-answer-error');
    error.hidden = true;
    wrap.setAttribute('aria-describedby', `${hint.id} ${error.id}`);
    wrap.append(legend, hint);
    let read, focus;
    const type = criterion.answer_type || 'binary';
    if (type === 'numeric') {
      const { min, max, step } = numericRange(criterion);
      hint.textContent = `Число ${max === null ? `не меньше ${formatNumber(min)}` : `от ${formatNumber(min)} до ${formatNumber(max)}`}${step ? `, шаг ${formatNumber(step)}` : ''}. Сейчас ${answerLabel(criterion, current)}.`;
      const input = el('input', 'cqa-review-input cqa-review-input--number');
      input.type = 'number';
      input.inputMode = 'decimal';
      input.min = String(min);
      if (max !== null) input.max = String(max);
      input.step = step ? String(step) : 'any';
      input.id = nextId('cqa-review-number');
      const label = el('label', 'cqa-review-label', 'Значение');
      label.htmlFor = input.id;
      const naBox = criterion.na_allowed ? el('input') : null;
      wrap.append(label, input);
      if (naBox) {
        naBox.type = 'checkbox';
        const naLabel = el('label', 'cqa-review-choice__option cqa-review-choice__option--inline');
        naLabel.append(naBox, el('span', '', `N/A${criterion.na_rule ? `: ${criterion.na_rule.replace(/^N\/A\s*[—-]\s*/i, '')}` : ''}`));
        naBox.addEventListener('change', () => { input.disabled = naBox.checked; });
        wrap.append(naLabel);
      }
      if (draft.answer === 'na' && naBox) { naBox.checked = true; input.disabled = true; } else if (draft.answer !== null && draft.answer !== undefined) input.value = String(draft.answer);
      read = () => (naBox?.checked ? 'na' : input.value.trim() === '' ? null : input.value.trim());
      focus = () => (naBox?.checked ? naBox : input).focus();
    } else {
      hint.textContent = type === 'scale' ? `Только ступени шкалы чек-листа. Сейчас ${answerLabel(criterion, current)}.` : isFlagOnly(criterion) ? `Флаг без баллов: «${ANSWER_LABELS[violationAnswer(criterion)]}» значит, что нарушение есть.` : `Сейчас ${answerLabel(criterion, current)}.`;
      const name = nextId('cqa-review-answer');
      const grid = el('div', 'cqa-review-choice__grid');
      for (const value of answerOptions(criterion)) {
        const option = el('label', 'cqa-review-choice__option');
        const input = el('input');
        input.type = 'radio';
        input.name = name;
        input.value = String(value);
        const isCurrent = sameAnswer(value, current);
        input.disabled = isCurrent;
        input.checked = !isCurrent && draft.answer !== null && draft.answer !== undefined && String(draft.answer) === String(value);
        const text = el('span', 'cqa-review-choice__text');
        text.append(el('span', 'cqa-review-choice__title', answerLabel(criterion, value)));
        const subs = [];
        if (isCurrent) subs.push('текущий ответ');
        if (value === 'na' && criterion.na_rule) subs.push(criterion.na_rule.replace(/^N\/A\s*[—-]\s*/i, ''));
        else if (isNum(value)) {
          const labelText = criterion.scale_labels?.[String(value)];
          const max = answerMax(criterion);
          const maxPoints = Number(criterion.max_points);
          if (max && Number.isFinite(maxPoints) && max !== maxPoints) subs.push(`${formatNumber(value / max * maxPoints)} б.`);
          if (labelText) subs.push(labelText);
        }
        if (subs.length) text.append(el('span', 'cqa-review-choice__sub', subs.join('. ')));
        option.append(input, text);
        grid.append(option);
      }
      wrap.append(grid);
      read = () => grid.querySelector('input:checked')?.value ?? null;
      focus = () => (grid.querySelector('input:checked') || grid.querySelector('input:not(:disabled)'))?.focus();
    }
    wrap.append(error);
    const setError = message => {
      error.replaceChildren();
      error.hidden = !message;
      if (message) { error.append(icon('alert'), el('span', '', message)); wrap.setAttribute('aria-invalid', 'true'); } else wrap.removeAttribute('aria-invalid');
    };
    return { wrap, read, focus, setError };
  }
  function decideButton(action, text, key, iconName) {
    const node = el('button', `cqa-review-button cqa-review-decide cqa-review-decide--${action}`);
    node.type = 'button';
    node.dataset.action = action;
    node.setAttribute('aria-keyshortcuts', key);
    const label = el('span', 'cqa-review-decide__label', text);
    node.append(icon(iconName), label, kbd(key));
    return { node, label };
  }
  function renderDecision() {
    const target = panel?.decisionHost;
    const view = state.view;
    decisionControl = null;
    if (!target || view?.status !== 'ready') return;
    target.replaceChildren();
    const { appeal, detail } = view;
    const checklist = checklistFromState();
    const criterion = criterionOf(detail, appeal.criterion_id, checklist);
    const decidedAlready = (detail.decisions || []).some(item => item?.appeal_id === appeal.id) || (appeal.status || 'open') !== 'open';
    if (role() !== 'reviewer') {
      const note = el('div', 'cqa-review-role-gate');
      note.append(icon('user'), el('p', '', 'Решение принимает проверяющий. Переключите роль на «Проверяющий» в шапке, чтобы подтвердить или изменить оценку.'));
      target.append(note);
      return;
    }
    if (decidedAlready) { target.append(el('p', 'cqa-review-hint cqa-review-decision-done', 'По этой апелляции решение уже принято. Откройте карточку диалога, чтобы увидеть историю оценки.')); return; }
    const current = appealConfirmAnswer(detail, appeal, checklist);
    const draft = state.decisionDrafts.get(appeal.id) || { answer: null, reason: '' };
    const form = el('form', 'cqa-review-decision');
    form.noValidate = true;
    const heading = el('h3', 'cqa-review-decision__title');
    heading.id = nextId('cqa-review-decision');
    heading.append(icon('gavel'), el('span', '', 'Решение проверяющего'));
    form.setAttribute('aria-labelledby', heading.id);
    const fields = el('div', 'cqa-review-decision__fields');
    const answer = answerControl(criterion, current, draft);
    const reason = el('textarea', 'cqa-review-textarea');
    reason.rows = 3;
    reason.maxLength = REASON_LIMIT;
    reason.value = draft.reason || '';
    const reasonField = makeField('Причина решения', reason, { hint: 'Обязательна для обоих решений: попадёт в карточку и журнал.', required: true });
    fields.append(answer.wrap, reasonField.wrap);
    const preview = el('p', 'cqa-review-preview');
    preview.setAttribute('aria-live', 'polite');
    const formError = el('p', 'cqa-review-form-error');
    formError.setAttribute('role', 'alert');
    formError.hidden = true;
    const actions = el('div', 'cqa-review-decision__actions');
    const confirm = decideButton('confirm', `Подтвердить: ${answerLabel(criterion, current)}`, 'Y', 'check');
    const change = decideButton('change', 'Изменить', 'N', 'pencil');
    actions.append(confirm.node, change.node);
    form.append(heading, fields, preview, formError, actions);
    const read = () => ({ answer: answer.read(), reason: reason.value });
    const update = () => {
      const values = read();
      state.decisionDrafts.set(appeal.id, values);
      const check = validateAnswer(criterion, values.answer);
      const ready = check.ok && !sameAnswer(check.value, current);
      change.label.textContent = ready ? `Изменить на ${answerLabel(criterion, check.value)}` : 'Изменить';
      preview.replaceChildren();
      if (!ready) {
        const { from, to } = previewDecision(detail, criterion.id, current, checklist);
        preview.append(el('span', '', sameAnswer(from, to)
          ? `Подтверждение оставит итог ${formatNumber(from)}. Для изменения выберите новый ответ.`
          : `Подтверждение исходной оценки: ${formatNumber(from)} → ${formatNumber(to)}. Предварительный расчёт по формуле чек-листа.`));
        return;
      }
      const { from, to } = previewDecision(detail, criterion.id, check.value, checklist);
      preview.append(el('span', '', 'Итог после изменения: '), el('strong', '', formatNumber(from)), icon('arrow'), srOnly(' станет '), el('strong', '', formatNumber(to)), el('span', 'cqa-review-preview__note', ', предварительный расчёт по формуле чек-листа'));
    };
    form.addEventListener('change', event => { if (answer.wrap.contains(event.target)) answer.setError(''); update(); });
    form.addEventListener('input', event => {
      if (event.target === reason && reason.value.trim()) reasonField.setError('');
      if (answer.wrap.contains(event.target)) answer.setError('');
      update();
    });
    form.addEventListener('submit', event => event.preventDefault());
    const controls = {
      read,
      showErrors(errors, action) {
        answer.setError(action === 'change' ? errors.answer || '' : '');
        reasonField.setError(errors.reason || '');
        if (action === 'change' && errors.answer) answer.focus();
        else if (errors.reason) reason.focus();
      },
      formError(message) { formError.textContent = message; formError.hidden = !message; },
      setBusy(busy, action) {
        for (const item of [confirm, change]) { item.node.disabled = busy; item.node.setAttribute('aria-busy', String(busy && item.node.dataset.action === action)); }
        if (busy) (action === 'change' ? change : confirm).label.textContent = 'Сохраняем…';
        else { confirm.label.textContent = `Подтвердить: ${answerLabel(criterion, current)}`; update(); }
      },
      connected: () => form.isConnected,
      focusAction: action => (action === 'change' ? change : confirm).node.focus(),
    };
    const decide = action => submitDecisionForm(action, { appeal, detail, criterion, current, checklist, form: controls }).catch(fail);
    confirm.node.addEventListener('click', () => decide('confirm'));
    change.node.addEventListener('click', () => decide('change'));
    decisionControl = { decide, connected: controls.connected };
    update();
    target.append(form);
  }
  async function submitDecisionForm(action, { appeal, detail, criterion, current, checklist, form }) {
    if (state.decisionBusy) return;
    form.formError('');
    if (role() !== 'reviewer') { form.formError('Решение принимает проверяющий. Переключите роль на «Проверяющий».'); return; }
    const values = form.read();
    const built = buildDecisionBody({ appeal_id: appeal.id, action, answer: values.answer, reason: values.reason }, criterion, current);
    if (!built.ok) { form.showErrors(built.errors, action); return; }
    form.showErrors({}, action);
    state.decisionBusy = true;
    form.setBusy(true, action);
    try {
      const outcome = await submitDecision({ api: CQA.api, bus: CQA.bus }, built.body, { conv_id: detail.id, criterion_id: criterion.id, before: detail, checklist });
      state.decisionDrafts.delete(appeal.id);
      state.pendingRoll = { convId: detail.id, from: outcome.from, to: outcome.to };
      const result = { from: outcome.from, to: outcome.to, source: outcome.source, decision: outcome.decision, criterion, appeal, convId: detail.id, previous: current };
      if (state.view?.appeal?.id === appeal.id && state.view.status === 'ready') {
        state.view.result = result;
        if (outcome.fresh) state.view.detail = outcome.fresh;
        renderResult(true);
      } else notify(`Решение сохранено: ${detail.id}, итог ${formatNumber(outcome.from)} → ${formatNumber(outcome.to)}.`);
      say(`Решение сохранено. Итог: было ${formatNumber(outcome.from)}, стало ${formatNumber(outcome.to)}.`);
    } catch (error) {
      state.decisionBusy = false;
      if (form.connected()) { form.setBusy(false, action); form.formError(`Решение не сохранено: ${messageOf(error)}`); form.focusAction(action); }
      else notify(`Решение не сохранено: ${messageOf(error)}`);
    } finally {
      state.decisionBusy = false;
      if (form.connected()) form.setBusy(false, action);
    }
  }
  function renderResult(animate) {
    const target = panel?.decisionHost;
    const result = state.view?.result;
    decisionControl = null;
    if (!target || !result) return;
    target.replaceChildren();
    const box = el('section', 'cqa-review-result');
    box.tabIndex = -1;
    const heading = el('h3', 'cqa-review-result__title');
    heading.id = nextId('cqa-review-result');
    heading.append(icon('check'), el('span', '', 'Решение сохранено'));
    box.setAttribute('aria-labelledby', heading.id);
    const line = el('p', 'cqa-review-result__line');
    const arrow = icon('arrow');
    arrow.classList.add('cqa-review-result__arrow');
    line.append(el('span', 'cqa-review-result__label', 'Итог диалога'), el('span', 'cqa-review-result__from', formatNumber(result.from)), arrow);
    const to = el('span', 'cqa-review-result__to');
    to.append(odometer(result.from, result.to, animate));
    line.append(to, srOnly(`Итог: было ${formatNumber(result.from)}, стало ${formatNumber(result.to)}.`));
    const { criterion, decision } = result;
    const next = normalizeAnswer(criterion, decision.new_answer);
    const what = decision.action === 'change'
      ? `${criterion.id}: ${answerLabel(criterion, normalizeAnswer(criterion, decision.old_answer) ?? result.previous)} → ${answerLabel(criterion, next)}.`
      : `${criterion.id}: оценка подтверждена.`;
    const summary = el('p', 'cqa-review-result__what', `${what} Причина: «${decision.reason || ''}»`);
    const source = el('p', 'cqa-review-hint', result.source === 'api' ? 'Числа из ответа сервера после пересчёта. Исходная оценка ИИ сохранена в карточке.' : 'Сервер не вернул итоги: значения пересчитаны по формуле чек-листа.');
    const actions = el('div', 'cqa-review-result__actions');
    const open = button('Открыть карточку диалога', 'cqa-review-button--primary', () => CQA.bus.emit('conv:open', result.convId));
    const nextButton = button('Следующая апелляция', '', () => {
      const nextAppeal = state.queue.find(item => item.id !== result.appeal.id);
      if (nextAppeal) selectAppeal(nextAppeal, { focus: true }); else say('Других апелляций в очереди нет.');
    });
    nextButton.append(kbd('J'));
    const remaining = state.queue.filter(item => item.id !== result.appeal.id).length;
    nextButton.hidden = !remaining;
    actions.append(open, nextButton);
    box.append(heading, line, summary, source, actions);
    target.append(box);
    if (animate) box.focus({ preventScroll: true });
  }
  function renderRole() {
    if (!panel) return;
    panel.roleNote.hidden = role() === 'reviewer';
    if (state.view?.status === 'ready') { if (state.view.result) renderResult(false); else renderDecision(); }
  }
  function cardVisible() { const card = document.getElementById('cqa-card'); return Boolean(card && card.getClientRects().length); }
  function appealFromKeyboard() {
    const detail = CQA.state?.conversation;
    if (!detail?.id || !cardVisible()) return;
    const card = document.getElementById('cqa-card');
    const activeRow = document.activeElement?.closest?.('.cqa-card__criterion[data-criterion-id]');
    const focusedRow = card.querySelector('.cqa-card__criterion.is-criterion-focus[data-criterion-id]');
    const criterionId = activeRow?.dataset.criterionId || focusedRow?.dataset.criterionId || (state.focusedCriterion?.conv_id === detail.id ? state.focusedCriterion.criterion_id : null);
    openAppeal({ conv_id: detail.id, criterion_id: criterionId }, appealButtonFor(criterionId) || document.activeElement).catch(fail);
  }
  function onKeydown(event) {
    if (event.defaultPrevented || event.altKey || event.ctrlKey || event.metaKey || event.repeat || isTextEntry(event.target) || ui?.dialog.open) return;
    const code = event.code;
    if (role() === 'reviewer' && panelVisible()) {
      if ((code === HOTKEYS.confirm || code === HOTKEYS.change) && decisionControl?.connected()) { event.preventDefault(); decisionControl.decide(code === HOTKEYS.confirm ? 'confirm' : 'change'); return; }
      if (code === HOTKEYS.next || code === HOTKEYS.previous) { event.preventDefault(); moveSelection(code === HOTKEYS.next ? 1 : -1); return; }
    }
    if (role() === 'operator' && code === HOTKEYS.appeal && cardVisible() && !panelVisible()) { event.preventDefault(); appealFromKeyboard(); }
  }

  // ------------------------------------------------ wiring
  if (host) {
    panel = buildPanel();
    host.classList.remove('cqa-module-placeholder');
    host.classList.add('cqa-review-host');
    host.replaceChildren(panel.section);
    renderRole();
    renderCase();
  }
  document.addEventListener('keydown', onKeydown);
  on('appeal:start', payload => { const opener = document.activeElement; openAppeal(payload, opener).catch(fail); });
  on('criterion:focus', payload => { if (payload?.conv_id && payload?.criterion_id) state.focusedCriterion = payload; });
  on('conv:open', id => {
    guards.history.invalidate();
    if (state.pendingRoll && state.pendingRoll.convId !== id) state.pendingRoll = null;
  });
  on('conv:loaded', detail => { harvestTitles(detail); renderHistory(detail); decorateCard(detail); });
  on('score:updated', payload => {
    if (document.getElementById('cqa-card')) return; // the card re-renders and emits conv:loaded
    if (payload?.id && Array.isArray(payload.messages)) renderHistory(payload);
  });
  on('role:changed', () => {
    if (role() !== 'operator' && ui?.dialog.open) closeAppeal({ keepDraft: true });
    renderRole();
  });
  on('appeal:created', () => { loadQueue().catch(fail); });
  on('decision:saved', () => { loadQueue().catch(fail); });
  on('tab:changed', tab => { if (tab === 'review') loadQueue({ autoselect: true }).catch(fail); });
  const current = CQA.state.conversation || CQA.state.currentConversation;
  if (current?.id) { harvestTitles(current); renderHistory(current); decorateCard(current); }
  loadQueue({ autoselect: false }).catch(fail);

  const destroy = () => {
    offs.forEach(off => off());
    document.removeEventListener('keydown', onKeydown);
    clearTimeout(closeTimer);
    caseLoader.cancel();
    guards.queue.invalidate(); guards.history.invalidate(); guards.form.invalidate();
    if (ui?.dialog.open) { if (typeof ui.dialog.close === 'function') ui.dialog.close(); else ui.dialog.removeAttribute('open'); }
    ui?.dialog.remove();
    document.querySelectorAll('[data-cqa-review-history], [data-cqa-review-chip]').forEach(node => node.remove());
    const badge = document.querySelector('#cqa-tab-review [data-review-count]');
    if (badge) { if (badge.previousSibling?.nodeType === 3) badge.previousSibling.remove(); badge.remove(); }
    mounts.delete(CQA);
  };
  mounts.set(CQA, destroy);
  return destroy;
}
