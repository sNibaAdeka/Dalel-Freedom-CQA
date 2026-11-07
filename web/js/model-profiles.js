/* Presentation of saved measurements; never a guarantee for a new dialogue. */
const finite = value => typeof value === 'number' && Number.isFinite(value);
const number = (value, digits = 1) => value.toLocaleString('ru-RU', { maximumFractionDigits: digits });

export function profileSettings(choice, config) {
  if (choice === 'configured') {
    if (typeof config?.profile !== 'string' || !config.profile) throw new Error('Текущий профиль сервера недоступен. Обновите настройки.');
    return { location: config.location === 'local' ? 'local' : 'cloud', profile: config.profile };
  }
  return choice === 'local' ? { location: 'local', profile: 'local' }
    : { location: 'cloud', profile: ['cascade', 'routing'].includes(choice) ? choice : 'cloud' };
}

export function profileChoice(config) {
  if (config?.profile && !['cloud', 'cascade', 'routing', 'local'].includes(config.profile)) return 'configured';
  return ['cascade', 'routing'].includes(config?.profile) ? config.profile : config?.location === 'local' ? 'local' : 'cloud';
}

export function modelHint(config) {
  const info = config?.model;
  if (config?.is_mock || config?.profile === 'mock') return 'Демонстрация: выбранный профиль не вызывает реальные модели.';
  if (!info?.model) return 'Модель определяется настройками сервера.';
  if (config.profile === 'routing') return `Звонки — ${info.escalate || 'сильная модель'} сразу; чаты — ${info.model}, затем перепроверка при сомнении или применимой идентификации (C3: Да/Нет).`;
  return info.escalate ? `Сначала ${info.model}, при сомнении ${info.escalate}.`
    : `Модель: ${info.model}${finite(info.votes) ? ` · ответов на критерий: ${info.votes}` : ''}`;
}

function labelled(report) {
  const counts = report?.counts;
  if (!counts || !['matched', 'total', 'unlabelled', 'missing', 'errors'].every(key => Number.isSafeInteger(counts[key]) && counts[key] >= 0)) return null;
  const total = counts.total - counts.unlabelled;
  return total > 0 && counts.matched <= total && !counts.missing && !counts.errors ? { matched: counts.matched, total } : null;
}

function comparable(a, b) {
  return a?.reference?.id && a.reference.id === b?.reference?.id && a.reference.type === b.reference.type
    && ['dataset', 'split', 'dialogues'].every(key => a.sample?.[key] != null && a.sample[key] === b.sample?.[key])
    && a.guidance_version === b.guidance_version && a.checklist_version === b.checklist_version && a.mode === b.mode;
}

export function profileGuide(data) {
  const catalog = data?.profile_catalog;
  if (!catalog?.cloud?.label || !catalog?.cascade?.first_label || !catalog?.cascade?.escalate_label || !catalog?.local?.label) return null;
  const reports = Array.isArray(data.reports) ? data.reports : [];
  const cloud = reports.find(report => report.id === catalog.cloud.report_id);
  const cascade = reports.find(report => report.id === catalog.cascade.report_id);
  const a = labelled(cloud), b = labelled(cascade), sameSample = comparable(cloud, cascade);
  let comparison = 'Сравнение стоимости и совпадений с эталоном недоступно.';
  const clauses = [];
  const costs = [cloud?.cost, cascade?.cost];
  if (sameSample && costs.every((cost, index) => finite(cost?.amount) && cost.amount > 0 && Number.isSafeInteger(cost.count)
      && cost.count === [cloud, cascade][index].sample.dialogues && cost.count > 0
      && [cloud, cascade][index].cost_audit?.pricing_complete === true
      && [cloud, cascade][index].cost_audit?.comparable === true
      && ['estimated', 'reported', 'measured'].includes(cost.basis))
      && costs[0].currency && costs[0].currency === costs[1].currency && costs[0].basis === costs[1].basis) {
    const ratio = (costs[0].amount / costs[0].count) / (costs[1].amount / costs[1].count);
    if (ratio > 1) clauses.push(`в ${number(ratio)} раза дешевле по ${costs.some(cost => ['estimated', 'reported'].includes(cost.basis)) ? 'расчётной' : 'измеренной'} стоимости`);
  }
  if (sameSample && a && b && a.matched === b.matched && a.total === b.total) {
    clauses.push(`одинаковое совпадение с ${cloud.reference.type === 'machine' ? 'машинным' : 'указанным'} эталоном на тех же ${number(cloud.sample.dialogues, 0)} диалогах ${cloud.sample.dataset}: ${a.matched}/${a.total} (${number(100 * a.matched / a.total)}%)`);
  }
  if (clauses.length) comparison = `Каскад: ${clauses.join('; ')}. Это сохранённый замер, не гарантия для новых диалогов.`;
  return {
    summary: `Облако — ${catalog.cloud.label} · Каскад — ${catalog.cascade.first_label}, при сомнении ${catalog.cascade.escalate_label}${catalog.routing ? ` · Маршрутизация — звонки сразу ${catalog.routing.escalate_label || catalog.cloud.label}; чаты ${catalog.routing.first_label || catalog.cascade.first_label}, перепроверка при сомнении или C3 Да/Нет` : ''} · Локально — ${catalog.local.label}: для контура банка, без отправки в облако.`,
    comparison,
    limitation: sameSample && cloud?.sample?.used_for_tuning
      ? 'Этот DEV-набор использовался для выбора решения; независимой проверки на новых данных здесь нет.' : '',
  };
}
