import assert from 'node:assert/strict';
import test from 'node:test';
import {readFile} from 'node:fs/promises';

const source = await readFile(new URL('../web/js/labeling.js', import.meta.url), 'utf8');
const labeling = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
const {annotatorId, validateChecklist, allowedAnswer, draftKey, emptyDraft, restoreDraft,
  setAnswer, exportRows, toJSONL, createBlindClient, createSession} = labeling;
const v1 = JSON.parse(await readFile(new URL('../data/checklists/v1.json', import.meta.url), 'utf8'));
const copy = value => structuredClone(value);
const context = () => ({apiScope: 'http://localhost:8047/api/', annotator: 'human-a',
  conversation: {id: 'test-ru', language: 'RU', channel: 'chat', messages: [], transcript_hash: 'b'.repeat(64)},
  checklist: {...copy(v1), criterion_count: v1.criteria.length, checklist_hash: 'a'.repeat(64)}});
const timestamp = '2026-11-07T12:00:00.000Z';
const completed = ctx => ctx.checklist.criteria.reduce((draft, c) =>
  setAnswer(draft, ctx, c.id, c.answer_type === 'binary' ? 'no' : 0, timestamp), emptyDraft(ctx));
function storage() {
  const map = new Map();
  return {getItem: key => map.get(key) ?? null, setItem: (key, value) => map.set(key, value), map};
}
function fakeApi(ctx = context()) {
  const calls = [];
  return {scope: ctx.apiScope, calls,
    conversation: async id => { calls.push(['conversation', id]); return {...copy(ctx.conversation), id}; },
    checklist: async version => { calls.push(['checklist', version]); return {...copy(ctx.checklist), version}; }};
}
function fillSession(session) {
  session.state.context.checklist.criteria.forEach(c => session.answer(c.id, c.answer_type === 'binary' ? 'no' : 0));
}

test('official answer domains preserve zero, C6 steps and N/A restrictions', () => {
  const ctx = context(); validateChecklist(ctx.checklist);
  for (const c of ctx.checklist.criteria) {
    assert.equal(allowedAnswer(c, 'na'), c.na_allowed);
    assert.equal(allowedAnswer(c, null), false);
    assert.equal(allowedAnswer(c, undefined), false);
    assert.equal(allowedAnswer(c, true), false);
    assert.equal(allowedAnswer(c, ''), false);
    assert.equal(allowedAnswer(c, '0'), false, 'export numbers must remain numbers');
    if (c.answer_type === 'scale') {
      for (const n of c.scale) assert.equal(allowedAnswer(c, n), true);
      assert.equal(allowedAnswer(c, 3), false);
    }
  }
  const c6 = ctx.checklist.criteria.find(c => c.id === 'C6');
  assert.equal(allowedAnswer(c6, 0), true);
  assert.equal(allowedAnswer(c6, 7), true);
  assert.equal(allowedAnswer(c6, 15), false, 'C6 weight is not a permitted scale value');
});

test('numeric criteria accept finite range values, not only scale endpoints', () => {
  const c = {answer_type: 'numeric', scale: [2, 8], na_allowed: true};
  for (const value of [2, 3.75, 8, 'na']) assert.equal(allowedAnswer(c, value), true);
  for (const value of [0, -1, 8.01, Infinity, NaN, '3', 'yes', false]) assert.equal(allowedAnswer(c, value), false);
  assert.equal(allowedAnswer({...c, scale: [10]}, 0), true, 'single-value numeric scale starts at zero');
  assert.equal(allowedAnswer({...c, na_allowed: false}, 'na'), false);
});

test('required stable annotator and integer checklist versions', () => {
  assert.equal(annotatorId('  Алия-01  '), 'Алия-01');
  for (const invalid of ['', '  ', null, 1, 'a\nb', 'x'.repeat(81)]) assert.throws(() => annotatorId(invalid));
  for (const version of [0, -1, 1.2, '1', null]) {
    const ctx = context(); ctx.checklist.version = version;
    assert.throws(() => emptyDraft(ctx));
  }
  assert.throws(() => emptyDraft({...context(), annotator: '  '}));
});

test('checklist rejects duplicates and malformed domains before any annotation', () => {
  for (const mutate of [
    c => c.criteria.push(copy(c.criteria[0])),
    c => { c.criteria[1].id = c.criteria[0].id; },
    c => { c.criteria[0].hash = ''; },
    c => { c.criteria[3].scale = [0, 0, 20]; },
    c => { c.criteria[3].scale = [0, Infinity]; },
    c => { c.criteria[3].scale = []; },
    c => { c.criteria[3].answer_type = 'freeform'; },
    c => { c.criteria[0].na_allowed = 'yes'; },
  ]) {
    const cl = context().checklist; mutate(cl); assert.throws(() => validateChecklist(cl));
  }
});

test('export contains exactly one row per criterion with original hash, provenance and numeric zero', () => {
  const ctx = context(); let draft = completed(ctx);
  draft = setAnswer(draft, ctx, 'C2', 'na', timestamp);
  draft = setAnswer(draft, ctx, 'C6', 7, timestamp);
  draft = setAnswer(draft, ctx, 'C6', 0, timestamp); // overwrite, never append a duplicate
  const rows = toJSONL(draft, ctx).trim().split('\n').map(JSON.parse);
  assert.equal(rows.length, 8);
  assert.equal(new Set(rows.map(r => `${r.conv_id}:${r.criterion_id}`)).size, 8);
  assert.equal(rows.find(r => r.criterion_id === 'C6').answer, 0);
  assert.equal(rows.find(r => r.criterion_id === 'C2').answer, 'na');
  for (const [i, row] of rows.entries()) {
    assert.deepEqual(Object.keys(row).sort(), ['conv_id', 'criterion_id', 'answer', 'annotator', 'checklist_version',
      'criterion_hash', 'checklist_hash', 'transcript_hash', 'annotation_source', 'annotated_at'].sort());
    assert.equal(row.criterion_hash, ctx.checklist.criteria[i].hash);
    assert.equal(row.checklist_version, 1);
    assert.equal(typeof row.checklist_version, 'number');
    assert.equal(row.annotator, 'human-a');
    assert.equal(row.annotation_source, 'human');
    assert.equal(row.annotated_at, timestamp);
    assert.equal(row.transcript_hash, ctx.conversation.transcript_hash);
    assert.equal(row.checklist_hash, ctx.checklist.checklist_hash);
  }
  assert.ok(toJSONL(draft, ctx).endsWith('\n'));
});

test('incomplete, unknown, invalid and stale answers cannot be exported', () => {
  const ctx = context();
  assert.throws(() => exportRows(emptyDraft(ctx), ctx), /Осталось: 8/);
  const complete = completed(ctx);
  assert.throws(() => exportRows(setAnswer(complete, ctx, 'C6', undefined), ctx), /Осталось: 1/);
  for (const mutate of [
    d => { d.answers.C1.answer = 'na'; },
    d => { d.answers.C6.answer = 15; },
    d => { d.answers.C1.criterion_hash = 'stale'; },
    d => { d.answers.REMOVED = copy(d.answers.C1); },
    d => { d.answers.C1.annotated_at = 'yesterday'; },
    d => { d.scope.checklist_version = 2; },
    d => { d.scope.transcript_hash = 'c'.repeat(64); },
    d => { d.scope.checklist_hash = 'd'.repeat(64); },
  ]) {
    const draft = copy(complete); mutate(draft); assert.throws(() => exportRows(draft, ctx));
  }
  assert.throws(() => setAnswer(complete, ctx, 'REMOVED', 'yes'));
  assert.throws(() => setAnswer(complete, ctx, 'C1', 'na'));
  assert.throws(() => setAnswer(complete, ctx, 'C6', 0, 'invalid date'));
});

test('drafts are isolated by origin, identity, conversation, version and content hashes', () => {
  const ctx = context(), key = draftKey(ctx), draft = completed(ctx);
  assert.deepEqual(restoreDraft(JSON.stringify(draft), ctx), draft);
  for (const mutate of [
    c => { c.apiScope = 'https://another.test/api/'; },
    c => { c.annotator = 'human-b'; },
    c => { c.conversation.id = 'other'; },
    c => { c.conversation.transcript_hash = 'c'.repeat(64); },
    c => { c.checklist.version = 2; },
    c => { c.checklist.checklist_id = 'other'; },
    c => { c.checklist.checklist_hash = 'c'.repeat(64); },
  ]) {
    const other = copy(ctx); mutate(other);
    assert.notEqual(draftKey(other), key);
    assert.throws(() => restoreDraft(draft, other));
  }
  assert.notEqual(draftKey({...ctx, annotator: 'a:b', conversation: {...ctx.conversation, id: 'c'}}),
    draftKey({...ctx, annotator: 'a', conversation: {...ctx.conversation, id: 'b:c'}}));
});

test('prototype-like IDs do not inject answers or collide with inherited properties', () => {
  const ctx = context(); ctx.checklist.criteria[0].id = '__proto__';
  const draft = completed(ctx);
  assert.equal(Object.hasOwn(draft.answers, '__proto__'), true);
  assert.equal(exportRows(draft, ctx).length, 8);
  assert.equal(exportRows(draft, ctx)[0].criterion_id, '__proto__');
  assert.equal({}.answer, undefined);
});

test('blind network client uses only GET /labeling inputs and never normal scored routes', async () => {
  const calls = [];
  const api = createBlindClient({apiBase: './api', baseURL: 'http://localhost:8047/', fetchImpl: async (url, init) => {
    calls.push({url, init}); return {ok: true, json: async () => ({})};
  }});
  await api.conversations(); await api.checklists(); await api.conversation('id ?#'); await api.checklist(1);
  assert.deepEqual(calls.map(c => c.url), ['http://localhost:8047/api/labeling/conversations',
    'http://localhost:8047/api/labeling/checklists', 'http://localhost:8047/api/labeling/conversations/id%20%3F%23',
    'http://localhost:8047/api/labeling/checklists/1']);
  assert.ok(calls.every(c => c.init.method === 'GET' && c.init.cache === 'no-store' && !c.init.body));
  assert.throws(() => api.checklist('1'));
  assert.throws(() => createBlindClient({apiBase: 'file:///api', baseURL: 'file:///web/'}));
  const failed = createBlindClient({apiBase: 'https://test.invalid/api', fetchImpl: async () => ({ok: false, status: 503})});
  await assert.rejects(failed.conversations(), /503/);
});

test('session saves only explicit human input, restores exact drafts and keeps other identities blank', async () => {
  const ctx = context(), store = storage(), api = fakeApi(ctx), session = createSession({api, storage: store});
  const selection = {annotator: ctx.annotator, convId: ctx.conversation.id, version: 1};
  await session.select(selection);
  assert.deepEqual(session.state.draft.answers, {});
  assert.equal(store.map.size, 0, 'opening a conversation must not create annotations');
  session.answer('C6', 0);
  assert.equal(store.map.size, 1);
  await session.select({...selection, annotator: 'human-b'});
  assert.deepEqual(session.state.draft.answers, {});
  await session.select(selection);
  assert.equal(session.state.draft.answers.C6.answer, 0);
  assert.match(session.state.notice, /Восстановлен/);
  await session.select({...selection, version: 2});
  assert.deepEqual(session.state.draft.answers, {});
  await session.select({...selection, annotator: ''});
  assert.equal(session.state.context, null);
  assert.equal(session.state.draft, null);
});

test('unavailable or corrupt local storage does not fabricate or silently mark answers complete', async () => {
  const ctx = context(), api = fakeApi(ctx);
  for (const store of [null, {getItem() { return 'not json'; }, setItem() { throw new Error('Quota'); }}]) {
    const session = createSession({api, storage: store});
    await session.select({annotator: ctx.annotator, convId: ctx.conversation.id, version: 1});
    assert.deepEqual(session.state.draft.answers, {});
    assert.match(session.state.notice, /недоступен|устарел/);
    session.answer('C6', 0);
    assert.equal(session.state.draft.answers.C6.answer, 0);
    assert.match(session.state.notice, /не сохраняет/);
    await assert.rejects(session.download(), /Осталось/);
  }
});

test('late conversation requests cannot restore another identity or overwrite current choices', async () => {
  const ctx = context(), api = fakeApi(ctx); let resolveFirst;
  api.conversation = id => id === 'slow' ? new Promise(resolve => { resolveFirst = resolve; }) : Promise.resolve({...ctx.conversation, id});
  const session = createSession({api, storage: storage()});
  const slow = session.select({annotator: 'human-a', convId: 'slow', version: 1});
  const fast = session.select({annotator: 'human-b', convId: 'fast', version: 2});
  await fast;
  session.answer('C6', 0);
  resolveFirst({...ctx.conversation, id: 'slow'}); await slow;
  assert.equal(session.state.context.annotator, 'human-b');
  assert.equal(session.state.context.conversation.id, 'fast');
  assert.equal(session.state.context.checklist.version, 2);
  assert.equal(session.state.draft.answers.C6.answer, 0);
});

test('download verifies fresh inputs; changed dataset or checklist blocks the file', async () => {
  for (const target of ['transcript', 'checklist', 'criterion']) {
    const ctx = context(), api = fakeApi(ctx), session = createSession({api, storage: storage()});
    await session.select({annotator: ctx.annotator, convId: ctx.conversation.id, version: 1});
    fillSession(session);
    const ready = await session.download(); assert.equal(ready.rows.length, 8);
    assert.equal(api.calls.length, 4, 'initial inputs + two fresh GETs');
    if (target === 'transcript') ctx.conversation.transcript_hash = 'c'.repeat(64);
    if (target === 'checklist') ctx.checklist.checklist_hash = 'd'.repeat(64);
    if (target === 'criterion') ctx.checklist.criteria[0].hash = 'e'.repeat(16);
    await assert.rejects(session.download());
    assert.match(session.state.error, /не выгружен/);
    assert.equal(session.state.exporting, false);
  }
});

test('failed preflight, concurrent export and selection changes cannot download stale rows', async () => {
  const ctx = context(), api = fakeApi(ctx), session = createSession({api, storage: storage()});
  await session.select({annotator: ctx.annotator, convId: ctx.conversation.id, version: 1});
  fillSession(session);
  let resolve;
  api.conversation = () => new Promise(r => { resolve = r; });
  const exporting = session.download();
  await assert.rejects(session.download(), /Дождитесь/);
  session.answer('C6', 7); assert.equal(session.state.draft.answers.C6.answer, 0, 'frozen during export');
  await session.select({annotator: '', convId: '', version: 0});
  resolve(ctx.conversation);
  await assert.rejects(exporting, /Выбор изменился/);
  assert.equal(session.state.context, null);
});

test('load errors and mismatched response identities clear the old task', async () => {
  const ctx = context(), api = fakeApi(ctx), session = createSession({api, storage: storage()});
  await session.select({annotator: ctx.annotator, convId: ctx.conversation.id, version: 1});
  session.answer('C1', 'yes');
  api.checklist = async () => { throw new Error('Offline'); };
  await session.select({annotator: ctx.annotator, convId: 'next', version: 2});
  assert.equal(session.state.context, null); assert.equal(session.state.draft, null);
  assert.equal(session.state.loading, false); assert.match(session.state.error, /Offline/);
  api.checklist = async () => ({...ctx.checklist, version: 3});
  await session.select({annotator: ctx.annotator, convId: 'next', version: 2});
  assert.equal(session.state.context, null); assert.match(session.state.error, /другую версию/);
});
