/** Small, DOM-free rules shared by the application shell and its tests. */
export function keyboardAction(event, demoIds = []) {
  if (event.defaultPrevented || event.altKey || event.ctrlKey || event.metaKey || event.repeat) return null;
  const key = typeof event.key === 'string' ? event.key.toLowerCase() : '';
  const code = typeof event.code === 'string' ? event.code : '';
  if (code === 'KeyP' || (!code && key === 'p')) return { type: 'projector' };
  if (code === 'Escape' || key === 'escape') return { type: 'dismiss' };
  const digit = /^Digit[1-5]$/.test(code) ? Number(code.slice(-1)) : !code && /^[1-5]$/.test(key) ? Number(key) : null;
  if (!digit) return null;
  const demo = Array.isArray(demoIds) && demoIds.length >= 2 && demoIds.slice(0, 2).every(id => typeof id === 'string' && id.length > 0 && id.length <= 200);
  if (demo && digit <= 2) return { type: 'conversation', id: demoIds[digit - 1] };
  return { type: 'tab', tab: demo ? ['dialogues', 'dialogues', 'review', 'editor', 'quality'][digit - 1] : ['dialogues', 'editor', 'review', 'board', 'quality'][digit - 1] };
}

/** A healthy HTTP server does not prove that the model answered a run. */
export function runFailureNotice(run) {
  if (!run || typeof run !== 'object') return null;
  const failed = new Set(['failed', 'error', 'incomplete']);
  const rows = Array.isArray(run.per_conv) ? run.per_conv : [];
  const count = rows.filter(row => failed.has(row.status) || Number(row.errors) > 0).length;
  const reportedErrors = Array.isArray(run.errors) ? run.errors.length : Number(run.errors);
  if (!count && !failed.has(run.status) && !(reportedErrors > 0)) return null;
  return {
    title: 'Не все оценки получены',
    description: count ? `Диалогов с ошибками или неполными ответами: ${count}. Откройте прогон для проверки и повторного расчёта.` : 'Последний прогон сообщил об ошибке. Откройте его, чтобы проверить причину и повторить расчёт.',
  };
}

export function modeCopy(config, health, unavailable = false) {
  if (config.mock !== false) return { mode: 'mock', title: 'Демонстрационный режим', description: 'Оценки и стоимость условные. Загруженные диалоги и изменения сохраняются до перезагрузки.' };
  if (config.offline) return { mode: 'offline', title: 'Офлайн', description: 'Новые запросы приостановлены. Показаны уже загруженные данные.' };
  if (unavailable) return { mode: 'unavailable', title: 'Сервер недоступен', description: 'Не удалось подключиться к сервису оценки. Проверьте запуск сервера и обновите страницу.' };
  if (!health) return { mode: 'connecting', title: 'Подключение к серверу', description: 'Проверяем соединение с сервисом оценки.' };
  if (health.provider === 'mock') return { mode: 'mock', title: 'Сервер · тестовый оценщик', description: 'Оценки получены в тестовом режиме и не отражают качество реальной модели.' };
  return { mode: 'live', title: 'Сервер подключён', description: health.evaluator === false || health.runs === false ? 'Данные доступны. Запуск оценки пока недоступен на сервере.' : `Результаты и решения сохраняются на сервере.${health.model ? ` Модель: ${health.model}.` : ''}` };
}
