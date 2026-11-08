/* Aggregate measurements only. Network reads belong exclusively to CQA.api.quality(). */
const mounts = new WeakMap();
const buckets = ['matched', 'total', 'missing', 'errors', 'unlabelled'];
const splitLabels = { tune: 'Tune · настройка', holdout: 'Holdout', dev: 'DEV', control: 'Контроль', unspecified: 'Разбиение не указано' };
const referenceLabels = { machine: 'Совпадение с машинным эталоном', human: 'Совпадение с ручной разметкой', mixed: 'Совпадение со смешанным эталоном' };
const cacheLabels = { fresh: 'Свежие вычисления', cached: 'Результаты из кэша', mixed: 'Свежие и кэшированные', unknown: 'Использование кэша неизвестно' };
const scopeLabels = { cli_wall: 'CLI · полное время на диалог', api_e2e: 'API · сквозное время на диалог', model_only: 'Только модель · время на диалог' };
const sourceLabels = { cli: 'CLI', api: 'API', offline: 'Офлайн-анализ', synthetic: 'Синтетическая проверка' };
const fail = (path, reason) => { throw new TypeError(`${path}: ${reason}`); };
function object(value, path) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) fail(path, 'expected object');
  return value;
}
function text(value, path) {
  if (typeof value !== 'string' || !value.trim()) fail(path, 'expected nonempty string');
  return value.trim();
}
function integer(value, path) {
  if (!Number.isSafeInteger(value) || value < 0) fail(path, 'expected nonnegative safe integer');
  return value;
}
function finite(value, path, maximum = Infinity) {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0 || value > maximum) fail(path, 'invalid measurement');
  return value;
}
function choice(value, options, path) {
  if (!options.includes(value)) fail(path, `expected ${options.join('|')}`);
  return value;
}
function boolean(value, path, nullable = false) {
  if (value === null && nullable) return null;
  if (typeof value !== 'boolean') fail(path, 'expected explicit boolean');
  return value;
}
function timestamp(value, path) {
  const result = text(value, path);
  const parts = /^(\d{4})-(\d{2})-(\d{2})T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.exec(result);
  if (!parts || !Number.isFinite(Date.parse(result))) fail(path, 'expected ISO timestamp with timezone');
  const [, year, month, day] = parts.map(Number);
  if (month < 1 || month > 12 || day < 1 || day > new Date(Date.UTC(year, month, 0)).getUTCDate()) fail(path, 'invalid calendar date');
  return result;
}
function counts(value, path) {
  const input = object(value, path), result = {};
  for (const key of buckets) result[key] = integer(key === 'unlabelled' && input[key] === undefined ? 0 : input[key], `${path}.${key}`);
  if (result.matched + result.missing + result.errors + result.unlabelled > result.total) fail(path, 'buckets exceed all planned cells');
  return result;
}
function unique(items, path) {
  if (new Set(items).size !== items.length) fail(path, 'duplicate identifiers or labels');
}
function groups(value, path, overall, maxCells = Infinity, sampleCount = Infinity, partition = false) {
  if (value == null) return null;
  if (!Array.isArray(value)) fail(path, 'expected array');
  const result = value.map((item, index) => {
    const p = `${path}[${index}]`; object(item, p);
    const row = { id: text(item.id, `${p}.id`), label: item.label == null ? null : text(item.label, `${p}.label`), counts: counts(item.counts, `${p}.counts`) };
    if (row.counts.total > maxCells) fail(p, 'criterion cells exceed conversation count');
    row.dialogues = item.dialogues == null ? null : integer(item.dialogues, `${p}.dialogues`);
    if (row.dialogues > sampleCount || (row.counts.total > 0 && row.dialogues === 0)) fail(p, 'invalid group dialogue count');
    row.agreement_ci = confidenceInterval(item.agreement_ci, `${p}.agreement_ci`, row.counts.total - row.counts.unlabelled, false, row.dialogues);
    row.all_cells_ci = confidenceInterval(item.all_cells_ci, `${p}.all_cells_ci`, row.counts.total, false, row.dialogues);
    return row;
  });
  unique(result.map(item => item.id), path);
  if (partition && result.length && result.every(row => row.dialogues !== null) && result.reduce((sum, row) => sum + row.dialogues, 0) !== sampleCount) fail(path, 'dialogue partition does not reconcile sample');
  for (const key of buckets) if (result.reduce((sum, row) => sum + row.counts[key], 0) !== overall[key]) fail(path, `partition does not reconcile ${key}`);
  return result;
}
function pairedMetric(value, path, sampleCount, type) {
  if (value == null) return null;
  object(value, path);
  const count = integer(value.count, `${path}.count`);
  if (count > sampleCount) fail(path, 'measurement count exceeds sample');
  const keys = type === 'mae' ? ['value'] : ['p50_seconds', 'p95_seconds'];
  const result = { count };
  for (const key of keys) {
    if (count === 0) { if (value[key] !== null) fail(`${path}.${key}`, 'no observations requires null'); result[key] = null; }
    else result[key] = type !== 'mae' && value[key] === null ? null : finite(value[key], `${path}.${key}`, type === 'mae' ? 100 : Infinity);
  }
  if (type !== 'mae') {
    result.scope = choice(value.scope, Object.keys(scopeLabels), `${path}.scope`);
    if (count > 0 && keys.every(key => result[key] === null)) fail(path, 'observations require at least one measured percentile');
    if (result.p95_seconds !== null && result.p50_seconds !== null && result.p95_seconds < result.p50_seconds) fail(path, 'p95 must be at least p50');
  }
  return result;
}
function costMetric(value, path, sampleCount) {
  if (value == null) return null;
  object(value, path);
  const count = integer(value.count, `${path}.count`);
  const basis = choice(value.basis, ['reported', 'estimated', 'unknown'], `${path}.basis`);
  if (count > sampleCount) fail(path, 'cost count exceeds sample');
  if (basis === 'unknown') {
    if (value.amount !== null || count !== 0 || value.currency != null) fail(path, 'unknown cost requires null amount/currency and zero observations');
    return { amount: null, currency: null, count, basis };
  }
  if (count === 0 || !/^[A-Z]{3}$/.test(value.currency)) fail(path, 'known cost requires observations and currency');
  return { amount: finite(value.amount, `${path}.amount`), currency: value.currency, count, basis };
}
function confusionMatrices(value, path, criteria) {
  if (value == null) return null;
  if (!Array.isArray(value)) fail(path, 'expected array');
  const result = value.map((matrix, index) => {
    const p = `${path}[${index}]`; object(matrix, p);
    const criterion_id = text(matrix.criterion_id, `${p}.criterion_id`);
    const criterion = criteria?.find(item => item.id === criterion_id);
    if (!criterion) fail(p, 'criterion missing from criteria partition');
    const labels = key => {
      if (!Array.isArray(matrix[key]) || !matrix[key].length) fail(`${p}.${key}`, 'expected nonempty labels');
      const list = matrix[key].map((label, i) => text(label, `${p}.${key}[${i}]`));
      unique(list, `${p}.${key}`); return list;
    };
    const rows = labels('rows'), columns = labels('columns');
    if (rows.includes('__missing__') || rows.includes('__error__') || columns.includes('__unlabelled__')) fail(p, 'reserved label on wrong axis');
    if (!Array.isArray(matrix.values) || matrix.values.length !== rows.length) fail(p, 'invalid matrix height');
    const summed = { total: 0, matched: 0, missing: 0, errors: 0, unlabelled: 0 };
    const values = matrix.values.map((row, i) => {
      if (!Array.isArray(row) || row.length !== columns.length) fail(p, 'invalid matrix width');
      return row.map((cell, j) => {
        const n = integer(cell, `${p}.values[${i}][${j}]`); summed.total += n;
        if (rows[i] === '__unlabelled__') summed.unlabelled += n;
        else if (columns[j] === '__missing__') summed.missing += n;
        else if (columns[j] === '__error__') summed.errors += n;
        else if (rows[i] === columns[j]) summed.matched += n;
        return n;
      });
    });
    for (const key of buckets) if (summed[key] !== criterion.counts[key]) fail(p, `matrix does not reconcile ${key}`);
    return { criterion_id, rows, columns, values };
  });
  unique(result.map(item => item.criterion_id), path); return result;
}
function confidenceInterval(value, path, observations, signed = false, dialogues = null) {
  if (value == null) return null;
  object(value, path);
  if (value.level !== 0.95 || value.method !== 'dialogue_cluster_percentile' || value.unit !== 'dialogue') fail(path, 'expected 95% dialogue-cluster percentile interval');
  const repetitions = integer(value.repetitions, `${path}.repetitions`), seed = integer(value.seed, `${path}.seed`);
  if (repetitions < 1) fail(path, 'bootstrap requires repetitions');
  const reason = value.reason == null ? null : choice(value.reason, ['insufficient_clusters', 'no_labelled_cells'], `${path}.reason`);
  const valid_repetitions = value.valid_repetitions == null ? null : integer(value.valid_repetitions, `${path}.valid_repetitions`);
  if (valid_repetitions > repetitions || (reason && valid_repetitions !== 0)) fail(path, 'invalid usable bootstrap count');
  if (reason === 'no_labelled_cells' && observations > 0) fail(path, 'empty denominator reason conflicts with observations');
  const effective_dialogues = Object.hasOwn(value, 'effective_dialogues') ? integer(value.effective_dialogues, `${path}.effective_dialogues`) : null;
  if (effective_dialogues !== null) {
    if ((dialogues !== null && effective_dialogues > dialogues) || effective_dialogues > observations) fail(path, 'effective dialogue count exceeds supported sample');
    if ((observations === 0) !== (effective_dialogues === 0)) fail(path, 'effective dialogue count conflicts with denominator');
    if (reason === 'insufficient_clusters' && effective_dialogues !== 1) fail(path, 'insufficient interval requires one nonempty cluster');
  }
  const unavailable = observations === 0 || reason === 'insufficient_clusters';
  if (!unavailable && (effective_dialogues !== null && effective_dialogues < 2 || valid_repetitions === 0)) fail(path, 'numeric interval requires multiple effective clusters and usable draws');
  if (!unavailable && dialogues !== null && dialogues < 2) fail(path, 'interval requires at least two dialogue clusters');
  const bound = key => {
    if (unavailable) { if (value[key] !== null) fail(path, 'unavailable interval requires null bounds'); return null; }
    if (typeof value[key] !== 'number' || !Number.isFinite(value[key]) || value[key] < (signed ? -1 : 0) || value[key] > 1) fail(`${path}.${key}`, 'invalid interval bound');
    return value[key];
  };
  const lower = bound('lower'), upper = bound('upper');
  if (lower !== null && lower > upper) fail(path, 'reversed interval');
  return { lower, upper, level: value.level, method: value.method, repetitions, seed, unit: value.unit, reason, valid_repetitions, effective_dialogues };
}
function comparisonMetrics(value, reports) {
  if (value == null) return [];
  if (!Array.isArray(value)) fail('comparisons', 'expected array');
  const result = value.map((item, i) => {
    const path = `comparisons[${i}]`; object(item, path);
    const report_id = text(item.report_id, `${path}.report_id`), baseline_report_id = text(item.baseline_report_id, `${path}.baseline_report_id`);
    const r = reports.find(row => row.id === report_id), baseline = reports.find(row => row.id === baseline_report_id);
    if (!r || !baseline || r === baseline) fail(path, 'distinct reports required');
    const reference_id = text(item.reference_id, `${path}.reference_id`);
    if (r.reference.id !== reference_id || baseline.reference.id !== reference_id) fail(path, 'reference mismatch');
    const win = integer(item.win, `${path}.win`), loss = integer(item.loss, `${path}.loss`), compared = integer(item.compared, `${path}.compared`), dialogues = integer(item.dialogues, `${path}.dialogues`);
    if (win + loss > compared || compared > Math.min(r.counts.total - r.counts.unlabelled, baseline.counts.total - baseline.counts.unlabelled) || dialogues > Math.min(r.sample.dialogues, baseline.sample.dialogues) || (compared > 0 && dialogues === 0)) fail(path, 'invalid paired support');
    const delta = compared ? (win - loss) / compared : null;
    if (delta === null ? item.delta !== null : typeof item.delta !== 'number' || Math.abs(item.delta - delta) > 5.1e-9) fail(path, 'delta does not reconcile wins/losses');
    const delta_ci = confidenceInterval(item.delta_ci, `${path}.delta_ci`, compared, true, dialogues);
    if (!delta_ci) fail(path, 'paired comparison requires interval');
    const p_value = compared ? finite(item.p_value, `${path}.p_value`, 1) : null;
    if (!compared && item.p_value !== null) fail(path, 'no observations requires null p-value');
    const significant = boolean(item.significant, `${path}.significant`);
    const detected = compared > 0 && delta_ci.lower !== null && p_value < 0.05 && (delta_ci.lower > 0 || delta_ci.upper < 0);
    if (significant !== detected || item.conclusion !== (detected ? 'difference_detected' : 'within_noise') || item.exploratory !== true) fail(path, 'significance must agree with unadjusted paired evidence');
    return { report_id, baseline_report_id, reference_id, win, loss, compared, dialogues, delta, delta_ci, p_value, significant, conclusion: item.conclusion, exploratory: true };
  });
  unique(result.map(row => `${row.report_id}::${row.baseline_report_id}`), 'comparisons'); return result;
}
function stabilityMetrics(value, reports) {
  if (value == null) return [];
  if (!Array.isArray(value)) fail('stability', 'expected array');
  const result = value.map((item, i) => {
    const path = `stability[${i}]`; object(item, path);
    const result = {};
    for (const key of ['id', 'left_run_id', 'right_run_id', 'configuration_note']) result[key] = text(item[key], `${path}.${key}`);
    if (!reports.some(row => row.run_id === result.left_run_id) || !reports.some(row => row.run_id === result.right_run_id) || result.left_run_id === result.right_run_id) fail(path, 'distinct measured runs required');
    for (const key of ['matched', 'total', 'dialogues']) result[key] = integer(item[key], `${path}.${key}`);
    if (result.matched > result.total || (result.total > 0 && result.dialogues === 0)) fail(path, 'invalid repeated support');
    result.same_configuration = boolean(item.same_configuration, `${path}.same_configuration`);
    result.agreement_ci = confidenceInterval(item.agreement_ci, `${path}.agreement_ci`, result.total, false, result.dialogues);
    return result;
  });
  unique(result.map(row => row.id), 'stability'); return result;
}

function report(value, path) {
  const r = object(value, path), result = {};
  for (const key of ['id', 'run_id', 'mode', 'guidance_version', 'checklist_version']) result[key] = text(r[key], `${path}.${key}`);
  result.measured_at = timestamp(r.measured_at, `${path}.measured_at`);
  const source = object(r.source, `${path}.source`), model = object(r.model, `${path}.model`);
  result.source = { kind: choice(source.kind, Object.keys(sourceLabels), `${path}.source.kind`), label: text(source.label, `${path}.source.label`), artifact: source.artifact == null ? null : text(source.artifact, `${path}.source.artifact`) };
  result.model = { requested: text(model.requested, `${path}.model.requested`), resolved: model.resolved === null ? null : text(model.resolved, `${path}.model.resolved`) };
  result.cache = choice(r.cache, Object.keys(cacheLabels), `${path}.cache`);
  result.denominator = choice(r.denominator, ['all_cells'], `${path}.denominator`);
  result.counts = counts(r.counts, `${path}.counts`);
  const ref = object(r.reference, `${path}.reference`);
  result.reference = { id: text(ref.id, `${path}.reference.id`), type: choice(ref.type, Object.keys(referenceLabels), `${path}.reference.type`), cells: integer(ref.cells, `${path}.reference.cells`), description: ref.description == null ? null : text(ref.description, `${path}.reference.description`) };
  if (result.reference.cells < result.counts.total - result.counts.unlabelled) fail(`${path}.reference.cells`, 'reference smaller than labelled sample');
  if (ref.annotator_agreement != null) {
    const a = object(ref.annotator_agreement, `${path}.reference.annotator_agreement`);
    const total = integer(a.total, `${path}.reference.annotator_agreement.total`), matched = integer(a.matched, `${path}.reference.annotator_agreement.matched`);
    if (matched > total) fail(`${path}.reference.annotator_agreement`, 'matches exceed compared labels');
    result.reference.annotator_agreement = { matched, total };
  }
  const sample = object(r.sample, `${path}.sample`);
  result.sample = { dataset: text(sample.dataset, `${path}.sample.dataset`), split: choice(sample.split, Object.keys(splitLabels), `${path}.sample.split`), dialogues: integer(sample.dialogues, `${path}.sample.dialogues`), dev_reused: boolean(sample.dev_reused, `${path}.sample.dev_reused`), used_for_tuning: boolean(sample.used_for_tuning, `${path}.sample.used_for_tuning`, true), untouched: boolean(sample.untouched, `${path}.sample.untouched`, true) };
  if (result.sample.untouched && (result.sample.dev_reused || result.sample.used_for_tuning !== false || result.sample.split === 'tune')) fail(`${path}.sample`, 'untouched conflicts with tuning/reuse provenance');
  if (result.counts.total > 0 && result.sample.dialogues === 0) fail(`${path}.sample.dialogues`, 'cells require dialogues');
  result.score_mae = pairedMetric(r.score_mae, `${path}.score_mae`, result.sample.dialogues, 'mae');
  result.latency = pairedMetric(r.latency, `${path}.latency`, result.sample.dialogues, 'latency');
  if (result.latency && r.latency.note != null) result.latency.note = text(r.latency.note, `${path}.latency.note`);
  if (result.latency && ((result.latency.scope === 'cli_wall' && result.source.kind !== 'cli') || (result.latency.scope === 'api_e2e' && result.source.kind !== 'api'))) fail(`${path}.latency.scope`, 'transport conflicts with measurement scope');
  result.cost = costMetric(r.cost, `${path}.cost`, result.sample.dialogues);
  result.cost_audit = null;
  if (r.cost_audit != null) {
    const a = object(r.cost_audit, `${path}.cost_audit`), audit = {};
    for (const key of ['known_dialogues', 'unknown_dialogues', 'total_dialogues']) audit[key] = integer(a[key], `${path}.cost_audit.${key}`);
    if (audit.total_dialogues !== result.sample.dialogues || audit.known_dialogues + audit.unknown_dialogues !== audit.total_dialogues) fail(`${path}.cost_audit`, 'cost coverage does not reconcile');
    if (!Array.isArray(a.reasons)) fail(`${path}.cost_audit.reasons`, 'expected array');
    audit.reasons = a.reasons.map((reason, i) => text(reason, `${path}.cost_audit.reasons[${i}]`));
    for (const key of ['lower_bound_usd', 'upper_bound_usd']) audit[key] = a[key] == null ? null : finite(a[key], `${path}.cost_audit.${key}`);
    if (audit.lower_bound_usd !== null && audit.upper_bound_usd !== null && audit.lower_bound_usd > audit.upper_bound_usd) fail(`${path}.cost_audit`, 'reversed cost bounds');
    if (result.cost?.count > audit.known_dialogues) fail(`${path}.cost_audit`, 'cost observations exceed reconstructed coverage');
    audit.method = a.method == null ? null : text(a.method, `${path}.cost_audit.method`);
    audit.pricing_complete = a.pricing_complete == null ? null : boolean(a.pricing_complete, `${path}.cost_audit.pricing_complete`);
    audit.comparable = a.comparable == null ? null : boolean(a.comparable, `${path}.cost_audit.comparable`);
    audit.zero_cost_paid_usage_dialogues = a.zero_cost_paid_usage_dialogues == null ? 0 : integer(a.zero_cost_paid_usage_dialogues, `${path}.cost_audit.zero_cost_paid_usage_dialogues`);
    if (audit.zero_cost_paid_usage_dialogues > audit.total_dialogues) fail(`${path}.cost_audit`, 'zero-price anomaly count exceeds sample');
    audit.coverage_note = a.coverage_note == null ? null : text(a.coverage_note, `${path}.cost_audit.coverage_note`);
    audit.recorded_total_kzt = a.recorded_total_kzt == null ? null : finite(a.recorded_total_kzt, `${path}.cost_audit.recorded_total_kzt`);
    audit.notes = a.notes == null ? [] : (Array.isArray(a.notes) ? a.notes.map((line, i) => text(line, `${path}.cost_audit.notes[${i}]`)) : [text(a.notes, `${path}.cost_audit.notes`)]);
    audit.conversion = null;
    const conversionSource = a.currency_conversion ?? a.conversion;
    if (conversionSource != null) {
      const conversion = object(conversionSource, `${path}.cost_audit.currency_conversion`);
      const usd_kzt = finite(conversion.usd_kzt, `${path}.cost_audit.conversion.usd_kzt`);
      if (usd_kzt === 0) fail(`${path}.cost_audit.conversion`, 'conversion rate must be positive');
      audit.conversion = { usd_kzt, source: text(conversion.source, `${path}.cost_audit.conversion.source`), assumption: boolean(conversion.assumption, `${path}.cost_audit.conversion.assumption`) };
    }
    result.cost_audit = audit;
  }
  for (const key of ['criteria', 'languages', 'channels']) result[key] = groups(r[key], `${path}.${key}`, result.counts, key === 'criteria' ? result.sample.dialogues : Infinity, result.sample.dialogues, key !== 'criteria');
  result.auto_approved = null;
  if (r.auto_approved != null) {
    object(r.auto_approved, `${path}.auto_approved`);
    const subset = counts(r.auto_approved.counts, `${path}.auto_approved.counts`), overall = result.counts;
    if (subset.missing || subset.errors || subset.total > overall.total || subset.matched > overall.matched || subset.unlabelled > overall.unlabelled || agreement(subset).disagreed > agreement(overall).disagreed) fail(`${path}.auto_approved`, 'auto-approved counts are not a valid answered subset');
    result.auto_approved = { counts: subset };
  }
  result.agreement_ci = confidenceInterval(r.agreement_ci, `${path}.agreement_ci`, result.counts.total - result.counts.unlabelled, false, result.sample.dialogues);
  result.all_cells_ci = confidenceInterval(r.all_cells_ci, `${path}.all_cells_ci`, result.counts.total, false, result.sample.dialogues);
  result.confusion = confusionMatrices(r.confusion, `${path}.confusion`, result.criteria);
  return result;
}

/** Pure, fail-closed validation. Never trusts precomputed percentages or mutates input. */
export function validateQuality(payload) {
  try {
    const root = object(payload, 'quality');
    if (root.schema_version !== undefined && root.schema_version !== 1) fail('schema_version', 'unsupported version');
    if (!Array.isArray(root.reports)) fail('reports', 'expected array');
    const reports = root.reports.map((r, i) => report(r, `reports[${i}]`));
    unique(reports.map(r => r.id), 'reports');
    const comparisons = comparisonMetrics(root.comparisons, reports);
    const stability = stabilityMetrics(root.stability, reports);
    let profile_catalog = null;
    if (root.profile_catalog != null) {
      object(root.profile_catalog, 'profile_catalog');
      profile_catalog = {};
      for (const key of ['cloud', 'cascade', 'routing']) {
        if (root.profile_catalog[key] == null) continue;
        const id = text(object(root.profile_catalog[key], `profile_catalog.${key}`).report_id, `profile_catalog.${key}.report_id`);
        if (!reports.some(row => row.id === id)) fail(`profile_catalog.${key}`, 'report not found');
        const source = root.profile_catalog[key];
        profile_catalog[key] = { report_id: id, label: source.label == null ? null : text(source.label, `profile_catalog.${key}.label`) };
      }
      if (root.profile_catalog.local != null) {
        const local = object(root.profile_catalog.local, 'profile_catalog.local');
        const normalized = { label: text(local.label, 'profile_catalog.local.label'), report_id: null, api_cost: null };
        if (local.report_id != null) {
          normalized.report_id = text(local.report_id, 'profile_catalog.local.report_id');
          if (!reports.some(row => row.id === normalized.report_id)) fail('profile_catalog.local', 'report not found');
        }
        if (local.api_cost != null) {
          const cost = object(local.api_cost, 'profile_catalog.local.api_cost');
          if (cost.amount !== 0 || cost.basis !== 'no_external_api' || cost.scope !== 'local_only' || !/^[A-Z]{3}$/.test(cost.currency)) fail('profile_catalog.local.api_cost', 'explicit zero external API cost required');
          normalized.api_cost = { amount: 0, currency: cost.currency, basis: cost.basis, scope: cost.scope };
        }
        profile_catalog.local = normalized;
      }
    }
    return { state: reports.length ? 'ready' : 'empty', reports, comparisons, stability, profile_catalog, issues: [] };
  } catch (error) { return { state: 'invalid', reports: [], issues: [error.message] }; }
}

/** Ratios, not percentages; no metric exists for an empty denominator. */
export function agreement(value) {
  const c = counts(value, 'counts'), labelled = c.total - c.unlabelled;
  return { all: c.total ? c.matched / c.total : null, labelled: labelled ? c.matched / labelled : null, referenceCoverage: c.total ? labelled / c.total : null, disagreed: c.total - c.matched - c.missing - c.errors - c.unlabelled };
}
const number = (value, digits = 2) => value == null ? 'не измерено' : value.toLocaleString('ru-RU', { maximumFractionDigits: digits });
export function formatPercent(numerator, denominator) {
  return denominator > 0 ? `${number(100 * numerator / denominator, 1)}%` : 'не измерено';
}
const fraction = (numerator, denominator) => `${number(numerator)} / ${number(denominator)}`;

export function reportCaveats(r) {
  const result = [], s = r.sample;
  if (s.dev_reused) result.push(`DEV использован повторно: этот замер не подтверждает качество на новых данных.${s.split === 'holdout' ? ' Этот holdout не является нетронутой выборкой.' : ''}`);
  if (s.split === 'tune' || s.used_for_tuning) result.push('Выборка использовалась для настройки: результат может быть оптимистичным.');
  if (s.split === 'holdout' && s.untouched !== true && !s.dev_reused) result.push('Название holdout не означает нетронутую выборку: независимость этого замера не подтверждена.');
  if (s.untouched === true) result.push('По данным источника, выборка не использовалась для настройки; независимость разметки этим не установлена.');
  else if (!s.dev_reused && (s.untouched === null || s.used_for_tuning === null)) result.push('История настройки или повторного использования неизвестна.');
  if (r.reference.type !== 'human') result.push('Совпадение с машинной или смешанной разметкой не равно точности относительно независимого человеческого эталона.');
  if (r.source.kind === 'cli') result.push('Замер через CLI не является проверкой сквозного времени или качества API продукта.');
  if (r.source.kind === 'synthetic') result.push('Синтетическая проверка интерфейса, не измерение качества на реальных диалогах.');
  if (r.counts.unlabelled) result.push(`Ячеек без метки эталона: ${number(r.counts.unlabelled)}. Они не проверены, а не признаны неверными. Полный знаменатель сохранён.`);
  if (r.score_mae && r.score_mae.count < s.dialogues) result.push(`MAE рассчитана по ${number(r.score_mae.count)} из ${number(s.dialogues)} диалогов с парными полными итогами; остальные в MAE не входят.`);
  if (r.latency && r.latency.count < s.dialogues) result.push(`Время измерено только для ${number(r.latency.count)} из ${number(s.dialogues)} диалогов.`);
  return result;
}

function node(doc, tag, className = '', content) {
  const el = doc.createElement(tag); if (className) el.className = className;
  if (content !== undefined) el.textContent = String(content); return el;
}
function table(doc, caption, headers, rows) {
  const el = node(doc, 'table', 'cqa-quality__table');
  el.append(node(doc, 'caption', '', caption));
  const head = node(doc, 'thead'), headRow = node(doc, 'tr'), body = node(doc, 'tbody');
  for (const label of headers) { const th = node(doc, 'th', '', label); th.scope = 'col'; headRow.append(th); }
  head.append(headRow);
  for (const values of rows) {
    const row = node(doc, 'tr');
    values.forEach((value, index) => {
      const cell = node(doc, index === 0 ? 'th' : 'td');
      if (Array.isArray(value)) cell.append(node(doc, 'span', '', value[0]), node(doc, 'span', 'cqa-quality__cell-note', value[1]));
      else cell.textContent = String(value);
      if (index === 0) cell.scope = 'row'; row.append(cell);
    });
    body.append(row);
  }
  el.append(head, body); return el;
}
function detail(doc, title, content) {
  const el = node(doc, 'details', 'cqa-quality__details'); el.append(node(doc, 'summary', '', title), content); return el;
}
function groupTable(doc, title, rows) {
  return table(doc, title, ['Группа', 'Совпало / все', 'Без ответа; сбои; без эталона'], rows.map(row => [
    [row.label ? `${row.id} · ${row.label}` : (sliceTitles[row.id] ?? row.id), dialogueCount(row.dialogues)],
    `${fraction(row.counts.matched, row.counts.total)}\n${formatPercent(row.counts.matched, row.counts.total)}${row.all_cells_ci ? `\n${formatInterval(row.all_cells_ci)}` : ''}${row.counts.unlabelled && row.agreement_ci ? `\nС эталоном: ${formatPercent(row.counts.matched, row.counts.total - row.counts.unlabelled)} ${formatInterval(row.agreement_ci)}` : ''}`,
    `${number(row.counts.missing)}; ${number(row.counts.errors)}; ${number(row.counts.unlabelled)}`,
  ]));
}
function renderReport(doc, r) {
  const article = node(doc, 'article', 'cqa-quality__report');
  article.append(node(doc, 'p', 'cqa-quality__eyebrow', `${splitLabels[r.sample.split]} · ${r.sample.dataset} · ${number(r.sample.dialogues)} диалогов`), node(doc, 'h2', '', referenceLabels[r.reference.type]));
  const scopeNote = [r.source.label];
  if (r.sample.dev_reused) scopeNote.push('DEV использован повторно');
  if (r.sample.used_for_tuning) scopeNote.push('выборка использовалась для настройки');
  if (r.reference.type !== 'human') scopeNote.push('независимая человеческая точность не измерена');
  article.append(node(doc, 'p', 'cqa-quality__note', scopeNote.join(' · ')));
  const methodology = node(doc, 'div');
  const meta = node(doc, 'dl', 'cqa-quality__meta');
  for (const [label, value] of [
    ['Прогон', r.run_id],
    ['Выборка', `${r.sample.dataset} · диалогов: ${number(r.sample.dialogues)}`],
    ['Модель', `${r.model.requested} · фактическая: ${r.model.resolved ?? 'неизвестна'}`],
    ['Режим / guidance', `${r.mode} / ${r.guidance_version} · чек-лист ${r.checklist_version}`],
    ['Источник / дата', `${r.source.label} · ${r.measured_at}`],
    ['Эталон', `${r.reference.id} · ${number(r.reference.cells)} меток в полном эталоне`],
  ]) { const pair = node(doc, 'div'); pair.append(node(doc, 'dt', '', label), node(doc, 'dd', '', value)); meta.append(pair); }
  methodology.append(meta);
  const caveats = node(doc, 'ul', 'cqa-quality__caveats');
  for (const line of reportCaveats(r)) caveats.append(node(doc, 'li', '', line));
  if (caveats.children.length) methodology.append(caveats);
  const c = r.counts, labelled = c.total - c.unlabelled;
  const rows = [
    ['Подтверждённые совпадения / все запланированные ячейки', `${formatPercent(c.matched, c.total)}\n${fraction(c.matched, c.total)}${r.all_cells_ci ? `\n${formatInterval(r.all_cells_ci)}` : ''}`, 'Пропуски, сбои и ячейки без эталона входят в знаменатель'],
    ['Охват эталоном', `${formatPercent(labelled, c.total)}\n${fraction(labelled, c.total)}`, 'Ячейки с доступной меткой / все ячейки'],
  ];
  if (c.unlabelled) rows.push(['Совпадение среди ячеек с эталоном', `${formatPercent(c.matched, labelled)}\n${fraction(c.matched, labelled)}${r.agreement_ci ? `\n${formatInterval(r.agreement_ci)}` : ''}`, 'Пропуски и сбои модели остаются в знаменателе; без метки эталона исключены']);
  rows.push(['Без ответа / сбои / без эталона', `${number(c.missing)} / ${number(c.errors)} / ${number(c.unlabelled)}`, `${number(agreement(c).disagreed)} действительных расхождений с меткой`]);
  const mae = r.score_mae, latency = r.latency, cost = r.cost;
  rows.push(['MAE итогового балла', mae?.count ? `${number(mae.value)} из 100` : 'не измерено', `${fraction(mae?.count ?? 0, r.sample.dialogues)} диалогов с парными полными итогами`]);
  const seconds = value => value === null ? 'не измерено' : `${number(value)} с`;
  rows.push(['Время p50 / p95', latency?.count ? `${seconds(latency.p50_seconds)} / ${seconds(latency.p95_seconds)}` : 'не измерено', `${latency ? scopeLabels[latency.scope] : 'Область измерения неизвестна'}; ${fraction(latency?.count ?? 0, r.sample.dialogues)} диалогов; ${cacheLabels[r.cache]}`]);
  rows.push(['Стоимость', cost?.count ? `${number(cost.amount)} ${cost.currency} всего\n${number(cost.amount / cost.count, 4)} ${cost.currency} / диалог` : 'неизвестна', cost?.count ? `${cost.basis === 'estimated' ? 'Оценка' : 'Заявлено источником'}; ${fraction(cost.count, r.sample.dialogues)} диалогов` : 'Не приравнивается к нулю']);
  const overview = table(doc, referenceLabels[r.reference.type], ['Показатель и охват', 'Результат'], rows.map(([metric, value, note]) => [[metric, note], value]));
  overview.classList.add('cqa-quality__overview'); article.append(overview);
  article.append(node(doc, 'p', 'cqa-quality__note', 'Полный знаменатель — консервативный показатель подтверждённых совпадений. Он не объявляет ячейки без эталона ошибками модели.'));
  if (r.reference.description) article.append(node(doc, 'p', 'cqa-quality__note', r.reference.description));
  if (r.reference.annotator_agreement) {
    const a = r.reference.annotator_agreement;
    methodology.append(node(doc, 'p', 'cqa-quality__note', `Согласие проходов разметки эталона: ${fraction(a.matched, a.total)} · ${formatPercent(a.matched, a.total)}. Это не результат оцениваемой модели.`));
  }
  if (r.latency?.note) methodology.append(node(doc, 'p', 'cqa-quality__note', r.latency.note));
  if (r.cost_audit) {
    const a = r.cost_audit;
    if (a.method === 'stored_full_cost') methodology.append(node(doc, 'p', 'cqa-quality__note', 'Стоимость — по ценам моделей в каждом вызове; токены по моделям не разделены.'));
    if (a.pricing_complete !== true && a.method) methodology.append(node(doc, 'p', 'cqa-quality__note', 'Полнота исторических цен не подтверждена; по этой сумме нельзя делать вывод об экономии.'));
    if (a.recorded_total_kzt !== null) methodology.append(node(doc, 'p', 'cqa-quality__note', `Сохранённая сумма: ${number(a.recorded_total_kzt, 4)} KZT.`));
    if (a.conversion) methodology.append(node(doc, 'p', 'cqa-quality__note', `Пересчёт USD/KZT: ${number(a.conversion.usd_kzt, 4)}; источник: ${a.conversion.source}; ${a.conversion.assumption ? 'допущение расчёта' : 'значение из источника'}.`));
    if (a.coverage_note) methodology.append(node(doc, 'p', 'cqa-quality__note', a.coverage_note));
    for (const line of a.notes) methodology.append(node(doc, 'p', 'cqa-quality__note', line));
    const bounds = a.lower_bound_usd === null || a.upper_bound_usd === null ? 'Диапазон не восстановлен.' : `Диапазон общей стоимости: ${number(a.lower_bound_usd, 4)}–${number(a.upper_bound_usd, 4)} USD; это границы, не точечная оценка.`;
    methodology.append(node(doc, 'p', 'cqa-quality__note', `Аудит стоимости: восстановлено ${fraction(a.known_dialogues, a.total_dialogues)} диалогов; неизвестно ${a.unknown_dialogues}. ${bounds} Причины: ${a.reasons.join(', ') || 'нет'}.`));
  }
  if (r.source.artifact) methodology.append(node(doc, 'p', 'cqa-quality__note', `Артефакт источника: ${r.source.artifact}`));
  article.append(detail(doc, 'Условия замера и источник', methodology));
  for (const [key, title] of [['criteria', 'По критериям'], ['languages', 'По языкам'], ['channels', 'По каналам']]) {
    if (r[key]?.length) article.append(detail(doc, title, groupTable(doc, `${title} · ${splitLabels[r.sample.split]} · все ячейки`, r[key])));
  }
  if (r.auto_approved) {
    const a = r.auto_approved.counts;
    article.append(table(doc, 'Автоодобренная подвыборка', ['Показатель', 'Результат'], [
      ['Доля автоодобренных ячеек', `${fraction(a.total, c.total)} · ${formatPercent(a.total, c.total)}`],
      ['Совпадение только внутри автоодобренной подвыборки', `${fraction(a.matched, a.total)} · ${formatPercent(a.matched, a.total)}`],
      ['Без метки эталона внутри подвыборки', number(a.unlabelled)],
    ]));
  }
  const reservedLabels = new Map([['__missing__', 'Нет ответа'], ['__error__', 'Ошибка'], ['__unlabelled__', 'Нет эталона']]);
  const label = value => reservedLabels.get(value) ?? value;
  for (const matrix of r.confusion || []) {
    const cells = [];
    matrix.values.forEach((row, i) => row.forEach((n, j) => { if (n) cells.push([label(matrix.rows[i]), label(matrix.columns[j]), number(n)]); }));
    article.append(detail(doc, `Матрица · ${matrix.criterion_id}`, table(doc, `${matrix.criterion_id}: эталон × ответ · ненулевые ячейки`, ['Эталон', 'Ответ модели', 'Ячеек'], cells)));
  }
  return article;
}

/** Explicitly labelled intervals, never a fabricated certainty score. */
export function formatInterval(ci, signed = false) {
  if (ci?.reason === 'insufficient_clusters') return '95% ДИ не оценивается: мало диалогов';
  if (!ci || ci.lower === null) return '95% ДИ не измерен';
  const value = n => `${signed && n > 0 ? '+' : ''}${number(n * 100, 1)}`;
  return `[${value(ci.lower)}–${value(ci.upper)}]${signed ? ' п.п.' : '%'} · 95% ДИ`;
}
function dialogueCount(n) {
  if (n === null || n === undefined) return 'число диалогов неизвестно';
  const last = n % 10, lastTwo = n % 100;
  return `${number(n)} ${last === 1 && lastTwo !== 11 ? 'диалог' : last >= 2 && last <= 4 && (lastTwo < 12 || lastTwo > 14) ? 'диалога' : 'диалогов'}`;
}
function modelLabel(r) {
  const raw = r.model.resolved ?? r.model.requested;
  if (r.run_id.includes('routing')) return 'Маршрутизация';
  if (r.run_id.includes('cascade')) return 'Каскад Haiku → Gemini';
  // Published labels disambiguate runs of the same model (e.g. three Haiku CLI measurements).
  if (r.source.label) return r.source.label;
  const named = raw.replace(/^(openai|claude_cli|ollama):/, '').split('/').pop();
  return named.replace(/-/g, ' ').replace(/\b(gemini|argon|haiku|sonnet|claude|deepseek|qwen|terra)\b/g, word => word[0].toUpperCase() + word.slice(1)) + (r.source.kind === 'cli' ? ' · CLI' : '');
}
const sliceTitles = { 'agent chat': 'Чаты', 'voice call': 'Звонки', EN: 'English', RU: 'Русский', KZ: 'Қазақша' };
function sliceLabel(row) { return row.label ?? sliceTitles[row.id] ?? row.id.toUpperCase(); }
function percentWithInterval(r) {
  return `${formatPercent(r.counts.matched, r.counts.total - r.counts.unlabelled)} ${formatInterval(r.agreement_ci)}`;
}
function metricCard(doc, r, title) {
  const card = node(doc, 'article', 'cqa-quality__metric-card');
  card.append(node(doc, 'h2', '', title), node(doc, 'p', 'cqa-quality__metric-value', formatPercent(r.counts.matched, r.counts.total - r.counts.unlabelled)), node(doc, 'p', 'cqa-quality__interval', formatInterval(r.agreement_ci)));
  card.append(node(doc, 'p', 'cqa-quality__metric-support', `${fraction(r.counts.matched, r.counts.total - r.counts.unlabelled)} ячеек с эталоном · ${dialogueCount(r.sample.dialogues)}`));
  const facts = node(doc, 'dl', 'cqa-quality__quick-facts');
  const seconds = n => n == null ? 'не измерено' : `${number(n, n > 0 && n < 1 ? 3 : 1)} с`;
  for (const [label, value] of [
    [r.cache === 'cached' ? 'Кэш · p50 / p95' : r.cache === 'mixed' ? 'Смешанный кэш · p50 / p95' : 'p50 / p95', `${seconds(r.latency?.p50_seconds)} / ${seconds(r.latency?.p95_seconds)}`],
    ['MAE итога', r.score_mae?.count ? `${number(r.score_mae.value)} · n=${r.score_mae.count}` : 'не измерено'],
    ['Цена / диалог', r.cost?.count ? `${number(r.cost.amount / r.cost.count, 4)} ${r.cost.currency}${r.cost.basis === 'estimated' ? ' (оценка)' : ' (источник)'}` : 'не восстановлена'],
  ]) { const pair = node(doc, 'div'); pair.append(node(doc, 'dt', '', label), node(doc, 'dd', '', value)); facts.append(pair); }
  card.append(facts);
  if (r.cache === 'cached') card.append(node(doc, 'p', 'cqa-quality__profile-description', 'Время повтора кэша; живое время не измерено.'));
  else if (r.cache === 'mixed') card.append(node(doc, 'p', 'cqa-quality__profile-description', 'Свежие ответы и кэш вместе; живое время отдельно не измерено.'));
  if (r.latency?.note && !['cached', 'mixed'].includes(r.cache)) card.append(node(doc, 'p', 'cqa-quality__profile-description', r.latency.note));
  return card;
}
function localMetricCard(doc, profile, measured) {
  if (measured) {
    const card = metricCard(doc, measured, `Локально · ${profile.label}`);
    card.append(node(doc, 'p', 'cqa-quality__profile-description', 'Стоимость API не включает сервер и эксплуатацию.')); return card;
  }
  const card = node(doc, 'article', 'cqa-quality__metric-card cqa-quality__metric-card--unmeasured');
  card.append(node(doc, 'h2', '', `Локально · ${profile.label}`), node(doc, 'p', 'cqa-quality__metric-value', 'Не измерено'), node(doc, 'p', 'cqa-quality__metric-support', 'Качество и время на этом наборе не проверены.'));
  const facts = node(doc, 'dl', 'cqa-quality__quick-facts');
  for (const [label, value] of [
    ['p50 / p95', 'не измерено'],
    ['API / диалог', profile.api_cost ? `${number(profile.api_cost.amount)} ${profile.api_cost.currency}` : 'не указано'],
    ['Сервер и эксплуатация', 'не рассчитаны'],
  ]) { const pair = node(doc, 'div'); pair.append(node(doc, 'dt', '', label), node(doc, 'dd', '', value)); facts.append(pair); }
  card.append(facts, node(doc, 'p', 'cqa-quality__profile-description', profile.api_cost ? 'Нулевой тариф API не означает нулевую полную стоимость.' : 'Стоимость сервера и эксплуатации не измерена.')); return card;
}
function comparisonText(item) {
  if (!item?.compared) return 'Парное сравнение не измерено.';
  const delta = `${item.delta > 0 ? '+' : ''}${number(item.delta * 100, 1)} п.п.`;
  const pValue = item.p_value > 0 && item.p_value < 0.0001 ? '<0,0001' : `=${number(item.p_value, 4)}`;
  return `${item.significant ? 'Различие обнаружено' : 'Различие в пределах шума'} · Δ ${delta} ${formatInterval(item.delta_ci, true)}. McNemar p${pValue}; выигрыш / проигрыш: ${item.win} / ${item.loss}; ${item.compared} общих ячеек, ${dialogueCount(item.dialogues)}.`;
}
function selectControl(doc, label, options, value, id) {
  const container = node(doc, 'label', 'cqa-quality__select-label', label);
  container.setAttribute('for', id);
  const select = node(doc, 'select', 'cqa-quality__select'); select.id = id;
  for (const option of options) { const el = node(doc, 'option', '', option.label); el.value = option.value; select.append(el); }
  select.value = value; container.append(select); return { container, select };
}
function recommendation(result, cloud, cascade) {
  if (!cloud) return 'Основная модель для выбранного эталона не указана.';
  const comparison = result.comparisons.find(row => row.report_id === cascade?.id && row.baseline_report_id === cloud.id);
  let message = `Почему ${modelLabel(cloud)}: основная модель по конфигурации.`;
  if (!cascade) return message + ' Сравнительное преимущество не установлено.';
  if (cloud.cache === 'fresh' && cascade.cache === 'fresh' && cloud.latency?.p95_seconds != null && cascade.latency?.p95_seconds != null && cloud.latency.scope === cascade.latency.scope) message += ` На этих прогонах p95 ${number(cloud.latency.p95_seconds, 1)} с; у каскада ${number(cascade.latency.p95_seconds, 1)} с.`;
  if (comparison?.compared && comparison.delta === 0) message += ' Каскад: наблюдаемое совпадение на общих ячейках такое же; эквивалентность не доказана.';
  else if (comparison) message += ` Каскад: ${comparison.significant ? 'обнаружено различие' : 'различие в пределах шума'} на общей выборке.`;
  const a = cloud.cost, b = cascade.cost;
  const comparable = (!cloud.cost_audit || !cloud.cost_audit.method || cloud.cost_audit.pricing_complete === true && cloud.cost_audit.comparable === true) && (!cascade.cost_audit || !cascade.cost_audit.method || cascade.cost_audit.pricing_complete === true && cascade.cost_audit.comparable === true) && a?.count && b?.count && a.count === cloud.sample.dialogues && b.count === cascade.sample.dialogues && a.currency === b.currency && a.basis === b.basis && cloud.cache === cascade.cache && cloud.sample.dataset === cascade.sample.dataset && cloud.sample.split === cascade.sample.split && cloud.guidance_version === cascade.guidance_version && cloud.checklist_version === cascade.checklist_version && cloud.mode === cascade.mode && comparison?.dialogues === cloud.sample.dialogues && comparison.dialogues === cascade.sample.dialogues;
  if (comparable && b.amount > 0) {
    const ratio = (a.amount / a.count) / (b.amount / b.count);
    message += ratio > 1 ? ` По ${a.basis === 'estimated' ? 'оценке' : 'данным источника'} стоимости каскад в ${number(ratio, 1)} раза дешевле.` : ' Преимущество каскада по стоимости на этих данных не показано.';
  } else if (b?.count) message += ' Полнота и сопоставимость цен требуют проверки; вывод об экономии не сделан.';
  else message += ' Стоимость каскада не восстановлена: полная сопоставимая разбивка по этапам отсутствует.';
  return message;
}
const pct1 = ratio => `${(ratio * 100).toLocaleString('ru-RU', { minimumFractionDigits: 1, maximumFractionDigits: 1 })}%`;
const usd3 = value => `$${value.toLocaleString('ru-RU', { minimumFractionDigits: 3, maximumFractionDigits: 3 })}`;
const perDialogueUsd = r => {
  const value = r?.economics?.cost_per_dialogue_usd ?? (r?.cost?.currency === 'USD' && r.cost.count > 0 ? r.cost.amount / r.cost.count : null);
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null;
};
function heldOutPhrase(n, split) {
  const last = n % 10, lastTwo = n % 100, one = last === 1 && lastTwo !== 11, few = last >= 2 && last <= 4 && (lastTwo < 12 || lastTwo > 14);
  const adjective = split === 'holdout' ? (one ? 'отложенный ' : 'отложенных ') : '';
  return `${number(n, 0)} ${adjective}${one ? 'диалог' : few ? 'диалога' : 'диалогов'}`;
}
/** Bento headline numbers, read from the published quality payload only; an absent value drops its tile. */
export function bentoStats(payload, referenceId = 'silver') {
  const reports = Array.isArray(payload?.reports) ? payload.reports : [], catalog = payload?.profile_catalog ?? {};
  const pick = key => {
    const anchor = reports.find(r => r.id === catalog[key]?.report_id);
    return anchor ? reports.find(r => r.run_id === anchor.run_id && r.reference?.id === referenceId) ?? null : null;
  };
  const ratio = c => c && c.total - c.unlabelled > 0 ? c.matched / (c.total - c.unlabelled) : null;
  const cloud = pick('cloud'), cascade = pick('cascade');
  const primaryRatio = ratio(cloud?.counts);
  const primary = primaryRatio == null ? null : {
    value: pct1(primaryRatio), label: 'Совпадение с эталоном',
    note: `верхняя оценка, ${heldOutPhrase(cloud.sample.dialogues, cloud.sample.split)} ${cloud.sample.dataset}`,
  };
  const tiles = [];
  const constant = payload?.baselines?.constant;
  const constantRatio = typeof constant?.agreement === 'number' ? constant.agreement : ratio(constant?.counts);
  if (constantRatio != null) tiles.push({ id: 'constant', label: 'Константа', value: pct1(constantRatio) });
  const kz = (payload?.subsamples ?? []).find(row => row.id === 'kz-all')?.references?.[referenceId];
  if (kz && kz.labelled > 0) tiles.push({ id: 'kz', label: 'Казахские чаты', value: pct1(kz.matched / kz.labelled) });
  const cloudCost = perDialogueUsd(cloud), cascadeCost = perDialogueUsd(cascade);
  if (cloudCost != null) tiles.push({ id: 'price', label: 'Цена диалога', value: usd3(cloudCost) });
  if (cascadeCost != null) tiles.push({ id: 'cascade', label: 'Каскад', value: usd3(cascadeCost) });
  return { primary, tiles };
}
function renderBento(doc, stats) {
  const grid = node(doc, 'section', 'cqa-quality__bento'); grid.setAttribute('aria-label', 'Ключевые замеры');
  if (stats.primary) {
    const main = node(doc, 'article', 'cqa-quality__bento-tile cqa-quality__bento-tile--primary');
    main.append(node(doc, 'p', 'cqa-quality__bento-pill', stats.primary.label), node(doc, 'p', 'cqa-quality__bento-value', stats.primary.value), node(doc, 'p', 'cqa-quality__bento-note', stats.primary.note));
    grid.append(main);
  }
  for (const tile of stats.tiles) {
    const el = node(doc, 'article', 'cqa-quality__bento-tile'); el.setAttribute('data-bento', tile.id);
    el.append(node(doc, 'p', 'cqa-quality__bento-label', tile.label), node(doc, 'p', 'cqa-quality__bento-value', tile.value));
    grid.append(el);
  }
  return grid;
}
function renderAnalyst(doc, result, payload = null) {
  const section = node(doc, 'section', 'cqa-quality__analyst');
  const refs = [...new Set(result.reports.map(r => r.reference.id))];
  const reference = selectControl(doc, 'Эталон', refs.map(value => ({ value, label: value })), refs.includes('silver') ? 'silver' : refs[0], 'cqa-quality-reference');
  const toolbar = node(doc, 'div', 'cqa-quality__toolbar');
  const provenanceNote = node(doc, 'p', 'cqa-quality__compact-note'); toolbar.append(reference.container, provenanceNote);
  const body = node(doc, 'div'); body.setAttribute('aria-live', 'polite'); section.append(toolbar, body);
  const findProfile = (key, rows) => {
    const anchor = result.reports.find(row => row.id === result.profile_catalog?.[key]?.report_id);
    return rows.find(row => row.run_id === anchor?.run_id) ?? (key === 'cloud' ? rows.find(row => !/cascade|routing/.test(row.run_id) && /gemini/i.test(row.model.requested)) : rows.find(row => row.run_id.includes(key)));
  };
  function draw() {
    const rows = result.reports.filter(r => r.reference.id === reference.select.value);
    const cloud = findProfile('cloud', rows) ?? rows[0], cascade = findProfile('cascade', rows), routing = findProfile('routing', rows);
    body.replaceChildren();
    const bento = bentoStats(payload, reference.select.value);
    if (bento.primary || bento.tiles.length) body.append(renderBento(doc, bento));
    provenanceNote.textContent = [referenceLabels[cloud.reference.type], cloud.sample.dev_reused ? 'DEV использован повторно' : splitLabels[cloud.sample.split], cloud.reference.type === 'human' ? 'независимость разметки требует проверки' : 'независимая человеческая точность не измерена · Gemini 95,6% — верхняя оценка; константа 83,0%'].join(' · ');
    const cards = node(doc, 'div', 'cqa-quality__cards');
    if (cloud) cards.append(metricCard(doc, cloud, modelLabel(cloud)));
    if (cascade && cascade !== cloud) cards.append(metricCard(doc, cascade, 'Каскад'));
    if (routing && routing !== cloud) cards.append(metricCard(doc, routing, 'Маршрутизация'));
    if (result.profile_catalog?.local) cards.append(localMetricCard(doc, result.profile_catalog.local, findProfile('local', rows)));
    if (cards.children.length > 2) cards.classList.add('cqa-quality__cards--profiles');
    body.append(cards, node(doc, 'p', 'cqa-quality__decision', recommendation(result, cloud, cascade)));
    const recorded = [cloud, cascade, routing].filter(r => r?.cost_audit?.method === 'stored_full_cost' && r.cost?.count);
    if (recorded.length) {
      const anomalies = recorded.filter(r => r.cost_audit.zero_cost_paid_usage_dialogues > 0).map(r => `${modelLabel(r)}: ${dialogueCount(r.cost_audit.zero_cost_paid_usage_dialogues)} с нулевой записанной ценой при расходе токенов; сумма занижена.`).join(' ');
      body.append(node(doc, 'p', 'cqa-quality__cost-provenance', `Стоимость — по ценам моделей в каждом вызове; токены по моделям не разделены. ${anomalies || 'Полнота цен и допущения пересчёта раскрыты в источниках.'}`));
    }
    const lower = node(doc, 'div', 'cqa-quality__analysis-grid');
    const slices = node(doc, 'section', 'cqa-quality__analysis-panel');
    slices.append(node(doc, 'h2', '', `Разрезы · ${modelLabel(cloud)}`));
    const sliceRows = [...(cloud.languages ?? []), ...(cloud.channels ?? [])];
    slices.append(table(doc, 'Совпадение среди ячеек с эталоном', ['Группа / диалоги', 'Совпадение и 95% ДИ'], sliceRows.map(row => [
      `${sliceLabel(row)} · ${dialogueCount(row.dialogues)}`,
      `${formatPercent(row.counts.matched, row.counts.total - row.counts.unlabelled)} ${row.agreement_ci?.lower != null ? `[${number(row.agreement_ci.lower * 100, 1)}–${number(row.agreement_ci.upper * 100, 1)}]` : formatInterval(row.agreement_ci)}`,
    ])));
    if (!sliceRows.length) slices.append(node(doc, 'p', 'cqa-quality__compact-note', 'Разрезы не измерены.'));
    const paired = node(doc, 'section', 'cqa-quality__analysis-panel');
    paired.append(node(doc, 'h2', '', 'Парное сравнение'));
    const comparisons = result.comparisons.filter(row => row.reference_id === reference.select.value && row.baseline_report_id === cloud.id);
    if (comparisons.length) {
      const current = comparisons.find(row => row.report_id === cascade?.id) ?? comparisons[0];
      const choice = selectControl(doc, `Сравнить с ${modelLabel(cloud)}`, comparisons.map(row => ({ value: row.report_id, label: modelLabel(rows.find(r => r.id === row.report_id)) })), current.report_id, 'cqa-quality-comparison');
      const output = node(doc, 'p', 'cqa-quality__paired-result', comparisonText(current)); output.setAttribute('role', 'status');
      choice.select.addEventListener('change', () => { output.textContent = comparisonText(comparisons.find(row => row.report_id === choice.select.value)); });
      paired.append(choice.container, output);
    } else paired.append(node(doc, 'p', 'cqa-quality__compact-note', 'Парное сравнение не измерено.'));
    const repeatPanel = node(doc, 'section', 'cqa-quality__analysis-panel');
    const repeat = node(doc, 'ul', 'cqa-quality__stability');
    for (const item of result.stability) {
      const left = result.reports.find(row => row.run_id === item.left_run_id);
      repeat.append(node(doc, 'li', '', `${modelLabel(left)}: ${formatPercent(item.matched, item.total)} ${formatInterval(item.agreement_ci)} · ${fraction(item.matched, item.total)} ячеек; ${dialogueCount(item.dialogues)}. ${item.same_configuration ? 'Одна конфигурация.' : 'Разные конфигурации / транспорт.'}`));
    }
    repeatPanel.append(node(doc, 'h2', '', 'Согласие между прогонами'), repeat);
    if (!result.stability.length) repeatPanel.append(node(doc, 'p', 'cqa-quality__compact-note', 'Повторяемость не измерена.'));
    paired.append(node(doc, 'p', 'cqa-quality__compact-note', 'Поисковые сравнения без поправки на множественные проверки. Отсутствие значимости не доказывает равенства.'));
    repeatPanel.append(node(doc, 'p', 'cqa-quality__compact-note', 'ДИ: бутстреп по диалогам. Независимость повторов не подтверждена.'));
    lower.append(slices, paired, repeatPanel); body.append(lower);
    const all = node(doc, 'div');
    if (result.stability.length) all.append(table(doc, 'Условия сравнения повторных прогонов', ['Прогоны', 'Ограничения'], result.stability.map(row => [`${row.left_run_id} / ${row.right_run_id}`, row.configuration_note])));
    all.append(node(doc, 'p', 'cqa-quality__note', 'В карточках — совпадение среди ячеек с эталоном. Пропуски и сбои модели входят в знаменатель; ячейки без эталона не проверены. Полный знаменатель и охват приведены ниже.'));
    for (const row of rows) all.append(detail(doc, `${modelLabel(row)} · ${percentWithInterval(row)}`, renderReport(doc, row)));
    const summary = detail(doc, `Все модели, критерии, знаменатели и источники · ${rows.length} прогонов`, all);
    summary.classList.add('cqa-quality__all-reports'); body.append(summary);
  }
  reference.select.addEventListener('change', draw); draw(); return section;
}

export function mount(CQA = globalThis.window?.CQA) {
  const doc = globalThis.document, host = doc?.getElementById('cqa-quality');
  if (!host) return () => {};
  if (mounts.has(host)) return mounts.get(host);
  if (!doc.querySelector('link[data-cqa-quality-style]')) {
    const style = node(doc, 'link'); style.rel = 'stylesheet'; style.href = new URL('../css/quality.css', import.meta.url).href;
    style.setAttribute('data-cqa-quality-style', ''); doc.head.append(style);
  }
  host.classList.remove('cqa-module-placeholder'); host.classList.add('cqa-quality'); host.replaceChildren();
  const header = node(doc, 'header', 'cqa-quality__header'), intro = node(doc, 'div');
  intro.append(node(doc, 'h1', '', 'Качество оценки'), node(doc, 'p', 'cqa-quality__intro', 'Измерения с источником, выборкой и полным знаменателем.'));
  const refresh = node(doc, 'button', 'cqa-quality__refresh', 'Обновить замеры'); refresh.type = 'button';
  header.append(intro, refresh);
  const status = node(doc, 'p', 'cqa-quality__status'); status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite'); status.setAttribute('aria-atomic', 'true');
  const content = node(doc, 'div', 'cqa-quality__content'); host.append(header, status, content);
  let epoch = 0, destroyed = false, calculatorController = null;
  const off = [];
  async function load() {
    if (destroyed) return;
    const current = ++epoch;
    calculatorController?.destroy(); calculatorController = null;
    host.classList.remove('cqa-quality--analyst');
    refresh.disabled = true; status.textContent = 'Загрузка замеров…'; content.setAttribute('aria-busy', 'true');
    const skeleton = node(doc, 'div', 'cqa-quality__skeleton'); skeleton.setAttribute('aria-hidden', 'true'); content.replaceChildren(skeleton);
    try {
      if (typeof CQA?.api?.quality !== 'function') throw new Error('missing adapter');
      const payload = await CQA.api.quality();
      if (destroyed || current !== epoch) return;
      const result = validateQuality(payload); content.replaceChildren();
      if (result.state === 'invalid') {
        status.textContent = 'Замеры не показаны: данные не соответствуют контракту.';
        content.append(node(doc, 'p', 'cqa-quality__error', 'Проверьте сводку источника и повторите загрузку.'), node(doc, 'p', 'cqa-quality__note', result.issues.join('; ')));
      } else if (result.state === 'empty') {
        status.textContent = 'Замеров пока нет';
        content.append(node(doc, 'p', 'cqa-quality__empty', 'Источник не передал измерения. Это не означает 0% качества: сравнение с эталоном ещё не опубликовано.'));
      } else {
        status.textContent = `Замеров: ${number(result.reports.length)}. Выборки и условия указаны отдельно.`;
        if (result.reports.some(r => r.agreement_ci)) { host.classList.add('cqa-quality--analyst'); content.append(renderAnalyst(doc, result, payload)); }
        else for (const r of result.reports) content.append(renderReport(doc, r));
        if (typeof CQA.economics?.mountCalculator === 'function') {
          const calculator = node(doc, 'div', 'cqa-quality__economics'); content.append(calculator);
          calculatorController = CQA.economics.mountCalculator(calculator, { quality: payload });
        }
      }
    } catch {
      if (destroyed || current !== epoch) return;
      status.textContent = 'Не удалось загрузить замеры';
      content.replaceChildren(node(doc, 'p', 'cqa-quality__error', 'Источник измерений недоступен. Проверьте подключение и нажмите «Обновить замеры».'));
    } finally {
      if (!destroyed && current === epoch) { refresh.disabled = false; content.setAttribute('aria-busy', 'false'); }
    }
  }
  const onRefresh = () => { void load(); }; refresh.addEventListener('click', onRefresh);
  for (const [name, callback] of [['quality:refresh', onRefresh], ['api:mode', onRefresh], ['tab:changed', tab => { if (tab === 'quality') onRefresh(); }]]) {
    const unsubscribe = CQA?.bus?.on?.(name, callback); if (typeof unsubscribe === 'function') off.push(unsubscribe);
  }
  const destroy = () => {
    if (destroyed) return;
    calculatorController?.destroy(); calculatorController = null;
    destroyed = true; ++epoch; off.forEach(unsubscribe => unsubscribe()); refresh.removeEventListener('click', onRefresh);
    host.replaceChildren(); mounts.delete(host);
  };
  mounts.set(host, destroy); void load(); return destroy;
}
