/* Presentation and retry state only. Scoring and provider selection belong to the API. */
const finite = value => typeof value === 'number' && Number.isFinite(value);
const terminal = new Set(['complete', 'completed', 'done', 'finished', 'failed', 'error', 'incomplete', 'cancelled']);
const failureCopy = {
  payment: 'Провайдер отклонил запрос: недостаточно средств (402). Проверьте оплату перед повтором.',
  rate_limit: 'Провайдер ограничил число запросов (429). Подождите перед повтором.',
  timeout: 'Провайдер не ответил за отведённое время. Можно повторить оценку.',
  unavailable: 'Сервис модели недоступен. Можно повторить оценку позже.',
  provider: 'Провайдер сообщил об ошибке. Проверьте его состояние перед повтором.',
};

/** Only diagnostic fields are inspected; never model explanations, quotations or transcript text. */
export function providerFailure(evaluation = {}) {
  const structured = evaluation?.provider_error;
  const reasons = (Array.isArray(evaluation?.review_reasons) ? evaluation.review_reasons : [])
    .filter(reason => typeof reason === 'string' && /ошибка модели:|ошибка провайдера:|LLMError:|provider error:/i.test(reason));
  const explicit = structured && typeof structured === 'object';
  const diagnostic = [explicit ? `${structured.code || ''} ${structured.status || ''}` : '', ...reasons].join(' ');
  let kind = null;
  if (/\b402\b|insufficient[_ ](?:credits|funds)|payment_required/i.test(diagnostic)) kind = 'payment';
  else if (/\b429\b|rate[_ ]?limit|too many requests/i.test(diagnostic)) kind = 'rate_limit';
  else if (/timeout|timed out|тайм.?аут/i.test(diagnostic)) kind = 'timeout';
  else if (/\b50[234]\b|service unavailable|connectionerror|connecterror/i.test(diagnostic)) kind = 'unavailable';
  else if (explicit) kind = 'provider';
  // An unrecognized model failure could be JSON validation, not a provider outage.
  return kind ? { kind, message: failureCopy[kind] } : null;
}

export function answerState(criterion = {}, evaluation, line) {
  // An explicit final answer (including null) is authoritative after human review.
  const answer = line && Object.hasOwn(line, 'answer') ? line.answer : evaluation?.answer;
  const missing = answer == null || (typeof answer === 'string' && !answer.trim());
  const scale = Array.isArray(criterion.scale) ? criterion.scale : null;
  const valid = !missing && (answer === 'na' ? criterion.na_allowed !== false && !criterion.critical
    : scale ? finite(answer) && scale.includes(answer)
      : criterion.answer_type === 'binary' || criterion.critical ? answer === 'yes' || answer === 'no'
        : ['yes', 'no', 'partial'].includes(answer) || finite(answer));
  return { answer, missing: !valid, invalid: !missing && !valid, failure: !valid ? providerFailure(evaluation) : null };
}

export function dialogueAnswerState(detail = {}) {
  const evals = detail.evals || [], lines = detail.score?.lines || [];
  const criteria = detail.checklist?.criteria?.length ? detail.checklist.criteria
    : [...new Set([...evals.map(e => e.criterion_id), ...lines.map(l => l.criterion_id)])].map(id => ({ id }));
  const entries = criteria.map(criterion => ({ criterion_id: criterion.id, ...answerState(criterion,
    evals.find(e => e.criterion_id === criterion.id), lines.find(l => l.criterion_id === criterion.id)) }));
  const missing = entries.filter(entry => entry.missing);
  const notEvaluated = !evals.length && !lines.some(line => line.answer != null)
    && !['error', 'failed'].includes(detail.status);
  return { entries, notEvaluated, missingIds: missing.map(entry => entry.criterion_id), missingCount: missing.length,
    providerFailureCount: missing.filter(entry => entry.failure).length,
    incomplete: missing.length > 0 || detail.score?.status === 'incomplete' || !criteria.length };
}

/** Browser-local recovery stores only a run pointer and a hash of its exact input scope. */
export async function attemptScope(detail, settings, apiBase, baseURI, crypto = globalThis.crypto) {
  if (!detail?.id || !Number.isInteger(detail.checklist?.version) || !Array.isArray(detail.messages)
    || !['fast', 'accurate'].includes(settings?.mode) || typeof settings?.profile !== 'string' || !settings.profile
    || !crypto?.subtle) return null;
  try {
    const server = new URL(`${String(apiBase || './api').replace(/\/$/, '')}/`, baseURI);
    if (!['http:', 'https:'].includes(server.protocol) || server.username || server.password || server.search || server.hash) return null;
    const input = JSON.stringify([detail.id, detail.checklist.version, detail.checklist.criteria, settings.model || null, settings.prompt_version || null,
      detail.messages.map(message => [message.id, message.speaker, message.text, message.ts_raw])]);
    const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(input));
    return { server: server.href, version: detail.checklist.version, mode: settings.mode, profile: settings.profile,
      fingerprint: [...new Uint8Array(digest)].map(byte => byte.toString(16).padStart(2, '0')).join('') };
  } catch { return null; }
}

const scopeKey = scope => scope && /^https?:\/\//.test(scope.server || '') && Number.isInteger(scope.version)
  && ['fast', 'accurate'].includes(scope.mode) && typeof scope.profile === 'string' && scope.profile
  && /^[a-f0-9]{64}$/.test(scope.fingerprint || '')
  ? `cqa.card-attempt.v1:${JSON.stringify([scope.server, scope.version, scope.mode, scope.profile, scope.fingerprint])}` : null;
const runIdValid = id => typeof id === 'string' && /^[A-Za-z0-9_-]{1,120}$/.test(id);
export function createAttemptStore(storage) {
  return {
    read(scope) {
      const key = scopeKey(scope);
      if (!key) return null;
      try { const value = JSON.parse(storage?.getItem(key) || 'null'); return runIdValid(value?.run_id) ? value.run_id : null; }
      catch { return null; }
    },
    write(scope, runId) {
      const key = scopeKey(scope);
      if (!key || !runIdValid(runId)) return false;
      try { storage?.setItem(key, JSON.stringify({ run_id: runId })); return !!storage; } catch { return false; }
    },
    forget(scope, runId) {
      const key = scopeKey(scope);
      if (!key) return;
      // A late completion must not remove a newer pointer written by another tab.
      if (runId && this.read(scope) !== runId) return;
      try { storage?.removeItem(key); } catch { /* unavailable storage must not break evaluation */ }
    },
  };
}

/** A local pointer is never evidence until the exact server run confirms every scope field. */
export function matchesAttempt(run, context, runId) {
  const scope = context?.attemptScope;
  return !!scope && scope.version === context.version && run?.run_id === runId && run.version === context.version && run.profile === scope.profile
    && (run.mode === scope.mode || run.mode === `${scope.mode}:${scope.profile}`)
    && Array.isArray(run.per_conv) && run.per_conv.length === 1 && run.per_conv[0]?.id === context.conv_id
    && ['running', 'queued', 'pending', ...terminal].includes(run.status);
}

/** Full scores can predate a failed forced run; only explicit success or newer provenance resolves it. */
export function attemptResolved(detail, run) {
  if (!run || dialogueAnswerState(detail).incomplete) return false;
  if (['complete', 'completed', 'done', 'finished'].includes(run.status)) return true;
  if (!['failed', 'error', 'incomplete', 'cancelled'].includes(run.status)) return false;
  const finished = typeof run.finished_at === 'string' ? Date.parse(run.finished_at) : NaN;
  const criteria = detail.checklist?.criteria;
  if (!Number.isFinite(finished) || !Array.isArray(criteria) || !criteria.length) return false;
  return criteria.every(criterion => {
    const evaluation = detail.evals?.find(item => item.criterion_id === criterion.id);
    const created = typeof evaluation?.created_at === 'string' ? Date.parse(evaluation.created_at) : NaN;
    return Number.isFinite(created) && created > finished;
  });
}

/** One job per dialogue/version. Paused observation is resumable and never submits a second job. */
export function createDialogueRetry(api, { onChange = () => {}, onComplete = () => {}, onProgress = () => {},
  schedule = setTimeout, unschedule = clearTimeout, interval = 1000 } = {}) {
  const jobs = new Map();
  let destroyed = false;
  const key = context => JSON.stringify([context?.conv_id, context?.version, scopeKey(context?.attemptScope)]);
  const active = job => !destroyed && jobs.get(key(job.context)) === job;
  const update = (job, phase) => { job.phase = phase; if (active(job)) onChange(job); };
  const clear = job => { if (job.timer != null) unschedule(job.timer); job.timer = null; };
  const forget = job => { clear(job); job.run = null; update(job, 'forgotten'); };
  async function refresh(job) {
    if (!active(job)) return;
    update(job, 'refreshing');
    try {
      const detail = await api.conversation(job.context.conv_id, { version: job.context.version });
      if (!active(job)) return;
      if (detail?.id !== job.context.conv_id || detail?.checklist?.version !== job.context.version || !Array.isArray(detail.messages)) {
        throw new Error('Mismatched conversation/version');
      }
      job.detail = detail;
      update(job, 'finished');
      onComplete(detail, job);
    } catch { if (active(job)) update(job, 'refresh_failed'); }
  }
  async function accept(job, snapshot) {
    if (!active(job)) return;
    if (job.recovered && !matchesAttempt(snapshot, job.context, job.referenceId)) { forget(job); return; }
    if (!snapshot || typeof snapshot.run_id !== 'string' || !snapshot.run_id
      || (job.run && snapshot.run_id !== job.run.run_id)
      || (snapshot.version != null && snapshot.version !== job.context.version)) {
      update(job, job.run ? 'paused' : 'unknown'); return;
    }
    job.run = { ...job.run, ...snapshot };
    onProgress(job);
    if (terminal.has(job.run.status)) { clear(job); await refresh(job); }
    else {
      update(job, 'running');
      job.timer = schedule(() => { job.timer = null; void poll(job); }, interval);
    }
  }
  async function poll(job) {
    if (!active(job)) return;
    try { await accept(job, await api.run(job.run?.run_id || job.referenceId)); }
    catch (error) { if (active(job)) { if (job.recovered && error?.status === 404) forget(job); else update(job, 'paused'); } }
  }
  return {
    get: context => jobs.get(key(context)) || null,
    async recover(context, runId) {
      if (destroyed || !scopeKey(context?.attemptScope) || !runIdValid(runId) || jobs.has(key(context))) return;
      const job = { context: { ...context }, phase: 'checking', run: null, referenceId: runId, recovered: true, timer: null };
      jobs.set(key(context), job); update(job, 'checking');
      await poll(job);
    },
    async start(context) {
      if (destroyed || typeof context?.conv_id !== 'string' || !context.conv_id || !Number.isInteger(context.version) || context.version < 1) return;
      const old = jobs.get(key(context));
      if (old && !['finished', 'rejected', 'forgotten'].includes(old.phase)) return;
      const job = { context: { ...context }, phase: 'starting', run: null, timer: null };
      jobs.set(key(context), job); update(job, 'starting');
      const body = { conv_ids: [context.conv_id], version: context.version, live: true, force: true, activate: false, preview: false };
      if (['fast', 'accurate'].includes(context.mode)) body.mode = context.mode;
      if (context.profile) body.profile = context.profile;
      try { await accept(job, await api.createRun(body)); }
      catch (error) {
        // A lost POST response can still mean the server accepted the run. Do not duplicate it.
        if (active(job)) update(job, error?.status >= 400 && error.status < 500 ? 'rejected' : 'unknown');
      }
    },
    async resume(context) {
      const job = jobs.get(key(context));
      if (!active(job || {}) || !job) return;
      clear(job);
      if (job.phase === 'refresh_failed') await refresh(job);
      else if (job.phase === 'paused') { update(job, job.run ? 'running' : 'checking'); await poll(job); }
    },
    destroy() { destroyed = true; jobs.forEach(clear); jobs.clear(); },
  };
}
