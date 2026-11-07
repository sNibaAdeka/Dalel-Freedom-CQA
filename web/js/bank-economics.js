/* Offline scenario estimates. No evaluation, network request or provider access. */
export const STORAGE_KEY = 'cqa.bank-economics.v1';
const profiles = ['cloud', 'cascade', 'routing', 'local'];
const names = { cloud: 'Облако · Gemini', cascade: 'Каскад · Haiku → Gemini', routing: 'Маршрутизация · чаты Haiku, звонки Gemini', local: 'Локально · qwen' };
const nonnegative = value => typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null;
const integer = value => Number.isSafeInteger(value) && value >= 0 ? value : null;
const fraction = value => nonnegative(value) !== null && value <= 1 ? value : null;
const multiply = (a, b) => a !== null && b !== null && Number.isFinite(a * b) ? a * b : null;
const knownSum = (a, b) => a !== null && b !== null && Number.isFinite(a + b) ? a + b : null;
const format = (value, digits = 1) => value === null ? 'не измерено' : value.toLocaleString('ru-RU', { maximumFractionDigits: digits });
const inputKeys = ['monthly_chats', 'monthly_calls', 'qa_hourly_kzt', 'manual_minutes', 'monthly_budget_kzt', 'spent_kzt'];
const mounts = new WeakMap();
const subscribers = new Set();
let mountNumber = 0;

function costValue(cost, n, fx) {
  if (!cost || !['reported', 'estimated'].includes(cost.basis) || cost.count !== n || !n || nonnegative(cost.amount) === null) return { kzt: null, usd: null };
  if (cost.currency === 'USD') return { usd: cost.amount / n, kzt: fx === null ? null : cost.amount / n * fx };
  if (cost.currency === 'KZT') return { kzt: cost.amount / n, usd: fx === null ? null : cost.amount / n / fx };
  return { kzt: null, usd: null };
}

function evidence(raw, n) {
  if (!raw || integer(raw.review_known_dialogues) !== n || !n || integer(raw.review_count) === null || raw.review_count > n) return null;
  return raw.review_count / n;
}

function measurements(raw = {}, fallback = {}) {
  return {
    cost_kzt: nonnegative(raw.cost_per_dialogue_kzt ?? fallback.cost_kzt),
    cost_usd: nonnegative(raw.cost_per_dialogue_usd ?? fallback.cost_usd),
    seconds: raw.latency_provenance?.projection_allowed === true ? nonnegative(raw.mean_seconds_per_dialogue) : null,
    review_fraction: evidence(raw, raw.dialogues),
    dialogues: integer(raw.dialogues) ?? null,
    cost_provenance: raw.cost_provenance ?? null,
    latency_provenance: raw.latency_provenance ?? null,
  };
}

/** Converts the aggregate report contract into four explicit profile entries. */
export function buildEconomics(quality = {}) {
  const fxInput = quality.economics?.usd_kzt ?? quality.provenance?.usd_kzt;
  const fx = nonnegative(fxInput) && fxInput > 0 ? fxInput : null;
  const rows = Array.isArray(quality.reports) ? quality.reports : [];
  return { usd_kzt: fx, profiles: profiles.map(id => {
    const catalog = quality.profile_catalog?.[id] ?? {};
    const report = rows.find(row => row.id === catalog.report_id) ?? null;
    const n = integer(report?.sample?.dialogues);
    const raw = report?.economics ?? {};
    const reportFx = nonnegative(raw.currency_fx?.usd_kzt) > 0 ? raw.currency_fx.usd_kzt : fx;
    const cost = costValue(report?.cost, n, reportFx);
    const base = measurements(raw, { cost_kzt: cost.kzt, cost_usd: cost.usd });
    const localApiZero = id === 'local' && catalog.cloud_fallback === false && catalog.api_cost?.amount === 0 && catalog.api_cost?.currency === 'USD' && catalog.api_cost?.scope === 'local_only';
    if (localApiZero) { base.cost_kzt = 0; base.cost_usd = 0; }
    const counts = report?.counts, denominator = counts ? counts.total - (counts.unlabelled ?? 0) : null;
    const matched = integer(counts?.matched);
    const agreement = integer(denominator) !== null && denominator > 0 && matched !== null && matched <= denominator ? matched / denominator : null;
    const ci = report?.agreement_ci;
    const interval = ci?.level === 0.95 && fraction(ci.lower) !== null && fraction(ci.upper) !== null && ci.lower <= ci.upper ? [ci.lower, ci.upper] : null;
    const strata = {};
    for (const [channel, item] of Object.entries(raw.channels ?? {})) if (['chat', 'call'].includes(channel)) strata[channel] = measurements(item);
    return { id, label: names[id], report_id: report?.id ?? null, mode: report?.mode ?? null, ...base, usd_kzt: reportFx,
      dialogues: n, agreement, interval, reference: report?.reference?.id ?? null,
      cost_per_pair_kzt: nonnegative(raw.cost_per_pair_kzt), cost_per_pair_usd: nonnegative(raw.cost_per_pair_usd),
      seconds_per_pair: raw.latency_provenance?.projection_allowed === true ? nonnegative(raw.mean_seconds_per_pair) : null, strata, local_api_only: localApiZero,
      measurement_note: report ? `${n} диалогов ${report.sample?.dataset ?? 'DEV'}; эталон ${report.reference?.id ?? 'не указан'} (${report.reference?.type === 'human' ? 'ручной' : 'машинный'})` : 'Замера качества и скорости нет',
    };
  }) };
}

export function validateInputs(inputs = {}) {
  const values = {}, errors = {};
  for (const key of inputKeys) {
    const raw = inputs[key];
    if (raw === '' || raw === undefined || raw === null) { values[key] = null; continue; }
    const value = typeof raw === 'number' ? raw : typeof raw === 'string' && raw.trim() ? Number(raw) : NaN;
    const count = key === 'monthly_chats' || key === 'monthly_calls';
    if (nonnegative(value) === null || (count && !Number.isSafeInteger(value))) { errors[key] = count ? 'Введите целое число не меньше нуля.' : 'Введите число не меньше нуля.'; values[key] = null; }
    else values[key] = value;
  }
  return { values, errors };
}

function weighted(profile, metric, chats, calls) {
  const total = knownSum(chats, calls);
  if (total === null) return { value: null, basis: 'missing_volume' };
  if (total === 0) return { value: 0, basis: 'empty_volume' };
  const needed = [['chat', chats], ['call', calls]].filter(([, n]) => n > 0);
  if (needed.every(([channel]) => nonnegative(profile.strata[channel]?.[metric]) !== null)) {
    return { value: needed.reduce((sum, [channel, n]) => sum + profile.strata[channel][metric] * n, 0) / total, basis: 'channel_weighted' };
  }
  // Aggregate sample means are never silently treated as estimates for another channel mix.
  if (profile.local_api_only && metric.startsWith('cost_')) return { value: 0, basis: 'local_api_only' };
  return { value: null, basis: 'missing_channel_measurements' };
}

function scenarioProvenance(profile, chats, calls) {
  const total = knownSum(chats, calls);
  if (total === null || total === 0) {
    const note = total === null ? 'Для оговорок по составу каналов задайте оба объёма.' : 'Объём равен нулю; прогноз работы новых диалогов не задан.';
    return { cost_provenance: { comparable: false, pricing_complete: null, coverage_note: note }, latency_provenance: { projection_allowed: false, note } };
  }
  const selected = [['chat', chats, 'Чаты'], ['call', calls, 'Звонки']].filter(([, n]) => n > 0);
  const costSources = selected.map(([channel]) => profile.strata[channel]?.cost_provenance);
  const latencySources = selected.map(([channel]) => profile.strata[channel]?.latency_provenance);
  const note = (key, field, missing) => selected.map(([channel, , label]) => `${label}: ${profile.strata[channel]?.[key]?.[field] ?? missing}`).join(' ');
  return {
    cost_provenance: {
      comparable: costSources.every(value => value?.comparable === true),
      pricing_complete: costSources.some(value => value?.pricing_complete === false) ? false : costSources.every(value => value?.pricing_complete === true) ? true : null,
      coverage_note: note('cost_provenance', 'coverage_note', 'полнота записанных цен не подтверждена.'),
    },
    latency_provenance: {
      projection_allowed: latencySources.every((value, i) => value?.projection_allowed === true && nonnegative(profile.strata[selected[i][0]]?.seconds) !== null),
      note: note('latency_provenance', 'note', 'сопоставимый замер свежего расчёта не указан.'),
    },
  };
}

export function calculateEconomics(inputs, model) {
  const { values, errors } = validateInputs(inputs), total = knownSum(values.monthly_chats, values.monthly_calls);
  const manualHours = multiply(total, values.manual_minutes === null ? null : values.manual_minutes / 60);
  const remaining = values.monthly_budget_kzt !== null && values.spent_kzt !== null ? values.monthly_budget_kzt - values.spent_kzt : null;
  return { inputs: values, errors, total, call_share: total > 0 ? values.monthly_calls / total : null, manual_hours: manualHours,
    remaining_kzt: remaining, profiles: model.profiles.map(profile => {
      const kzt = weighted(profile, 'cost_kzt', values.monthly_chats, values.monthly_calls);
      const usd = weighted(profile, 'cost_usd', values.monthly_chats, values.monthly_calls);
      const time = weighted(profile, 'seconds', values.monthly_chats, values.monthly_calls);
      const review = weighted(profile, 'review_fraction', values.monthly_chats, values.monthly_calls);
      const costKzt = multiply(total, kzt.value), saved = multiply(manualHours, review.value === null ? null : 1 - review.value);
      const gross = multiply(saved, values.qa_hourly_kzt);
      return { ...profile, ...scenarioProvenance(profile, values.monthly_chats, values.monthly_calls), monthly_cost_kzt: costKzt, monthly_cost_usd: multiply(total, usd.value), cost_basis: kzt.basis,
        sequential_hours_10000: time.value === null || total === 0 ? null : time.value * 10000 / 3600,
        qa_hours_saved: saved, qa_value_saved_kzt: gross, net_before_other_costs_kzt: gross !== null && costKzt !== null ? gross - costKzt : null,
        review_fraction: review.value, over_budget: remaining !== null && costKzt !== null ? costKzt > remaining : null };
    }) };
}

/** Plan pairs are criterion assessments, never provider calls. */
export function estimatePlanBudget(plan = {}, profileId, model, budget = {}) {
  const profile = model.profiles.find(item => item.id === profileId);
  const pairs = integer(plan.pairs_to_evaluate ?? plan.estimated_pairs ?? plan.to_evaluate);
  const dialogues = integer(plan.total_dialogues ?? plan.conversations_total ?? plan.conversations ?? plan.total ?? plan.dialogues);
  const values = validateInputs(budget).values;
  const remaining = values.monthly_budget_kzt !== null && values.spent_kzt !== null ? values.monthly_budget_kzt - values.spent_kzt : null;
  let kzt = null, usd = null, seconds = null, basis = 'unknown';
  let provenance = { cost_provenance: profile?.cost_provenance, latency_provenance: profile?.latency_provenance };
  const mode = typeof plan.mode === 'string' ? plan.mode.split(':')[0] : null;
  const comparable = profile && (!mode || mode === profile.mode);
  if (pairs === 0 || (pairs === null && dialogues === 0)) { kzt = 0; usd = 0; seconds = 0; basis = 'no_evaluations'; }
  else if (comparable && pairs !== null && profile.cost_per_pair_kzt !== null) {
    kzt = multiply(pairs, profile.cost_per_pair_kzt); usd = multiply(pairs, profile.cost_per_pair_usd);
    seconds = multiply(pairs, profile.seconds_per_pair); basis = 'mean_pair_allocation';
  } else if (comparable && dialogues !== null) {
    const chats = integer(plan.channels?.chat), calls = integer(plan.channels?.call);
    if (knownSum(chats, calls) === dialogues && dialogues > 0) {
      kzt = multiply(dialogues, weighted(profile, 'cost_kzt', chats, calls).value);
      usd = multiply(dialogues, weighted(profile, 'cost_usd', chats, calls).value);
      seconds = multiply(dialogues, weighted(profile, 'seconds', chats, calls).value); basis = 'channel_weighted_dialogues';
      provenance = scenarioProvenance(profile, chats, calls);
    } else {
      kzt = multiply(dialogues, profile.cost_kzt); usd = multiply(dialogues, profile.cost_usd);
      seconds = multiply(dialogues, profile.seconds); basis = 'mean_dialogue_fallback';
    }
  }
  if (profile?.local_api_only && (pairs !== null || dialogues !== null)) { kzt = 0; usd = 0; }
  return { profile: profileId, pairs, dialogues, cost_kzt: kzt, cost_usd: usd, sequential_seconds: seconds, basis, ...provenance,
    remaining_kzt: remaining, over_budget: kzt !== null && remaining !== null ? kzt > remaining : null,
    mode_comparable: !!comparable };
}

export function loadInputs(storage) {
  try { const raw = JSON.parse(storage?.getItem(STORAGE_KEY) ?? '{}'); return Object.fromEntries(inputKeys.map(key => [key, validateInputs(raw).values[key] ?? ''])); }
  catch { return Object.fromEntries(inputKeys.map(key => [key, ''])); }
}
export function saveInputs(storage, inputs) {
  const values = validateInputs(inputs).values;
  let saved = false;
  try { if (storage) { storage.setItem(STORAGE_KEY, JSON.stringify(values)); saved = true; } } catch { /* The page remains usable without persistent storage. */ }
  for (const notify of subscribers) notify(storage, { ...inputs });
  return saved;
}

const element = (tag, content, className) => { const node = document.createElement(tag); if (content != null) node.textContent = content; if (className) node.className = className; return node; };
function style() {
  if (!document.querySelector('[data-bank-economics-style]')) { const link = element('link'); link.rel = 'stylesheet'; link.href = new URL('../css/bank-economics.css', import.meta.url).href; link.setAttribute('data-bank-economics-style', ''); document.head.append(link); }
}
function defaultStorage() { try { return globalThis.localStorage; } catch { return null; } }
function makeField(key, label, value, onChange, prefix) {
  const wrapper = element('div', null, 'cqa-economics__field'), id = `${prefix}-${key}`;
  const lab = element('label', label); lab.htmlFor = id;
  const input = element('input'); input.type = 'number'; input.id = id; input.min = '0'; input.step = key.includes('chats') || key.includes('calls') ? '1' : 'any'; input.value = value;
  input.setAttribute('inputmode', 'decimal'); input.setAttribute('aria-describedby', `${id}-error`);
  const error = element('span', '', 'cqa-economics__error'); error.id = `${id}-error`;
  input.addEventListener('input', onChange); wrapper.append(lab, input, error); return { wrapper, input, error, onChange };
}
function row(list, title, value) { const item = element('div'), dt = element('dt', title), dd = element('dd', value); item.append(dt, dd); list.append(item); }
function accuracy(profile) {
  if (profile.agreement === null) return 'не измерено';
  return `${format(profile.agreement * 100)}%${profile.interval ? ` [${format(profile.interval[0] * 100)}–${format(profile.interval[1] * 100)}], 95% ДИ` : '; ДИ не измерен'}`;
}

export function mountCalculator(host, { quality = {}, storage = defaultStorage(), onChange } = {}) {
  if (!host) return { destroy() {}, updateQuality() {}, getInputs: () => ({}) };
  mounts.get(host)?.destroy(); style();
  let model = buildEconomics(quality), inputs = loadInputs(storage), alive = true;
  const prefix = `bank-economics-${++mountNumber}`;
  const details = element('details', null, 'cqa-economics'), summary = element('summary', 'Экономика банка · расчёт по вашему объёму');
  const note = element('p', 'Введите свои предположения. Пустое поле означает «не задано». Ставка и минуты — ввод пользователя, а не измеренные показатели.', 'cqa-economics__note');
  const fields = element('div', null, 'cqa-economics__fields'), results = element('div', null, 'cqa-economics__results'), status = element('p', '', 'cqa-economics__note');
  status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite');
  const labels = { monthly_chats: 'Чатов в месяц', monthly_calls: 'Звонков в месяц', qa_hourly_kzt: 'Ставка QA, ₸/час', manual_minutes: 'Ручная проверка, минут/диалог', monthly_budget_kzt: 'Месячный бюджет API, ₸', spent_kzt: 'Уже потрачено за месяц, ₸' };
  const controls = {};
  function render() {
    if (!alive) return;
    const computed = calculateEconomics(inputs, model); results.replaceChildren();
    for (const [key, control] of Object.entries(controls)) { control.error.textContent = computed.errors[key] ?? ''; control.input.setAttribute('aria-invalid', computed.errors[key] ? 'true' : 'false'); }
    status.textContent = `Доля звонков: ${computed.call_share === null ? 'задайте объём' : `${format(computed.call_share * 100)}%`}. Остаток бюджета: ${computed.remaining_kzt === null ? 'задайте бюджет и уже потраченную сумму' : `${format(computed.remaining_kzt)} ₸`}.`;
    for (const profile of computed.profiles) {
      const card = element('section', null, 'cqa-economics__profile'), h = element('h3', profile.label), list = element('dl');
      row(list, 'API за месяц · сценарий по записанным расходам', `${format(profile.monthly_cost_kzt, 2)} ₸ / ${format(profile.monthly_cost_usd, 2)} $`);
      row(list, '10 000 диалогов · последовательная работа', `${format(profile.sequential_hours_10000, 2)} ч`);
      row(list, 'Совпадение с эталоном', accuracy(profile));
      row(list, 'Часов QA высвобождается', format(profile.qa_hours_saved, 2));
      row(list, 'Ценность времени за вычетом API', `${format(profile.net_before_other_costs_kzt, 2)} ₸`);
      const caveat = element('p', `${profile.measurement_note}. ${profile.local_api_only ? '$0 — только API. Оборудование, электричество и сопровождение не измерены.' : 'Стоимость — по ценам моделей в каждом вызове; токены по моделям не разделены.'}`, 'cqa-economics__note');
      card.append(h, list, caveat);
      if (!profile.local_api_only) card.append(element('p', `Выборка каналов: чаты — ${format(profile.strata.chat?.dialogues ?? null, 0)}, звонки — ${format(profile.strata.call?.dialogues ?? null, 0)}. Совпадение с эталоном относится к исходной выборке, а не к введённому месячному составу. ${profile.usd_kzt === null ? 'Курс не задан.' : `Перевод валют: ${format(profile.usd_kzt)} ₸/$ — допущение конфигурации.`}`, 'cqa-economics__note'));
      if (profile.id === 'routing') card.append(element('p', 'Звонки идут сразу на Gemini. Чаты — на Haiku, затем на Gemini при сомнении или ответе C3 «Да/Нет».', 'cqa-economics__note'));
      if (profile.cost_provenance?.comparable !== true && !profile.local_api_only) card.append(element('p', profile.cost_provenance?.coverage_note ?? 'Полнота исторических цен не подтверждена; расходы не доказывают экономию относительно другого профиля.', 'cqa-economics__note'));
      if (profile.latency_provenance?.projection_allowed !== true) card.append(element('p', profile.latency_provenance?.note ?? 'Нет сопоставимого замера свежего расчёта: время на 10 000 новых диалогов неизвестно.', 'cqa-economics__note'));
      if (profile.cost_basis === 'missing_channel_measurements') card.append(element('p', 'Нет полного замера затрат для заданного состава чатов и звонков. Общая средняя выборки не переносится автоматически.', 'cqa-economics__note'));
      if (profile.review_fraction === null) card.append(element('p', 'Доля «на проверку» не измерена: экономия труда неизвестна. Эскалация к Gemini не равна ручной проверке.', 'cqa-economics__note'));
      if (profile.over_budget === true) card.append(element('p', 'Сценарий превышает заданный остаток бюджета.', 'cqa-economics__warning'));
      results.append(card);
    }
    onChange?.({ ...inputs }, computed);
  }
  for (const key of inputKeys) {
    const change = () => { inputs[key] = controls[key].input.value; saveInputs(storage, inputs); render(); };
    controls[key] = makeField(key, labels[key], inputs[key], change, prefix); fields.append(controls[key].wrapper);
  }
  details.append(summary, note, fields, status, results, element('p', 'Экстраполяция малого DEV-замера не гарантирует качество на новых данных. Время — сумма последовательной работы, не срок обработки банка. Экономия труда предполагает прежнюю длительность проверки и рассмотрение только явно помеченных диалогов; это сценарий до пилота, не подтверждённый ROI.', 'cqa-economics__note'));
  host.replaceChildren(details); render();
  const sync = (source, values) => { if (source !== storage || !alive) return; inputs = { ...inputs, ...values }; for (const [key, control] of Object.entries(controls)) control.input.value = inputs[key] ?? ''; render(); };
  subscribers.add(sync);
  const handle = { destroy() { if (!alive) return; alive = false; subscribers.delete(sync); for (const field of Object.values(controls)) field.input.removeEventListener('input', field.onChange); host.replaceChildren(); mounts.delete(host); }, updateQuality(value) { model = buildEconomics(value); render(); }, getInputs: () => ({ ...inputs }) };
  mounts.set(host, handle); return handle;
}

export function mountBudget(host, { quality = {}, plan = {}, profile = 'cloud', storage = defaultStorage(), onSwitchCascade, busy = false } = {}) {
  if (!host) return { destroy() {}, update() {} };
  mounts.get(host)?.destroy(); style();
  let model = buildEconomics(quality), currentPlan = plan, currentProfile = profile, alive = true, inputs = loadInputs(storage), isBusy = busy, switching = false, callback = onSwitchCascade, switchFailed = false;
  const prefix = `bank-budget-${++mountNumber}`;
  const section = element('section', null, 'cqa-economics cqa-economics--budget'), fields = element('div', null, 'cqa-economics__fields'), result = element('p'), warning = element('p', '', 'cqa-economics__warning');
  result.setAttribute('role', 'status'); warning.setAttribute('role', 'status');
  const switchButton = element('button', 'Переключить на каскад', 'cqa-economics__button'); switchButton.type = 'button';
  const controls = {};
  function render() {
    if (!alive) return;
    const estimate = estimatePlanBudget(currentPlan, currentProfile, model, inputs), checked = validateInputs(inputs);
    for (const [key, field] of Object.entries(controls)) { field.error.textContent = checked.errors[key] ?? ''; field.input.setAttribute('aria-invalid', checked.errors[key] ? 'true' : 'false'); field.input.disabled = isBusy || switching; }
    result.textContent = `Оценка прогона по записанным расходам: ${format(estimate.cost_kzt, 2)} ₸ / ${format(estimate.cost_usd, 4)} $. Последовательная работа: ${format(estimate.sequential_seconds, 1)} с. ${estimate.basis === 'mean_pair_allocation' ? 'Средняя цена пары × пары к оценке; пакетный вызов может стоить иначе.' : estimate.basis === 'channel_weighted_dialogues' ? 'По составу чатов и звонков в плане.' : estimate.basis === 'mean_dialogue_fallback' ? 'По средней цене диалога; состав каналов и новые критерии могут изменить стоимость.' : estimate.basis === 'no_evaluations' ? 'Новых оценок не требуется.' : 'Нет сопоставимого замера для этого режима или размера плана.'} ${estimate.cost_provenance?.coverage_note ?? ''}`;
    warning.textContent = estimate.over_budget === true ? `План превышает остаток ${format(estimate.remaining_kzt, 2)} ₸. Переключение профиля не гарантирует соблюдение бюджета.` : estimate.remaining_kzt === null ? 'Чтобы проверить лимит, укажите бюджет и уже потраченную сумму. Автоматического учёта расходов здесь нет.' : `Остаток бюджета: ${format(estimate.remaining_kzt, 2)} ₸.`;
    if (switchFailed) warning.textContent += ' Не удалось переключить профиль; попробуйте ещё раз.';
    switchButton.hidden = !(estimate.over_budget === true && currentProfile !== 'cascade' && typeof callback === 'function'); switchButton.disabled = isBusy || switching;
  }
  for (const [key, label] of [['monthly_budget_kzt', 'Месячный бюджет API, ₸'], ['spent_kzt', 'Уже потрачено за месяц, ₸']]) {
    const change = () => { inputs = { ...loadInputs(storage), ...inputs, [key]: controls[key].input.value }; saveInputs(storage, inputs); render(); };
    controls[key] = makeField(key, label, inputs[key], change, prefix); fields.append(controls[key].wrapper);
  }
  const switchProfile = async () => { if (isBusy || switching || !alive || typeof callback !== 'function') return; switching = true; switchFailed = false; render(); try { await callback(); } catch { switchFailed = true; } finally { switching = false; render(); } }; switchButton.addEventListener('click', switchProfile);
  const limitation = element('p', 'Если сервер не передал число пар к оценке, показан сценарий полной оценки выбранных диалогов. Кэш и переиспользование критериев не учтены; фактические расходы могут отличаться.', 'cqa-economics__note');
  section.append(element('h3', 'Бюджет перед применением'), fields, result, limitation, warning, switchButton); host.replaceChildren(section); render();
  const sync = (source, values) => { if (source !== storage || !alive) return; inputs = { ...inputs, ...values }; for (const [key, control] of Object.entries(controls)) control.input.value = inputs[key] ?? ''; render(); };
  subscribers.add(sync);
  const handle = { destroy() { alive = false; subscribers.delete(sync); for (const field of Object.values(controls)) field.input.removeEventListener('input', field.onChange); switchButton.removeEventListener('click', switchProfile); host.replaceChildren(); mounts.delete(host); }, update(value = {}) { if ('quality' in value) model = buildEconomics(value.quality); if ('plan' in value) currentPlan = value.plan; if ('profile' in value) currentProfile = value.profile; if ('busy' in value) isBusy = !!value.busy; if ('onSwitchCascade' in value) callback = value.onSwitchCascade; render(); } };
  mounts.set(host, handle); return handle;
}
