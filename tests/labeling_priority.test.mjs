import assert from 'node:assert/strict';
import test from 'node:test';
import {readFile} from 'node:fs/promises';

const original = await readFile(new URL('../web/js/labeling.js', import.meta.url), 'utf8');
const source = original.replaceAll('import.meta.url', JSON.stringify('http://localhost/web/js/labeling.js'));
const m = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
const v1 = JSON.parse(await readFile(new URL('../data/checklists/v1.json', import.meta.url), 'utf8'));
const checklist = {...v1, criterion_count: v1.criteria.length, checklist_hash: 'a'.repeat(64)};
const ctx = (id = 'd1', annotator = 'A') => ({apiScope: 'http://localhost/api/', annotator,
  checklist: structuredClone(checklist), conversation: {id, language: 'RU', channel: 'chat',
    messages: [], transcript_hash: 'b'.repeat(64)}});
const cell = (conv_id, criterion_id) => ({conv_id, criterion_id, criterion_hash: checklist.criteria.find(c => c.id === criterion_id).hash});
const queue = () => ({schema_version: 1, checklist_id: v1.checklist_id, checklist_version: 1,
  cells: [cell('d1', 'C6'), cell('d2', 'C8'), cell('d1', 'C1')]});
const api = () => ({scope: ctx().apiScope, checklist: async () => structuredClone(checklist),
  conversation: async id => ctx(id).conversation});
const answer = (context, cid, value) => m.setAnswer(m.emptyDraft(context), context, cid, value, '2026-11-08T00:00:00Z');

test('priority queue refuses duplicate, unblinded and stale criterion payloads', () => {
  m.validatePriorityQueue(queue(), checklist);
  for (const mutate of [q => q.cells.push(q.cells[0]), q => { q.cells[0].answer = 'yes'; },
    q => { q.models = ['a']; }, q => { q.checklist_version = 2; },
    q => { q.cells[0].criterion_hash = 'f'.repeat(16); }, q => { q.cells = []; }]) {
    const q = queue(); mutate(q); assert.throws(() => m.validatePriorityQueue(q, checklist));
  }
});

test('priority cursor is bounded and follows cells even when conversations repeat', () => {
  const cursor = m.createPriorityCursor(queue());
  assert.equal(cursor.index, 0); assert.equal(cursor.move(-1).criterion_id, 'C6');
  assert.equal(cursor.move(1).conv_id, 'd2');
  assert.equal(cursor.move(1).conv_id, 'd1'); assert.equal(cursor.current.criterion_id, 'C1');
  cursor.move(10); assert.equal(cursor.index, 2);
  cursor.move(-10); assert.equal(cursor.index, 0);
  assert.throws(() => cursor.move(.5));
});

test('partial export is explicit, preserves schema and zero; normal mode still requires all criteria', () => {
  const c = ctx(), draft = answer(c, 'C6', 0);
  assert.throws(() => m.exportRows(draft, c), /Осталось: 7/);
  const rows = m.exportRows(draft, c, ['C6']);
  assert.equal(rows.length, 1); assert.equal(rows[0].answer, 0);
  assert.equal(rows[0].annotator, 'A'); assert.equal(rows[0].annotation_source, 'human');
  assert.equal(rows[0].criterion_hash, checklist.criteria.find(c => c.id === 'C6').hash);
  assert.equal(rows[0].checklist_hash, 'a'.repeat(64)); assert.equal(rows[0].transcript_hash, 'b'.repeat(64));
  for (const ids of [[], ['missing'], ['C6', 'C6'], 'C6', ['C1']]) assert.throws(() => m.exportRows(draft, c, ids));
});

test('batch export orders only explicitly answered queue cells, not extra or other annotator drafts', async () => {
  const c1 = ctx(), c2 = ctx('d2'), drafts = new Map();
  let d1 = answer(c1, 'C6', 0); d1 = m.setAnswer(d1, c1, 'C2', 'yes');
  const d2 = answer(c2, 'C8', 'no');
  drafts.set(m.draftKey(c1), {context: c1, draft: d1}); drafts.set(m.draftKey(c2), {context: c2, draft: d2});
  const got = await m.exportPriorityQueue({queue: queue(), api: api(), drafts, annotator: 'A'});
  assert.deepEqual(got.rows.map(r => [r.conv_id, r.criterion_id, r.answer]), [['d1', 'C6', 0], ['d2', 'C8', 'no']]);
  assert.equal(got.jsonl.trim().split('\n').length, 2);
  await assert.rejects(m.exportPriorityQueue({queue: queue(), api: api(), drafts, annotator: 'B'}), /нет заполненных/);
});

test('fresh transcript/hash changes and lookup errors block batch download', async () => {
  const c = ctx(), drafts = new Map([[m.draftKey(c), {context: c, draft: answer(c, 'C6', 7)}]]);
  for (const mutate of [a => { a.conversation = async id => ({...ctx(id).conversation, transcript_hash: 'e'.repeat(64)}); },
    a => { a.checklist = async () => ({...checklist, checklist_hash: 'c'.repeat(64)}); },
    a => { a.conversation = async () => { throw new Error('Offline'); }; },
    a => { a.conversation = async () => ctx('wrong').conversation; }]) {
    const a = api(); mutate(a);
    await assert.rejects(m.exportPriorityQueue({queue: queue(), api: a, drafts, annotator: 'A'}));
  }
});

test('batch can restore persisted explicit answers but cannot fill absent answers', async () => {
  const c = ctx(), map = new Map([[m.draftKey(c), JSON.stringify(answer(c, 'C6', 0))]]);
  const storage = {getItem: key => map.get(key) ?? null};
  const got = await m.exportPriorityQueue({queue: queue(), api: api(), storage, annotator: 'A'});
  assert.equal(got.rows.length, 1); assert.equal(got.rows[0].criterion_id, 'C6');
});

test('blocked storage still permits download of explicit in-memory answers', async () => {
  const c = ctx(), drafts = new Map([[m.draftKey(c), {context: c, draft: answer(c, 'C6', 0)}]]);
  const storage = {getItem() { throw new Error('Storage blocked'); }};
  const got = await m.exportPriorityQueue({queue: queue(), api: api(), storage, drafts, annotator: 'A'});
  assert.equal(got.rows.length, 1); assert.equal(got.rows[0].answer, 0);
});

test('shipped blind queue contains only IDs and hashes', async () => {
  const shipped = JSON.parse(await readFile(new URL('../web/data/label-priority.json', import.meta.url), 'utf8'));
  m.validatePriorityQueue(shipped, checklist);
  assert.equal(shipped.cells.length, 40);
  assert.ok(shipped.cells.some(c => c.criterion_id === 'C8'), 'critical zero-weight disagreements must be reachable');
});

// A DOM state harness, not a browser/rendering check. All fetches below are in-memory fixtures.
class Element {
  constructor(tag, doc) { this.tag = tag; this.ownerDocument = doc; this.children = []; this.attributes = {}; this.events = {}; this.value = ''; this._text = ''; this.classList = {add() {}}; }
  setAttribute(k, v) { this.attributes[k] = v; if (k === 'value') this.value = v; if (k === 'hidden') this.hidden = true; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; this._text = ''; }
  set textContent(v) { this.replaceChildren(); this._text = v; } get textContent() { return this._text + this.children.map(n => n.textContent).join(''); }
  addEventListener(k, f) { (this.events[k] ||= []).push(f); }
  async fire(k) { for (const f of this.events[k] || []) await f(); }
  focus() { this.ownerDocument.activeElement = this; }
}
const descendants = node => [node, ...node.children.flatMap(descendants)];
test('mounted priority UI loads only blind inputs, shows one criterion and moves with native buttons', async () => {
  const calls = [], previousFetch = globalThis.fetch;
  const doc = {baseURI: 'http://localhost/?label=1&queue=priority', defaultView: {localStorage: null},
    querySelector: () => ({}), createElement: tag => new Element(tag, doc),
    createTextNode: text => { const e = new Element('#text', doc); e.textContent = text; return e; }};
  const root = new Element('div', doc);
  globalThis.fetch = async url => {
    calls.push(url);
    const pathname = new URL(url).pathname;
    const data = pathname.endsWith('label-priority.json') ? queue() : pathname === '/api/labeling/conversations' ?
      ['d1', 'd2'].map(id => ({id, language: 'RU', channel: 'chat'})) : pathname === '/api/labeling/checklists' ?
      [checklist] : pathname === '/api/labeling/checklists/1' ? checklist : ctx(pathname.split('/').at(-1)).conversation;
    return {ok: true, json: async () => structuredClone(data)};
  };
  try {
    const app = await m.boot({root, config: {apiBase: '/api'}});
    const find = predicate => descendants(root).find(predicate);
    const who = find(e => e.attributes.id === 'label-annotator'); who.value = 'A'; await who.fire('input');
    assert.equal(descendants(root).filter(e => e.tag === 'fieldset').length, 1);
    assert.match(find(e => e.tag === 'legend').textContent, /^C6/);
    assert.ok(descendants(root).filter(e => e.tag === 'input' && e.attributes.type === 'radio').every(e => !e.checked));
    await find(e => e.tag === 'button' && e.textContent === 'Следующий критерий').fire('click');
    // The click callback schedules the awaited API selection.
    await new Promise(resolve => setTimeout(resolve, 0));
    assert.match(find(e => e.tag === 'legend').textContent, /^C8/);
    assert.equal(app.session.state.context.conversation.id, 'd2');
    assert.ok(calls.every(url => new URL(url).pathname.startsWith('/api/labeling/') || url.endsWith('/data/label-priority.json')));
    assert.equal(Object.keys(app.session.state.draft.answers).length, 0);
    app.destroy();
  } finally { globalThis.fetch = previousFetch; }
});
