import assert from 'node:assert/strict';
import test from 'node:test';
import { readFile } from 'node:fs/promises';

const source = await readFile(new URL('../web/js/provider-state.js', import.meta.url), 'utf8');
const { answerState, dialogueAnswerState, providerFailure, createDialogueRetry } = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
const c = { id: 'C1', answer_type: 'binary', na_allowed: true };
const detail = (id = 'A', version = 3) => ({ id, messages: [], checklist: { version, criteria: [c] }, evals: [{ criterion_id: 'C1', answer: 'yes' }], score: { total: 100 } });
const deferred = () => { let resolve, reject; const promise = new Promise((r, j) => { resolve = r; reject = j; }); return { promise, resolve, reject }; };
const tick = () => new Promise(resolve => setImmediate(resolve));

test('missing, blank and invalid answers cannot become N/A, zero or successful totals', () => {
  for (const answer of [null, undefined, '', ' ', 'unknown', false, {}, NaN]) {
    assert.equal(answerState(c, { answer }).missing, true);
    const result = dialogueAnswerState({ ...detail(), evals: [{ criterion_id: 'C1', answer }], score: { total: 100 } });
    assert.equal(result.incomplete, true); assert.equal(result.missingCount, 1);
  }
  assert.equal(answerState(c, { answer: 'na' }).missing, false);
  assert.equal(answerState({ ...c, na_allowed: false }, { answer: 'na' }).missing, true);
  assert.equal(answerState({ id: 'C4', answer_type: 'scale', scale: [0, 5, 10] }, { answer: 0 }).missing, false);
  assert.equal(answerState({ id: 'C4', answer_type: 'scale', scale: [0, 5, 10] }, { answer: 7 }).missing, true);
});

test('count omitted and critical criteria; final human answer wins, explicit empty final answer does not fall back', () => {
  const d = detail(); d.checklist.criteria.push({ id: 'C8', critical: true, answer_type: 'binary' });
  assert.deepEqual(dialogueAnswerState(d).missingIds, ['C8']);
  assert.equal(answerState(c, { answer: null }, { answer: 'yes' }).missing, false);
  assert.equal(answerState(c, { answer: 'yes' }, { answer: null }).missing, true);
  assert.equal(dialogueAnswerState({ ...detail(), score: { total: 100, status: 'incomplete' } }).incomplete, true);
});

test('provider classification requires diagnostic evidence, never inference from absence or conversation prose', () => {
  assert.equal(providerFailure({ answer: null, needs_review: true }), null);
  assert.equal(providerFailure({ explanation: 'HTTP 402 timeout', review_reasons: ['низкая уверенность модели', 'код 429 упомянут в диалоге'] }), null);
  assert.equal(providerFailure({ review_reasons: ['оценка не получена: ответ модели не прошёл проверку'] }), null);
  assert.equal(providerFailure({ review_reasons: ['оценка не получена: ошибка модели: APIStatusError HTTP 402 Insufficient credits'] }).kind, 'payment');
  assert.equal(providerFailure({ review_reasons: ['оценка не получена: ошибка модели: RateLimitError: Error code: 429'] }).kind, 'rate_limit');
  assert.equal(providerFailure({ review_reasons: ['оценка не получена: ошибка модели: APITimeoutError Request timed out'] }).kind, 'timeout');
  assert.equal(providerFailure({ provider_error: { code: 'rate_limit', status: 429 } }).kind, 'rate_limit');
  assert.equal(providerFailure({ review_reasons: ['оценка не получена: нет ответа'] }), null);
});

test('retry is fresh, full-dialogue, version-pinned, double-click safe and refetches before completion', async () => {
  const start = deferred(), calls = [], completed = [];
  const retry = createDialogueRetry({
    createRun: body => { calls.push(body); return start.promise; },
    conversation: async (...args) => { calls.push(args); return detail(); },
  }, { onComplete: (...args) => completed.push(args) });
  const context = { conv_id: 'A', version: 3, mode: 'accurate', profile: 'cloud' };
  const first = retry.start(context); await retry.start(context);
  assert.equal(calls.length, 1);
  assert.deepEqual(calls[0], { conv_ids: ['A'], version: 3, mode: 'accurate', profile: 'cloud', live: true, force: true, activate: false, preview: false });
  start.resolve({ run_id: 'r1', version: 3, status: 'completed' }); await first;
  assert.deepEqual(calls[1], ['A', { version: 3 }]);
  assert.equal(completed.length, 1); assert.equal(retry.get(context).phase, 'finished');
  retry.destroy();
});

test('poll failure resumes observation of the same run, without another POST', async () => {
  let scheduled, posts = 0, polls = 0, completes = 0;
  const retry = createDialogueRetry({
    createRun: async () => { posts++; return { run_id: 'r2', version: 3, status: 'running' }; },
    run: async () => { if (++polls === 1) throw new Error('network'); return { run_id: 'r2', version: 3, status: 'failed' }; },
    conversation: async () => detail(),
  }, { schedule: fn => { scheduled = fn; return 1; }, unschedule() {}, onComplete: () => completes++ });
  const ctx = { conv_id: 'A', version: 3 };
  await retry.start(ctx); scheduled(); await tick();
  assert.equal(retry.get(ctx).phase, 'paused');
  await retry.start(ctx); assert.equal(posts, 1);
  await retry.resume(ctx); assert.equal(posts, 1); assert.equal(polls, 2); assert.equal(completes, 1);
  retry.destroy();
});

test('a terminal incomplete run refreshes missing answers instead of polling forever', async () => {
  let reads = 0, schedules = 0;
  const retry = createDialogueRetry({
    createRun: async () => ({ run_id: 'incomplete-run', status: 'incomplete' }),
    conversation: async () => { reads++; return { ...detail(), evals: [], score: { total: null, status: 'incomplete' } }; },
  }, { schedule: () => { schedules++; return 1; } });
  const ctx = { conv_id: 'A', version: 3 };
  await retry.start(ctx);
  assert.equal(reads, 1); assert.equal(schedules, 0); assert.equal(retry.get(ctx).phase, 'finished');
  retry.destroy();
});

test('a failed refetch retries only the read; wrong versions and destroyed callbacks are rejected', async () => {
  let posts = 0, reads = 0, completes = 0;
  const retry = createDialogueRetry({
    createRun: async () => { posts++; return { run_id: 'r3', version: 3, status: 'complete' }; },
    conversation: async () => { reads++; return detail('A', reads === 1 ? 4 : 3); },
  }, { onComplete: () => completes++ });
  const ctx = { conv_id: 'A', version: 3 };
  await retry.start(ctx); assert.equal(retry.get(ctx).phase, 'refresh_failed'); assert.equal(completes, 0);
  await retry.resume(ctx); assert.equal(posts, 1); assert.equal(reads, 2); assert.equal(completes, 1);
  const pending = deferred();
  const gone = createDialogueRetry({ createRun: () => pending.promise, conversation: async () => { throw new Error('must not read'); } }, { onComplete: () => completes++ });
  const work = gone.start(ctx); gone.destroy(); pending.resolve({ run_id: 'r4', status: 'completed' }); await work;
  assert.equal(completes, 1); retry.destroy();
});

test('unknown POST outcome stays locked; explicit rejection can retry; settings and IDs are validated', async () => {
  let posts = 0;
  const ctx = { conv_id: 'A', version: 3 };
  const retry = createDialogueRetry({ createRun: async () => { posts++; throw new Error('connection lost'); } });
  await retry.start(ctx); await retry.start(ctx); assert.equal(posts, 1); assert.equal(retry.get(ctx).phase, 'unknown'); retry.destroy();
  const rejected = createDialogueRetry({ createRun: async () => { posts++; throw Object.assign(new Error('bad request'), { status: 422 }); } });
  await rejected.start(ctx); assert.equal(rejected.get(ctx).phase, 'rejected');
  await rejected.start(ctx); assert.equal(posts, 3);
  await rejected.start({ ...ctx, version: null }); assert.equal(posts, 3); rejected.destroy();
});


test('untouched dialogues are neutral while attempted missing answers remain failures', () => {
  const empty = {...detail(),evals:[],score:{total:null,status:'incomplete',lines:[{criterion_id:'C1',answer:null}]}};
  assert.equal(dialogueAnswerState(empty).notEvaluated,true);
  assert.equal(dialogueAnswerState({...empty,evals:[{criterion_id:'C1',answer:null}]}).notEvaluated,false);
  assert.equal(dialogueAnswerState({...empty,status:'error'}).notEvaluated,false);
  assert.equal(dialogueAnswerState({...empty,score:{lines:[{criterion_id:'C1',answer:'no'}]}}).notEvaluated,false);
});
