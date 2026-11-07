/* Дәлел — checklist editor (T23).
 * Versions, criteria grouped by section, live validation, diff with re-evaluation cost, linter,
 * «Сохранить как vN», apply/preview runs, JSON import/export and protection against losing a draft.
 * Pure functions are exported for node tests and never touch the DOM. All user text is rendered with
 * textContent; network access goes through CQA.api only. */

export const ANSWER_TYPES = Object.freeze(['binary', 'scale', 'numeric']);
/** Same rule as needs_reeval in the api.js mock: every criterion change except weight and section needs the model. */
export const REEVAL_FREE_FIELDS = Object.freeze(['weight', 'section']);
/** Derived on the server: hash (prompt-relevant fields) and max_points (== weight). Never a reason to re-evaluate. */
export const DERIVED_FIELDS = Object.freeze(['hash', 'max_points']);
export const DIFF_FIELDS = Object.freeze(['section', 'title', 'description', 'answer_type', 'scale', 'scale_labels', 'weight', 'na_allowed', 'na_rule', 'critical', 'critical_when', 'critical_rule', 'guidance']);
export const DEFAULT_ROUNDING = Object.freeze({ total: 2, mode: 'half_up' });
export const STORAGE_KEY = 'cqa.editor.draft.v1';
export const FIELD_LABELS = Object.freeze({
  id: 'ID', section: 'Секция', title: 'Название', description: 'Формулировка', answer_type: 'Тип ответа', scale: 'Шкала',
  scale_labels: 'Подписи ступеней', weight: 'Вес', max_points: 'Максимум баллов', na_allowed: 'N/A разрешён', na_rule: 'Правило N/A',
  critical: 'Грубая ошибка', critical_when: 'Нарушение при ответе', critical_rule: 'Правило грубой ошибки', guidance: 'Уточнения для оценщика', hash: 'Хеш',
});
export const TYPE_LABELS = Object.freeze({ binary: 'Да/Нет', scale: 'Шкала', numeric: 'Число' });
const KNOWN_FIELDS = new Set(['id', ...DIFF_FIELDS, ...DERIVED_FIELDS, 'is_mock']);
const TOP_LEVEL_SKIP = new Set(['version', 'created_at', 'change_note', 'is_mock', 'active', 'is_active', 'activated_at', 'criteria', 'sections', 'hash', 'source', 'official', 'parent_version', 'checklist_id', 'name', 'rounding']);
const TYPE_SYNONYMS = {
  binary: 'binary', bool: 'binary', boolean: 'binary', yes_no: 'binary', yesno: 'binary', 'yes/no': 'binary', 'да/нет': 'binary', 'да-нет': 'binary',
  scale: 'scale', ordinal: 'scale', 'шкала': 'scale', numeric: 'numeric', number: 'numeric', 'число': 'numeric', 'числовой': 'numeric',
};
const YES = new Set(['yes', 'y', 'да', 'д', 'иә', 'ия', 'true', '1', '+']);
const NO = new Set(['no', 'n', 'нет', 'н', 'жоқ', 'жок', 'false', '0', '-']);
const TERMINAL = new Set(['completed', 'complete', 'done', 'finished', 'success', 'succeeded', 'failed', 'error', 'cancelled', 'canceled']);
const FAILED = new Set(['failed', 'error', 'cancelled', 'canceled']);
const ID_PATTERN = /^[\p{L}\p{N}_.-]{1,40}$/u;
const DEFAULT_CRITICAL_RULE = 'Без баллов. Отмечается отдельным флагом и в итоговый балл не входит.';

const isObject = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const asText = value => value == null ? '' : String(value);
const trimmed = value => asText(value).trim();
const clone = value => value === undefined ? undefined : JSON.parse(JSON.stringify(value));
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

/** Lenient number parser for form input: accepts «10,5», «10.5», spaces; returns NaN otherwise. */
export function toNumber(value) {
  if (typeof value === 'number') return Number.isFinite(value) ? value : NaN;
  if (typeof value !== 'string') return NaN;
  const source = value.replace(/[\s  ]/g, '').replace(',', '.');
  return /^[+-]?(\d+(\.\d*)?|\.\d+)$/.test(source) ? Number(source) : NaN;
}

export function formatNumber(value, digits = 2) {
  const number = toNumber(value);
  if (Number.isNaN(number)) return asText(value);
  return number.toLocaleString('ru-RU', { maximumFractionDigits: digits });
}

export function plural(count, one, few, many) {
  const a = Math.abs(count) % 100; const b = a % 10;
  if (a > 10 && a < 20) return many;
  if (b > 1 && b < 5) return few;
  return b === 1 ? one : many;
}

function normalizeType(value) { return TYPE_SYNONYMS[trimmed(value).toLowerCase()] || null; }
function toBool(value) {
  if (typeof value === 'boolean') return value;
  if (typeof value === 'number') return value !== 0;
  const key = trimmed(value).toLowerCase();
  return YES.has(key) ? true : NO.has(key) ? false : !!key;
}
function binaryAnswer(value) {
  const key = trimmed(value).toLowerCase();
  return YES.has(key) ? 'yes' : NO.has(key) ? 'no' : '';
}
function numberKey(key) { const number = toNumber(key); return Number.isNaN(number) ? trimmed(key) : String(number); }
function guidanceList(value) {
  if (Array.isArray(value)) return value.map(trimmed).filter(Boolean);
  if (typeof value === 'string') return value.split(/\r?\n/).map(line => line.trim()).filter(Boolean);
  return [];
}

/** scale_labels as {"<step>": "<label>"}; accepts an object, [{value,label}] or labels parallel to the scale. */
export function normalizeLabels(labels, scale) {
  const out = {};
  if (Array.isArray(labels)) {
    labels.forEach((item, index) => {
      if (isObject(item)) { const key = numberKey(item.value ?? item.step); if (key) out[key] = trimmed(item.label ?? item.text); }
      else if (Array.isArray(scale) && index < scale.length) { const number = toNumber(scale[index]); if (!Number.isNaN(number)) out[String(number)] = trimmed(item); }
    });
  } else if (isObject(labels)) {
    for (const [key, value] of Object.entries(labels)) out[numberKey(key)] = trimmed(value);
  }
  return out;
}

export function normalizeRounding(rounding) {
  if (!isObject(rounding)) return { ...DEFAULT_ROUNDING };
  const total = toNumber(rounding.total);
  return { ...rounding, total: Number.isInteger(total) ? total : DEFAULT_ROUNDING.total, mode: trimmed(rounding.mode) || DEFAULT_ROUNDING.mode };
}

/** Comparable form of a criterion: absent ≡ empty, labels sorted, values that do not apply are cleared. */
export function canonicalCriterion(criterion = {}) {
  const c = isObject(criterion) ? criterion : {};
  const type = normalizeType(c.answer_type) || trimmed(c.answer_type);
  const scaled = type === 'scale' || type === 'numeric';
  const critical = !!c.critical;
  const weight = toNumber(c.weight);
  const labels = Object.entries(normalizeLabels(c.scale_labels, c.scale)).filter(([, label]) => label)
    .sort((a, b) => (toNumber(a[0]) - toNumber(b[0])) || a[0].localeCompare(b[0]));
  return {
    id: trimmed(c.id),
    section: trimmed(c.section),
    title: trimmed(c.title),
    description: trimmed(c.description),
    answer_type: type,
    scale: scaled && Array.isArray(c.scale) ? c.scale.map(value => { const number = toNumber(value); return Number.isNaN(number) ? trimmed(value) : number; }) : null,
    scale_labels: scaled && labels.length ? labels : null,
    weight: Number.isNaN(weight) ? trimmed(c.weight) : weight,
    na_allowed: !!c.na_allowed,
    na_rule: c.na_allowed ? trimmed(c.na_rule) : '',
    critical,
    critical_when: critical ? (binaryAnswer(c.critical_when) || 'yes') : '',
    critical_rule: critical ? trimmed(c.critical_rule) : '',
    guidance: guidanceList(c.guidance),
  };
}

function extraKeys(criterion) {
  return isObject(criterion) ? Object.keys(criterion).filter(key => !KNOWN_FIELDS.has(key) && !key.startsWith('_')) : [];
}

/** True when a change to these fields requires calling the model (mirrors api.js needs_reeval, derived fields ignored). */
export function needsReeval(fields) {
  const list = (Array.isArray(fields) ? fields : [fields]).map(item => typeof item === 'string' ? item : item?.field).filter(Boolean);
  return list.some(field => !REEVAL_FREE_FIELDS.includes(field) && !DERIVED_FIELDS.includes(field));
}

/** Criteria order with contiguous sections: explicit order first, then by first appearance; stable inside a section. */
export function groupBySection(criteria, sectionOrder = []) {
  const order = new Map();
  for (const section of sectionOrder || []) { const key = trimmed(section); if (key && !order.has(key)) order.set(key, order.size); }
  for (const criterion of criteria) { const key = trimmed(criterion?.section); if (!order.has(key)) order.set(key, order.size); }
  return criteria.map((criterion, index) => [criterion, index])
    .sort((a, b) => (order.get(trimmed(a[0]?.section)) - order.get(trimmed(b[0]?.section))) || a[1] - b[1])
    .map(([criterion]) => criterion);
}

/** Swap with the nearest criterion of the same section. Returns a new array, or null at the edge. */
export function moveCriterion(criteria, index, delta) {
  if (!Array.isArray(criteria) || index < 0 || index >= criteria.length || !delta) return null;
  const section = trimmed(criteria[index]?.section);
  let target = index + Math.sign(delta);
  while (target >= 0 && target < criteria.length && trimmed(criteria[target]?.section) !== section) target += Math.sign(delta);
  if (target < 0 || target >= criteria.length) return null;
  const next = criteria.slice();
  [next[index], next[target]] = [next[target], next[index]];
  return next;
}

export function nextCriterionId(criteria, prefix = 'C') {
  const used = new Set((criteria || []).map(item => trimmed(item?.id)));
  let max = 0;
  for (const id of used) { const match = /^C(\d+)$/i.exec(id); if (match) max = Math.max(max, Number(match[1])); }
  let number = max + 1;
  while (used.has(`${prefix}${number}`)) number += 1;
  return `${prefix}${number}`;
}

/**
 * Live validation of a checklist (contract shape). Errors block saving; warnings do not.
 * Issue: {level, scope:'checklist'|'criterion', index?, id?, field, step?, message}.
 */
export function validate(checklist) {
  const issues = [];
  const criteria = Array.isArray(checklist?.criteria) ? checklist.criteria : [];
  const push = (level, field, message, extra = {}) => issues.push({ level, field, message, scope: 'checklist', ...extra });
  if (!criteria.length) push('error', 'criteria', 'Добавьте хотя бы один критерий.');
  const counts = new Map();
  for (const criterion of criteria) { const id = trimmed(criterion?.id); if (id) counts.set(id, (counts.get(id) || 0) + 1); }
  let weightSum = 0; let positive = 0;
  criteria.forEach((criterion, index) => {
    const c = isObject(criterion) ? criterion : {};
    const id = trimmed(c.id);
    const at = (level, field, message, extra = {}) => issues.push({ level, field, message, scope: 'criterion', index, id, ...extra });
    if (!id) at('error', 'id', 'Укажите ID, например C9.');
    else if (!ID_PATTERN.test(id)) at('error', 'id', 'ID: буквы, цифры, «_», «-» или «.», без пробелов, до 40 символов.');
    else if (counts.get(id) > 1) at('error', 'id', `ID «${id}» уже есть у другого критерия. Задайте уникальный.`);
    if (!trimmed(c.title)) at('error', 'title', 'Укажите название критерия.');
    if (!trimmed(c.description)) at('error', 'description', 'Опишите, что проверяет критерий: когда ставится «Да» или какая ступень.');
    const type = c.answer_type;
    const critical = !!c.critical;
    if (!ANSWER_TYPES.includes(type)) at('error', 'answer_type', 'Выберите тип ответа: Да/Нет, Шкала или Число.');
    else if (type !== 'binary') validateScale(c, at);
    if (critical && ANSWER_TYPES.includes(type) && type !== 'binary') at('error', 'answer_type', 'Грубая ошибка задаётся только как Да/Нет. Смените тип или снимите отметку «Грубая ошибка».');
    const weight = toNumber(c.weight);
    if (Number.isNaN(weight) || weight < 0) at('error', 'weight', 'Вес — неотрицательное число, например 15.');
    else if (critical && weight !== 0) at('error', 'weight', 'Грубая ошибка не даёт баллов: вес должен быть 0.');
    else if (!critical) {
      weightSum += weight;
      if (weight > 0) positive += 1;
      else at('warning', 'weight', 'Вес 0: критерий не влияет на итог.');
    }
    if (c.na_allowed && !trimmed(c.na_rule)) at('error', 'na_rule', 'Опишите, когда критерий не применим (N/A). Без правила оценщик не сможет поставить N/A.');
    if (critical) {
      if (c.critical_when != null && c.critical_when !== '' && !binaryAnswer(c.critical_when)) at('error', 'critical_when', 'Укажите, при каком ответе нарушение: «Да» или «Нет».');
      if (!trimmed(c.critical_rule)) at('warning', 'critical_rule', 'Опишите, что считается грубой ошибкой.');
    }
    if (c.guidance != null && !Array.isArray(c.guidance)) at('error', 'guidance', 'Уточнения — список строк.');
  });
  weightSum = Math.round(weightSum * 1e6) / 1e6;
  if (criteria.length && !positive) push('error', 'weights', 'Нужен хотя бы один критерий с весом больше 0, иначе итог не посчитать.');
  else if (positive && Math.abs(weightSum - 100) > 0.005) push('warning', 'weights', `Сумма весов ${formatNumber(weightSum)} вместо 100. Движок всё равно перераспределит вес пропорционально; можно нормировать до 100.`, { sum: weightSum });
  const errors = issues.filter(issue => issue.level === 'error');
  const warnings = issues.filter(issue => issue.level === 'warning');
  return { ok: errors.length === 0, issues, errors, warnings, weightSum, weightOk: positive > 0 && Math.abs(weightSum - 100) <= 0.005 };
}

function validateScale(c, at) {
  const steps = Array.isArray(c.scale) ? c.scale : [];
  const numeric = c.answer_type === 'numeric';
  if (steps.length < 2) { at('error', 'scale', numeric ? 'Задайте минимум и максимум, например 0 и 10.' : 'Нужно минимум две ступени, например 0 и 10.'); return; }
  const numbers = steps.map(toNumber);
  let ok = true;
  numbers.forEach((number, step) => {
    if (Number.isNaN(number)) { at('error', 'scale', `Ступень ${step + 1}: введите число.`, { step }); ok = false; }
    else if (number < 0) { at('error', 'scale', `Ступень ${step + 1}: значение не может быть отрицательным.`, { step }); ok = false; }
  });
  if (!ok) return;
  const seen = new Set();
  numbers.forEach((number, step) => {
    if (seen.has(number)) { at('error', 'scale', `Ступень ${formatNumber(number)} повторяется. Удалите дубль.`, { step }); ok = false; }
    seen.add(number);
  });
  for (let step = 1; ok && step < numbers.length; step += 1) {
    if (numbers[step] < numbers[step - 1]) { at('error', 'scale', `Ступени идут по возрастанию: ${formatNumber(numbers[step])} стоит после ${formatNumber(numbers[step - 1])}.`, { step }); ok = false; }
  }
  if (Math.max(...numbers) <= 0) at('error', 'scale', 'Максимальная ступень должна быть больше 0: она даёт полный вес.');
  if (ok && !numeric) {
    const labels = normalizeLabels(c.scale_labels, c.scale);
    const missing = numbers.filter(number => !trimmed(labels[String(number)]));
    if (missing.length) at('warning', 'scale_labels', `Подпишите ${missing.length === 1 ? 'ступень' : 'ступени'} ${missing.map(value => formatNumber(value)).join(', ')}: так оценщику понятнее.`);
  }
}

/** Proportionally rescales non-critical weights to `target`, largest-remainder rounding keeps the exact sum. */
export function normalizeWeights(criteria, { target = 100, precision = 2 } = {}) {
  const list = (criteria || []).map(item => ({ ...item }));
  const indexes = list.map((item, index) => [item, index]).filter(([item]) => !item.critical && toNumber(item.weight) > 0);
  const sum = indexes.reduce((total, [item]) => total + toNumber(item.weight), 0);
  if (!sum) return list;
  const unit = 10 ** precision;
  const total = Math.round(target * unit);
  const raw = indexes.map(([item]) => toNumber(item.weight) / sum * total);
  const floors = raw.map(value => Math.floor(value + 1e-9));
  const rest = total - floors.reduce((a, b) => a + b, 0);
  const order = raw.map((value, k) => [value - floors[k], k]).sort((a, b) => (b[0] - a[0]) || (a[1] - b[1]));
  for (let j = 0; j < rest; j += 1) floors[order[j % order.length][1]] += 1;
  indexes.forEach(([, index], k) => { list[index].weight = floors[k] / unit; });
  return list;
}

/**
 * Diff of two checklists by criterion id. needs_reeval follows the api.js mock rule (anything but weight/section);
 * the backend decides by hash, which covers exactly these prompt fields.
 */
export function classifyDiff(before, after) {
  const listBefore = Array.isArray(before?.criteria) ? before.criteria : [];
  const listAfter = Array.isArray(after?.criteria) ? after.criteria : [];
  const byIdBefore = new Map(listBefore.map(item => [trimmed(item?.id), item]));
  const byIdAfter = new Map(listAfter.map(item => [trimmed(item?.id), item]));
  const added = [], removed = [], changed = [], reusable = [];
  for (const item of listAfter) {
    const id = trimmed(item?.id);
    if (!byIdBefore.has(id)) { added.push({ id, title: trimmed(item?.title), criterion: item, needs_reeval: true }); continue; }
    const old = byIdBefore.get(id);
    const a = canonicalCriterion(item), b = canonicalCriterion(old);
    const fields = [];
    for (const field of DIFF_FIELDS) if (!same(a[field], b[field])) fields.push({ field, before: b[field], after: a[field], reeval: needsReeval([field]) });
    for (const key of new Set([...extraKeys(item), ...extraKeys(old)])) if (!same(item[key], old[key])) fields.push({ field: key, before: old[key], after: item[key], reeval: true, extra: true });
    if (fields.length) changed.push({ id, title: a.title || b.title, before_title: b.title, fields, field_names: fields.map(f => f.field), needs_reeval: fields.some(f => f.reeval) });
    else reusable.push(id);
  }
  for (const item of listBefore) { const id = trimmed(item?.id); if (!byIdAfter.has(id)) removed.push({ id, title: trimmed(item?.title), criterion: item, needs_reeval: false }); }
  const commonBefore = listBefore.map(item => trimmed(item?.id)).filter(id => byIdAfter.has(id));
  const commonAfter = listAfter.map(item => trimmed(item?.id)).filter(id => byIdBefore.has(id));
  const reordered = !same(commonBefore, commonAfter);
  const checklistFields = [];
  if (before && after && trimmed(before.name) !== trimmed(after.name)) checklistFields.push('name');
  if (before && after && !same(normalizeRounding(before.rounding), normalizeRounding(after.rounding))) checklistFields.push('rounding');
  const reevalIds = [...changed.filter(item => item.needs_reeval).map(item => item.id), ...added.map(item => item.id)];
  const rescoreOnly = changed.filter(item => !item.needs_reeval).map(item => item.id);
  const count = added.length + removed.length + changed.length + (reordered ? 1 : 0) + checklistFields.length;
  return { added, removed, changed, reusable, needs_reeval: reevalIds, rescore_only: rescoreOnly, reordered, checklist_fields: checklistFields, count, has_changes: count > 0 };
}

/** One criterion in the POST /checklists shape. hash is omitted (the server recomputes it); max_points = weight. */
export function contractCriterion(criterion) {
  const c = isObject(criterion) ? criterion : {};
  const k = canonicalCriterion(c);
  const out = {};
  for (const key of extraKeys(c)) out[key] = clone(c[key]);
  Object.assign(out, {
    id: k.id,
    section: k.section || null,
    title: k.title,
    description: k.description,
    answer_type: k.answer_type,
    scale: k.scale,
    scale_labels: k.scale_labels ? Object.fromEntries(k.scale_labels) : null,
    weight: k.weight,
    max_points: k.critical ? 0 : k.weight,
    na_allowed: k.na_allowed,
    na_rule: k.na_rule,
    critical: k.critical,
    critical_when: k.critical ? k.critical_when : null,
    critical_rule: k.critical ? k.critical_rule : null,
    guidance: k.guidance,
  });
  return out;
}

/** Body for CQA.api.createChecklist: ChecklistVersion without version/created_at/hash. */
export function buildBody(checklist, options = {}) {
  const source = isObject(checklist) ? checklist : {};
  const criteria = groupBySection((source.criteria || []).map(contractCriterion), source.sections);
  const body = {};
  for (const [key, value] of Object.entries(source)) if (!TOP_LEVEL_SKIP.has(key) && !key.startsWith('_')) body[key] = clone(value);
  const parent = toNumber(options.parent_version ?? source.parent_version);
  Object.assign(body, {
    checklist_id: trimmed(source.checklist_id) || 'checklist',
    name: trimmed(options.name ?? source.name) || 'Чек-лист',
    change_note: trimmed(options.change_note ?? source.change_note),
    rounding: normalizeRounding(source.rounding),
    sections: [...new Set(criteria.map(item => item.section).filter(Boolean))],
    criteria,
  });
  if (Number.isInteger(parent) && parent > 0) body.parent_version = parent;
  return body;
}

/** Replaces the content of criterion `id`, keeping id, section, position, critical flag and unknown fields. */
export function replaceCriterion(checklist, id, patch = {}) {
  const criteria = (checklist?.criteria || []).map(item => {
    if (trimmed(item?.id) !== trimmed(id)) return item;
    const next = {
      ...item,
      title: patch.title ?? '', description: patch.description ?? '', answer_type: patch.answer_type || item.answer_type,
      scale: patch.scale ?? null, scale_labels: patch.scale_labels ?? null,
      na_allowed: !!patch.na_allowed, na_rule: patch.na_allowed ? asText(patch.na_rule) : '',
      guidance: Array.isArray(patch.guidance) ? patch.guidance.slice() : [],
    };
    if (patch.weight !== undefined) next.weight = patch.weight;
    if (patch.critical_when !== undefined) next.critical_when = patch.critical_when;
    if (patch.critical_rule !== undefined) next.critical_rule = patch.critical_rule;
    return next;
  });
  return { ...checklist, criteria };
}

function describeJsonError(error, source) {
  const message = asText(error?.message);
  const lineColumn = /line\s+(\d+)\s+column\s+(\d+)/i.exec(message);
  const position = /position\s+(\d+)/i.exec(message);
  let where = '';
  if (lineColumn) where = `строка ${lineColumn[1]}, столбец ${lineColumn[2]}`;
  else if (position) {
    const offset = Number(position[1]);
    const head = source.slice(0, offset);
    where = `строка ${head.split('\n').length}, столбец ${offset - head.lastIndexOf('\n')}`;
  } else if (/end of (json )?input|unterminated|unexpected end/i.test(message)) where = 'текст обрывается';
  return `JSON не читается${where ? `: ${where}` : ''}. Проверьте запятые, кавычки и скобки рядом с этим местом.`;
}

/**
 * Import of a checklist from text or an object. Structural problems (not JSON, no criteria array) are errors;
 * field problems are returned in `validation` and stay editable in the draft.
 */
export function parseImport(input, options = {}) {
  const fallback = options.fallback || {};
  const warnings = [];
  const fail = message => ({ ok: false, errors: [message], warnings, checklist: null, validation: null, version: null });
  let data = input;
  if (typeof input === 'string') {
    const source = input.replace(/^﻿/, '');
    if (!source.trim()) return fail('Пусто: вставьте JSON чек-листа или выберите файл .json.');
    try { data = JSON.parse(source); } catch (error) { return fail(describeJsonError(error, source)); }
  }
  if (Array.isArray(data)) data = { criteria: data };
  if (isObject(data) && !Array.isArray(data.criteria) && isObject(data.checklist)) data = data.checklist;
  if (!isObject(data)) return fail('Нужен объект чек-листа вида { "criteria": [ … ] }.');
  if (!Array.isArray(data.criteria)) return fail('Нет списка критериев: поле «criteria» должно быть массивом.');
  if (!data.criteria.length) return fail('Список критериев пуст: добавьте хотя бы один критерий.');
  const errors = [];
  const criteria = [];
  let droppedHash = false;
  data.criteria.forEach((raw, index) => {
    const label = `Критерий ${index + 1}${isObject(raw) && trimmed(raw.id) ? ` (${trimmed(raw.id)})` : ''}`;
    if (!isObject(raw)) { errors.push(`${label}: ожидался объект с полями id, title, description, answer_type, weight.`); return; }
    if (raw.hash != null) droppedHash = true;
    const c = {};
    for (const key of extraKeys(raw)) c[key] = clone(raw[key]);
    let type = normalizeType(raw.answer_type ?? raw.type);
    if (!type) {
      if (raw.answer_type != null && trimmed(raw.answer_type)) { errors.push(`${label}: неизвестный тип ответа «${trimmed(raw.answer_type)}». Допустимо: binary, scale, numeric.`); return; }
      type = raw.scale != null && raw.scale !== '' ? 'scale' : 'binary';
      warnings.push(`${label}: тип ответа не указан, выбран «${TYPE_LABELS[type]}».`);
    }
    let scale = null;
    if (Array.isArray(raw.scale)) scale = raw.scale.slice();
    else if (typeof raw.scale === 'string' && raw.scale.trim()) scale = raw.scale.split(/[\s,;/|]+/).filter(Boolean).map(part => { const number = toNumber(part); return Number.isNaN(number) ? part : number; });
    else if (typeof raw.scale === 'number') scale = [raw.scale];
    else if (raw.scale != null && raw.scale !== '') { errors.push(`${label}: «scale» должен быть списком чисел, например [0, 5, 10].`); return; }
    if (type === 'binary') scale = null;
    const labels = normalizeLabels(raw.scale_labels, scale);
    const weight = raw.weight ?? raw.points;
    Object.assign(c, {
      id: trimmed(raw.id),
      section: raw.section == null ? null : trimmed(raw.section),
      title: asText(raw.title ?? raw.name),
      description: asText(raw.description ?? raw.text),
      answer_type: type,
      scale,
      scale_labels: type === 'binary' || !Object.keys(labels).length ? null : labels,
      weight: weight == null ? '' : (Number.isNaN(toNumber(weight)) ? asText(weight) : toNumber(weight)),
      na_allowed: toBool(raw.na_allowed ?? raw.na ?? false),
      na_rule: asText(raw.na_rule ?? ''),
      critical: toBool(raw.critical ?? false),
      critical_when: raw.critical_when == null ? null : (binaryAnswer(raw.critical_when) || asText(raw.critical_when)),
      critical_rule: raw.critical_rule == null ? null : asText(raw.critical_rule),
      guidance: guidanceList(raw.guidance),
    });
    criteria.push(c);
  });
  if (errors.length) return { ok: false, errors, warnings, checklist: null, validation: null, version: null };
  for (const c of criteria) if (!c.id) { c.id = nextCriterionId(criteria); warnings.push(`Критерию «${trimmed(c.title) || 'без названия'}» присвоен ID ${c.id}.`); }
  if (droppedHash) warnings.push('Хеши критериев из файла не используются: сервер пересчитает их при сохранении.');
  if (data.version != null) warnings.push(`Номер версии ${asText(data.version)} из файла не используется: сервер назначит новый.`);
  const checklist = {};
  for (const [key, value] of Object.entries(data)) if (!TOP_LEVEL_SKIP.has(key) && !key.startsWith('_')) checklist[key] = clone(value);
  Object.assign(checklist, {
    checklist_id: trimmed(data.checklist_id) || trimmed(fallback.checklist_id) || 'checklist',
    name: trimmed(data.name) || trimmed(fallback.name) || 'Импортированный чек-лист',
    rounding: normalizeRounding(data.rounding ?? fallback.rounding),
    sections: Array.isArray(data.sections) ? data.sections.map(trimmed).filter(Boolean) : [],
    criteria: groupBySection(criteria, data.sections),
    change_note: trimmed(data.change_note),
  });
  return { ok: true, errors: [], warnings, checklist, validation: validate(checklist), version: data.version ?? null };
}

/**
 * Server diff → ids and warnings. Accepts the backend ChecklistDiff (needs_reeval/rescore_only/reusable/warnings lists)
 * and the api.js mock shape (changed[].needs_reeval). The server decides by hash, so after saving it is the authority.
 */
export function serverDiffSummary(diff) {
  if (!isObject(diff)) return null;
  const ids = list => Array.isArray(list) ? list.map(item => trimmed(isObject(item) ? (item.criterion_id ?? item.id) : item)).filter(Boolean) : [];
  const changed = Array.isArray(diff.changed) ? diff.changed.filter(isObject) : [];
  const changedId = item => trimmed(item.criterion_id ?? item.id);
  const added = ids(diff.added);
  const needs = Array.isArray(diff.needs_reeval) ? ids(diff.needs_reeval) : [...changed.filter(item => item.needs_reeval).map(changedId), ...added];
  const rescore = Array.isArray(diff.rescore_only) ? ids(diff.rescore_only) : changed.filter(item => !item.needs_reeval).map(changedId);
  const warnings = Array.isArray(diff.warnings) ? diff.warnings.map(item => typeof item === 'string' ? item.trim() : trimmed(item?.message)).filter(Boolean) : [];
  return { needs_reeval: [...new Set(needs)], rescore_only: [...new Set(rescore)], reusable: ids(diff.reusable), added, removed: ids(diff.removed), warnings: [...new Set(warnings)], mock: diff.is_mock === true };
}

/** Linter response → at most 3 {message, field, criterion_id}. Accepts {warnings:[...]}, an array, strings or objects. */
export function lintWarnings(response) {
  const list = Array.isArray(response) ? response : Array.isArray(response?.warnings) ? response.warnings : [];
  return list.map(item => {
    if (typeof item === 'string') return { message: item.trim(), field: null, criterion_id: null };
    if (isObject(item)) return { message: trimmed(item.message ?? item.text ?? item.warning), field: item.field ?? null, criterion_id: item.criterion_id ?? null };
    return null;
  }).filter(item => item?.message).slice(0, 3);
}

/** GET /runs/{id} → numbers for the inline status. */
export function runSummary(data) {
  const number = key => { const value = toNumber(data?.[key]); return Number.isNaN(value) ? null : value; };
  const done = number('done') ?? 0;
  const total = number('total') ?? 0;
  const status = trimmed(data?.status).toLowerCase();
  const finished = TERMINAL.has(status) || (!status && total > 0 && done >= total);
  const perConv = Array.isArray(data?.per_conv) ? data.per_conv.filter(isObject) : [];
  const changes = perConv.filter(row => Number.isFinite(row.old_total) && Number.isFinite(row.new_total) && Math.abs(row.new_total - row.old_total) >= 0.005)
    .sort((a, b) => Math.abs(b.new_total - b.old_total) - Math.abs(a.new_total - a.old_total));
  return {
    done, total, progress: total > 0 ? Math.min(1, done / total) : 0, status, finished, failed: FAILED.has(status),
    from_cache: number('from_cache'), llm_calls: number('llm_calls'), cost_kzt: number('cost_kzt'), elapsed: number('elapsed'),
    per_conv: perConv, changes, mock: !!data?.is_mock,
  };
}

/** GET /checklists → {versions (newest first), active}. Accepts an array or {versions|items|checklists, active_version}. */
export function parseVersionList(result) {
  const list = Array.isArray(result) ? result : isObject(result) ? (result.versions || result.items || result.checklists || []) : [];
  let active = isObject(result) ? toNumber(result.active_version ?? result.active) : NaN;
  const versions = [];
  for (const item of list) {
    if (!isObject(item)) continue;
    const version = toNumber(item.version);
    if (!Number.isInteger(version)) continue;
    versions.push({ ...item, version });
    if (item.active === true || item.is_active === true) active = version;
  }
  versions.sort((a, b) => b.version - a.version);
  return { versions, active: Number.isInteger(active) ? active : null };
}

// ----------------------------------------------------------------------------------------------------------------
// DOM

const SVG_NS = 'http://www.w3.org/2000/svg';
const ICON_PATHS = {
  plus: 'M12 5v14M5 12h14',
  copy: 'M9 9h11v11H9zM5 15H4V4h11v1',
  trash: 'M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3',
  up: 'M12 19V5M6 11l6-6 6 6',
  down: 'M12 5v14M18 13l-6 6-6-6',
  chevron: 'm9 6 6 6-6 6',
  upload: 'M12 15V4M7 9l5-5 5 5M4 15v5h16v-5',
  download: 'M12 4v11M7 10l5 5 5-5M4 15v5h16v-5',
  swap: 'M8 3 4 7l4 4M4 7h16M16 21l4-4-4-4M20 17H4',
  check: 'm5 12 4 4L19 6',
  alert: 'M12 9v4M12 17h.01M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z',
  octagon: 'M7.9 2h8.2L22 7.9v8.2L16.1 22H7.9L2 16.1V7.9zM12 8v4M12 16h.01',
  info: 'M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20zM12 16v-5M12 8h.01',
  x: 'M18 6 6 18M6 6l12 12',
  play: 'M7 4v16l13-8z',
  eye: 'M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12zM12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z',
  pencil: 'M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z',
  reset: 'M3 12a9 9 0 1 0 3-6.7L3 8M3 3v5h5',
  save: 'M5 3h11l5 5v13H5zM8 3v5h7M8 21v-7h8v7',
  percent: 'M19 5 5 19M7 9a2 2 0 1 0 0-4 2 2 0 0 0 0 4zM17 19a2 2 0 1 0 0-4 2 2 0 0 0 0 4z',
  lint: 'M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2zM12 7v4M12 14h.01',
  spinner: 'M21 12a9 9 0 1 1-6.2-8.6',
  open: 'M14 4h6v6M20 4l-9 9M18 14v6H4V6h6',
  layers: 'M12 3 2 8l10 5 10-5zM2 13l10 5 10-5',
  zero: 'M12 21a7 9 0 1 0 0-18 7 9 0 0 0 0 18zM7 19 17 5',
};
const mounts = new WeakMap();
let uidSequence = 0;
const newUid = () => `k${(++uidSequence).toString(36)}${Math.random().toString(36).slice(2, 6)}`;

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = String(value);
    else if (key === 'value') node.value = String(value);
    else if (key === 'checked') node.checked = !!value;
    else node.setAttribute(key, value === true ? '' : String(value));
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(typeof child === 'object' ? child : document.createTextNode(String(child)));
  }
  return node;
}
function icon(name, className = '') {
  const svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('focusable', 'false');
  svg.setAttribute('class', `cqa-ed__icon ${className}`.trim());
  const path = document.createElementNS(SVG_NS, 'path');
  path.setAttribute('d', ICON_PATHS[name] || ICON_PATHS.info);
  svg.append(path);
  return svg;
}
function button(label, { action, iconName, variant = 'secondary', attrs = {}, keys = null } = {}) {
  const node = el('button', { type: 'button', class: `cqa-ed__btn cqa-ed__btn--${variant}`, 'data-action': action, ...attrs });
  if (iconName) node.append(icon(iconName));
  node.append(el('span', { class: 'cqa-ed__btn-label', text: label }));
  if (keys) node.append(kbd(...keys));
  return node;
}
function iconButton(iconName, label, attrs = {}) {
  return el('button', { type: 'button', class: 'cqa-ed__icon-btn', 'aria-label': label, title: label, ...attrs }, icon(iconName));
}
const IS_MAC = typeof navigator !== 'undefined' && /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent || '');
function kbd(...keys) { return el('span', { class: 'cqa-ed__kbd', 'aria-hidden': 'true' }, ...keys.map(key => el('kbd', { text: key }))); }
function chip(text, kind = 'neutral', iconName = null) {
  return el('span', { class: `cqa-ed__chip cqa-ed__chip--${kind}` }, iconName ? icon(iconName) : null, el('span', { text }));
}
function formatDate(value) {
  const raw = trimmed(value);
  if (!raw) return '';
  if (/^\d{4}-\d{2}-\d{2}$/.test(raw)) { const [y, m, d] = raw.split('-'); return `${d}.${m}.${y}`; }
  const date = new Date(raw);
  if (Number.isNaN(date.getTime())) return raw;
  return date.toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
}
function shorten(value, length = 90) { const source = trimmed(value).replace(/\s+/g, ' '); return source.length > length ? `${source.slice(0, length - 1)}…` : source; }
function storageGet() { try { const raw = window.localStorage.getItem(STORAGE_KEY); return raw ? JSON.parse(raw) : null; } catch { return null; } }
function storageSet(value) { try { window.localStorage.setItem(STORAGE_KEY, JSON.stringify(value)); return true; } catch { return false; } }
function storageRemove() { try { window.localStorage.removeItem(STORAGE_KEY); return true; } catch { return false; } }
function attachStyle() {
  if (document.querySelector('link[data-cqa-editor-style]')) return;
  const link = document.createElement('link');
  link.rel = 'stylesheet';
  link.href = new URL('../css/editor.css', import.meta.url).href;
  link.dataset.cqaEditorStyle = '';
  document.head.append(link);
}

/** UI form of a criterion: steps keep raw typed values so validation can point at a row. */
function toUi(criterion) {
  const c = isObject(criterion) ? criterion : {};
  const labels = normalizeLabels(c.scale_labels, c.scale);
  const extras = {};
  for (const key of extraKeys(c)) extras[key] = clone(c[key]);
  return {
    uid: newUid(), id: asText(c.id), section: asText(c.section ?? ''), title: asText(c.title), description: asText(c.description),
    answer_type: normalizeType(c.answer_type) || asText(c.answer_type) || 'binary',
    steps: Array.isArray(c.scale) ? c.scale.map(value => ({ value: asText(value), label: asText(labels[numberKey(value)] ?? '') })) : [],
    weight: c.weight == null ? '' : asText(c.weight), na_allowed: !!c.na_allowed, na_rule: asText(c.na_rule ?? ''),
    critical: !!c.critical, critical_when: binaryAnswer(c.critical_when) || (c.critical ? 'yes' : ''), critical_rule: asText(c.critical_rule ?? ''),
    guidance: guidanceList(c.guidance), extras, memo: {}, isNew: false,
  };
}
function fromUi(ui) {
  const scaled = ui.answer_type === 'scale' || ui.answer_type === 'numeric';
  const labels = {};
  if (scaled) for (const step of ui.steps) { const number = toNumber(step.value); if (!Number.isNaN(number) && trimmed(step.label)) labels[String(number)] = step.label; }
  const weight = toNumber(ui.weight);
  return {
    ...ui.extras, id: ui.id, section: ui.section, title: ui.title, description: ui.description, answer_type: ui.answer_type,
    scale: scaled ? ui.steps.map(step => { const number = toNumber(step.value); return Number.isNaN(number) ? step.value : number; }) : null,
    scale_labels: scaled && Object.keys(labels).length ? labels : null,
    weight: Number.isNaN(weight) ? ui.weight : weight, na_allowed: ui.na_allowed, na_rule: ui.na_rule,
    critical: ui.critical, critical_when: ui.critical ? (ui.critical_when || 'yes') : null, critical_rule: ui.critical ? ui.critical_rule : null,
    guidance: ui.guidance.slice(),
  };
}
function blankUi(id, section) {
  return { uid: newUid(), id, section: section || '', title: '', description: '', answer_type: 'binary', steps: [], weight: '0', na_allowed: false, na_rule: '', critical: false, critical_when: '', critical_rule: '', guidance: [], extras: {}, memo: {}, isNew: true };
}
function draftFromChecklist(source, { base_version = null, origin = 'version' } = {}) {
  const extras = {};
  for (const [key, value] of Object.entries(source || {})) if (!TOP_LEVEL_SKIP.has(key) && !key.startsWith('_')) extras[key] = clone(value);
  return {
    schema: 1, base_version, origin, checklist_id: trimmed(source?.checklist_id) || 'checklist', name: trimmed(source?.name) || 'Чек-лист',
    rounding: normalizeRounding(source?.rounding), sections: Array.isArray(source?.sections) ? source.sections.slice() : [], extras,
    criteria: groupBySection((source?.criteria || []).map(toUi), source?.sections), change_note: '', updated_at: new Date().toISOString(),
  };
}
function reviveDraft(stored) {
  if (!isObject(stored) || stored.schema !== 1 || !Array.isArray(stored.criteria)) return null;
  const criteria = stored.criteria.filter(isObject).map(item => ({
    ...blankUi(asText(item.id), asText(item.section)), ...item, uid: newUid(),
    steps: Array.isArray(item.steps) ? item.steps.filter(isObject).map(step => ({ value: asText(step.value), label: asText(step.label) })) : [],
    guidance: Array.isArray(item.guidance) ? item.guidance.map(asText) : [], extras: isObject(item.extras) ? item.extras : {}, memo: isObject(item.memo) ? item.memo : {},
  }));
  return { ...stored, criteria, extras: isObject(stored.extras) ? stored.extras : {}, rounding: normalizeRounding(stored.rounding), change_note: asText(stored.change_note) };
}
function draftChecklist(draft) {
  return { ...draft.extras, checklist_id: draft.checklist_id, name: draft.name, rounding: draft.rounding, sections: draft.sections, criteria: draft.criteria.map(fromUi) };
}
function lintSignature(ui) { return JSON.stringify([ui.title, ui.description, ui.answer_type, ui.steps, ui.na_allowed, ui.na_rule, ui.critical, ui.critical_rule, ui.guidance]); }

/** Prefer known answers for the active baseline, retaining input order within each group. */
export function selectPreview(rows, activeVersion, limit = 5) {
  const known = row => row.checklist_version === activeVersion && (row.cells || []).some(cell => cell.answer != null);
  const valid = (Array.isArray(rows) ? rows : []).filter(row => typeof row?.id === 'string' && row.id);
  const unique = [...new Map(valid.map(row => [row.id, row])).values()];
  const selected = [...unique.filter(known), ...unique.filter(row => !known(row))].slice(0, limit);
  return { ids: selected.map(row => row.id), unscored: selected.filter(row => !known(row)).length };
}

export function mount(CQA = typeof window !== 'undefined' ? window.CQA : undefined) {
  if (typeof document === 'undefined') return null;
  const host = document.getElementById('cqa-editor');
  if (!host || !CQA?.api || !CQA?.bus) return null;
  if (mounts.has(host)) return mounts.get(host);
  attachStyle();
  CQA.state ||= {};

  const state = {
    versions: [], full: new Map(), loading: true, showSkeleton: false, loadError: '', listActive: null, sessionActive: null,
    view: null, draft: null, compareTo: null, expanded: new Set(), touched: new Set(), showAll: false,
    lint: new Map(), busy: '', saveError: '', applyError: '', noteError: '', saveHint: '', undo: null, restoredAt: '',
    storageOk: true, serverNotes: null, run: null, groups: null, conversationRows: [], groupsLoading: false, groupsError: '', group: '', highlight: '', validation: null,
  };
  const serverDiffs = new Map();
  const wizard = { targetId: '', ui: null, showAll: false, touched: new Set(), lint: null, lintTimer: 0, lintSeq: 0, opener: null };
  const importState = { result: null, opener: null };
  const timers = { refresh: 0, persist: 0, poll: 0, lint: new Map(), skeleton: 0, highlight: 0 };
  const subscriptions = [];
  let pendingFocus = '';
  let lintSequence = 0;
  let pollToken = 0;
  let groupsToken = 0;
  let disposed = false;
  let ownSaveVersion = null;
  let budgetController = null, economicsData = null, economicsConfig = null, economicsEpoch = 0;
  const economicsSource = () => JSON.stringify([CQA.config?.mock !== false, !!CQA.config?.offline, CQA.config?.apiBase || '', CQA.config?.mockBase || '']);
  let economicsContext = economicsSource();
  const reducedMotion = typeof window.matchMedia === 'function' ? window.matchMedia('(prefers-reduced-motion: reduce)') : { matches: false };

  host.classList.remove('cqa-module-placeholder');
  host.classList.add('cqa-ed');
  const live = el('div', { class: 'cqa-visually-hidden', 'aria-live': 'polite', 'aria-atomic': 'true' });
  const versionsBox = el('nav', { class: 'cqa-ed__versions', 'aria-labelledby': 'cqa-ed-versions-title' });
  const head = el('header', { class: 'cqa-ed__head' });
  const noticeBox = el('div', { class: 'cqa-ed__notices' });
  const summaryBox = el('div', { class: 'cqa-ed__summary' });
  const runBox = el('section', { class: 'cqa-ed__panel cqa-ed__run', 'aria-labelledby': 'cqa-ed-run-title', hidden: true });
  const diffBox = el('section', { class: 'cqa-ed__panel cqa-ed__diff', 'aria-labelledby': 'cqa-ed-diff-title', hidden: true });
  const diffHead = el('div', { class: 'cqa-ed__panel-head' });
  const diffBody = el('div', { class: 'cqa-ed__diff-body' });
  diffBox.append(diffHead, diffBody);
  const side = el('div', { class: 'cqa-ed__side' }, runBox, diffBox);
  const listBox = el('div', { class: 'cqa-ed__criteria' });
  const bar = el('div', { class: 'cqa-ed__bar' });
  const budgetHost = el('div', { class: 'cqa-ed__budget', hidden: true });
  const main = el('div', { class: 'cqa-ed__main' }, head, noticeBox, summaryBox, el('div', { class: 'cqa-ed__body' }, side, listBox), budgetHost, bar);
  const replaceDialog = el('dialog', { class: 'cqa-ed__dialog cqa-ed__dialog--wide', 'aria-labelledby': 'cqa-ed-replace-title' });
  const importDialog = el('dialog', { class: 'cqa-ed__dialog', 'aria-labelledby': 'cqa-ed-import-title' });
  const confirmDialog = el('dialog', { class: 'cqa-ed__dialog cqa-ed__dialog--small', role: 'alertdialog', 'aria-labelledby': 'cqa-ed-confirm-title', 'aria-describedby': 'cqa-ed-confirm-text' });
  const datalist = el('datalist', { id: 'cqa-ed-sections' });
  host.replaceChildren(el('div', { class: 'cqa-ed__layout' }, versionsBox, main), replaceDialog, importDialog, confirmDialog, datalist, live);

  // ---------- small utilities
  function announce(message) {
    live.textContent = '';
    setTimeout(() => { if (!disposed) live.textContent = message; }, 30);
  }
  function toast(message) { if (typeof CQA.toast === 'function') CQA.toast(message); announce(message); }
  const on = (name, handler) => { const off = CQA.bus.on(name, handler); if (typeof off === 'function') subscriptions.push(off); };
  const versionItem = number => state.versions.find(item => item.version === number) || null;
  const nextVersion = () => state.versions.reduce((max, item) => Math.max(max, item.version), 0) + 1;
  const isMock = () => CQA.config?.mock !== false;
  function updateBudget() {
    budgetHost.hidden = !Number.isInteger(state.view) || typeof CQA.economics?.mountBudget !== 'function';
    if (!economicsData || typeof CQA.economics?.mountBudget !== 'function') return;
    const selected = state.group ? state.conversationRows.filter(row => (trimmed(row.group_id) || 'Без набора') === state.group) : state.conversationRows;
    const options = { quality: economicsData, profile: economicsConfig?.profile,
      plan: { dialogues: selected.length, channels: { chat: selected.filter(row => row.channel === 'chat').length, call: selected.filter(row => row.channel === 'call').length }, mode: economicsConfig?.mode },
      busy: !!state.busy || state.groupsLoading || !!state.groupsError };
    if (budgetController) budgetController.update(options);
    else budgetController = CQA.economics.mountBudget(budgetHost, { ...options, onSwitchCascade: async () => {
      if (state.busy || typeof CQA.api.setConfig !== 'function') return;
      const token = economicsEpoch;
      state.busy = 'profile'; updateBar();
      try {
        const config = await CQA.api.setConfig({ location: 'cloud', profile: 'cascade', mode: 'fast' });
        if (disposed || token !== economicsEpoch) return;
        economicsConfig = config; toast('Выбран каскад. Оценка ещё не запущена.');
      } finally {
        if (!disposed && state.busy === 'profile') { state.busy = ''; updateBar(); }
      }
    } });
  }
  async function loadEconomics() {
    if (typeof CQA.economics?.mountBudget !== 'function' || typeof CQA.api.modelGuide !== 'function') return;
    economicsContext = economicsSource();
    const token = ++economicsEpoch;
    economicsData = null; economicsConfig = null; budgetController?.destroy(); budgetController = null;
    budgetHost.textContent = 'Оценка бюджета загружается; расчёт ещё недоступен.'; updateBudget();
    try {
      const [quality, config] = await Promise.all([CQA.api.modelGuide(), CQA.api.config?.()]);
      if (disposed || token !== economicsEpoch) return;
      economicsData = quality; economicsConfig = config; updateBudget();
    } catch { if (!disposed && token === economicsEpoch) { budgetHost.hidden = false; budgetHost.textContent = 'Бюджет не рассчитан: сохранённые замеры недоступны.'; } }
  }
  function activeVersion() {
    const shared = toNumber(CQA.state?.activeChecklistVersion);
    if (Number.isInteger(state.sessionActive)) return state.sessionActive;
    if (Number.isInteger(state.listActive)) return state.listActive;
    if (Number.isInteger(shared) && versionItem(shared)) return shared;
    return state.versions.length === 1 ? state.versions[0].version : null;
  }
  function captureFocus() {
    const active = document.activeElement;
    if (!active || !host.contains(active)) return null;
    const key = active.dataset?.focusKey;
    if (!key) return null;
    let selection = null;
    try { if (typeof active.selectionStart === 'number') selection = [active.selectionStart, active.selectionEnd]; } catch { selection = null; }
    return { key, selection };
  }
  function focusKey(key, selection = null) {
    if (!key) return false;
    const target = [...host.querySelectorAll('[data-focus-key]')].find(node => node.dataset.focusKey === key && !node.disabled && !node.closest('[hidden]'));
    if (!target) return false;
    target.focus({ preventScroll: false });
    if (selection) { try { target.setSelectionRange(selection[0], selection[1]); } catch { /* not a text field */ } }
    return true;
  }
  function restoreFocus(saved) {
    if (pendingFocus) { const key = pendingFocus; pendingFocus = ''; if (focusKey(key)) return; }
    if (saved) focusKey(saved.key, saved.selection);
  }
  let batching = false;
  function rerender(region, build) {
    if (batching) { build(); return; }
    const saved = captureFocus();
    build();
    restoreFocus(saved);
  }

  // ---------- models of the current view
  async function getVersion(number) {
    if (!Number.isInteger(number)) return null;
    if (state.full.has(number)) return state.full.get(number);
    const item = versionItem(number);
    if (item && Array.isArray(item.criteria)) { state.full.set(number, item); return item; }
    const loaded = await CQA.api.checklist(number);
    const version = { ...loaded, version: toNumber(loaded?.version) || number };
    state.full.set(number, version);
    return version;
  }
  const viewedVersion = () => Number.isInteger(state.view) ? state.full.get(state.view) || null : null;
  function viewChecklist() {
    if (state.view === 'draft' && state.draft) return draftChecklist(state.draft);
    return viewedVersion();
  }
  function compareChecklist() { return Number.isInteger(state.compareTo) ? state.full.get(state.compareTo) || null : null; }
  function serverDiffFor(from, to) {
    if (!Number.isInteger(from) || !Number.isInteger(to) || from === to || typeof CQA.api.checklistDiff !== 'function') return null;
    const key = `${from}>${to}`;
    if (!serverDiffs.has(key)) {
      serverDiffs.set(key, { status: 'loading' });
      Promise.resolve().then(() => CQA.api.checklistDiff(from, to))
        .then(result => serverDiffs.set(key, { status: 'done', summary: serverDiffSummary(result) }), error => serverDiffs.set(key, { status: 'error', error: error?.message || 'нет ответа' }))
        .finally(() => { if (!disposed && state.view === to && state.compareTo === from) { renderDiffBody(); updateBar(); } });
    }
    return serverDiffs.get(key);
  }
  /** Server verdict for the saved view (null for drafts, mocks and while loading). */
  function authoritativeDiff() {
    if (!Number.isInteger(state.view)) return null;
    const entry = serverDiffFor(state.compareTo, state.view);
    return entry?.status === 'done' && entry.summary && !entry.summary.mock ? entry.summary : null;
  }
  function currentDiff() {
    const after = viewChecklist(); const before = compareChecklist();
    return after && before ? classifyDiff(before, after) : null;
  }
  function draftBaseDiff() {
    if (!state.draft) return null;
    const base = Number.isInteger(state.draft.base_version) ? state.full.get(state.draft.base_version) : null;
    return base ? classifyDiff(base, draftChecklist(state.draft)) : null;
  }
  function isDirty() {
    if (!state.draft) return false;
    const diff = draftBaseDiff();
    return diff ? diff.has_changes : state.draft.criteria.length > 0;
  }
  function defaultCompare(view) {
    const numbers = state.versions.map(item => item.version);
    if (view === 'draft') {
      const base = state.draft?.base_version;
      if (numbers.includes(base)) return base;
      return activeVersion() ?? numbers[0] ?? null;
    }
    const active = activeVersion();
    if (Number.isInteger(active) && active !== view && numbers.includes(active)) return active;
    const older = numbers.filter(number => number < view);
    return older.length ? Math.max(...older) : null;
  }

  // ---------- loading and views
  async function load({ keepView = false } = {}) {
    state.loading = true; state.loadError = '';
    host.setAttribute('aria-busy', 'true');
    clearTimeout(timers.skeleton);
    timers.skeleton = setTimeout(() => { if (state.loading && !disposed) { state.showSkeleton = true; renderAll(); } }, 300);
    try {
      const parsed = parseVersionList(await CQA.api.checklists());
      state.versions = parsed.versions;
      state.listActive = parsed.active;
      for (const item of parsed.versions) if (Array.isArray(item.criteria)) state.full.set(item.version, item);
    } catch (error) {
      state.loadError = error?.message || 'Не удалось загрузить версии чек-листа.';
    } finally {
      clearTimeout(timers.skeleton);
      state.loading = false; state.showSkeleton = false;
      host.removeAttribute('aria-busy');
    }
    try {
      if (!keepView || state.view === null) {
        if (state.draft) state.view = 'draft';
        else state.view = activeVersion() ?? state.versions[0]?.version ?? null;
      }
      if (Number.isInteger(state.view)) await getVersion(state.view);
      if (state.draft && Number.isInteger(state.draft.base_version) && versionItem(state.draft.base_version)) await getVersion(state.draft.base_version);
      if (!keepView || state.compareTo === null) state.compareTo = defaultCompare(state.view);
      if (Number.isInteger(state.compareTo)) await getVersion(state.compareTo);
    } catch (error) { state.loadError = error?.message || 'Не удалось открыть версию чек-листа.'; }
    renderAll();
    if (Number.isInteger(state.view)) ensureGroups();
  }
  async function openView(target) {
    try {
      if (Number.isInteger(target)) await getVersion(target);
      state.view = target;
      state.compareTo = defaultCompare(target);
      if (Number.isInteger(state.compareTo)) await getVersion(state.compareTo);
      state.saveError = ''; state.applyError = ''; state.noteError = ''; state.saveHint = '';
      renderAll();
      announce(target === 'draft' ? 'Открыт черновик чек-листа.' : `Открыта версия v${target}, только просмотр.`);
      if (Number.isInteger(target)) ensureGroups();
    } catch (error) { toast(error?.message || 'Не удалось открыть версию.'); }
  }
  async function selectView(target) {
    if (target === state.view) return;
    if (state.view === 'draft' && target !== 'draft' && isDirty()) {
      const ok = await confirmAction({
        title: 'Черновик не сохранён',
        message: `Перейти к v${target}? Черновик останется в списке версий и в браузере: к нему можно вернуться и сохранить позже.`,
        confirm: `Перейти к v${target}`, cancel: 'Остаться в черновике',
      });
      if (!ok) return;
    }
    await openView(target);
  }
  async function startDraftFrom(version) {
    if (state.draft && isDirty()) {
      const ok = await confirmAction({
        title: 'Заменить черновик?',
        message: `В текущем черновике есть несохранённые изменения. Новый черновик из v${version} заменит его.`,
        confirm: 'Заменить черновик', cancel: 'Оставить текущий', danger: true,
      });
      if (!ok) return false;
    }
    const source = await getVersion(version);
    if (!source) return false;
    state.draft = draftFromChecklist(source, { base_version: version, origin: 'version' });
    state.view = 'draft'; state.compareTo = version;
    state.expanded.clear(); state.touched.clear(); state.lint.clear(); state.showAll = false; state.undo = null; state.restoredAt = '';
    state.saveError = ''; state.noteError = ''; state.saveHint = '';
    persistNow();
    renderAll();
    announce(`Создан черновик из v${version}. Сохранение создаст v${nextVersion()}.`);
    return true;
  }
  function startBlankDraft() {
    state.draft = draftFromChecklist({ criteria: [] }, { origin: 'blank' });
    const item = blankUi('C1', 'Основная секция');
    state.draft.criteria.push(item);
    state.expanded.add(item.uid);
    state.view = 'draft'; state.compareTo = defaultCompare('draft');
    pendingFocus = `${item.uid}:title`;
    persistNow();
    renderAll();
  }

  // ---------- render: all regions
  function renderAll() {
    if (disposed) return;
    const saved = captureFocus();
    batching = true;
    try { renderVersions(); renderHead(); renderNotices(); renderSummary(); renderDiffHead(); renderDiffBody(); renderRun(); renderCriteria(false); renderBar(); }
    finally { batching = false; }
    restoreFocus(saved);
    if (state.highlight) {
      const node = listBox.querySelector(`.cqa-ed__item[data-uid="${state.highlight}"]`);
      state.highlight = '';
      if (node) {
        node.classList.add('is-highlight');
        node.scrollIntoView?.({ block: 'nearest', behavior: reducedMotion.matches ? 'auto' : 'smooth' });
        clearTimeout(timers.highlight);
        timers.highlight = setTimeout(() => node.classList.remove('is-highlight'), 1600);
      }
    }
  }
  function renderVersions() {
    const title = el('h2', { id: 'cqa-ed-versions-title', class: 'cqa-ed__aside-title', text: 'Версии' });
    const list = el('ul', { class: 'cqa-ed__version-list' });
    if (state.draft) {
      const diff = draftBaseDiff();
      const changes = diff ? diff.count : state.draft.criteria.length;
      list.append(el('li', {}, el('button', {
        type: 'button', class: 'cqa-ed__version cqa-ed__version--draft', 'data-action': 'view', 'data-version': 'draft', 'data-focus-key': 'version:draft',
        'aria-current': state.view === 'draft' ? 'true' : null,
      },
      el('span', { class: 'cqa-ed__version-top' }, icon('pencil'), el('strong', { text: `Черновик → v${nextVersion()}` })),
      el('span', { class: 'cqa-ed__version-name', text: state.draft.base_version ? `из v${state.draft.base_version}` : state.draft.origin === 'import' ? 'из импорта' : 'новый чек-лист' }),
      el('span', { class: 'cqa-ed__version-note', text: diff ? `${changes} ${plural(changes, 'изменение', 'изменения', 'изменений')}` : 'не сохранён' }),
      chip('Не сохранён', 'draft'))));
    }
    if (state.loading && state.showSkeleton && !state.versions.length) {
      for (let i = 0; i < 3; i += 1) list.append(el('li', { class: 'cqa-ed__skeleton-row', 'aria-hidden': 'true' }, el('span', { class: 'cqa-ed__skeleton' }), el('span', { class: 'cqa-ed__skeleton cqa-ed__skeleton--short' })));
    }
    const active = activeVersion();
    for (const item of state.versions) {
      const isActive = item.version === active;
      list.append(el('li', {}, el('button', {
        type: 'button', class: 'cqa-ed__version', 'data-action': 'view', 'data-version': String(item.version), 'data-focus-key': `version:${item.version}`,
        'aria-current': state.view === item.version ? 'true' : null,
      },
      el('span', { class: 'cqa-ed__version-top' }, el('strong', { text: `v${item.version}` }), isActive ? chip('Активная', 'active', 'check') : null),
      el('span', { class: 'cqa-ed__version-name', text: trimmed(item.name) || 'Без названия' }),
      trimmed(item.change_note) ? el('span', { class: 'cqa-ed__version-note', text: shorten(item.change_note, 120) }) : null,
      item.created_at ? el('time', { class: 'cqa-ed__version-date', datetime: trimmed(item.created_at), text: formatDate(item.created_at) }) : null)));
    }
    const nodes = [title, list];
    if (!state.loading && !state.versions.length && !state.draft) nodes.push(el('p', { class: 'cqa-ed__muted', text: 'Сохранённых версий пока нет.' }));
    versionsBox.replaceChildren(...nodes);
  }
  function renderHead() {
    const view = state.view;
    const heading = el('h1', { id: 'cqa-ed-title', text: 'Чек-лист' });
    const line = el('p', { class: 'cqa-ed__subtitle' });
    const actions = el('div', { class: 'cqa-ed__actions', role: 'group', 'aria-label': 'Действия с чек-листом' });
    if (view === 'draft' && state.draft) {
      line.append(chip(`Черновик → v${nextVersion()}`, 'draft', 'pencil'), el('span', { text: state.draft.base_version ? ` На основе v${state.draft.base_version} · «${shorten(state.draft.name, 60)}». Изменения хранятся в этом браузере до сохранения.` : ' Новая версия. Изменения хранятся в этом браузере до сохранения.' }));
      actions.append(
        button('Заменить критерий', { action: 'replace', iconName: 'swap', variant: 'accent', keys: ['R'], attrs: { 'data-focus-key': 'head:replace', 'aria-keyshortcuts': 'R' } }),
        button('Импорт JSON', { action: 'import', iconName: 'upload', attrs: { 'data-focus-key': 'head:import' } }),
        button('Экспорт JSON', { action: 'export', iconName: 'download', attrs: { 'data-focus-key': 'head:export' } }),
        button('Сбросить черновик', { action: 'discard', iconName: 'reset', variant: 'quiet', attrs: { 'data-focus-key': 'head:discard' } }));
    } else if (Number.isInteger(view)) {
      const item = versionItem(view) || viewedVersion() || {};
      const isActive = view === activeVersion();
      line.append(chip(`v${view}`, 'version'), isActive ? chip('Активная', 'active', 'check') : chip('Сохранена · не активна', 'neutral'), chip('Только просмотр', 'neutral', 'eye'),
        el('span', { text: ` ${shorten(item.name, 80)}${item.created_at ? ` · ${formatDate(item.created_at)}` : ''}` }));
      actions.append(
        button('Заменить критерий', { action: 'replace', iconName: 'swap', variant: 'accent', keys: ['R'], attrs: { 'data-focus-key': 'head:replace', 'aria-keyshortcuts': 'R' } }),
        button('Новая версия из этой', { action: 'new-from', iconName: 'layers', attrs: { 'data-focus-key': 'head:new-from' } }),
        button('Импорт JSON', { action: 'import', iconName: 'upload', attrs: { 'data-focus-key': 'head:import' } }),
        button('Экспорт JSON', { action: 'export', iconName: 'download', attrs: { 'data-focus-key': 'head:export' } }));
    } else if (!state.loading) {
      line.append(el('span', { text: state.loadError ? 'Версии не загрузились.' : 'Версий пока нет: импортируйте JSON или начните с нуля.' }));
      actions.append(button('Импорт JSON', { action: 'import', iconName: 'upload', attrs: { 'data-focus-key': 'head:import' } }));
    }
    const note = Number.isInteger(view) ? trimmed((versionItem(view) || {}).change_note) : '';
    head.replaceChildren(el('div', { class: 'cqa-ed__head-text' }, heading, line, note ? el('p', { class: 'cqa-ed__change-note' }, el('span', { class: 'cqa-ed__muted', text: 'Что изменили: ' }), note) : null), actions);
  }
  function renderNotices() {
    const nodes = [];
    if (state.loadError) nodes.push(notice('error', state.loadError, button('Повторить', { action: 'reload', iconName: 'reset', attrs: { 'data-focus-key': 'notice:reload' } })));
    if (state.restoredAt && state.view === 'draft') nodes.push(notice('info', `Восстановлен несохранённый черновик (${formatDate(state.restoredAt)}).`, button('Удалить черновик', { action: 'discard', iconName: 'trash', variant: 'quiet', attrs: { 'data-focus-key': 'notice:discard' } })));
    if (!state.storageOk && state.draft) nodes.push(notice('warning', 'Браузер не даёт сохранить черновик локально. Не закрывайте вкладку, пока не сохраните версию.'));
    if (state.serverNotes && state.view === state.serverNotes.version && state.serverNotes.warnings.length) nodes.push(notice('warning', `Сервер сохранил v${state.serverNotes.version} с предупреждениями: ${state.serverNotes.warnings.join(' ')}`));
    if (state.undo && state.view === 'draft') nodes.push(notice('info', `Критерий ${state.undo.criterion.id || 'без ID'} удалён из черновика.`, button('Вернуть', { action: 'undo', iconName: 'reset', attrs: { 'data-focus-key': 'notice:undo' } })));
    if (!state.loading && !state.loadError && !state.versions.length && !state.draft) {
      nodes.push(el('div', { class: 'cqa-ed__empty' }, el('h2', { text: 'Начните с чек-листа' }), el('p', { text: 'Импортируйте JSON организаторов или соберите критерии вручную. Версия появится после сохранения.' }),
        el('div', { class: 'cqa-ed__empty-actions' }, button('Импорт JSON', { action: 'import', iconName: 'upload', variant: 'accent', attrs: { 'data-focus-key': 'empty:import' } }), button('Создать с нуля', { action: 'blank', iconName: 'plus', attrs: { 'data-focus-key': 'empty:blank' } }))));
    }
    noticeBox.replaceChildren(...nodes);
  }
  function notice(kind, message, action = null) {
    return el('div', { class: `cqa-ed__notice cqa-ed__notice--${kind}`, role: kind === 'error' ? 'alert' : 'status' }, icon(kind === 'error' ? 'octagon' : kind === 'warning' ? 'alert' : 'info'), el('p', { text: message }), action);
  }

  function renderSummary() {
    const checklist = viewChecklist();
    if (!checklist) { summaryBox.replaceChildren(); summaryBox.hidden = true; return; }
    summaryBox.hidden = false;
    const result = validate(checklist);
    if (state.view === 'draft') state.validation = result;
    const criteria = checklist.criteria || [];
    const critical = criteria.filter(item => item.critical).length;
    const sections = new Set(criteria.map(item => trimmed(item.section)).filter(Boolean)).size;
    const parts = [];
    const sum = formatNumber(result.weightSum);
    if (result.weightOk) parts.push(el('p', { class: 'cqa-ed__metric cqa-ed__metric--ok' }, icon('check'), el('span', { text: `Сумма весов: ${sum} из 100` })));
    else {
      parts.push(el('p', { class: 'cqa-ed__metric cqa-ed__metric--warn' }, icon('alert'), el('span', { text: `Сумма весов: ${sum} — не 100. Вес всё равно перераспределится пропорционально.` })));
      if (state.view === 'draft') parts.push(button('Нормировать до 100', { action: 'normalize', iconName: 'percent', attrs: { 'data-focus-key': 'summary:normalize' } }));
    }
    parts.push(el('p', { class: 'cqa-ed__metric' }, el('span', { text: `${criteria.length} ${plural(criteria.length, 'критерий', 'критерия', 'критериев')} · ${sections} ${plural(sections, 'секция', 'секции', 'секций')}${critical ? ` · грубых ошибок: ${critical}` : ''}` })));
    if (state.view === 'draft') {
      const errors = result.errors.length;
      const warnings = result.warnings.filter(issue => issue.field !== 'weights').length;
      if (errors) parts.push(el('p', { class: 'cqa-ed__metric cqa-ed__metric--error' }, icon('octagon'), el('span', { text: `${errors} ${plural(errors, 'ошибка', 'ошибки', 'ошибок')} — сохранение недоступно` })), button('Показать ошибку', { action: 'first-error', iconName: 'chevron', attrs: { 'data-focus-key': 'summary:first-error' } }));
      else parts.push(el('p', { class: 'cqa-ed__metric cqa-ed__metric--ok' }, icon('check'), el('span', { text: 'Ошибок нет' })));
      if (warnings) parts.push(el('p', { class: 'cqa-ed__metric cqa-ed__metric--warn' }, icon('alert'), el('span', { text: `${warnings} ${plural(warnings, 'предупреждение', 'предупреждения', 'предупреждений')} (не мешают сохранению)` })));
    }
    rerender(summaryBox, () => summaryBox.replaceChildren(...parts));
  }

  // ---------- diff panel
  function renderDiffHead() {
    const after = viewChecklist();
    const options = state.versions.filter(item => item.version !== state.view);
    if (!after || (!options.length && state.view !== 'draft')) { diffBox.hidden = true; diffHead.replaceChildren(); return; }
    diffBox.hidden = false;
    const heading = el('h2', { id: 'cqa-ed-diff-title', text: state.view === 'draft' ? 'Что изменится' : 'Отличия версии' });
    const nodes = [heading];
    if (options.length) {
      const select = el('select', { id: 'cqa-ed-compare', class: 'cqa-ed__select', 'data-focus-key': 'diff:compare' });
      const active = activeVersion();
      for (const item of options) select.append(el('option', { value: String(item.version), text: `v${item.version}${item.version === active ? ' · активная' : ''}${state.draft?.base_version === item.version && state.view === 'draft' ? ' · основа' : ''}` }));
      if (Number.isInteger(state.compareTo)) select.value = String(state.compareTo);
      nodes.push(el('label', { class: 'cqa-ed__compare', for: 'cqa-ed-compare' }, el('span', { text: 'Сравнить с' }), select));
    }
    rerender(diffHead, () => diffHead.replaceChildren(...nodes));
  }
  function renderDiffBody() {
    if (diffBox.hidden) { diffBody.replaceChildren(); return; }
    const before = compareChecklist();
    const after = viewChecklist();
    if (!before || !after) { diffBody.replaceChildren(el('p', { class: 'cqa-ed__muted', text: state.view === 'draft' ? 'Это первая версия: сравнивать не с чем. Все критерии будут оценены.' : 'Нет версии для сравнения.' })); return; }
    const diff = classifyDiff(before, after);
    const total = (after.criteria || []).length;
    const nodes = [];
    const serverEntry = Number.isInteger(state.view) ? serverDiffFor(state.compareTo, state.view) : null;
    const server = serverEntry?.status === 'done' && serverEntry.summary && !serverEntry.summary.mock ? serverEntry.summary : null;
    if (server) {
      diff.needs_reeval = server.needs_reeval;
      diff.rescore_only = server.rescore_only.filter(id => !server.needs_reeval.includes(id));
      for (const item of diff.changed) item.needs_reeval = server.needs_reeval.includes(item.id);
      for (const item of diff.added) item.needs_reeval = server.needs_reeval.includes(item.id);
      const touched = new Set([...server.needs_reeval, ...diff.rescore_only, ...diff.removed.map(item => item.id)]);
      diff.reusable = (after.criteria || []).map(item => trimmed(item.id)).filter(id => !touched.has(id));
    }
    if (!diff.has_changes) nodes.push(el('p', { class: 'cqa-ed__verdict cqa-ed__verdict--ok' }, icon('check'), el('span', { text: `Нет отличий от v${state.compareTo}. Применение не потребует вызовов модели.` })));
    else {
      const reeval = diff.needs_reeval;
      const verdict = el('div', { class: `cqa-ed__verdict ${reeval.length ? 'cqa-ed__verdict--reeval' : 'cqa-ed__verdict--ok'}` }, icon(reeval.length ? 'alert' : 'check'));
      const lines = el('div', { class: 'cqa-ed__verdict-lines' });
      if (reeval.length) lines.append(el('p', {}, el('strong', { text: `Нужна переоценка: ${reeval.join(', ')}` }), ` — ${reeval.length} ${plural(reeval.length, 'критерий', 'критерия', 'критериев')} на каждом диалоге.`));
      else lines.append(el('p', {}, el('strong', { text: 'Переоценка не нужна' }), ' — 0 вызовов модели, итоги пересчитает движок.'));
      if (diff.rescore_only.length) lines.append(el('p', { text: `Вес/секция: ${diff.rescore_only.join(', ')} — 0 вызовов модели, только пересчёт итога.` }));
      if (diff.removed.length) lines.append(el('p', { text: `Удалены: ${diff.removed.map(item => item.id).join(', ')} — 0 вызовов модели, вес перераспределится.` }));
      const cached = diff.reusable.length;
      lines.append(el('p', { class: 'cqa-ed__muted', text: `Без изменений: ${cached} из ${total} — оценки берутся из кэша.` }));
      verdict.append(lines);
      nodes.push(verdict, reevalMap(diff, after));
      if (Number.isInteger(state.view)) nodes.push(serverLine(serverEntry));
      const list = el('ul', { class: 'cqa-ed__diff-list' });
      const openDetails = diff.changed.length <= 3;
      for (const item of diff.added) list.append(diffItem('added', item.id, item.title, 'Новый критерий', `Нужна оценка ${item.id}`, true, null));
      for (const item of diff.changed) list.append(diffItem('changed', item.id, item.title, item.title !== item.before_title && item.before_title ? `было: «${shorten(item.before_title, 50)}»` : item.fields.map(f => FIELD_LABELS[f.field] || f.field).join(', '), item.needs_reeval ? `Нужна переоценка ${item.id}` : 'Пересчёт без LLM: вес/секция, 0 вызовов модели', item.needs_reeval, item.fields, openDetails));
      for (const item of diff.removed) list.append(diffItem('removed', item.id, item.title, 'Удалён', 'Пересчёт без LLM', false, null));
      if (diff.reordered) list.append(el('li', { class: 'cqa-ed__diff-item cqa-ed__diff-item--info' }, icon('swap'), el('span', { text: 'Порядок критериев изменён — 0 вызовов модели.' })));
      for (const field of diff.checklist_fields) list.append(el('li', { class: 'cqa-ed__diff-item cqa-ed__diff-item--info' }, icon('info'), el('span', { text: field === 'rounding' ? 'Изменено округление итога — 0 вызовов модели, только пересчёт.' : 'Изменено название чек-листа.' })));
      nodes.push(list);
    }
    diffBody.replaceChildren(...nodes);
  }
  function serverLine(entry) {
    if (!entry) return null;
    if (entry.status === 'loading') return el('p', { class: 'cqa-ed__muted cqa-ed__loading' }, icon('spinner', 'cqa-ed__spin'), el('span', { text: 'Сверяем с сервером: какие оценки можно взять из кэша…' }));
    if (entry.status === 'error') return el('p', { class: 'cqa-ed__msg cqa-ed__msg--warning' }, icon('alert'), el('span', { text: `Сервер не вернул сравнение (${entry.error}). Показан расчёт интерфейса.` }));
    const summary = entry.summary;
    if (!summary) return null;
    const nodes = [el('p', { class: 'cqa-ed__server' }, icon('check'), el('span', { text: summary.mock ? 'Демо-сервер: решение о переоценке показано по правилам интерфейса.' : `Проверено сервером по хешам критериев${summary.needs_reeval.length ? `: переоценка ${summary.needs_reeval.join(', ')}` : ': переоценка не нужна'}.` }))];
    if (summary.warnings.length) nodes.push(el('ul', { class: 'cqa-ed__server-warnings', 'aria-label': 'Предупреждения сервера' }, ...summary.warnings.map(message => el('li', {}, icon('alert'), el('span', { text: message })))));
    return el('div', { class: 'cqa-ed__server-box' }, ...nodes);
  }
  function reevalMap(diff, after) {
    const status = new Map();
    for (const id of diff.reusable) status.set(id, 'cached');
    for (const item of diff.changed) status.set(item.id, item.needs_reeval ? 'reeval' : 'free');
    for (const item of diff.added) status.set(item.id, 'reeval');
    const labels = { cached: 'из кэша', free: 'пересчёт без LLM', reeval: 'нужна переоценка', removed: 'удалён' };
    const cells = (after.criteria || []).map(item => { const id = trimmed(item.id); return [id, status.get(id) || 'cached']; });
    for (const item of diff.removed) cells.push([item.id, 'removed']);
    const list = el('ul', { class: 'cqa-ed__map', 'aria-label': 'Карта переоценки по критериям' },
      ...cells.map(([id, kind]) => el('li', { class: `cqa-ed__cell cqa-ed__cell--${kind}`, title: `${id}: ${labels[kind]}` }, el('span', { class: 'cqa-ed__cell-id', text: id }), el('span', { class: 'cqa-visually-hidden', text: `: ${labels[kind]}` }))));
    const legend = el('ul', { class: 'cqa-ed__legend', 'aria-hidden': 'true' }, ...['reeval', 'free', 'cached'].map(kind => el('li', {}, el('span', { class: `cqa-ed__swatch cqa-ed__cell--${kind}` }), el('span', { text: labels[kind] }))));
    return el('div', { class: 'cqa-ed__map-wrap' }, list, legend);
  }
  function diffItem(kind, id, title, subtitle, badge, reeval, fields, open = false) {
    const item = el('li', { class: `cqa-ed__diff-item cqa-ed__diff-item--${kind}` });
    const kindLabel = { added: 'Добавлен', changed: 'Изменён', removed: 'Удалён' }[kind];
    const headLine = el('div', { class: 'cqa-ed__diff-head' },
      chip(kindLabel, kind === 'added' ? 'added' : kind === 'removed' ? 'removed' : 'changed', kind === 'added' ? 'plus' : kind === 'removed' ? 'trash' : 'pencil'),
      el('strong', { class: 'cqa-ed__diff-title', text: `${id} · ${shorten(title, 70) || 'без названия'}` }),
      chip(badge, reeval ? 'reeval' : 'free', reeval ? 'alert' : 'check'));
    item.append(headLine);
    if (subtitle && kind === 'changed') item.append(el('p', { class: 'cqa-ed__muted cqa-ed__diff-sub', text: subtitle }));
    if (fields?.length) {
      const details = el('details', { class: 'cqa-ed__diff-details', open: open || null });
      details.append(el('summary', { text: `Поля: ${fields.map(f => FIELD_LABELS[f.field] || f.field).join(', ')}` }));
      const dl = el('dl', { class: 'cqa-ed__diff-fields' });
      for (const field of fields) {
        dl.append(el('dt', {}, el('span', { text: FIELD_LABELS[field.field] || field.field }), chip(field.reeval ? 'переоценка' : 'без LLM', field.reeval ? 'reeval' : 'free')));
        dl.append(el('dd', {}, el('del', { class: 'cqa-ed__was' }, el('span', { class: 'cqa-ed__was-label', text: 'было' }), displayValue(field.field, field.before)), el('ins', { class: 'cqa-ed__now' }, el('span', { class: 'cqa-ed__was-label', text: 'стало' }), displayValue(field.field, field.after))));
      }
      details.append(dl);
      item.append(details);
    }
    return item;
  }
  function displayValue(field, value) {
    if (value === null || value === undefined || value === '' || (Array.isArray(value) && !value.length)) return '—';
    switch (field) {
      case 'answer_type': return TYPE_LABELS[value] || asText(value);
      case 'scale': return Array.isArray(value) ? value.map(item => formatNumber(item)).join(' / ') : asText(value);
      case 'scale_labels': return Array.isArray(value) ? value.map(([key, label]) => `${formatNumber(key)} — ${label}`).join('\n') : asText(value);
      case 'na_allowed': case 'critical': return value ? 'да' : 'нет';
      case 'critical_when': return value === 'no' ? '«Нет» = нарушение' : '«Да» = нарушение';
      case 'guidance': return Array.isArray(value) ? value.map((line, index) => `${index + 1}. ${line}`).join('\n') : asText(value);
      case 'weight': return formatNumber(value);
      default: return typeof value === 'string' ? value : JSON.stringify(value);
    }
  }

  // ---------- criteria list
  function renderCriteria(preserve = true) {
    const build = () => {
      datalist.replaceChildren(...[...new Set([...(state.draft?.criteria || []).map(item => trimmed(item.section)), ...state.versions.flatMap(item => (item.criteria || []).map(c => trimmed(c.section)))].filter(Boolean))].map(section => el('option', { value: section })));
      if (state.loading && state.showSkeleton && !state.versions.length && !state.draft) { listBox.replaceChildren(...[0, 1, 2].map(() => el('div', { class: 'cqa-ed__skeleton-card', 'aria-hidden': 'true' }, el('span', { class: 'cqa-ed__skeleton' }), el('span', { class: 'cqa-ed__skeleton cqa-ed__skeleton--short' })))); return; }
      if (state.view === 'draft' && state.draft) renderDraftList();
      else if (Number.isInteger(state.view) && viewedVersion()) renderReadonlyList(viewedVersion());
      else listBox.replaceChildren();
    };
    if (preserve) rerender(listBox, build); else build();
  }
  function statusMap() {
    const diff = currentDiff();
    const map = new Map();
    if (!diff) return map;
    for (const item of diff.added) map.set(item.id, { kind: 'added', label: 'Новый: нужна оценка', reeval: true });
    for (const item of diff.changed) map.set(item.id, item.needs_reeval ? { kind: 'reeval', label: 'Нужна переоценка', reeval: true } : { kind: 'free', label: 'Пересчёт без LLM', reeval: false });
    return map;
  }
  function sectionWeight(items) {
    return items.reduce((sum, item) => sum + (item.critical ? 0 : (Number.isNaN(toNumber(item.weight)) ? 0 : toNumber(item.weight))), 0);
  }
  function sectionBlocks(items) {
    const blocks = [];
    for (const item of items) {
      const section = trimmed(item.section);
      let block = blocks[blocks.length - 1];
      if (!block || block.section !== section) { block = { section, items: [] }; blocks.push(block); }
      block.items.push(item);
    }
    return blocks;
  }
  function sectionHeader(block, index) {
    const id = `cqa-ed-section-${index}`;
    const weight = sectionWeight(block.items);
    const allCritical = block.items.every(item => item.critical);
    return { id, node: el('div', { class: 'cqa-ed__section-head' }, el('h2', { id, text: block.section || 'Без секции' }), el('span', { class: 'cqa-ed__section-weight', text: allCritical ? 'флаг, без баллов' : `вес секции ${formatNumber(weight)}` })) };
  }
  function renderReadonlyList(version) {
    const statuses = statusMap();
    const blocks = sectionBlocks(version.criteria || []);
    listBox.replaceChildren(...blocks.map((block, blockIndex) => {
      const header = sectionHeader(block, blockIndex);
      const list = el('ol', { class: 'cqa-ed__list' });
      for (const criterion of block.items) list.append(readonlyCard(version, criterion, statuses.get(trimmed(criterion.id))));
      return el('section', { class: 'cqa-ed__section', 'aria-labelledby': header.id }, header.node, list);
    }));
  }
  function readonlyCard(version, criterion, status) {
    const k = canonicalCriterion(criterion);
    const key = `ro-${version.version}-${k.id}`.replace(/[^\w-]/g, '_');
    const expanded = state.expanded.has(key);
    const bodyId = `cqa-ed-${key}-body`;
    const item = el('li', { class: `cqa-ed__item${status ? ` is-${status.kind}` : ''}`, 'data-uid': key });
    const toggle = el('button', { type: 'button', class: 'cqa-ed__toggle', 'aria-expanded': String(expanded), 'aria-controls': bodyId, 'data-action': 'toggle', 'data-uid': key, 'data-focus-key': `${key}:toggle` },
      icon('chevron', 'cqa-ed__chevron'), el('span', { class: 'cqa-ed__id', text: k.id }), el('span', { class: 'cqa-ed__item-title', text: k.title || 'Без названия' }));
    const meta = el('div', { class: 'cqa-ed__meta' }, ...metaChips(k, status, null, null));
    const body = el('div', { class: 'cqa-ed__item-body', id: bodyId, hidden: !expanded });
    if (expanded) body.append(readonlyDetails(k));
    item.append(el('div', { class: 'cqa-ed__item-head' }, toggle), meta, el('p', { class: 'cqa-ed__desc', text: k.description }), body);
    return item;
  }
  function readonlyDetails(k) {
    const dl = el('dl', { class: 'cqa-ed__details' });
    const add = (term, ...value) => dl.append(el('dt', { text: term }), el('dd', {}, ...value));
    add('Формулировка', el('span', { class: 'cqa-ed__pre', text: k.description || '—' }));
    if (k.scale) {
      const list = el('ol', { class: 'cqa-ed__scale-view' });
      const labels = new Map((k.scale_labels || []).map(([key, label]) => [key, label]));
      for (const step of k.scale) list.append(el('li', {}, el('strong', { text: formatNumber(step) }), el('span', { text: labels.get(String(step)) || (k.answer_type === 'numeric' ? '' : 'без подписи') })));
      add('Тип ответа', el('span', { text: `${TYPE_LABELS[k.answer_type] || k.answer_type}${k.answer_type === 'numeric' ? ' в диапазоне' : ''}` }), list);
    } else add('Тип ответа', el('span', { text: TYPE_LABELS[k.answer_type] || k.answer_type }));
    add('Вес', el('span', { text: k.critical ? '0 — без баллов' : formatNumber(k.weight) }));
    add('N/A', el('span', { class: 'cqa-ed__pre', text: k.na_allowed ? `разрешён. ${k.na_rule}` : 'не разрешён' }));
    if (k.critical) add('Грубая ошибка', el('span', { class: 'cqa-ed__pre', text: `${k.critical_when === 'no' ? '«Нет»' : '«Да»'} = нарушение. Флаг без баллов.${k.critical_rule ? ` ${k.critical_rule}` : ''}` }));
    if (k.guidance.length) add('Уточнения для оценщика', el('ul', { class: 'cqa-ed__guidance-view' }, ...k.guidance.map(line => el('li', { text: line }))));
    if (k.section) add('Секция', el('span', { text: k.section }));
    return dl;
  }
  function metaChips(k, status, issues, lint) {
    const nodes = [];
    if (k.answer_type === 'binary') nodes.push(chip('Да/Нет', 'type'));
    else if (k.answer_type === 'numeric') {
      const numbers = (k.scale || []).filter(value => typeof value === 'number');
      nodes.push(chip(numbers.length ? `Число ${formatNumber(Math.min(...numbers))}–${formatNumber(Math.max(...numbers))}` : 'Число', 'type'));
    } else if (k.answer_type === 'scale') {
      const numbers = (k.scale || []).filter(value => typeof value === 'number');
      nodes.push(el('span', { class: 'cqa-ed__scale-chips', role: 'img', 'aria-label': `Шкала: ${numbers.map(value => formatNumber(value)).join(', ') || 'ступени не заданы'}` },
        el('span', { class: 'cqa-ed__scale-name', text: 'Шкала' }), ...numbers.map(value => el('span', { class: 'cqa-ed__step-chip', text: formatNumber(value) }))));
    }
    if (k.critical) nodes.push(chip(`Грубая ошибка · ${k.critical_when === 'no' ? '«Нет»' : '«Да»'} = нарушение`, 'critical', 'octagon'), chip('Без баллов', 'neutral'));
    else nodes.push(chip(`Вес ${typeof k.weight === 'number' ? formatNumber(k.weight) : '—'}`, 'weight'));
    if (k.na_allowed) nodes.push(chip('N/A разрешён', 'na'));
    if (status) nodes.push(chip(status.label, status.kind === 'free' ? 'free' : status.kind === 'added' ? 'added' : 'reeval', status.reeval ? 'alert' : 'check'));
    if (issues) {
      const errors = issues.filter(issue => issue.level === 'error').length;
      const warnings = issues.filter(issue => issue.level === 'warning').length;
      if (errors) nodes.push(chip(`${errors} ${plural(errors, 'ошибка', 'ошибки', 'ошибок')}`, 'error', 'octagon'));
      if (warnings) nodes.push(chip(`${warnings} ${plural(warnings, 'предупр.', 'предупр.', 'предупр.')}`, 'warn', 'alert'));
    }
    if (lint?.status === 'done' && lint.warnings.length) nodes.push(chip(`Линтер: ${lint.warnings.length}`, 'warn', 'lint'));
    return nodes;
  }

  function renderDraftList() {
    const draft = state.draft;
    const statuses = statusMap();
    const result = validate(draftChecklist(draft));
    state.validation = result;
    const blocks = sectionBlocks(draft.criteria);
    const sections = blocks.map((block, blockIndex) => {
      const header = sectionHeader(block, blockIndex);
      const list = el('ol', { class: 'cqa-ed__list' });
      block.items.forEach(ui => {
        const index = draft.criteria.indexOf(ui);
        list.append(draftCard(ui, index, result.issues.filter(issue => issue.index === index), statuses.get(trimmed(ui.id))));
      });
      const addButton = button(`Добавить критерий${block.section ? ` в «${shorten(block.section, 40)}»` : ''}`, { action: 'add', iconName: 'plus', variant: 'quiet', attrs: { 'data-section': block.section, 'data-focus-key': `add:${blockIndex}` } });
      return el('section', { class: 'cqa-ed__section', 'aria-labelledby': header.id }, header.node, list, el('div', { class: 'cqa-ed__section-foot' }, addButton));
    });
    if (!blocks.length) sections.push(el('div', { class: 'cqa-ed__empty' }, el('p', { text: 'В черновике нет критериев.' }), button('Добавить критерий', { action: 'add', iconName: 'plus', variant: 'accent', attrs: { 'data-section': '', 'data-focus-key': 'add:empty' } })));
    listBox.replaceChildren(...sections);
    applyIssues(listBox, result);
  }
  function draftCard(ui, index, issues, status) {
    const expanded = state.expanded.has(ui.uid);
    const bodyId = `cqa-ed-${ui.uid}-body`;
    const label = ui.id || 'критерий';
    const criteria = state.draft.criteria;
    const item = el('li', { class: `cqa-ed__item${status ? ` is-${status.kind}` : ''}`, 'data-uid': ui.uid });
    const moves = el('div', { class: 'cqa-ed__moves' },
      iconButton('up', `Переместить ${label} выше`, { 'data-action': 'up', 'data-uid': ui.uid, 'data-focus-key': `${ui.uid}:up`, disabled: !moveCriterion(criteria, index, -1) }),
      iconButton('down', `Переместить ${label} ниже`, { 'data-action': 'down', 'data-uid': ui.uid, 'data-focus-key': `${ui.uid}:down`, disabled: !moveCriterion(criteria, index, 1) }));
    const toggle = el('button', { type: 'button', class: 'cqa-ed__toggle', 'aria-expanded': String(expanded), 'aria-controls': bodyId, 'data-action': 'toggle', 'data-uid': ui.uid, 'data-focus-key': `${ui.uid}:toggle` },
      icon('chevron', 'cqa-ed__chevron'), el('span', { class: 'cqa-ed__id', text: ui.id || '—' }), el('span', { class: 'cqa-ed__item-title', text: ui.title || 'Без названия' }));
    const meta = el('div', { class: 'cqa-ed__meta' }, ...metaChips(canonicalCriterion(fromUi(ui)), status, issues, state.lint.get(ui.uid)));
    const body = el('div', { class: 'cqa-ed__item-body', id: bodyId, hidden: !expanded });
    if (expanded) {
      body.append(buildForm(ui, `cqa-ed-${ui.uid}`, 'draft'), lintBlock(ui, state.lint.get(ui.uid), 'draft'),
        el('div', { class: 'cqa-ed__item-actions' },
          button('Дублировать', { action: 'duplicate', iconName: 'copy', variant: 'quiet', attrs: { 'data-uid': ui.uid, 'data-focus-key': `${ui.uid}:duplicate` } }),
          button('Удалить', { action: 'delete', iconName: 'trash', variant: 'danger', attrs: { 'data-uid': ui.uid, 'data-focus-key': `${ui.uid}:delete` } })));
    }
    item.append(el('div', { class: 'cqa-ed__item-head' }, moves, toggle), meta, el('p', { class: 'cqa-ed__desc', text: ui.description }), body);
    return item;
  }

  // ---------- form building (shared by the draft list and the replace dialog)
  function fieldIds(prefix, field) { return { id: `${prefix}-${field}`, hint: `${prefix}-${field}-hint`, msg: `${prefix}-${field}-msg` }; }
  function messages(ids, field, uid) { return el('div', { class: 'cqa-ed__msgs', id: ids.msg, 'data-msg-for': field, 'data-uid': uid }); }
  function describedBy(ids, hint) { return [hint ? ids.hint : null, ids.msg].filter(Boolean).join(' '); }
  function textField(ui, prefix, field, label, options = {}) {
    const ids = fieldIds(prefix, field);
    const attrs = { id: ids.id, class: `cqa-ed__input${options.short ? ' cqa-ed__input--short' : ''}`, 'data-field': field, 'data-uid': ui.uid, 'data-focus-key': `${ui.uid}:${field}`, 'aria-describedby': describedBy(ids, options.hint), required: options.required || null, placeholder: options.placeholder || null, autocomplete: 'off' };
    const control = options.rows ? el('textarea', { ...attrs, rows: String(options.rows), value: ui[field] ?? '' }) : el('input', { ...attrs, type: 'text', value: ui[field] ?? '', list: options.list || null, inputmode: options.inputmode || null, spellcheck: options.spellcheck === false ? 'false' : null });
    return el('div', { class: `cqa-ed__field${options.wide ? ' cqa-ed__field--wide' : ''}` },
      el('label', { class: 'cqa-ed__label', for: ids.id }, el('span', { text: label }), options.required ? el('span', { class: 'cqa-ed__req', 'aria-hidden': 'true', text: '*' }) : null),
      options.hint ? el('p', { class: 'cqa-ed__hint', id: ids.hint, text: options.hint }) : null,
      control, messages(ids, field, ui.uid));
  }
  function buildForm(ui, prefix, scope) {
    const form = el('div', { class: 'cqa-ed__form' });
    if (scope === 'draft') {
      form.append(el('div', { class: 'cqa-ed__row' },
        textField(ui, prefix, 'id', 'ID', { short: true, required: true, hint: 'Например C9. Оценки и апелляции ссылаются на ID.', spellcheck: false }),
        textField(ui, prefix, 'section', 'Секция', { list: 'cqa-ed-sections', hint: 'Выберите из списка или введите новую.' })));
    }
    form.append(textField(ui, prefix, 'title', 'Название', { required: true, wide: true, placeholder: scope === 'wizard' ? 'Например: Предупреждение о рисках' : null, hint: scope === 'wizard' && wizard.old ? `Было: «${shorten(wizard.old.title, 90)}»` : null }));
    form.append(textField(ui, prefix, 'description', 'Формулировка — что проверяем', { required: true, wide: true, rows: 3, hint: scope === 'wizard' && wizard.old ? `Было: «${shorten(wizard.old.description, 140)}»` : 'Когда ставится «Да» или какая ступень. Оценщик видит этот текст и уточнения.' }));
    form.append(el('div', { class: 'cqa-ed__row' }, typeField(ui, prefix), weightField(ui, prefix)));
    if (ui.answer_type === 'scale' || ui.answer_type === 'numeric') form.append(stepsField(ui, prefix));
    form.append(naField(ui, prefix));
    if (scope === 'draft' || ui.critical) form.append(criticalField(ui, prefix, scope));
    form.append(guidanceField(ui, prefix));
    return form;
  }
  function typeField(ui, prefix) {
    const ids = fieldIds(prefix, 'answer_type');
    const group = el('div', { class: 'cqa-ed__segments' });
    for (const type of ANSWER_TYPES) {
      const id = `${prefix}-type-${type}`;
      group.append(el('label', { class: 'cqa-ed__segment', for: id },
        el('input', { type: 'radio', id, name: `${prefix}-type`, value: type, checked: ui.answer_type === type, 'data-field': 'answer_type', 'data-uid': ui.uid, 'data-focus-key': `${ui.uid}:answer_type:${type}`, 'aria-describedby': ids.msg }),
        el('span', { text: TYPE_LABELS[type] })));
    }
    return el('fieldset', { class: 'cqa-ed__field cqa-ed__fieldset' }, el('legend', { class: 'cqa-ed__label', text: 'Тип ответа' }), group, messages(ids, 'answer_type', ui.uid));
  }
  function weightField(ui, prefix) {
    const ids = fieldIds(prefix, 'weight');
    const locked = ui.critical && toNumber(ui.weight) === 0;
    const hint = locked ? 'Без баллов: грубая ошибка отмечается флагом и в итог не входит.' : 'Относительный вес. Если сумма отличается от 100, движок нормирует её пропорционально.';
    return el('div', { class: 'cqa-ed__field' },
      el('label', { class: 'cqa-ed__label', for: ids.id, text: 'Вес' }),
      el('p', { class: 'cqa-ed__hint', id: ids.hint, text: hint }),
      el('div', { class: 'cqa-ed__inline' },
        el('input', { id: ids.id, type: 'text', inputmode: 'decimal', class: 'cqa-ed__input cqa-ed__input--number', value: locked ? '0' : ui.weight, readonly: locked || null, 'data-field': 'weight', 'data-uid': ui.uid, 'data-focus-key': `${ui.uid}:weight`, 'aria-describedby': describedBy(ids, true), autocomplete: 'off' }),
        locked ? chip('без баллов', 'neutral') : null,
        ui.critical && !locked ? button('Обнулить вес', { action: 'zero-weight', iconName: 'zero', variant: 'quiet', attrs: { 'data-uid': ui.uid, 'data-focus-key': `${ui.uid}:zero-weight` } }) : null),
      messages(ids, 'weight', ui.uid));
  }
  function stepsField(ui, prefix) {
    const numeric = ui.answer_type === 'numeric';
    const ids = fieldIds(prefix, 'scale');
    const head = el('div', { class: 'cqa-ed__step-head', 'aria-hidden': 'true' }, el('span', { text: '№' }), el('span', { id: `${prefix}-col-value`, text: 'Значение' }), el('span', { id: `${prefix}-col-label`, text: numeric ? 'Подпись (необязательно)' : 'Подпись: когда ставится' }), el('span'));
    const list = el('ol', { class: 'cqa-ed__steps' });
    ui.steps.forEach((step, index) => {
      const rowId = `${prefix}-step-${index}`;
      list.append(el('li', { class: 'cqa-ed__step' },
        el('span', { class: 'cqa-ed__step-num', id: `${rowId}-num`, text: `${index + 1}` }),
        el('input', { type: 'text', inputmode: 'decimal', class: 'cqa-ed__input cqa-ed__input--number', value: step.value, 'aria-labelledby': `${prefix}-col-value ${rowId}-num`, 'aria-describedby': `${rowId}-msg ${ids.msg}`, 'data-field': 'step-value', 'data-index': String(index), 'data-uid': ui.uid, 'data-focus-key': `${ui.uid}:step-value:${index}`, autocomplete: 'off' }),
        el('input', { type: 'text', class: 'cqa-ed__input', value: step.label, 'aria-labelledby': `${prefix}-col-label ${rowId}-num`, 'data-field': 'step-label', 'data-index': String(index), 'data-uid': ui.uid, 'data-focus-key': `${ui.uid}:step-label:${index}`, autocomplete: 'off' }),
        iconButton('x', `Удалить ступень ${index + 1}`, { 'data-action': 'remove-step', 'data-index': String(index), 'data-uid': ui.uid, 'data-focus-key': `${ui.uid}:remove-step:${index}`, disabled: ui.steps.length <= 2 }),
        el('div', { class: 'cqa-ed__msgs cqa-ed__step-msg', id: `${rowId}-msg`, 'data-msg-for': `scale.${index}`, 'data-uid': ui.uid })));
    });
    return el('fieldset', { class: 'cqa-ed__field cqa-ed__fieldset cqa-ed__field--wide' },
      el('legend', { class: 'cqa-ed__label', text: numeric ? 'Диапазон числа' : 'Ступени шкалы' }),
      el('p', { class: 'cqa-ed__hint', id: ids.hint, text: numeric ? 'Минимум и максимум: оценщик ставит любое число в диапазоне. Балл = значение / максимум × вес.' : 'По возрастанию, без повторов. Балл = значение / максимум × вес: верхняя ступень даёт полный вес.' }),
      el('div', { class: 'cqa-ed__scale-chips', 'aria-hidden': 'true' }, el('span', { class: 'cqa-ed__scale-name', text: numeric ? 'Диапазон' : 'Ступени' }), ...ui.steps.map(step => el('span', { class: `cqa-ed__step-chip${Number.isNaN(toNumber(step.value)) ? ' is-invalid' : ''}`, text: trimmed(step.value) || '?' }))),
      head, list,
      button(numeric ? 'Добавить значение' : 'Добавить ступень', { action: 'add-step', iconName: 'plus', variant: 'quiet', attrs: { 'data-uid': ui.uid, 'data-focus-key': `${ui.uid}:add-step` } }),
      messages(ids, 'scale', ui.uid), el('div', { class: 'cqa-ed__msgs', id: `${prefix}-scale_labels-msg`, 'data-msg-for': 'scale_labels', 'data-uid': ui.uid }));
  }
  function naField(ui, prefix) {
    const id = `${prefix}-na_allowed`;
    return el('div', { class: 'cqa-ed__field cqa-ed__field--wide cqa-ed__group' },
      el('label', { class: 'cqa-ed__switch', for: id }, el('input', { type: 'checkbox', role: 'switch', id, checked: ui.na_allowed, 'data-field': 'na_allowed', 'data-uid': ui.uid, 'data-focus-key': `${ui.uid}:na_allowed` }), el('span', { class: 'cqa-ed__switch-track', 'aria-hidden': 'true' }), el('span', { text: 'N/A разрешён: критерий бывает неприменим' })),
      ui.na_allowed ? textField(ui, prefix, 'na_rule', 'Когда ставится N/A', { required: true, wide: true, rows: 2, hint: 'Например: «N/A — запрос не требовал идентификации». При N/A вес критерия делится между остальными.' }) : null);
  }
  function criticalField(ui, prefix, scope) {
    const id = `${prefix}-critical`;
    const nodes = [];
    if (scope === 'draft') nodes.push(el('label', { class: 'cqa-ed__switch', for: id }, el('input', { type: 'checkbox', role: 'switch', id, checked: ui.critical, 'data-field': 'critical', 'data-uid': ui.uid, 'data-focus-key': `${ui.uid}:critical` }), el('span', { class: 'cqa-ed__switch-track', 'aria-hidden': 'true' }), el('span', { text: 'Грубая ошибка: флаг без баллов' })));
    if (ui.critical) {
      const whenId = `${prefix}-critical_when`;
      const select = el('select', { id: whenId, class: 'cqa-ed__select', 'data-field': 'critical_when', 'data-uid': ui.uid, 'data-focus-key': `${ui.uid}:critical_when`, 'aria-describedby': `${whenId}-msg` },
        el('option', { value: 'yes', text: '«Да» — нарушение выявлено (как C8)' }), el('option', { value: 'no', text: '«Нет» — требование не выполнено' }));
      select.value = ui.critical_when === 'no' ? 'no' : 'yes';
      nodes.push(el('p', { class: 'cqa-ed__callout' }, icon('octagon'), el('span', { text: 'Полярность задаётся полем ниже. Вес 0, в итог не входит, отмечается отдельным флагом и всегда уходит проверяющему.' })),
        el('div', { class: 'cqa-ed__field' }, el('label', { class: 'cqa-ed__label', for: whenId, text: 'Нарушение, когда ответ' }), select, el('div', { class: 'cqa-ed__msgs', id: `${whenId}-msg`, 'data-msg-for': 'critical_when', 'data-uid': ui.uid })),
        textField(ui, prefix, 'critical_rule', 'Что считается грубой ошибкой', { wide: true, rows: 2 }));
    }
    return el('div', { class: 'cqa-ed__field cqa-ed__field--wide cqa-ed__group' }, ...nodes);
  }
  function guidanceField(ui, prefix) {
    const list = el('ol', { class: 'cqa-ed__guidance' });
    ui.guidance.forEach((line, index) => {
      const id = `${prefix}-guidance-${index}`;
      list.append(el('li', { class: 'cqa-ed__guidance-item' },
        el('label', { class: 'cqa-visually-hidden', for: id, text: `Уточнение ${index + 1}` }),
        el('textarea', { id, rows: '2', class: 'cqa-ed__input', value: line, 'data-field': 'guidance', 'data-index': String(index), 'data-uid': ui.uid, 'data-focus-key': `${ui.uid}:guidance:${index}` }),
        iconButton('x', `Удалить уточнение ${index + 1}`, { 'data-action': 'remove-guidance', 'data-index': String(index), 'data-uid': ui.uid, 'data-focus-key': `${ui.uid}:remove-guidance:${index}` })));
    });
    return el('fieldset', { class: 'cqa-ed__field cqa-ed__fieldset cqa-ed__field--wide' },
      el('legend', { class: 'cqa-ed__label', text: 'Уточнения для оценщика' }),
      el('p', { class: 'cqa-ed__hint', text: 'Необязательно. Пограничные случаи и примеры; меняют оценку, поэтому требуют переоценки критерия.' }),
      list, button('Добавить уточнение', { action: 'add-guidance', iconName: 'plus', variant: 'quiet', attrs: { 'data-uid': ui.uid, 'data-focus-key': `${ui.uid}:add-guidance` } }));
  }
  function lintBlock(ui, entry, scope) {
    const box = el('div', { class: 'cqa-ed__lint', 'data-lint-for': ui.uid });
    fillLint(box, ui, entry, scope);
    return box;
  }
  function fillLint(box, ui, entry, scope) {
    const stale = entry && entry.sig !== lintSignature(ui) && entry.status !== 'loading';
    const status = el('div', { class: 'cqa-ed__lint-status', role: 'status' });
    if (!entry) status.append(el('p', { class: 'cqa-ed__muted', text: 'Линтер ищет двусмысленности и пересечения с другими критериями. Запускается после паузы в наборе или по кнопке.' }));
    else if (entry.status === 'loading') status.append(el('p', { class: 'cqa-ed__muted cqa-ed__loading' }, icon('spinner', 'cqa-ed__spin'), el('span', { text: 'Проверяем формулировку…' })));
    else if (entry.status === 'error') status.append(el('p', { class: 'cqa-ed__lint-error' }, icon('alert'), el('span', { text: `Линтер недоступен: ${entry.error}. Сохранение не блокируется.` })));
    else if (!entry.warnings.length) status.append(el('p', { class: 'cqa-ed__lint-ok' }, icon('check'), el('span', { text: `Замечаний нет.${entry.mock ? ' Демо-линтер: проверка упрощённая.' : ''}` })));
    else {
      status.append(el('p', { class: 'cqa-ed__lint-title', text: `Линтер: ${entry.warnings.length} ${plural(entry.warnings.length, 'предупреждение', 'предупреждения', 'предупреждений')} — сохранению не мешает${entry.mock ? ' (демо)' : ''}` }));
      status.append(el('ul', { class: 'cqa-ed__lint-list' }, ...entry.warnings.map(warning => el('li', {}, icon('alert'), el('span', { text: warning.message })))));
    }
    if (stale) status.prepend(el('p', { class: 'cqa-ed__muted', text: 'Текст изменился — результат проверки устарел.' }));
    box.replaceChildren(el('div', { class: 'cqa-ed__lint-head' }, el('span', { class: 'cqa-ed__lint-label' }, icon('lint'), el('span', { text: 'Линтер формулировки' })),
      button('Проверить формулировку', { action: 'lint', iconName: 'lint', variant: 'quiet', attrs: { 'data-uid': ui.uid, 'data-scope': scope, 'data-focus-key': `${ui.uid}:lint`, 'aria-busy': entry?.status === 'loading' ? 'true' : null } })), status);
  }

  // ---------- validation messages in place
  function issueKey(issue) { return issue.field === 'scale' && Number.isInteger(issue.step) ? `scale.${issue.step}` : issue.field; }
  function visibleIssue(ui, issue, touched, showAll) {
    if (showAll || !ui?.isNew) return true;
    if (!['id', 'title', 'description', 'na_rule'].includes(issue.field)) return true;
    return touched.has(`${ui.uid}:${issue.field}`);
  }
  function applyIssues(container, result, single = null) {
    const byUid = new Map();
    if (single) byUid.set(single.ui.uid, { ui: single.ui, issues: single.issues, touched: wizard.touched, showAll: wizard.showAll });
    else state.draft?.criteria.forEach((ui, index) => byUid.set(ui.uid, { ui, issues: result.issues.filter(issue => issue.index === index), touched: state.touched, showAll: state.showAll }));
    for (const node of container.querySelectorAll('[data-msg-for]')) {
      const entry = byUid.get(node.dataset.uid);
      if (!entry) continue;
      const field = node.dataset.msgFor;
      const list = entry.issues.filter(issue => issueKey(issue) === field && visibleIssue(entry.ui, issue, entry.touched, entry.showAll));
      const signature = JSON.stringify(list.map(issue => [issue.level, issue.message]));
      if (node.dataset.signature === signature) continue;
      node.dataset.signature = signature;
      node.replaceChildren(...list.map(issue => el('p', { class: `cqa-ed__msg cqa-ed__msg--${issue.level}` }, icon(issue.level === 'error' ? 'octagon' : 'alert'), el('span', { text: issue.message }))));
    }
    for (const input of container.querySelectorAll('[data-field][data-uid]')) {
      const entry = byUid.get(input.dataset.uid);
      if (!entry) continue;
      const key = input.dataset.field === 'step-value' ? `scale.${input.dataset.index}` : input.dataset.field;
      const invalid = entry.issues.some(issue => issue.level === 'error' && issueKey(issue) === key && visibleIssue(entry.ui, issue, entry.touched, entry.showAll));
      if (invalid) input.setAttribute('aria-invalid', 'true'); else input.removeAttribute('aria-invalid');
    }
  }
  function refreshCards(result) {
    const statuses = statusMap();
    state.draft?.criteria.forEach((ui, index) => {
      const card = listBox.querySelector(`.cqa-ed__item[data-uid="${ui.uid}"]`);
      if (!card) return;
      const issues = result.issues.filter(issue => issue.index === index);
      const status = statuses.get(trimmed(ui.id));
      card.classList.remove('is-added', 'is-reeval', 'is-free');
      if (status) card.classList.add(`is-${status.kind}`);
      const id = card.querySelector('.cqa-ed__id'); if (id) id.textContent = ui.id || '—';
      const title = card.querySelector('.cqa-ed__item-title'); if (title) title.textContent = ui.title || 'Без названия';
      const desc = card.querySelector(':scope > .cqa-ed__desc'); if (desc) desc.textContent = ui.description;
      const meta = card.querySelector(':scope > .cqa-ed__meta'); if (meta) meta.replaceChildren(...metaChips(canonicalCriterion(fromUi(ui)), status, issues, state.lint.get(ui.uid)));
    });
  }
  function scheduleRefresh() {
    if (timers.refresh) return;
    timers.refresh = requestAnimationFrame(() => { timers.refresh = 0; refreshDerived(); });
  }
  function refreshDerived() {
    if (disposed) return;
    if (state.view === 'draft' && state.draft) {
      const result = validate(draftChecklist(state.draft));
      state.validation = result;
      applyIssues(listBox, result);
      refreshCards(result);
    }
    renderSummary(); renderDiffBody(); updateBar();
    rerender(versionsBox, renderVersions);
  }

  // ---------- editing
  function findUi(uid) { return uid === 'wizard' ? wizard.ui : state.draft?.criteria.find(item => item.uid === uid) || null; }
  function defaultSteps(type) { return type === 'numeric' ? [{ value: '0', label: '' }, { value: '10', label: '' }] : [{ value: '0', label: '' }, { value: '1', label: '' }, { value: '2', label: '' }]; }
  function setAnswerType(ui, type) {
    if (!ANSWER_TYPES.includes(type)) return;
    ui.answer_type = type;
    if (type !== 'binary' && ui.steps.length < 2) ui.steps = defaultSteps(type);
  }
  function setCritical(ui, enabled) {
    if (enabled) {
      ui.memo.weight = ui.weight;
      ui.critical = true; ui.weight = '0'; ui.critical_when = ui.critical_when || 'yes';
      if (!trimmed(ui.critical_rule)) ui.critical_rule = DEFAULT_CRITICAL_RULE;
      if (ui.answer_type !== 'binary') ui.answer_type = 'binary';
    } else {
      ui.critical = false;
      if (ui.memo.weight !== undefined) ui.weight = ui.memo.weight;
    }
  }
  function applyField(ui, target, kind) {
    const field = target.dataset.field;
    const index = Number(target.dataset.index);
    switch (field) {
      case 'section': ui.section = target.value; return kind === 'change' ? 'section' : 'input';
      case 'id': case 'title': case 'description': case 'na_rule': case 'critical_rule': ui[field] = target.value; return 'input';
      case 'weight': if (!target.readOnly) ui.weight = target.value; return 'input';
      case 'step-value': {
        if (ui.steps[index]) ui.steps[index].value = target.value;
        const chips = target.closest('.cqa-ed__fieldset')?.querySelectorAll('.cqa-ed__scale-chips .cqa-ed__step-chip');
        if (chips?.[index]) { chips[index].textContent = trimmed(target.value) || '?'; chips[index].classList.toggle('is-invalid', Number.isNaN(toNumber(target.value))); }
        return 'input';
      }
      case 'step-label': if (ui.steps[index]) ui.steps[index].label = target.value; return 'input';
      case 'guidance': if (index < ui.guidance.length) ui.guidance[index] = target.value; return 'input';
      case 'answer_type': setAnswerType(ui, target.value); return 'structure';
      case 'na_allowed': ui.na_allowed = target.checked; if (ui.na_allowed && !trimmed(ui.na_rule)) pendingFocus = `${ui.uid}:na_rule`; return 'structure';
      case 'critical': setCritical(ui, target.checked); return 'structure';
      case 'critical_when': ui.critical_when = target.value === 'no' ? 'no' : 'yes'; return 'input';
      default: return null;
    }
  }
  const LINT_FIELDS = new Set(['title', 'description', 'na_rule', 'critical_rule', 'guidance', 'step-label', 'answer_type', 'na_allowed']);
  function onFieldEvent(event, kind) {
    const target = event.target;
    const field = target?.dataset?.field; const uid = target?.dataset?.uid;
    if (!field || !uid) return;
    const toggleLike = target.type === 'checkbox' || target.type === 'radio' || target.tagName === 'SELECT';
    if (toggleLike && kind === 'input') return;
    if (!toggleLike && kind === 'change' && field !== 'section') return;
    const ui = findUi(uid);
    if (!ui) return;
    const result = applyField(ui, target, kind);
    if (!result) return;
    if (uid === 'wizard') { afterWizardEdit(result, field); return; }
    if (result === 'section') regroupSection(ui);
    if (result === 'structure' || result === 'section') renderCriteria();
    scheduleRefresh();
    persistSoon();
    if (LINT_FIELDS.has(field)) scheduleLint(uid);
  }
  function regroupSection(ui) {
    const list = state.draft.criteria;
    const from = list.indexOf(ui);
    if (from < 0) return;
    const section = trimmed(ui.section);
    list.splice(from, 1);
    let last = -1;
    list.forEach((item, index) => { if (trimmed(item.section) === section) last = index; });
    list.splice(last >= 0 ? last + 1 : from, 0, ui);
    state.draft.criteria = groupBySection(list, state.draft.sections);
    pendingFocus = `${ui.uid}:section`;
  }
  function moveItem(uid, delta) {
    const list = state.draft.criteria;
    const index = list.findIndex(item => item.uid === uid);
    const next = moveCriterion(list, index, delta);
    if (!next) return;
    state.draft.criteria = next;
    const ui = next.find(item => item.uid === uid);
    const sameSection = next.filter(item => trimmed(item.section) === trimmed(ui.section));
    renderCriteria();
    focusKey(`${uid}:${delta < 0 ? 'up' : 'down'}`) || focusKey(`${uid}:${delta < 0 ? 'down' : 'up'}`) || focusKey(`${uid}:toggle`);
    scheduleRefresh(); persistSoon();
    announce(`${ui.id || 'Критерий'} перемещён ${delta < 0 ? 'выше' : 'ниже'}: позиция ${sameSection.indexOf(ui) + 1} из ${sameSection.length} в секции «${trimmed(ui.section) || 'без секции'}».`);
  }
  function addCriterion(section) {
    const draft = state.draft;
    const id = nextCriterionId(draft.criteria);
    const ui = blankUi(id, section);
    let last = -1;
    draft.criteria.forEach((item, index) => { if (trimmed(item.section) === trimmed(section)) last = index; });
    draft.criteria.splice(last >= 0 ? last + 1 : draft.criteria.length, 0, ui);
    state.expanded.add(ui.uid);
    pendingFocus = `${ui.uid}:title`;
    renderCriteria(); scheduleRefresh(); persistSoon();
    announce(`Добавлен критерий ${id}. Заполните название и формулировку.`);
  }
  function duplicateItem(uid) {
    const list = state.draft.criteria;
    const index = list.findIndex(item => item.uid === uid);
    if (index < 0) return;
    const source = list[index];
    const copy = { ...clone({ ...source, memo: {} }), uid: newUid(), id: nextCriterionId(list), title: `${source.title} (копия)`, isNew: false };
    list.splice(index + 1, 0, copy);
    state.expanded.add(copy.uid);
    pendingFocus = `${copy.uid}:title`;
    renderCriteria(); scheduleRefresh(); persistSoon();
    announce(`Создана копия ${source.id}: ${copy.id}. Не забудьте поправить вес.`);
  }
  function deleteItem(uid) {
    const list = state.draft.criteria;
    const index = list.findIndex(item => item.uid === uid);
    if (index < 0) return;
    const [removed] = list.splice(index, 1);
    state.undo = { criterion: removed, index };
    const next = list[index] || list[index - 1];
    pendingFocus = next ? `${next.uid}:toggle` : 'notice:undo';
    renderNotices(); renderCriteria(); scheduleRefresh(); persistSoon();
    announce(`Критерий ${removed.id || 'без ID'} удалён. Кнопка «Вернуть» отменит удаление.`);
  }
  function undoDelete() {
    if (!state.undo || !state.draft) return;
    const { criterion, index } = state.undo;
    state.draft.criteria.splice(Math.min(index, state.draft.criteria.length), 0, criterion);
    state.undo = null;
    pendingFocus = `${criterion.uid}:toggle`;
    renderNotices(); renderCriteria(); scheduleRefresh(); persistSoon();
    announce(`Критерий ${criterion.id} возвращён.`);
  }
  function addStep(ui) {
    const values = ui.steps.map(step => toNumber(step.value)).filter(value => !Number.isNaN(value));
    const last = values.length ? values[values.length - 1] : 0;
    const prev = values.length > 1 ? values[values.length - 2] : last - 1;
    ui.steps.push({ value: asText(Math.max(last + (last - prev || 1), last + 1)), label: '' });
    return `${ui.uid}:step-value:${ui.steps.length - 1}`;
  }
  function normalize() {
    const next = normalizeWeights(state.draft.criteria);
    state.draft.criteria.forEach((ui, index) => { if (!ui.critical) ui.weight = asText(next[index].weight); });
    renderCriteria(); scheduleRefresh(); persistSoon();
    pendingFocus = '';
    announce('Веса нормированы пропорционально: сумма 100. Переоценка не нужна.');
  }
  function firstError() {
    state.showAll = true;
    const result = validate(draftChecklist(state.draft));
    const issue = result.errors.find(item => item.scope === 'criterion');
    if (!issue) { renderCriteria(); scheduleRefresh(); return; }
    const ui = state.draft.criteria[issue.index];
    state.expanded.add(ui.uid);
    const key = issue.field === 'scale' && Number.isInteger(issue.step) ? `${ui.uid}:step-value:${issue.step}` : issue.field === 'answer_type' ? `${ui.uid}:answer_type:${ui.answer_type || 'binary'}` : `${ui.uid}:${issue.field}`;
    pendingFocus = key;
    renderCriteria(); scheduleRefresh();
    announce(`${ui.id || 'Критерий'}: ${issue.message}`);
  }

  // ---------- linter
  function scheduleLint(uid) {
    clearTimeout(timers.lint.get(uid));
    timers.lint.set(uid, setTimeout(() => runLint(uid, false), 1200));
  }
  function lintContext(uid) {
    const list = state.draft ? state.draft.criteria.filter(item => item.uid !== uid).map(fromUi) : (viewChecklist()?.criteria || []).filter(item => trimmed(item.id) !== wizard.targetId);
    return list.map(item => ({ id: trimmed(item.id), title: trimmed(item.title), description: trimmed(item.description) }));
  }
  async function runLint(uid, manual) {
    const ui = findUi(uid);
    if (!ui) return;
    const isWizard = uid === 'wizard';
    const store = isWizard ? { get: () => wizard.lint, set: value => { wizard.lint = value; } } : { get: () => state.lint.get(uid), set: value => state.lint.set(uid, value) };
    if (!trimmed(ui.title) && !trimmed(ui.description)) {
      if (manual) { store.set({ status: 'error', sig: lintSignature(ui), error: 'сначала заполните название и формулировку', warnings: [] }); updateLint(uid); }
      return;
    }
    const sig = lintSignature(ui);
    if (!manual && store.get()?.sig === sig && store.get()?.status !== 'error') return;
    const token = ++lintSequence;
    store.set({ status: 'loading', sig, token, warnings: [] });
    updateLint(uid);
    try {
      const body = { ...contractCriterion(fromUi(ui)), checklist_id: state.draft?.checklist_id || viewChecklist()?.checklist_id || null, others: lintContext(uid) };
      const response = await CQA.api.lintChecklist(body);
      if (disposed || store.get()?.token !== token) return;
      store.set({ status: 'done', sig, token, warnings: lintWarnings(response), mock: !!response?.is_mock || isMock() });
    } catch (error) {
      if (disposed || store.get()?.token !== token) return;
      store.set({ status: 'error', sig, token, error: error?.message || 'нет ответа', warnings: [] });
    }
    updateLint(uid);
  }
  function updateLint(uid) {
    const container = uid === 'wizard' ? replaceDialog : listBox;
    const box = container.querySelector(`[data-lint-for="${uid}"]`);
    const ui = findUi(uid);
    if (box && ui) { const saved = captureFocus(); fillLint(box, ui, uid === 'wizard' ? wizard.lint : state.lint.get(uid), uid === 'wizard' ? 'wizard' : 'draft'); restoreFocus(saved); }
    if (uid !== 'wizard' && state.validation) refreshCards(state.validation);
  }

  // ---------- storage
  function persistSoon() { clearTimeout(timers.persist); timers.persist = setTimeout(persistNow, 300); }
  function persistNow() {
    clearTimeout(timers.persist);
    const ok = state.draft ? storageSet({ ...state.draft, saved_at: new Date().toISOString() }) : storageRemove();
    if (ok !== state.storageOk) { state.storageOk = ok; renderNotices(); }
  }
  async function discardDraft() {
    if (!state.draft) return;
    if (isDirty()) {
      const ok = await confirmAction({ title: 'Сбросить черновик?', message: 'Все несохранённые изменения будут удалены. Сохранённые версии не изменятся.', confirm: 'Сбросить', cancel: 'Отмена', danger: true });
      if (!ok) return;
    }
    const base = state.draft.base_version;
    state.draft = null; state.restoredAt = ''; state.undo = null; state.lint.clear(); state.touched.clear(); state.showAll = false;
    persistNow();
    await openView(Number.isInteger(base) && versionItem(base) ? base : activeVersion() ?? state.versions[0]?.version ?? null);
    announce('Черновик сброшен.');
  }

  // ---------- action bar: save, apply, preview
  function renderBar() {
    const nodes = [];
    if (state.view === 'draft' && state.draft) {
      const status = el('p', { class: 'cqa-ed__bar-status', id: 'cqa-ed-bar-status' });
      const note = el('input', { id: 'cqa-ed-note', type: 'text', class: 'cqa-ed__input', value: state.draft.change_note, required: true, maxlength: '300', autocomplete: 'off', placeholder: 'Например: заменён C5 на «Предупреждение о рисках»', 'aria-describedby': 'cqa-ed-note-msg cqa-ed-bar-status', 'data-focus-key': 'bar:note' });
      const save = button(`Сохранить как v${nextVersion()}`, { action: 'save', iconName: 'save', variant: 'primary', keys: IS_MAC ? ['⌘', 'S'] : ['Ctrl', 'S'], attrs: { id: 'cqa-ed-save', 'data-focus-key': 'bar:save', 'aria-keyshortcuts': 'Control+S Meta+S' } });
      nodes.push(status, el('div', { class: 'cqa-ed__bar-field' },
        el('label', { class: 'cqa-ed__label', for: 'cqa-ed-note' }, el('span', { text: 'Что изменили' }), el('span', { class: 'cqa-ed__req', 'aria-hidden': 'true', text: '*' })),
        note, el('div', { class: 'cqa-ed__msgs', id: 'cqa-ed-note-msg', role: 'alert' })), el('div', { class: 'cqa-ed__bar-actions' }, save));
    } else if (Number.isInteger(state.view)) {
      const version = state.view;
      const status = el('p', { class: 'cqa-ed__bar-status', id: 'cqa-ed-bar-status' });
      const actions = el('div', { class: 'cqa-ed__bar-actions' });
      const groups = state.groups && state.groups.length > 1 ? state.groups : null;
      if (groups) {
        const select = el('select', { id: 'cqa-ed-group', class: 'cqa-ed__select', 'data-focus-key': 'bar:group' }, el('option', { value: '', text: `Все диалоги (${groups.reduce((sum, group) => sum + group.count, 0)})` }), ...groups.map(group => el('option', { value: group.id, text: `${group.id} (${group.count})` })));
        select.value = state.group;
        actions.append(el('label', { class: 'cqa-ed__set', for: 'cqa-ed-group' }, el('span', { text: 'Набор' }), select));
      }
      actions.append(
        button('Предпросмотр на 5 диалогах', { action: 'preview', iconName: 'eye', attrs: { id: 'cqa-ed-preview', 'data-focus-key': 'bar:preview' } }),
        button(`Применить v${version} к диалогам`, { action: 'apply', iconName: 'play', variant: 'primary', attrs: { id: 'cqa-ed-apply', 'data-focus-key': 'bar:apply' } }));
      if (state.groupsError) actions.append(button('Обновить наборы', { action: 'refresh-groups', iconName: 'reset' }));
      nodes.push(status, el('p', { class: 'cqa-ed__preview-hint', id: 'cqa-ed-preview-hint', role: 'status' }), el('div', { class: 'cqa-ed__msgs cqa-ed__bar-error', id: 'cqa-ed-apply-msg', role: 'alert' }), actions);
    }
    bar.hidden = !nodes.length;
    bar.className = `cqa-ed__bar ${state.view === 'draft' ? 'cqa-ed__bar--draft' : 'cqa-ed__bar--saved'}`;
    rerender(bar, () => bar.replaceChildren(...nodes));
    updateBar();
  }
  function updateBar() {
    updateBudget();
    const status = bar.querySelector('#cqa-ed-bar-status');
    if (state.view === 'draft' && state.draft) {
      const diff = draftBaseDiff();
      const result = state.validation || validate(draftChecklist(state.draft));
      if (status) {
        const parts = [];
        if (diff) parts.push(diff.has_changes ? `${diff.count} ${plural(diff.count, 'изменение', 'изменения', 'изменений')} к v${state.draft.base_version}` : `Нет изменений к v${state.draft.base_version}`);
        if (diff?.needs_reeval.length) parts.push(`переоценка: ${diff.needs_reeval.join(', ')}`);
        else if (diff?.has_changes) parts.push('без вызовов модели');
        if (!result.ok) parts.push(`${result.errors.length} ${plural(result.errors.length, 'ошибка', 'ошибки', 'ошибок')}`);
        status.textContent = parts.join(' · ') || 'Черновик';
      }
      const msg = bar.querySelector('#cqa-ed-note-msg');
      const note = bar.querySelector('#cqa-ed-note');
      const text = state.noteError || state.saveError || state.saveHint;
      if (msg && msg.dataset.text !== text) {
        msg.dataset.text = text;
        msg.replaceChildren(...(text ? [el('p', { class: `cqa-ed__msg cqa-ed__msg--${state.saveHint && !state.noteError && !state.saveError ? 'warning' : 'error'}` }, icon(state.saveHint && !state.noteError && !state.saveError ? 'alert' : 'octagon'), el('span', { text }))] : []));
      }
      if (note) { if (state.noteError) note.setAttribute('aria-invalid', 'true'); else note.removeAttribute('aria-invalid'); }
      const save = bar.querySelector('#cqa-ed-save');
      if (save) {
        const busy = state.busy === 'save';
        save.setAttribute('aria-disabled', String(busy)); save.setAttribute('aria-busy', String(busy));
        save.querySelector('.cqa-ed__btn-label').textContent = busy ? 'Сохраняем…' : `Сохранить как v${nextVersion()}`;
      }
    } else if (Number.isInteger(state.view)) {
      const version = state.view;
      const item = versionItem(version) || {};
      const isActive = version === activeVersion();
      if (status) {
        const server = authoritativeDiff();
        const client = server ? null : currentDiff();
        const reeval = server ? server.needs_reeval : client?.needs_reeval;
        const parts = [`v${version}${isActive ? ', активная' : ''}`];
        if (Number.isInteger(state.compareTo) && reeval) parts.push(reeval.length ? `применение к v${state.compareTo}: переоценка ${reeval.join(', ')}, остальное из кэша` : `применение к v${state.compareTo}: без вызовов модели`);
        if (server?.warnings.length) parts.push(`предупреждений сервера: ${server.warnings.length}`);
        status.textContent = parts.join('. ');
      }
      const previewHint = bar.querySelector('#cqa-ed-preview-hint');
      if (previewHint) {
        const plan = previewSelection();
        previewHint.textContent = state.groupsLoading ? 'Подбираем диалоги для предпросмотра…'
          : !plan.ids.length ? 'Нет диалогов для предпросмотра.'
            : plan.unscored ? `Предпросмотр: ${plan.ids.length} диалогов. У ${plan.unscored} нет оценок текущей версии — они будут оценены полностью (~${plan.unscored} пакетных вызовов в быстром режиме; повторы и каскад увеличивают число).`
              : `Предпросмотр: ${plan.ids.length} диалогов с оценками текущей версии v${activeVersion()}. Неизменённые критерии можно использовать повторно.`;
      }
      const msg = bar.querySelector('#cqa-ed-apply-msg');
      const error = state.applyError || state.groupsError;
      if (msg && msg.dataset.text !== error) { msg.dataset.text = error; msg.replaceChildren(...(error ? [el('p', { class: 'cqa-ed__msg cqa-ed__msg--error' }, icon('octagon'), el('span', { text: error }))] : [])); }
      for (const [id, kind, label] of [['cqa-ed-apply', 'apply', `Применить v${version} к диалогам`], ['cqa-ed-preview', 'preview', 'Предпросмотр на 5 диалогах']]) {
        const node = bar.querySelector(`#${id}`);
        if (!node) continue;
        const busy = state.busy === kind;
        const waitingForGroups = state.groupsLoading || !!state.groupsError;
        node.disabled = !!state.busy || waitingForGroups;
        node.setAttribute('aria-disabled', String(node.disabled)); node.setAttribute('aria-busy', String(busy));
        node.querySelector('.cqa-ed__btn-label').textContent = busy ? (kind === 'apply' ? 'Применяем…' : 'Запускаем…') : kind === 'apply' && state.groupsLoading ? 'Обновляем наборы…' : label;
      }
    }
  }
  async function save() {
    if (!state.draft || state.busy) return;
    state.saveError = ''; state.noteError = ''; state.saveHint = '';
    const checklist = draftChecklist(state.draft);
    const result = validate(checklist);
    if (!result.ok) {
      state.saveError = `Исправьте ${result.errors.length} ${plural(result.errors.length, 'ошибку', 'ошибки', 'ошибок')}: версия не сохранена.`;
      updateBar(); firstError();
      return;
    }
    const diff = draftBaseDiff();
    if (diff && !diff.has_changes) { state.saveError = `Нет изменений относительно v${state.draft.base_version}: сохранять нечего.`; updateBar(); focusKey('bar:save'); return; }
    const note = trimmed(state.draft.change_note);
    if (!note) { state.noteError = 'Опишите, что изменили: это видно в истории версий и в отчёте.'; updateBar(); focusKey('bar:note'); return; }
    const body = buildBody(checklist, { change_note: note, parent_version: state.draft.base_version });
    state.busy = 'save'; updateBar();
    try {
      const created = await CQA.api.createChecklist(body);
      const number = toNumber(created?.version);
      if (!Number.isInteger(number)) throw new Error('Сервер не вернул номер версии. Черновик сохранён в браузере.');
      const base = state.draft.base_version;
      ownSaveVersion = number;
      if (Number.isInteger(base) && isObject(created?.diff)) {
        serverDiffs.set(`${base}>${number}`, { status: 'done', summary: serverDiffSummary({ ...created.diff, warnings: [...(created.diff.warnings || []), ...(Array.isArray(created.warnings) ? created.warnings : [])] }) });
      } else if (Array.isArray(created?.warnings) && created.warnings.length) {
        state.serverNotes = { version: number, warnings: created.warnings.map(item => trimmed(typeof item === 'string' ? item : item?.message)).filter(Boolean) };
      }
      const saved = { ...body, ...created, version: number };
      delete saved.diff; delete saved.warnings;
      state.full.set(number, saved);
      state.draft = null; state.restoredAt = ''; state.undo = null; state.lint.clear(); state.touched.clear(); state.showAll = false;
      persistNow();
      state.busy = '';
      state.view = number;
      state.compareTo = Number.isInteger(base) && state.full.has(base) ? base : null;
      pendingFocus = 'bar:apply';
      await load({ keepView: true });
      if (!versionItem(number)) { state.versions.unshift(saved); state.versions.sort((a, b) => b.version - a.version); pendingFocus = 'bar:apply'; renderAll(); }
      toast(`Сохранено как v${number}. Чтобы оценки пересчитались, нажмите «Применить v${number}».`);
      CQA.bus.emit('checklist:saved', { ...saved, ...(created || {}), version: number });
    } catch (error) {
      state.busy = '';
      state.saveError = `Не удалось сохранить: ${error?.message || 'ошибка сервера'}. Черновик не потерян.`;
      updateBar();
      focusKey('bar:save');
      announce(state.saveError);
    } finally { ownSaveVersion = null; }
  }
  async function ensureGroups({ refresh = false } = {}) {
    if (disposed || typeof CQA.api.conversations !== 'function' || (!refresh && (state.groups || state.groupsLoading))) return;
    const token = ++groupsToken;
    state.groupsLoading = true;
    state.groupsError = '';
    if (Number.isInteger(state.view)) updateBar();
    try {
      const rows = await CQA.api.conversations();
      if (disposed || token !== groupsToken) return;
      state.conversationRows = Array.isArray(rows) ? rows : [];
      const counts = new Map();
      for (const row of Array.isArray(rows) ? rows : []) { const group = trimmed(row?.group_id) || 'Без набора'; if (!counts.has(group)) counts.set(group, []); counts.get(group).push(row.id); }
      state.groups = [...counts.entries()].map(([id, ids]) => ({ id, count: ids.length, ids }));
      if (state.group && !state.groups.some(group => group.id === state.group)) state.group = '';
    } catch (error) {
      if (disposed || token !== groupsToken) return;
      state.groupsError = `Не удалось обновить наборы диалогов: ${error?.message || 'ошибка сервера'}. Обновите список перед применением.`;
    } finally {
      if (!disposed && token === groupsToken) {
        state.groupsLoading = false;
        if (Number.isInteger(state.view)) renderBar();
      }
    }
  }
  async function apply() {
    const version = state.view;
    if (!Number.isInteger(version) || state.busy || state.groupsLoading || state.groupsError) return;
    state.busy = 'apply'; state.applyError = ''; updateBar();
    try {
      const group = state.groups?.find(item => item.id === state.group);
      // Let the run preserve the old active version as its before/after baseline.
      // Activation is reported only after the server confirms a successful run.
      const body = group ? { version, activate: true, conv_ids: group.ids.slice() } : { version, activate: true };
      if (economicsConfig?.profile) { body.profile = economicsConfig.profile; body.mode = economicsConfig.mode; }
      const run = await CQA.api.createRun(body);
      const runId = run?.run_id ?? run?.id;
      if (runId == null || runId === '') throw new Error('сервер не вернул номер прогона');
      state.busy = '';
      CQA.bus.emit('run:started', { run_id: runId, version, ...(group ? { group_id: group.id, conv_ids: body.conv_ids } : {}) });
      const hasBoard = CQA.state?.modules?.board === true;
      startPolling(runId, { kind: 'apply', version, group: group?.id || '', external: hasBoard });
      renderAll();
      if (hasBoard) CQA.bus.emit('nav:open', 'board');
      else focusKey('run:title');
      toast(`Применение v${version} запущено${group ? ` на наборе ${group.id}` : ''}. Версия станет активной после успешного прогона.`);
    } catch (error) {
      state.busy = '';
      state.applyError = `Не удалось подтвердить запуск применения v${version}: ${error?.message || 'ошибка сервера'}.`;
      renderAll();
      announce(state.applyError);
    }
  }
  function previewSelection() {
    const rows = state.group ? state.conversationRows.filter(row => (trimmed(row.group_id) || 'Без набора') === state.group) : state.conversationRows;
    return selectPreview(rows, activeVersion());
  }
  async function preview() {
    const version = state.view;
    if (!Number.isInteger(version) || state.busy || state.groupsLoading || state.groupsError) return;
    const settings = economicsConfig?.profile ? { profile: economicsConfig.profile, mode: economicsConfig.mode } : {};
    state.busy = 'preview'; state.applyError = ''; updateBar();
    try {
      // Refresh metadata only: selecting a sample must never call the model.
      const rows = await CQA.api.conversations();
      state.conversationRows = Array.isArray(rows) ? rows : [];
      const plan = previewSelection();
      if (!plan.ids.length) throw new Error('нет диалогов для предпросмотра');
      const run = await CQA.api.createRun({ version, preview: true, conv_ids: plan.ids, ...settings });
      const runId = run?.run_id ?? run?.id;
      if (runId == null || runId === '') throw new Error('сервер не вернул номер прогона');
      state.busy = '';
      startPolling(runId, { kind: 'preview', version, external: false });
      renderAll();
      focusKey('run:title');
    } catch (error) {
      state.busy = '';
      state.applyError = `Предпросмотр не запущен: ${error?.message || 'ошибка сервера'}.`;
      updateBar();
      announce(state.applyError);
    }
  }

  // ---------- run status (inline when there is no board module)
  function startPolling(runId, meta) {
    clearTimeout(timers.poll);
    const token = ++pollToken;
    state.run = { id: runId, ...meta, data: null, error: '', failures: 0, announced: false };
    if (meta.external) { renderRun(); return; }
    const tick = async () => {
      if (disposed || token !== pollToken) return;
      try {
        const data = await CQA.api.run(runId);
        if (disposed || token !== pollToken) return;
        state.run.data = data; state.run.error = ''; state.run.failures = 0;
        const summary = runSummary(data);
        CQA.bus.emit('run:progress', { run_id: runId, version: meta.version, preview: meta.kind === 'preview', ...summary, per_conv: undefined, changes: undefined });
        renderRun();
        if (summary.finished) {
          if (!state.run.announced) {
            state.run.announced = true;
            if (!summary.failed && data.activated === true && meta.kind === 'apply') {
              state.sessionActive = meta.version;
              CQA.state.activeChecklistVersion = meta.version;
              CQA.bus.emit('checklist:activated', { version: meta.version });
            }
            announce(summary.failed ? 'Прогон завершился с ошибкой.' : `Прогон завершён: ${summary.done} из ${summary.total}. Итог изменился у ${summary.changes.length} ${plural(summary.changes.length, 'диалога', 'диалогов', 'диалогов')}.`);
            if (!summary.failed && meta.kind === 'apply') CQA.bus.emit('run:finished', { run_id: runId, version: meta.version });
          }
          return;
        }
      } catch (error) {
        if (disposed || token !== pollToken) return;
        state.run.failures += 1;
        state.run.error = error?.message || 'Нет ответа от сервера.';
        renderRun();
        if (state.run.failures >= 5) return;
      }
      timers.poll = setTimeout(tick, 500);
    };
    tick();
  }
  function renderRun() {
    const run = state.run;
    if (!run) { runBox.hidden = true; runBox.replaceChildren(); return; }
    runBox.hidden = false;
    const summary = runSummary(run.data);
    const title = run.kind === 'preview' ? `Предпросмотр v${run.version} на 5 диалогах` : `Применение v${run.version}${run.group ? ` · ${run.group}` : ''}`;
    const stateLabel = run.external ? ['Открыто на табло', 'neutral'] : run.error && run.failures >= 5 ? ['Нет связи', 'error'] : summary.failed ? ['Ошибка', 'error'] : summary.finished ? ['Готово', 'ok'] : ['Идёт', 'progress'];
    const nodes = [el('div', { class: 'cqa-ed__panel-head' }, el('h2', { id: 'cqa-ed-run-title', tabindex: '-1', 'data-focus-key': 'run:title', text: title }), chip(stateLabel[0], stateLabel[1], stateLabel[1] === 'ok' ? 'check' : stateLabel[1] === 'error' ? 'octagon' : null))];
    if (run.external) {
      nodes.push(el('p', { text: `Прогон ${run.id} запущен. Прогресс показывает табло.` }), button('Открыть табло', { action: 'open-board', iconName: 'open', attrs: { 'data-focus-key': 'run:board' } }));
    } else {
      const progress = el('div', { class: 'cqa-ed__progress', role: 'progressbar', 'aria-label': 'Оценено диалогов', 'aria-valuemin': '0', 'aria-valuemax': String(summary.total || 0), 'aria-valuenow': String(summary.done || 0) }, el('span', { class: 'cqa-ed__progress-fill' }));
      progress.style.setProperty('--cqa-ed-progress', String(summary.progress));
      const stats = el('dl', { class: 'cqa-ed__stats' });
      const stat = (term, value) => stats.append(el('div', {}, el('dt', { text: term }), el('dd', { text: value })));
      stat('Готово', summary.total ? `${summary.done} из ${summary.total}` : run.data ? '0' : '…');
      stat('Из кэша', summary.from_cache == null ? '—' : formatNumber(summary.from_cache, 0));
      stat('Вызовов модели', summary.llm_calls == null ? '—' : formatNumber(summary.llm_calls, 0));
      stat('Стоимость', summary.cost_kzt == null ? '—' : `${formatNumber(summary.cost_kzt)} ₸`);
      stat('Время', summary.elapsed == null ? '—' : `${formatNumber(summary.elapsed, 1)} с`);
      nodes.push(progress, stats);
      if (summary.mock || isMock()) nodes.push(el('p', { class: 'cqa-ed__muted', text: 'Демо-данные: стоимость и итоги условные.' }));
      if (run.error) nodes.push(el('p', { class: 'cqa-ed__msg cqa-ed__msg--error' }, icon('octagon'), el('span', { text: `${run.error}${run.failures >= 5 ? ' Опрос остановлен.' : ' Повторяем…'}` })));
      if (run.error && run.failures >= 5) nodes.push(button('Повторить опрос', { action: 'repoll', iconName: 'reset', attrs: { 'data-focus-key': 'run:repoll' } }));
      const rows = run.kind === 'preview' ? summary.per_conv : summary.changes.slice(0, 6);
      if (rows.length) {
        const list = el('ul', { class: 'cqa-ed__run-list' });
        for (const row of rows) {
          const delta = Number.isFinite(row.old_total) && Number.isFinite(row.new_total) ? row.new_total - row.old_total : null;
          list.append(el('li', {},
            el('span', { class: 'cqa-ed__run-id', text: asText(row.id) }),
            el('span', { class: 'cqa-ed__run-total', text: `${row.old_total == null ? '—' : formatNumber(row.old_total)} → ${row.new_total == null ? '…' : formatNumber(row.new_total)}` }),
            delta == null ? el('span') : chip(`${delta > 0 ? '+' : ''}${formatNumber(delta)}`, delta > 0.004 ? 'up' : delta < -0.004 ? 'down' : 'neutral'),
            button('Открыть', { action: 'open-conv', iconName: 'open', variant: 'quiet', attrs: { 'data-id': asText(row.id), 'aria-label': `Открыть диалог ${row.id}`, 'data-focus-key': `run:open:${row.id}` } })));
        }
        nodes.push(el('p', { class: 'cqa-ed__run-caption', text: run.kind === 'preview' ? 'Итог до → после на диалогах предпросмотра:' : summary.finished ? `Итог изменился у ${summary.changes.length} ${plural(summary.changes.length, 'диалога', 'диалогов', 'диалогов')}. Сильнее всего:` : 'Уже пересчитаны:' }), list);
      } else if (summary.finished && run.kind === 'apply') nodes.push(el('p', { class: 'cqa-ed__muted', text: 'Итоги не изменились.' }));
    }
    nodes.push(button('Скрыть', { action: 'hide-run', iconName: 'x', variant: 'quiet', attrs: { 'data-focus-key': 'run:hide' } }));
    rerender(runBox, () => runBox.replaceChildren(...nodes));
  }

  // ---------- export
  function exportJson() {
    let data; let filename;
    if (state.view === 'draft' && state.draft) { data = buildBody(draftChecklist(state.draft), { change_note: state.draft.change_note, parent_version: state.draft.base_version }); filename = `checklist-draft-v${nextVersion()}.json`; }
    else if (Number.isInteger(state.view) && viewedVersion()) { data = clone(viewedVersion()); delete data.is_mock; filename = `checklist-v${state.view}.json`; }
    else return;
    try {
      const blob = new Blob([`${JSON.stringify(data, null, 2)}\n`], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const link = el('a', { href: url, download: filename, class: 'cqa-visually-hidden', tabindex: '-1' });
      host.append(link); link.click(); link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 2000);
      toast(`Файл ${filename} скачан.`);
    } catch (error) { toast(`Не удалось скачать файл: ${error?.message || 'ошибка браузера'}.`); }
  }

  // ---------- dialogs
  function openDialog(dialog, focusTarget) {
    dialog.classList.remove('is-closing');
    if (!dialog.open) { try { dialog.showModal(); } catch { dialog.setAttribute('open', ''); } }
    if (focusTarget) setTimeout(() => focusTarget.focus(), 0);
  }
  function closeDialog(dialog, returnFocus, after = null) {
    const finish = () => {
      dialog.classList.remove('is-closing');
      if (dialog.open) { try { dialog.close(); } catch { dialog.removeAttribute('open'); } }
      if (returnFocus && returnFocus.isConnected && !returnFocus.disabled) returnFocus.focus();
      if (typeof after === 'function') after();
    };
    if (reducedMotion.matches || !dialog.open) { finish(); return; }
    dialog.classList.add('is-closing');
    setTimeout(finish, 140);
  }
  function confirmAction({ title, message, confirm = 'Продолжить', cancel = 'Отмена', danger = false }) {
    return new Promise(resolve => {
      const opener = document.activeElement;
      const cancelButton = button(cancel, { variant: 'secondary' });
      const okButton = button(confirm, { variant: danger ? 'danger-solid' : 'primary' });
      let done = false;
      const finish = value => { if (done) return; done = true; closeDialog(confirmDialog, opener, () => resolve(value)); };
      cancelButton.addEventListener('click', () => finish(false));
      okButton.addEventListener('click', () => finish(true));
      confirmDialog.oncancel = event => { event.preventDefault(); finish(false); };
      confirmDialog.replaceChildren(el('h2', { id: 'cqa-ed-confirm-title', text: title }), el('p', { id: 'cqa-ed-confirm-text', text: message }), el('div', { class: 'cqa-ed__dialog-actions' }, cancelButton, okButton));
      openDialog(confirmDialog, cancelButton);
    });
  }

  // replace wizard
  function wizardSource() { return state.view === 'draft' && state.draft ? draftChecklist(state.draft) : viewChecklist(); }
  function initWizardUi(old, keep = null) {
    const base = toUi(old);
    const ui = { ...base, uid: 'wizard', title: keep?.title ?? '', description: keep?.description ?? '', na_rule: keep?.na_rule ?? '', guidance: keep?.guidance ?? [],
      steps: base.steps.map(step => ({ value: step.value, label: '' })), critical_rule: base.critical ? base.critical_rule : '', isNew: true };
    return ui;
  }
  function openReplace(targetId) {
    const source = wizardSource();
    if (!source?.criteria?.length) { toast('В чек-листе нет критериев для замены.'); return; }
    const ids = source.criteria.map(item => trimmed(item.id));
    const expanded = state.view === 'draft' ? state.draft.criteria.find(item => state.expanded.has(item.uid))?.id : null;
    wizard.targetId = ids.includes(targetId) ? targetId : ids.includes(trimmed(expanded)) ? trimmed(expanded) : ids[0];
    wizard.old = source.criteria.find(item => trimmed(item.id) === wizard.targetId);
    wizard.ui = initWizardUi(wizard.old);
    wizard.showAll = false; wizard.touched = new Set(); wizard.lint = null;
    wizard.opener = document.activeElement;
    renderWizard();
    openDialog(replaceDialog, replaceDialog.querySelector('#cqa-ed-wiz-target'));
  }
  function renderWizard() {
    const source = wizardSource();
    const select = el('select', { id: 'cqa-ed-wiz-target', class: 'cqa-ed__select', 'data-focus-key': 'wiz:target' }, ...source.criteria.map(item => el('option', { value: trimmed(item.id), text: `${trimmed(item.id)} · ${shorten(item.title, 60)}` })));
    select.value = wizard.targetId;
    const old = canonicalCriterion(wizard.old);
    const was = el('div', { class: 'cqa-ed__was-box' },
      el('p', { class: 'cqa-ed__muted', text: 'Сейчас' }),
      el('p', {}, el('strong', { text: `${old.id} · ${old.title}` })),
      el('p', { class: 'cqa-ed__pre', text: old.description }),
      el('div', { class: 'cqa-ed__meta' }, ...metaChips(old, null, null, null)));
    const form = el('div', { class: 'cqa-ed__wiz-form' }, el('h3', { class: 'cqa-ed__wiz-subtitle', text: `Новый критерий на месте ${wizard.targetId}` }), buildForm(wizard.ui, 'cqa-ed-wiz', 'wizard'));
    const impact = el('section', { class: 'cqa-ed__impact', 'aria-labelledby': 'cqa-ed-wiz-impact-title', 'aria-live': 'polite' });
    const lint = lintBlock(wizard.ui, wizard.lint, 'wizard');
    const body = el('div', { class: 'cqa-ed__dialog-body' },
      el('div', { class: 'cqa-ed__field' }, el('label', { class: 'cqa-ed__label', for: 'cqa-ed-wiz-target', text: 'Какой критерий заменить' }), select),
      was, form, impact, lint);
    const footer = el('div', { class: 'cqa-ed__dialog-actions' },
      button('Отмена', { action: 'wiz-cancel', attrs: { 'data-focus-key': 'wiz:cancel' } }),
      button('Заменить в черновике', { action: 'wiz-apply', iconName: 'swap', variant: 'primary', keys: [IS_MAC ? '⌘' : 'Ctrl', '↵'], attrs: { 'data-focus-key': 'wiz:apply', 'aria-keyshortcuts': 'Control+Enter Meta+Enter' } }));
    replaceDialog.replaceChildren(
      el('div', { class: 'cqa-ed__dialog-head' }, el('h2', { id: 'cqa-ed-replace-title', text: 'Заменить критерий' }), iconButton('x', 'Закрыть', { 'data-action': 'wiz-cancel', 'data-focus-key': 'wiz:close' })),
      el('p', { class: 'cqa-ed__muted cqa-ed__dialog-lead', text: 'Новый текст встанет на место выбранного критерия: тот же ID, секция и позиция. Изменения попадут в черновик — их нужно сохранить как новую версию и применить.' }),
      body, footer);
    refreshWizard();
  }
  function wizardPatch() {
    const contract = fromUi(wizard.ui);
    return { title: contract.title, description: contract.description, answer_type: contract.answer_type, scale: contract.scale, scale_labels: contract.scale_labels, na_allowed: contract.na_allowed, na_rule: contract.na_rule, guidance: contract.guidance, weight: contract.weight, critical_when: contract.critical_when, critical_rule: contract.critical_rule };
  }
  function wizardIssues() {
    const source = wizardSource();
    const hypothetical = replaceCriterion(source, wizard.targetId, wizardPatch());
    const index = hypothetical.criteria.findIndex(item => trimmed(item.id) === wizard.targetId);
    const result = validate(hypothetical);
    return { hypothetical, result, issues: result.issues.filter(issue => issue.index === index && issue.field !== 'id') };
  }
  function refreshWizard() {
    if (!wizard.ui) return;
    const { hypothetical, result, issues } = wizardIssues();
    applyIssues(replaceDialog, null, { ui: wizard.ui, issues });
    const impact = replaceDialog.querySelector('.cqa-ed__impact');
    if (!impact) return;
    const diff = classifyDiff(wizardSource(), hypothetical);
    const change = diff.changed.find(item => item.id === wizard.targetId);
    const others = (hypothetical.criteria || []).length - 1;
    const nodes = [el('h3', { id: 'cqa-ed-wiz-impact-title', class: 'cqa-ed__wiz-subtitle', text: 'Что изменится' })];
    if (!trimmed(wizard.ui.title) && !trimmed(wizard.ui.description)) nodes.push(el('p', { class: 'cqa-ed__muted', text: `Пока ничего: впишите новое название и формулировку. Будет переоценён только ${wizard.targetId}, остальные ${others} ${plural(others, 'критерий', 'критерия', 'критериев')} возьмутся из кэша.` }));
    else if (!change) nodes.push(el('p', { class: 'cqa-ed__muted', text: 'Текст совпадает с текущим критерием: менять нечего.' }));
    else {
      nodes.push(el('p', { class: `cqa-ed__verdict ${change.needs_reeval ? 'cqa-ed__verdict--reeval' : 'cqa-ed__verdict--ok'}` }, icon(change.needs_reeval ? 'alert' : 'check'),
        el('span', { text: change.needs_reeval ? `Нужна переоценка ${wizard.targetId} на каждом диалоге. Остальные ${others} ${plural(others, 'критерий', 'критерия', 'критериев')} — из кэша, 0 вызовов модели.` : 'Меняется только вес или секция: 0 вызовов модели.' })));
      nodes.push(el('p', { class: 'cqa-ed__muted', text: `Поля: ${change.fields.map(field => FIELD_LABELS[field.field] || field.field).join(', ')}.` }));
    }
    const weightIssue = result.issues.find(issue => issue.field === 'weights');
    if (weightIssue) nodes.push(el('p', { class: 'cqa-ed__msg cqa-ed__msg--warning' }, icon('alert'), el('span', { text: weightIssue.message })));
    impact.replaceChildren(...nodes);
  }
  function afterWizardEdit(result, field) {
    if (result === 'structure') { const saved = captureFocus(); const form = replaceDialog.querySelector('.cqa-ed__wiz-form'); if (form) form.replaceChildren(el('h3', { class: 'cqa-ed__wiz-subtitle', text: `Новый критерий на месте ${wizard.targetId}` }), buildForm(wizard.ui, 'cqa-ed-wiz', 'wizard')); restoreFocus(saved); }
    refreshWizard();
    if (LINT_FIELDS.has(field)) { clearTimeout(wizard.lintTimer); wizard.lintTimer = setTimeout(() => runLint('wizard', false), 1200); }
  }
  async function applyWizard() {
    wizard.showAll = true;
    const { issues } = wizardIssues();
    const errors = issues.filter(issue => issue.level === 'error');
    if (errors.length) {
      refreshWizard();
      const issue = errors[0];
      const key = issue.field === 'scale' && Number.isInteger(issue.step) ? `wizard:step-value:${issue.step}` : issue.field === 'answer_type' ? `wizard:answer_type:${wizard.ui.answer_type}` : `wizard:${issue.field}`;
      focusKey(key) || focusKey('wiz:apply');
      announce(`Не заменено: ${issue.message}`);
      return;
    }
    const targetId = wizard.targetId;
    const oldTitle = trimmed(wizard.old?.title);
    const ui = wizard.ui;
    if (state.view !== 'draft' || !state.draft) {
      const ok = await startDraftFrom(state.view);
      if (!ok) return;
    }
    const target = state.draft.criteria.find(item => trimmed(item.id) === targetId);
    if (!target) { toast(`Критерий ${targetId} не найден в черновике.`); return; }
    Object.assign(target, {
      title: ui.title, description: ui.description, answer_type: ui.answer_type, steps: ui.steps.map(step => ({ ...step })), weight: ui.weight,
      na_allowed: ui.na_allowed, na_rule: ui.na_rule, critical_when: ui.critical_when, critical_rule: ui.critical_rule, guidance: ui.guidance.slice(), isNew: false,
    });
    state.lint.set(target.uid, wizard.lint ? { ...wizard.lint } : undefined);
    if (!state.lint.get(target.uid)) state.lint.delete(target.uid);
    state.expanded.add(target.uid);
    state.highlight = target.uid;
    if (!trimmed(state.draft.change_note)) state.draft.change_note = `Заменён ${targetId}: «${shorten(oldTitle, 60)}» → «${shorten(ui.title, 60)}»`;
    persistNow();
    wizard.ui = null;
    renderAll();
    closeDialog(replaceDialog, null, () => {
      const note = bar.querySelector('#cqa-ed-note');
      if (note?.isConnected) { note.focus(); try { note.select(); } catch { /* not selectable */ } }
    });
    announce(`${targetId} заменён в черновике. Проверьте «Что изменили» и сохраните версию v${nextVersion()}.`);
  }

  // import dialog
  function openImport() {
    importState.result = null;
    importState.opener = document.activeElement;
    const file = el('input', { id: 'cqa-ed-import-file', type: 'file', accept: '.json,application/json', class: 'cqa-ed__file', 'data-focus-key': 'imp:file' });
    const area = el('textarea', { id: 'cqa-ed-import-text', class: 'cqa-ed__input cqa-ed__code', rows: '10', spellcheck: 'false', autocomplete: 'off', 'aria-describedby': 'cqa-ed-import-hint', 'data-focus-key': 'imp:text', placeholder: '{ "name": "…", "criteria": [ { "id": "C1", "title": "…", "description": "…", "answer_type": "binary", "weight": 10 } ] }' });
    const report = el('div', { class: 'cqa-ed__import-report', id: 'cqa-ed-import-report', 'aria-live': 'polite' });
    file.addEventListener('change', async () => {
      const chosen = file.files?.[0];
      if (!chosen) return;
      if (chosen.size > 2 * 1024 * 1024) { importState.result = { ok: false, errors: ['Файл больше 2 МБ: это не похоже на чек-лист.'], warnings: [] }; renderImportReport(); return; }
      try { area.value = await chosen.text(); checkImport(); }
      catch { importState.result = { ok: false, errors: ['Не удалось прочитать файл.'], warnings: [] }; renderImportReport(); }
    });
    area.addEventListener('input', () => { importState.result = null; renderImportReport(); });
    importDialog.replaceChildren(
      el('div', { class: 'cqa-ed__dialog-head' }, el('h2', { id: 'cqa-ed-import-title', text: 'Импорт чек-листа из JSON' }), iconButton('x', 'Закрыть', { 'data-action': 'imp-cancel', 'data-focus-key': 'imp:close' })),
      el('p', { class: 'cqa-ed__muted cqa-ed__dialog-lead', id: 'cqa-ed-import-hint', text: 'Формат как у «Экспорт JSON»: объект с полем criteria. Импорт создаёт черновик — сохранённые версии не меняются. Хеши и номер версии из файла не используются.' }),
      el('div', { class: 'cqa-ed__dialog-body' },
        el('div', { class: 'cqa-ed__field' }, el('label', { class: 'cqa-ed__label', for: 'cqa-ed-import-file', text: 'Файл .json' }), file),
        el('div', { class: 'cqa-ed__field' }, el('label', { class: 'cqa-ed__label', for: 'cqa-ed-import-text', text: 'Или вставьте JSON' }), area),
        button('Проверить', { action: 'imp-check', iconName: 'check', attrs: { 'data-focus-key': 'imp:check' } }), report),
      el('div', { class: 'cqa-ed__dialog-actions' }, button('Отмена', { action: 'imp-cancel', attrs: { 'data-focus-key': 'imp:cancel' } }), button('Загрузить в черновик', { action: 'imp-load', iconName: 'upload', variant: 'primary', keys: [IS_MAC ? '⌘' : 'Ctrl', '↵'], attrs: { id: 'cqa-ed-import-load', 'data-focus-key': 'imp:load', 'aria-keyshortcuts': 'Control+Enter Meta+Enter' } })));
    renderImportReport();
    openDialog(importDialog, area);
  }
  function checkImport() {
    const area = importDialog.querySelector('#cqa-ed-import-text');
    const fallback = viewChecklist() || {};
    importState.result = parseImport(area?.value ?? '', { fallback: { checklist_id: fallback.checklist_id, name: fallback.name, rounding: fallback.rounding } });
    renderImportReport();
  }
  function renderImportReport() {
    const report = importDialog.querySelector('#cqa-ed-import-report');
    if (!report) return;
    const result = importState.result;
    const nodes = [];
    if (result && !result.ok) {
      nodes.push(el('div', { class: 'cqa-ed__notice cqa-ed__notice--error', role: 'alert' }, icon('octagon'), el('div', {}, el('p', { text: 'Импорт невозможен:' }), el('ul', {}, ...result.errors.map(message => el('li', { text: message }))))));
    } else if (result?.ok) {
      const v = result.validation;
      const criteria = result.checklist.criteria.length;
      nodes.push(el('div', { class: 'cqa-ed__notice cqa-ed__notice--info', role: 'status' }, icon('check'), el('p', { text: `Прочитано: «${result.checklist.name}», ${criteria} ${plural(criteria, 'критерий', 'критерия', 'критериев')}, сумма весов ${formatNumber(v.weightSum)}.` })));
      if (v.errors.length) nodes.push(el('div', { class: 'cqa-ed__notice cqa-ed__notice--warning' }, icon('alert'), el('div', {}, el('p', { text: `${v.errors.length} ${plural(v.errors.length, 'ошибка', 'ошибки', 'ошибок')} — их можно исправить в черновике после загрузки:` }),
        el('ul', {}, ...v.errors.slice(0, 8).map(issue => el('li', { text: `${issue.id ? `${issue.id}, ` : ''}${FIELD_LABELS[issue.field] || issue.field}: ${issue.message}` }))))));
      const warnings = [...result.warnings, ...v.warnings.map(issue => `${issue.id ? `${issue.id}: ` : ''}${issue.message}`)];
      if (warnings.length) nodes.push(el('ul', { class: 'cqa-ed__import-warnings' }, ...warnings.slice(0, 8).map(message => el('li', {}, icon('info'), el('span', { text: message })))));
    }
    report.replaceChildren(...nodes);
    const load = importDialog.querySelector('#cqa-ed-import-load');
    if (load) load.setAttribute('aria-disabled', String(!result?.ok));
  }
  async function loadImport() {
    if (!importState.result) checkImport();
    const result = importState.result;
    if (!result?.ok) { renderImportReport(); importDialog.querySelector('#cqa-ed-import-text')?.focus(); return; }
    if (state.draft && isDirty()) {
      const ok = await confirmAction({ title: 'Заменить черновик?', message: 'Импорт заменит текущий черновик с несохранёнными изменениями.', confirm: 'Заменить', cancel: 'Отмена', danger: true });
      if (!ok) return;
    }
    const base = Number.isInteger(state.view) ? state.view : state.draft?.base_version ?? activeVersion() ?? state.versions[0]?.version ?? null;
    state.draft = draftFromChecklist(result.checklist, { base_version: Number.isInteger(base) ? base : null, origin: 'import' });
    if (Number.isInteger(base)) await getVersion(base).catch(() => null);
    state.view = 'draft'; state.compareTo = defaultCompare('draft');
    state.expanded.clear(); state.touched.clear(); state.lint.clear(); state.undo = null; state.restoredAt = '';
    state.showAll = true;
    persistNow();
    renderAll();
    const count = result.checklist.criteria.length;
    closeDialog(importDialog, null, () => { if (!result.validation.ok) firstError(); else focusKey('bar:note'); });
    announce(`Импортировано ${count} ${plural(count, 'критерий', 'критерия', 'критериев')} в черновик.${result.validation.ok ? '' : ' Исправьте ошибки перед сохранением.'}`);
  }

  // ---------- events
  host.addEventListener('input', event => {
    if (event.target?.id === 'cqa-ed-note' && state.draft) {
      state.draft.change_note = event.target.value;
      if (state.noteError || state.saveError) { state.noteError = ''; state.saveError = ''; }
      updateBar(); persistSoon();
      return;
    }
    onFieldEvent(event, 'input');
  });
  host.addEventListener('change', event => {
    const target = event.target;
    if (target?.id === 'cqa-ed-compare') { const value = toNumber(target.value); getVersion(value).then(() => { state.compareTo = value; renderCriteria(); renderDiffBody(); }).catch(error => toast(error?.message || 'Версия недоступна.')); return; }
    if (target?.id === 'cqa-ed-group') { state.group = target.value; updateBar(); return; }
    if (target?.id === 'cqa-ed-wiz-target') {
      const source = wizardSource();
      wizard.targetId = target.value;
      wizard.old = source.criteria.find(item => trimmed(item.id) === wizard.targetId);
      wizard.ui = initWizardUi(wizard.old, { title: wizard.ui?.title, description: wizard.ui?.description, na_rule: wizard.ui?.na_rule, guidance: wizard.ui?.guidance });
      const saved = captureFocus(); renderWizard(); restoreFocus(saved);
      return;
    }
    onFieldEvent(event, 'change');
  });
  host.addEventListener('focusout', event => {
    const target = event.target;
    const uid = target?.dataset?.uid; const field = target?.dataset?.field;
    if (!uid || !field) return;
    if (uid === 'wizard') { wizard.touched.add(`wizard:${field}`); refreshWizard(); return; }
    if (!state.touched.has(`${uid}:${field}`)) { state.touched.add(`${uid}:${field}`); scheduleRefresh(); }
  });
  host.addEventListener('click', event => {
    const target = event.target.closest?.('[data-action]');
    if (!target || !host.contains(target) || target.disabled) return;
    const action = target.dataset.action;
    const uid = target.dataset.uid;
    const ui = uid ? findUi(uid) : null;
    switch (action) {
      case 'view': selectView(target.dataset.version === 'draft' ? 'draft' : toNumber(target.dataset.version)); break;
      case 'toggle': {
        if (state.expanded.has(uid)) state.expanded.delete(uid); else state.expanded.add(uid);
        renderCriteria();
        break;
      }
      case 'up': case 'down': moveItem(uid, action === 'up' ? -1 : 1); break;
      case 'duplicate': duplicateItem(uid); break;
      case 'delete': deleteItem(uid); break;
      case 'undo': undoDelete(); break;
      case 'add': addCriterion(target.dataset.section || ''); break;
      case 'add-step': if (ui) { pendingFocus = addStep(ui); structural(uid); } break;
      case 'remove-step': if (ui && ui.steps.length > 2) { const index = Number(target.dataset.index); ui.steps.splice(index, 1); pendingFocus = `${uid}:step-value:${Math.max(0, index - 1)}`; structural(uid); } break;
      case 'add-guidance': if (ui) { ui.guidance.push(''); pendingFocus = `${uid}:guidance:${ui.guidance.length - 1}`; structural(uid); } break;
      case 'remove-guidance': if (ui) { const index = Number(target.dataset.index); ui.guidance.splice(index, 1); pendingFocus = ui.guidance.length ? `${uid}:guidance:${Math.max(0, index - 1)}` : `${uid}:add-guidance`; structural(uid, 'guidance'); } break;
      case 'zero-weight': if (ui) { ui.weight = '0'; pendingFocus = `${uid}:weight`; structural(uid); } break;
      case 'lint': runLint(uid, true); break;
      case 'normalize': normalize(); break;
      case 'first-error': firstError(); break;
      case 'replace': openReplace(''); break;
      case 'new-from': if (Number.isInteger(state.view)) startDraftFrom(state.view); break;
      case 'discard': discardDraft(); break;
      case 'import': openImport(); break;
      case 'export': exportJson(); break;
      case 'blank': startBlankDraft(); break;
      case 'reload': load({ keepView: true }); break;
      case 'refresh-groups': ensureGroups({ refresh: true }); break;
      case 'save': save(); break;
      case 'apply': apply(); break;
      case 'preview': preview(); break;
      case 'open-board': CQA.bus.emit('nav:open', 'board'); break;
      case 'open-conv': CQA.bus.emit('conv:open', target.dataset.id); break;
      case 'hide-run': clearTimeout(timers.poll); pollToken += 1; state.run = null; renderRun(); focusKey('bar:apply') || focusKey('head:replace'); break;
      case 'repoll': if (state.run) startPolling(state.run.id, { kind: state.run.kind, version: state.run.version, group: state.run.group, external: false }); break;
      case 'wiz-cancel': closeDialog(replaceDialog, wizard.opener); wizard.ui = null; break;
      case 'wiz-apply': applyWizard(); break;
      case 'imp-check': checkImport(); break;
      case 'imp-cancel': closeDialog(importDialog, importState.opener); break;
      case 'imp-load': loadImport(); break;
      default: break;
    }
  });
  function structural(uid, field = '') {
    if (uid === 'wizard') { afterWizardEdit('structure', field); return; }
    renderCriteria(); scheduleRefresh(); persistSoon();
    if (field) scheduleLint(uid);
  }
  replaceDialog.addEventListener('cancel', event => { event.preventDefault(); closeDialog(replaceDialog, wizard.opener); wizard.ui = null; });
  importDialog.addEventListener('cancel', event => { event.preventDefault(); closeDialog(importDialog, importState.opener); });
  for (const dialog of [replaceDialog, importDialog, confirmDialog]) {
    dialog.addEventListener('keydown', event => {
      if (event.key !== 'Escape') event.stopPropagation();
      const submit = (event.ctrlKey || event.metaKey) && (event.code === 'Enter' || event.code === 'NumpadEnter');
      if (!submit) return;
      if (dialog === replaceDialog) { event.preventDefault(); applyWizard(); }
      else if (dialog === importDialog) { event.preventDefault(); loadImport(); }
    });
  }
  const editableTarget = target => !!target?.closest?.('input, textarea, select, [contenteditable]:not([contenteditable="false"])');
  const editorVisible = () => host.getClientRects().length > 0 && !host.closest('[hidden]');
  const onDocumentKey = event => {
    if (event.defaultPrevented || !editorVisible() || replaceDialog.open || importDialog.open || confirmDialog.open) return;
    if ((event.ctrlKey || event.metaKey) && !event.altKey && event.code === 'KeyS' && state.view === 'draft' && state.draft) { event.preventDefault(); save(); return; }
    if (event.code === 'KeyR' && !event.ctrlKey && !event.metaKey && !event.altKey && !event.shiftKey && !editableTarget(event.target) && viewChecklist()) { event.preventDefault(); openReplace(''); }
  };
  document.addEventListener('keydown', onDocumentKey);
  const beforeUnload = event => { if (!state.storageOk && isDirty()) { event.preventDefault(); event.returnValue = ''; } };
  window.addEventListener('beforeunload', beforeUnload);

  on('tab:changed', tab => {
    if (tab === 'editor' && state.busy !== 'profile') void loadEconomics();
    if (tab !== 'editor' && state.view === 'draft' && isDirty()) toast(`Черновик чек-листа не сохранён. Он ${state.storageOk ? 'хранится в браузере' : 'не сохранён даже в браузере'} — вернитесь во вкладку «Чек-лист», чтобы сохранить v${nextVersion()}.`);
  });
  on('checklist:selected', version => {
    const number = toNumber(version);
    if (state.view === 'draft' || !Number.isInteger(number) || number === state.view) return;
    openView(number);
  });
  on('checklist:saved', data => {
    const number = toNumber(data?.version);
    if (Number.isInteger(number) && (number === ownSaveVersion || versionItem(number))) return;
    load({ keepView: true });
  });
  on('checklist:activated', data => {
    const number = toNumber(data?.version);
    if (Number.isInteger(number) && number !== state.sessionActive) { state.sessionActive = number; renderAll(); }
  });
  on('conversations:refresh', () => ensureGroups({ refresh: true }));
  on('api:mode', () => {
    // The mock adapter emits this on every request, including config().
    if (economicsSource() !== economicsContext) void loadEconomics();
  });
  on('run:finished', data => {
    if (!state.run?.external || String(data?.run_id ?? data?.id) !== String(state.run.id)) return;
    state.run.data = data;
    state.run.external = false;
    state.run.announced = true;
    renderRun();
  });

  // ---------- start
  const stored = storageGet();
  const revived = reviveDraft(stored);
  if (revived) { state.draft = revived; state.restoredAt = asText(stored.saved_at); }
  else if (stored) storageRemove();
  renderAll();
  load();
  void loadEconomics();

  const controller = {
    reload: () => load({ keepView: true }),
    state: () => state,
    destroy() {
      disposed = true;
      ++economicsEpoch; budgetController?.destroy();
      clearTimeout(timers.persist); clearTimeout(timers.poll); clearTimeout(timers.skeleton); clearTimeout(timers.highlight); clearTimeout(wizard.lintTimer);
      for (const timer of timers.lint.values()) clearTimeout(timer);
      if (timers.refresh) cancelAnimationFrame(timers.refresh);
      for (const off of subscriptions) off();
      window.removeEventListener('beforeunload', beforeUnload);
      document.removeEventListener('keydown', onDocumentKey);
      mounts.delete(host);
    },
  };
  mounts.set(host, controller);
  return controller;
}
