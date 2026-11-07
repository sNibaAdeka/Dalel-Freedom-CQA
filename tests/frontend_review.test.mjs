// Run: node --test tests/frontend_review.test.mjs
// T24 review module: answer rules by criterion type, request bodies, scoring (checklist v1), «last decision wins»,
// journal, stale async responses and the save → events flow. Synthetic data only.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const load = async path => import(`data:text/javascript;base64,${Buffer.from(await readFile(new URL(path, import.meta.url), 'utf8')).toString('base64')}`);
const review = await load('../web/js/review.js');
const { createApi } = await load('../web/js/api.js');

// Organizer checklist v1 (configuration, not client data): weights, scales, N/A rules, C8 flag.
const V1 = {
  version: 1, rounding: { total: 2, mode: 'half_up' },
  criteria: [
    { id: 'C1', title: 'Установление контакта', answer_type: 'binary', weight: 10, max_points: 10, scale: null, na_allowed: false, critical: false },
    { id: 'C2', title: 'Выявление потребности', answer_type: 'binary', weight: 15, max_points: 15, scale: null, na_allowed: true, critical: false },
    { id: 'C3', title: 'Идентификация', answer_type: 'binary', weight: 20, max_points: 20, scale: null, na_allowed: true, critical: false },
    { id: 'C4', title: 'Корректная и полная консультация', answer_type: 'scale', weight: 20, max_points: 20, scale: [0, 5, 10, 15, 20], na_allowed: true, critical: false },
    { id: 'C5', title: 'Эмпатия и клиентоориентированность', answer_type: 'binary', weight: 15, max_points: 15, scale: null, na_allowed: false, critical: false },
    { id: 'C6', title: 'Управление диалогом', answer_type: 'scale', weight: 15, max_points: 15, scale: [0, 4, 7, 10], na_allowed: false, critical: false },
    { id: 'C7', title: 'Завершение', answer_type: 'binary', weight: 5, max_points: 5, scale: null, na_allowed: true, critical: false },
    { id: 'C8', title: 'Грубые ошибки', answer_type: 'binary', weight: 0, max_points: 0, scale: null, na_allowed: false, critical: true },
  ],
};
const C = id => V1.criteria.find(item => item.id === id);
const PDF = { C1: 'yes', C2: 'na', C3: 'na', C4: 15, C5: 'yes', C6: 7, C7: 'yes', C8: 'no' };

function makeDetail({ answers = PDF, total = 85.38, appeals = [], decisions = [], audit = [] } = {}) {
  const messages = ['Здравствуйте! Чем помочь?', 'Вопрос про вывод.', 'Здравствуйте! Меня зовут Тест.', 'Вывод занимает один день.', 'Спасибо.', 'Всего доброго!']
    .map((text, idx) => ({ id: `t:${idx}`, idx, speaker: ['bot', 'client', 'operator', 'operator', 'client', 'operator'][idx], text, ts_raw: `00:0${idx}`, t_sec: idx }));
  return {
    id: 't', channel: 'chat', language: 'RU', messages, checklist: structuredClone(V1),
    evals: V1.criteria.map(item => ({ criterion_id: item.id, answer: answers[item.id], evidence: item.id === 'C4' ? [{ message_id: 't:3', quote: 'один день', start: 15, end: 24 }] : [], explanation: 'синтетика', critical_hit: item.id === 'C8' && answers.C8 === 'yes' })),
    score: { total, critical: answers.C8 === 'yes' }, appeals, decisions, audit,
  };
}
const appealC4 = { id: 'a1', conv_id: 't', criterion_id: 'C4', comment: 'Срок назван', message_ids: ['t:3'], status: 'decided', created_at: '2026-11-07T10:00:00Z' };
const changeC4 = { id: 'd1', appeal_id: 'a1', action: 'change', old_answer: 15, new_answer: 20, reason: 'Реплика закрывает вопрос', created_at: '2026-11-07T10:05:00Z' };
function makeBus() {
  const events = [];
  return { events, emit: (name, payload) => events.push([name, payload]), on: () => () => {} };
}
const deferred = () => { let resolve, reject; const promise = new Promise((ok, fail) => { resolve = ok; reject = fail; }); return { promise, resolve, reject }; };

test('dossier keeps every backend quote and its exact UTF-16 boundaries', () => {
  const dossier = review.normalizeDossier({ arguments_for: [{ text: 'Два основания', message_ids: ['s', 'o'], quotes: [
    {message_id:'s',quote:'Чат закрыт',start:0,end:10,valid:true},
    {message_id:'o',quote:'До свидания',start:3,end:14,valid:true},
  ]}], arguments_against: [{text:'Старый формат',message_id:'c',quote:'не помогли',start:0,end:10}] });
  assert.equal(dossier.pro[0].quotes.length,2);
  assert.deepEqual(dossier.pro[0].quotes[1],{message_id:'o',quote:'До свидания',start:3,end:14});
  assert.equal(dossier.contra[0].quotes[0].message_id,'c');
  assert.equal(review.checkQuote(dossier.pro[0].quotes[0],[{id:'s',speaker:'system',text:'Чат закрыт'}]).valid,true);
  assert.equal(review.checkQuote(dossier.contra[0].quotes[0],[{id:'c',speaker:'client',text:'Вы мне помогли'}]).valid,false);
});

test('appeal case loads its pinned version and refuses a response from another checklist', async () => {
  const calls=[],seen=[],errors=[];
  let returnedVersion=1;
  const loader=review.createCaseLoader({conversation:async (id,options)=>{calls.push(options);return {id,checklist:{version:returnedVersion}};},dossier:async()=>({})},
    {onDetail:(_,detail)=>seen.push(detail.checklist.version),onError:(_,error)=>errors.push(error.message)});
  assert.equal(await loader.load({id:'a',conv_id:'t',checklist_version:1}),'done');
  assert.deepEqual(calls[0],{version:1});assert.deepEqual(seen,[1]);
  returnedVersion=2;
  assert.equal(await loader.load({id:'a',conv_id:'t',checklist_version:1}),'error');
  assert.deepEqual(seen,[1]);assert.match(errors[0],/версию чек-листа/);
});

test('appeal assessment keeps its immutable answer, points and evidence after same-wording reruns or profile switches', () => {
  const detail = makeDetail();
  const criterion = detail.checklist.criteria.find(c => c.id === 'C4'); criterion.hash = 'same-wording';
  const snapshot = { ...structuredClone(detail.evals.find(e => e.criterion_id === 'C4')), criterion_hash: criterion.hash, answer: 15 };
  const appeal = { ...appealC4, criterion_hash: criterion.hash, checklist_version: 1, evaluation: snapshot };
  const latest = detail.evals.find(e => e.criterion_id === 'C4');
  Object.assign(latest, { criterion_hash: criterion.hash, answer: 20, explanation: 'new run', evidence: [{ message_id: 't:5', quote: 'Всего доброго!', start: 0, end: 13 }] });
  const before = structuredClone({detail,appeal});
  const original = review.appealAssessment(detail, appeal);
  assert.equal(original.answer,15); assert.equal(original.line.points,15);
  assert.equal(original.evaluation.explanation,'синтетика');
  assert.deepEqual(original.evaluation.evidence,snapshot.evidence);
  assert.equal(review.aiAnswerFor(detail,'C4'),20);
  assert.equal(review.finalAnswerFor(detail,'C4'),20,'Current decision context remains current');
  assert.equal(review.appealAssessment({...detail,evals:[]},appeal).answer,15,'Snapshot survives profile-filtered empty evals');
  assert.equal(review.appealAssessment(detail,{...appeal,evaluation:{...snapshot,criterion_hash:'wrong'}}).evaluation,null);
  assert.deepEqual({detail,appeal},before,'Neither current evaluation nor original snapshot is mutated');
});

test('appeal request carries the explicitly selected checklist version', () => {
  const built = review.buildAppealBody({conv_id:'t',criterion_id:'C4',comment:'Synthetic',checklist_version:2});
  assert.equal(built.body.checklist_version,2);
  assert.equal(review.buildAppealBody({conv_id:'t',criterion_id:'C4',comment:'Synthetic',checklist_version:0}).ok,false);
});

test('confirm uses the pinned appeal answer after reruns, preserves numeric zero and falls back for legacy appeals', () => {
  const detail = makeDetail({ answers: { ...PDF, C4: 20 } });
  const appeal = { ...appealC4, current_answer: 15, original_answer: 10 };
  assert.equal(review.appealConfirmAnswer(detail, appeal), 15);
  assert.equal(review.appealConfirmAnswer(detail, { ...appeal, current_answer: 0 }), 0);
  assert.equal(review.appealConfirmAnswer(detail, { ...appeal, current_answer: null, original_answer: 0 }), 0);
  assert.equal(review.appealConfirmAnswer(detail, appealC4), 20);
  assert.equal(review.previewDecision(detail, 'C4', review.appealConfirmAnswer(detail, appeal)).to, 85.38);
});

test('answer options: binary is yes/no, scale is only its steps, N/A only where the checklist allows it', () => {
  assert.deepEqual(review.answerOptions(C('C1')), ['yes', 'no']);
  assert.deepEqual(review.answerOptions(C('C2')), ['yes', 'no', 'na']);
  assert.deepEqual(review.answerOptions(C('C4')), [0, 5, 10, 15, 20, 'na']);
  assert.deepEqual(review.answerOptions(C('C6')), [0, 4, 7, 10]);
  assert.deepEqual(review.answerOptions(C('C8')), ['yes', 'no']);
  assert.equal(review.answerLabel(C('C8'), 'yes'), 'Да — нарушение есть', 'C8 has inverted polarity');
  assert.equal(review.answerLabel(C('C6'), 7), '7 из 10');
});

test('validateAnswer: binary accepts only yes/no; N/A forbidden on C1, allowed on C2', () => {
  assert.equal(review.validateAnswer(C('C1'), 'yes').value, 'yes');
  assert.equal(review.validateAnswer(C('C1'), 'partial').ok, false);
  assert.equal(review.validateAnswer(C('C1'), 10).ok, false);
  assert.match(review.validateAnswer(C('C1'), 'na').error, /N\/A/);
  assert.equal(review.validateAnswer(C('C2'), 'na').value, 'na');
  assert.equal(review.validateAnswer(C('C2'), '').ok, false);
});

test('validateAnswer: scale accepts only steps of its own scale (form strings are parsed)', () => {
  assert.equal(review.validateAnswer(C('C6'), '7').value, 7);
  assert.equal(review.validateAnswer(C('C6'), 6).ok, false);
  assert.equal(review.validateAnswer(C('C6'), 15).ok, false, 'C4 step is not a C6 step');
  assert.equal(review.validateAnswer(C('C6'), 'yes').ok, false);
  assert.equal(review.validateAnswer(C('C6'), 'na').ok, false, 'C6 has no N/A');
  assert.equal(review.validateAnswer(C('C4'), 'na').value, 'na');
  assert.equal(review.validateAnswer(C('C4'), '20').value, 20);
});

test('validateAnswer: numeric must be a number inside the allowed range and step', () => {
  const numeric = { id: 'N1', answer_type: 'numeric', min: 0, max: 10, step: 0.5, na_allowed: false, weight: 10, max_points: 10 };
  assert.equal(review.validateAnswer(numeric, '7,5').value, 7.5);
  assert.equal(review.validateAnswer(numeric, 10).value, 10);
  for (const bad of [10.5, -1, 7.3, 'abc', 'na']) assert.equal(review.validateAnswer(numeric, bad).ok, false, String(bad));
  const fromMaxPoints = { id: 'N2', answer_type: 'numeric', na_allowed: true, weight: 5, max_points: 5 };
  assert.deepEqual(review.numericRange(fromMaxPoints), { min: 0, max: 5, step: null });
  assert.equal(review.validateAnswer(fromMaxPoints, 6).ok, false);
  assert.equal(review.validateAnswer(fromMaxPoints, 'na').value, 'na');
});

test('buildDecisionBody: reason is required for both actions; confirm sends no new_answer', () => {
  assert.ok(review.buildDecisionBody({ appeal_id: 'a1', action: 'confirm', reason: '' }, C('C4'), 15).errors.reason);
  assert.ok(review.buildDecisionBody({ appeal_id: 'a1', action: 'change', answer: '20', reason: '   ' }, C('C4'), 15).errors.reason);
  assert.ok(review.buildDecisionBody({ appeal_id: 'a1', reason: 'есть' }, C('C4'), 15).errors.action);
  const confirm = review.buildDecisionBody({ appeal_id: 'a1', action: 'confirm', reason: '  Оценка верна  ' }, C('C4'), 15);
  assert.deepEqual(confirm.body, { appeal_id: 'a1', action: 'confirm', reason: 'Оценка верна' });
  assert.equal(Object.hasOwn(confirm.body, 'new_answer'), false);
});

test('buildDecisionBody: change sends a typed answer and rejects the current or an invalid one', () => {
  assert.deepEqual(review.buildDecisionBody({ appeal_id: 'a1', action: 'change', answer: '20', reason: 'Реплика #4' }, C('C4'), 15).body, { appeal_id: 'a1', action: 'change', reason: 'Реплика #4', new_answer: 20 });
  assert.match(review.buildDecisionBody({ appeal_id: 'a1', action: 'change', answer: '15', reason: 'x' }, C('C4'), 15).errors.answer, /совпадает/);
  assert.ok(review.buildDecisionBody({ appeal_id: 'a1', action: 'change', answer: 'na', reason: 'x' }, C('C6'), 7).errors.answer);
  assert.ok(review.buildDecisionBody({ appeal_id: 'a1', action: 'change', answer: null, reason: 'x' }, C('C1'), 'yes').errors.answer);
  assert.equal(review.buildDecisionBody({ appeal_id: 'a1', action: 'change', answer: 'yes', reason: 'x' }, C('C8'), 'no').body.new_answer, 'yes');
});

test('buildAppealBody: comment required, message IDs unique, known and in transcript order', () => {
  const messages = makeDetail().messages;
  assert.ok(review.buildAppealBody({ conv_id: 't', criterion_id: 'C4', comment: '   ', message_ids: [] }, messages).errors.comment);
  assert.ok(review.buildAppealBody({ conv_id: 't', criterion_id: '', comment: 'текст' }, messages).errors.criterion_id);
  const built = review.buildAppealBody({ conv_id: 't', criterion_id: 'C4', comment: '  Срок назван в #4 ', message_ids: ['t:5', 't:2', 't:5', 'x:9'] }, messages);
  assert.deepEqual(built.body, { conv_id: 't', criterion_id: 'C4', comment: 'Срок назван в #4', message_ids: ['t:2', 't:5'] });
});

test('scoreAnswers: PDF example 85.38, C8 is only a flag, no applicable criteria → null', () => {
  const pdf = review.scoreAnswers(V1, PDF);
  assert.equal(pdf.total, 85.38);
  assert.equal(pdf.applicable_weight, 65);
  assert.equal(pdf.lines.find(line => line.criterion_id === 'C6').points, 10.5);
  assert.ok(Math.abs(pdf.lines.reduce((sum, line) => sum + line.weight_effective, 0) - 100) < 1e-9);
  assert.equal(review.scoreAnswers(V1, { ...PDF, C8: 'yes' }).total, 85.38, 'C8 never changes the total');
  assert.equal(review.scoreAnswers(V1, { ...PDF, C4: 20 }).total, 93.08);
  assert.equal(review.scoreAnswers(V1, Object.fromEntries(V1.criteria.map(item => [item.id, 'na']))).total, null);
  const missing = review.scoreAnswers(V1, { ...PDF, C5: undefined });
  assert.deepEqual([missing.total, missing.incomplete], [null, true], 'missing weighted answer → no total (engine)');
  assert.equal(review.scoreAnswers(V1, { ...PDF, C8: undefined }).total, 85.38, 'missing C8 (flag only) does not block the total');
  assert.equal(review.roundHalfUp(85.385), 85.39);
  assert.equal(review.roundHalfUp(1.005), 1.01);
  assert.equal(review.roundHalfUp(0.125), 0.13);
  assert.equal(review.roundHalfUp(58.333, 0), 58);
});

test('final answer: the last decision wins (engine order), confirm keeps old_answer, the AI evaluation is not mutated', () => {
  const earlier = { ...changeC4, id: 'd0', new_answer: 10, created_at: '2026-11-07T10:01:00Z' };
  const confirm = { id: 'd2', appeal_id: 'a2', action: 'confirm', new_answer: null, old_answer: 20, reason: 'ok', created_at: '2026-11-07T10:09:00Z' };
  const detail = makeDetail({ appeals: [appealC4, { ...appealC4, id: 'a2' }], decisions: [changeC4, earlier, confirm] });
  const before = structuredClone(detail);
  assert.equal(review.aiAnswerFor(detail, 'C4'), 15);
  assert.equal(review.finalAnswerFor(detail, 'C4'), 20, 'd1 (10:05) is later than d0 (10:01) despite array order; confirm takes old_answer');
  assert.equal(review.finalAnswerFor(detail, 'C6'), 7, 'no decision → AI answer');
  assert.deepEqual(detail, before, 'helpers never mutate the conversation');
  assert.equal(review.decisionsByCriterion(detail).get('C4').map(item => item.id).join(','), 'd0,d1,d2');
  const bareConfirm = makeDetail({ appeals: [appealC4, { ...appealC4, id: 'a2' }], decisions: [changeC4, { ...confirm, old_answer: undefined }] });
  assert.equal(review.finalAnswerFor(bareConfirm, 'C4'), 15, 'engine rule: confirm without new_answer and old_answer falls back to the AI answer');
});

test('outdated wording: decisions and evaluations for another criterion_hash do not apply', () => {
  const hashed = makeDetail({ appeals: [appealC4], decisions: [{ ...changeC4, criterion_hash: 'old-wording' }] });
  hashed.checklist.criteria.find(item => item.id === 'C4').hash = 'new-wording';
  assert.equal(review.finalAnswerFor(hashed, 'C4'), 15, 'decision for the old wording is ignored');
  assert.equal(review.decisionsByCriterion(hashed).get('C4')[0].stale, true);
  assert.equal(review.buildHistory({ ...hashed, score: { total: 85.38 } }).decided, false);
  hashed.evals.find(item => item.criterion_id === 'C4').criterion_hash = 'old-wording';
  assert.equal(review.aiAnswerFor(hashed, 'C4'), null, 'evaluation of the old wording counts as missing');
  assert.equal(review.buildHistory(hashed).aiScore.total, null, 'a weighted criterion without an answer → no total');
});

test('three layers: AI total from untouched answers, final total from the API, formula check', () => {
  const detail = makeDetail({ total: 93.08, appeals: [appealC4], decisions: [changeC4] });
  const history = review.buildHistory(detail);
  assert.equal(history.aiTotal, 85.38);
  assert.equal(history.finalTotal, 93.08);
  assert.equal(history.verified, true);
  assert.equal(history.disputed.length, 1);
  assert.equal(history.disputed[0].ai, 15);
  assert.equal(history.disputed[0].final, 20);
  assert.equal(history.disputed[0].open, false);
  assert.equal(review.buildHistory(makeDetail({ total: 90, appeals: [appealC4], decisions: [changeC4] })).verified, false, 'a server total that disagrees with the formula is flagged');
  const open = review.buildHistory(makeDetail({ appeals: [{ ...appealC4, status: 'open' }] }));
  assert.equal(open.aiTotal, 85.38);
  assert.equal(open.finalTotal, 85.38);
  assert.equal(open.disputed[0].open, true);
  assert.deepEqual(review.previewDecision(makeDetail(), 'C4', 20), { from: 85.38, to: 93.08 });
});

test('journal: AI evaluation → appeal → decision → recalculation, audit duplicates merged', () => {
  const audit = [
    { id: 1, entity: 'conversation', entity_id: 't', event: 'eval.completed', payload: { total: 85.38, model: 'demo' }, ts: '2026-11-07T09:00:00Z' },
    { id: 2, entity: 'appeal', entity_id: 'a1', event: 'appeal.created', payload: {}, ts: '2026-11-07T10:00:00Z' },
    { id: 3, entity: 'decision', entity_id: 'd1', event: 'decision.saved', payload: {}, ts: '2026-11-07T10:05:00Z' },
    { id: 4, entity: 'conversation', entity_id: 't', event: 'score.recalculated', payload: { decision_id: 'd1', old_total: 85.38, new_total: 93.08 }, ts: '2026-11-07T10:05:00Z' },
    { id: 5, entity: 'checklist', entity_id: 'v2', event: 'checklist.activated', payload: {}, ts: '2026-11-07T11:00:00Z' },
  ];
  const journal = review.buildJournal(makeDetail({ total: 93.08, appeals: [appealC4], decisions: [changeC4], audit }));
  assert.deepEqual(journal.map(item => item.kind), ['ai', 'appeal', 'decision', 'recalc', 'other']);
  assert.match(journal[0].text, /85,38/);
  assert.match(journal[2].text, /15 из 20 → 20 из 20/);
  assert.match(journal[2].text, /Реплика закрывает вопрос/);
  assert.equal(journal[3].text, 'итог 85,38 → 93,08');
  // Wave-1 mock: no audit for AI or appeal — the journal still has the four steps, recalculation from the formula.
  const bare = review.buildJournal(makeDetail({ total: 93.08, appeals: [appealC4], decisions: [changeC4] }), { aiTotal: 85.38 });
  assert.deepEqual(bare.map(item => item.kind), ['ai', 'appeal', 'decision', 'recalc']);
  assert.equal(bare[3].text, 'итог 85,38 → 93,08');
  assert.equal(review.classifyAuditEvent({ entity: 'appeal', event: 'dossier.ready' }), 'dossier');
});

test('stale responses: a slow answer for a previous appeal never reaches the screen', async () => {
  const pending = { A: deferred(), B: deferred(), dA: deferred(), dB: deferred() };
  const api = { conversation: id => pending[id].promise, dossier: id => pending[`d${id}`].promise };
  const seen = [];
  const loader = review.createCaseLoader(api, { onDetail: appeal => seen.push(`detail:${appeal.id}`), onDossier: appeal => seen.push(`dossier:${appeal.id}`), onError: appeal => seen.push(`error:${appeal.id}`) });
  const first = loader.load({ id: 'A', conv_id: 'A' });
  const second = loader.load({ id: 'B', conv_id: 'B' });
  pending.B.resolve({ id: 'B' });
  pending.dB.resolve({ arguments_for: [] });
  assert.equal(await second, 'done');
  pending.A.resolve({ id: 'A' });
  pending.dA.reject(new Error('late failure'));
  assert.equal(await first, 'stale');
  assert.deepEqual(seen, ['detail:B', 'dossier:B']);
  const guard = review.createLatest();
  const token = guard.next();
  guard.invalidate();
  assert.equal(guard.isCurrent(token), false);
});

test('submitAppeal: emits appeal:created only after the API confirms; a failed write is an error, not success', async () => {
  const bus = makeBus();
  const body = { conv_id: 't', criterion_id: 'C4', comment: 'Срок назван', message_ids: ['t:3'] };
  const saved = await review.submitAppeal({ api: { createAppeal: async value => ({ ...value, id: 'a9', status: 'open' }) }, bus }, body);
  assert.equal(saved.id, 'a9');
  assert.deepEqual(bus.events.map(([name]) => name), ['appeal:created']);
  const failing = makeBus();
  await assert.rejects(review.submitAppeal({ api: { createAppeal: async () => { throw new Error('Сервер вернул ошибку 503.'); } }, bus: failing }, body), /503/);
  await assert.rejects(review.submitAppeal({ api: { createAppeal: async () => ({}) }, bus: failing }, body), /не подтвердил/);
  assert.equal(failing.events.length, 0);
});

test('submitDecision: X → Y from the API response, then decision:saved and score:updated with the fresh detail', async () => {
  const before = makeDetail({ appeals: [{ ...appealC4, status: 'open' }] });
  const fresh = makeDetail({ total: 93.08, appeals: [appealC4], decisions: [changeC4] });
  const calls = [];
  const api = { createDecision: async body => { calls.push(body); return { old_total: 85.38, new_total: 93.08, decision: { ...changeC4 }, score: fresh.score }; }, conversation: async () => structuredClone(fresh) };
  const bus = makeBus();
  const body = review.buildDecisionBody({ appeal_id: 'a1', action: 'change', answer: '20', reason: 'Реплика закрывает вопрос' }, C('C4'), 15).body;
  const outcome = await review.submitDecision({ api, bus }, body, { conv_id: 't', criterion_id: 'C4', before });
  assert.deepEqual(calls, [{ appeal_id: 'a1', action: 'change', reason: 'Реплика закрывает вопрос', new_answer: 20 }]);
  assert.deepEqual([outcome.from, outcome.to, outcome.source], [85.38, 93.08, 'api']);
  assert.deepEqual(bus.events.map(([name]) => name), ['decision:saved', 'score:updated']);
  assert.equal(bus.events[0][1].id, 'd1');
  assert.equal(bus.events[0][1].new_total, 93.08);
  assert.equal(bus.events[1][1].id, 't');
  assert.ok(Array.isArray(bus.events[1][1].messages), 'full detail lets the card re-render appeals and decisions');
});

test('submitDecision: without totals in the response the numbers are recalculated, failures emit nothing', async () => {
  const before = makeDetail({ appeals: [{ ...appealC4, status: 'open' }] });
  const bus = makeBus();
  const api = { createDecision: async () => ({ decision: { ...changeC4 } }), conversation: async () => { throw new Error('offline'); } };
  const outcome = await review.submitDecision({ api, bus }, { appeal_id: 'a1', action: 'change', new_answer: 20, reason: 'x' }, { conv_id: 't', criterion_id: 'C4', before });
  assert.deepEqual([outcome.from, outcome.to, outcome.source], [85.38, 93.08, 'recalc']);
  assert.deepEqual(bus.events.map(([name]) => name), ['decision:saved'], 'no score payload and no fresh detail → no partial score:updated');
  const failing = makeBus();
  await assert.rejects(review.submitDecision({ api: { createDecision: async () => { throw new Error('Сервер вернул ошибку 500.'); } }, bus: failing }, { appeal_id: 'a1', action: 'confirm', reason: 'x' }, { conv_id: 't', before }), /500/);
  assert.equal(failing.events.length, 0);
});

test('evidence of any role is accepted when the quote matches exactly; C8 polarity follows critical_when', () => {
  const messages = makeDetail().messages;
  assert.equal(review.checkQuote({ message_id: 't:1', quote: 'про вывод', start: 7, end: 16 }, messages).valid, true, 'client message (PDF p.3: any message or system event)');
  assert.equal(review.checkQuote({ message_id: 't:1', quote: 'про выход', start: 7, end: 16 }, messages).valid, false);
  assert.equal(review.checkQuote({ message_id: 't:9', quote: 'x' }, messages).valid, false);
  assert.equal(review.answerLabel({ ...C('C8'), critical_when: 'no' }, 'no'), 'Нет — нарушение есть');
  assert.equal(review.answerLabel(C('C8'), 'no'), 'Нет — нарушения нет');
});

test('hotkeys use physical keys (event.code) and never fire while typing', () => {
  assert.deepEqual(review.HOTKEYS, { confirm: 'KeyY', change: 'KeyN', next: 'KeyJ', previous: 'KeyK', appeal: 'KeyA' });
  assert.equal(review.isTextEntry({ tagName: 'TEXTAREA' }), true);
  assert.equal(review.isTextEntry({ tagName: 'INPUT', type: 'number' }), true);
  assert.equal(review.isTextEntry({ tagName: 'INPUT', type: 'radio' }), false, 'answer radios keep Y/N working');
  assert.equal(review.isTextEntry({ tagName: 'DIV', isContentEditable: true }), true);
  assert.equal(review.isTextEntry({ tagName: 'BUTTON' }), false);
});

test('dialogue fragment: marked messages ±2 with gap markers; odometer digits aligned by the decimal comma', () => {
  const messages = Array.from({ length: 12 }, (_, idx) => ({ id: `m:${idx}` }));
  const shape = rows => rows.map(row => (row.gap ? `gap${row.gap}` : row.index));
  assert.deepEqual(shape(review.fragmentRows(messages, ['m:5', 'm:6', 'm:11'])), ['gap3', 3, 4, 5, 6, 7, 8, 9, 10, 11]);
  assert.deepEqual(shape(review.fragmentRows(messages, ['m:1', 'm:9'])), [0, 1, 2, 3, 'gap3', 7, 8, 9, 10, 11]);
  assert.deepEqual(shape(review.fragmentRows(messages, ['m:5'])), ['gap3', 3, 4, 5, 6, 7, 'gap4']);
  assert.deepEqual(review.fragmentRows(messages, ['zzz']), []);
  assert.deepEqual(review.odometerColumns(85.38, 93.08), [{ digit: 9, from: 8 }, { digit: 3, from: 5 }, { char: ',' }, { digit: 0, from: 3 }, { digit: 8, from: 8 }]);
  assert.deepEqual(review.odometerColumns(9.5, 10), [{ digit: 1, from: 0 }, { digit: 0, from: 9 }]);
  assert.deepEqual(review.odometerColumns(80, null), [{ char: '—' }]);
});

test('full cycle on the shared api.js mock: appeal → open queue → decision → recalculated history', async () => {
  globalThis.document ||= { baseURI: 'http://cqa.test/' };
  const bus = makeBus();
  const CQA = { config: { mock: true }, state: {}, bus };
  const api = createApi(CQA);
  const [row] = await api.conversations();
  const before = await api.conversation(row.id);
  const criterion = before.checklist.criteria.find(item => before.evals.find(ev => ev.criterion_id === item.id)?.answer === 'no' && item.answer_type === 'binary' && !item.critical);
  assert.ok(criterion, 'fixture has a binary «no» to dispute');
  const appeal = await review.submitAppeal({ api, bus }, review.buildAppealBody({ conv_id: row.id, criterion_id: criterion.id, comment: 'Проверьте реплику', message_ids: [before.messages[3].id] }, before.messages).body);
  assert.ok((await api.appeals({ status: 'open' })).some(item => item.id === appeal.id));
  const withAppeal = await api.conversation(row.id);
  const body = review.buildDecisionBody({ appeal_id: appeal.id, action: 'change', answer: 'yes', reason: 'Действие выполнено' }, criterion, review.finalAnswerFor(withAppeal, criterion.id)).body;
  const outcome = await review.submitDecision({ api, bus }, body, { conv_id: row.id, criterion_id: criterion.id, before: withAppeal });
  const after = await api.conversation(row.id);
  const history = review.buildHistory(after);
  assert.equal(outcome.from, before.score.total);
  assert.equal(outcome.to, after.score.total);
  assert.equal(history.aiTotal, before.score.total, 'AI layer recomputed from the untouched evaluation');
  assert.equal(history.finalTotal, outcome.to);
  assert.equal(history.verified, true);
  assert.equal(review.finalAnswerFor(after, criterion.id), 'yes');
  assert.equal(review.aiAnswerFor(after, criterion.id), 'no');
  assert.deepEqual(review.buildJournal(after, { aiTotal: history.aiTotal }).map(item => item.kind), ['ai', 'appeal', 'decision', 'recalc']);
  assert.equal((await api.appeals({ status: 'open' })).some(item => item.id === appeal.id), false);
});
