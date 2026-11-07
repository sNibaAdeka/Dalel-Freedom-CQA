/* Read-only, one-criterion-at-a-time sensitivity preview. Official scores belong to the API.
 * Uses score.lines (including human overrides), never substitutes model answers for missing lines.
 * Exported helpers are safe to import from a browser test or Node without mounting the UI. */
const finite = value => typeof value === 'number' && Number.isFinite(value);
const list = value => Array.isArray(value) ? value : [];
const failed = value => Boolean(value?.error || list(value?.errors).length || ['error', 'failed', 'incomplete', 'partial'].includes(value?.status));
const stale = (item, criterion) => Boolean(item?.criterion_hash && criterion.hash && item.criterion_hash !== criterion.hash);
const fmt = (value, digits = 2) => finite(value) ? value.toLocaleString('ru-RU', { maximumFractionDigits: digits }) : '—';
const answerText = value => ({ yes: 'Да', no: 'Нет', na: 'N/A' })[value] || (finite(value) ? fmt(value, 6) : 'нет ответа');

// Decimal fractions keep checklist half-up/half-even ties consistent with cqa/engine.py.
function fraction(value) {
  const [base, exponent = '0'] = String(value).toLowerCase().split('e');
  const decimals = (base.split('.')[1] || '').length - Number(exponent);
  const n = BigInt(base.replace('.', ''));
  return decimals >= 0 ? [n, 10n ** BigInt(decimals)] : [n * 10n ** BigInt(-decimals), 1n];
}
function rational(n, d) {
  let a = n < 0n ? -n : n, b = d;
  while (b) [a, b] = [b, a % b];
  return [n / (a || 1n), d / (a || 1n)];
}
const add = (a, b) => rational(a[0] * b[1] + b[0] * a[1], a[1] * b[1]);
const multiply = (a, b) => rational(a[0] * b[0], a[1] * b[1]);
function rounded(value, digits, mode) {
  const factor = 10n ** BigInt(digits);
  const sign = value[0] < 0n ? -1n : 1n;
  const n = value[0] * sign * factor, d = value[1];
  let whole = n / d;
  if (2n * (n % d) > d || (2n * (n % d) === d && (mode !== 'half_even' || whole % 2n))) whole++;
  return Number(whole * sign) / Number(factor);
}

/** Exact allowed steps; numeric criteria use range endpoints to bound their possible effect. */
export function reviewAnswers(criterion) {
  let answers;
  if (criterion?.answer_type === 'binary') answers = ['no', 'yes'];
  else if (['scale', 'numeric'].includes(criterion?.answer_type)) {
    const scale = list(criterion.scale);
    if (!scale.length || !scale.every(finite) || Math.max(...scale) <= 0) return [];
    answers = [...new Set(scale)].sort((a, b) => a - b);
    if (criterion.answer_type === 'numeric') answers = [answers.length > 1 ? answers[0] : 0, answers.at(-1)];
  } else return [];
  if (criterion.na_allowed === true) answers.push('na');
  return answers;
}
function validAnswer(criterion, answer) {
  const options = reviewAnswers(criterion);
  if (criterion.answer_type === 'numeric' && finite(answer)) return options.length > 0 && answer >= options[0] && answer <= options[1];
  return options.includes(answer);
}
function totalFor(criteria, answers, digits, mode) {
  let weight = [0n, 1n], earned = [0n, 1n];
  for (const criterion of criteria) {
    const answer = answers.get(criterion.id);
    if (criterion.weight === 0 || answer === 'na') continue;
    const w = fraction(criterion.weight);
    const share = answer === 'yes' ? [1n, 1n] : answer === 'no' ? [0n, 1n]
      : multiply(fraction(answer), fraction(Math.max(...criterion.scale)).slice().reverse());
    weight = add(weight, w); earned = add(earned, multiply(w, share));
  }
  return { total: weight[0] === 0n ? null : rounded(multiply(multiply(earned, [weight[1], weight[0]]), [100n, 1n]), digits, mode), denominator: Number(weight[0]) / Number(weight[1]) };
}
function activeAppeals(detail, criterion) {
  const decided = new Set(list(detail?.decisions).map(item => item.appeal_id));
  return list(detail?.appeals).filter(item => item.criterion_id === criterion.id && !stale(item, criterion) &&
    (item.status || 'open') === 'open' && !decided.has(item.id));
}
function reasonsFor(detail, criterion, line, evaluation) {
  const reasons = [];
  if (activeAppeals(detail, criterion).length || evaluation?.disputed === true || evaluation?.status === 'disputed') reasons.push('Открытый спор');
  // A settled human answer must not inherit the original model's uncertainty.
  if ((line?.source === 'decision' || line?.decision_id) && !reasons.length) return reasons;
  if (!evaluation || evaluation.answer == null || failed(evaluation) || line?.missing || line?.answer == null) reasons.push('Неполные данные');
  if (evaluation?.needs_review === true || evaluation?.status === 'needs_review') reasons.push('Нужна проверка');
  const threshold = evaluation?.mode === 'accurate' ? 0.67 : 0.7;
  if (evaluation?.uncertain === true || evaluation?.status === 'uncertain' ||
      (finite(evaluation?.confidence) && evaluation.confidence < threshold)) reasons.push('Неопределённость');
  else if (evaluation && !finite(evaluation.confidence)) reasons.push('Уверенность не указана');
  return reasons;
}

/** Returns ranked hypothetical changes, separate critical flags, and an explicit unavailable state.
 * Ranking = maximum absolute change in the rounded total. Confidence is never a probability or multiplier.
 * Scenarios are independent (not additive); removing the last applicable criterion produces null, not zero. */
export function buildReviewImpact(detail) {
  const criteria = list(detail?.checklist?.criteria), lines = list(detail?.score?.lines);
  const score = detail?.score;
  const digits = detail?.checklist?.rounding?.total ?? 2, mode = detail?.checklist?.rounding?.mode ?? 'half_up';
  const entries = criteria.map((criterion, index) => {
    const line = lines.find(item => item.criterion_id === criterion.id);
    const evaluation = list(detail?.evals).filter(item => item.criterion_id === criterion.id && !stale(item, criterion)).at(-1);
    return { criterion, criterionId: criterion.id, title: criterion.title || criterion.id, index, line, evaluation,
      currentAnswer: line?.answer ?? null, reasons: reasonsFor(detail, criterion, line, evaluation) };
  });
  let reason = null;
  if (!score || !finite(score.total)) reason = score?.status === 'no_applicable'
    ? 'Нет применимых критериев и официального итога для сравнения.' : 'Официальный итог не получен. Влияние пока недоступно.';
  else if (failed(detail) || failed(score) || score.status !== 'complete') reason = 'Оценка неполная или содержит ошибку. Влияние пока недоступно.';
  else if (!criteria.length || lines.length !== criteria.length || new Set(lines.map(item => item.criterion_id)).size !== lines.length ||
      new Set(criteria.map(item => item.id)).size !== criteria.length) reason = 'Не получен полный чек-лист и расчёт по критериям.';
  else if (score.checklist_version != null && score.checklist_version !== detail.checklist.version) reason = 'Версии оценки и чек-листа различаются. Обновите карточку.';
  else if (!Number.isInteger(digits) || digits < 0 || digits > 6 || !['half_up', 'half_even'].includes(mode)) reason = 'Неизвестное правило округления. Влияние недоступно.';
  else if (entries.some(({ criterion, line, evaluation }) => !finite(criterion.weight) || criterion.weight < 0 ||
      !line || !finite(line.weight) || line.weight !== criterion.weight || line.missing || line.source === 'missing' ||
      !validAnswer(criterion, line.answer) || (line.is_na != null && line.is_na !== (line.answer === 'na')) ||
      (line.source !== 'decision' && !line.decision_id && (!evaluation || failed(evaluation) || evaluation.answer !== line.answer)))) {
    reason = 'Не хватает корректных ответов или весов. Влияние недоступно до обновления оценки.';
  }
  const answers = new Map(entries.map(item => [item.criterionId, item.currentAnswer]));
  const baseline = reason ? null : totalFor(criteria, answers, digits, mode);
  if (!reason && (baseline.total === null || Math.abs(baseline.total - score.total) > 1e-8)) reason = 'Ответы и веса не совпадают с официальным итогом. Обновите карточку.';
  const critical = entries.filter(item => item.criterion.critical === true).map(item => {
    const polarity = item.criterion.critical_when || (item.criterion.critical_rule === 'no' ? 'no' : 'yes');
    const known = item.line && !item.line.missing && validAnswer(item.criterion, item.currentAnswer) && item.currentAnswer !== 'na';
    return { criterionId: item.criterionId, title: item.title, reasons: item.reasons, weight: finite(item.criterion.weight) ? item.criterion.weight : null,
      state: known ? item.currentAnswer === polarity ? 'hit' : 'clear' : 'unavailable' };
  });
  const ranked = entries.filter(item => item.reasons.length && item.criterion.critical !== true).map(item => {
    const scenarios = reason ? [] : reviewAnswers(item.criterion).filter(answer => answer !== item.currentAnswer).map(answer => {
      const changed = new Map(answers); changed.set(item.criterionId, answer);
      const projected = totalFor(criteria, changed, digits, mode);
      return { answer, total: projected.total, delta: projected.total === null ? null : Number((projected.total - score.total).toFixed(digits)),
        denominator: projected.denominator, denominatorChanged: projected.denominator !== baseline.denominator };
    });
    const changes = scenarios.filter(item => item.delta !== null).map(item => Math.abs(item.delta));
    return { criterionId: item.criterionId, title: item.title, currentAnswer: item.currentAnswer, reasons: item.reasons, index: item.index,
      scenarios, maxImpact: reason || !changes.length ? null : Math.max(...changes), removesTotal: scenarios.some(item => item.total === null) };
  }).sort((a, b) => (b.maxImpact ?? -1) - (a.maxImpact ?? -1) || a.index - b.index);
  return { available: !reason, reason, officialTotal: finite(score?.total) ? score.total : null, digits: Number.isInteger(digits) && digits >= 0 && digits <= 6 ? digits : 2,
    denominator: reason ? null : baseline.denominator, ranked, critical };
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = String(text);
  return node;
}
function arrow() {
  // Lucide arrow-up-right, matching the existing product's stroke icons.
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 24 24'); svg.setAttribute('aria-hidden', 'true');
  const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  path.setAttribute('d', 'M7 7h10v10M7 17 17 7'); svg.append(path);
  return svg;
}
function focusButton(item, onFocus, text) {
  const node = el('button', 'cqa-review-impact__focus'); node.type = 'button';
  node.dataset.reviewImpactId = item.criterionId;
  node.setAttribute('aria-label', `Проверить ${item.criterionId}: ${item.title}. Открыть критерий и доказательства`);
  node.append(el('span', 'cqa-review-impact__name', `${item.criterionId} · ${item.title}`), el('span', 'cqa-review-impact__value', text), arrow());
  node.addEventListener('click', () => onFocus?.(item.criterionId));
  return node;
}

/** Pure DOM view: its only action is onFocus; it has no API, score update or save capability. */
export function createReviewImpact(detail, { onFocus } = {}) {
  const preview = buildReviewImpact(detail);
  const attentionFlags = preview.critical.filter(item => item.state !== 'clear' || item.reasons.length);
  const quiet = preview.available && !preview.ranked.length && !attentionFlags.length;
  const section = el(quiet ? 'details' : 'section', 'cqa-review-impact');
  section.dataset.reviewImpact = preview.available ? 'ready' : 'unavailable';
  section.setAttribute('aria-label', 'Что проверить первым');
  if (quiet) {
    section.classList.add('cqa-review-impact--quiet');
    section.append(el('summary', '', 'Что проверить первым · нет сигналов'), el('p', 'cqa-review-impact__note', 'Нет открытых споров или сигналов неопределённости. Все критерии доступны ниже.'));
    return section;
  }
  section.append(el('h3', 'cqa-review-impact__title', 'Что проверить первым'),
    el('p', 'cqa-review-impact__note', 'Как изменится итог при пересмотре одного ответа. Сохранённый балл не меняется.'));
  if (!preview.available) section.append(el('p', 'cqa-review-impact__unavailable', `Расчёт недоступен. ${preview.reason}`));
  for (const item of attentionFlags) {
    const flag = el('div', 'cqa-review-impact__critical'); flag.dataset.flagState = item.state;
    const status = item.state === 'hit' ? 'Грубое нарушение' : item.state === 'clear' ? 'Нарушений нет' : 'Флаг не определён';
    flag.append(focusButton(item, onFocus, status), el('p', 'cqa-review-impact__reason', item.weight === 0
      ? 'Отдельный критический флаг · вес 0 · на числовой итог не влияет.'
      : 'Отдельный критический флаг · проверьте независимо от числового итога.'));
    section.append(flag);
  }
  if (preview.ranked.length) {
    section.append(el('p', 'cqa-review-impact__order', preview.available ? 'По максимальному изменению балла в любую сторону' : 'Требуют внимания · без ранжирования по влиянию'));
    const queue = el('ol', 'cqa-review-impact__list');
    for (const item of preview.ranked) {
      const row = el('li', 'cqa-review-impact__item');
      const impact = item.maxImpact == null ? 'Влияние недоступно' : item.maxImpact === 0 ? 'Без изменения балла' : `до ${fmt(item.maxImpact, preview.digits)} б.`;
      row.append(focusButton(item, onFocus, impact), el('p', 'cqa-review-impact__reason', `${item.reasons.join(' · ')} · сейчас ${answerText(item.currentAnswer)}`));
      if (item.scenarios.length) {
        const details = el('details', 'cqa-review-impact__scenarios');
        details.append(el('summary', '', 'Если изменить ответ'));
        const scenarios = el('ul');
        for (const scenario of item.scenarios) {
          const delta = scenario.delta === null ? '' : scenario.delta === 0 ? 'без изменения' : `${scenario.delta > 0 ? '+' : '−'}${fmt(Math.abs(scenario.delta), preview.digits)} б.`;
          const text = scenario.total === null ? 'итог недоступен: не останется применимых критериев' : `${fmt(preview.officialTotal, preview.digits)} → ${fmt(scenario.total, preview.digits)} / 100 (${delta})`;
          const line = el('li');
          line.append(el('strong', '', `${answerText(scenario.answer)}: `), el('span', '', text));
          if (scenario.denominatorChanged) line.append(el('span', 'cqa-review-impact__denominator', `Сумма применимых весов: ${fmt(preview.denominator, 6)} → ${fmt(scenario.denominator, 6)}. N/A исключается из знаменателя.`));
          scenarios.append(line);
        }
        details.append(scenarios); row.append(details);
      }
      queue.append(row);
    }
    section.append(queue);
  } else if (preview.available) section.append(el('p', 'cqa-review-impact__empty', 'Нет спорных ответов или сигналов неопределённости. Критерии доступны ниже.'));
  section.append(el('p', 'cqa-review-impact__note', 'Сценарии гипотетические, независимые; это не вероятность ошибки и не официальная оценка. Для пересмотра — «Оспорить» у критерия.'));
  return section;
}
