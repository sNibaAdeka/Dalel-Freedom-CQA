import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
const profileSource = await readFile(new URL('../web/js/model-profiles.js', import.meta.url), 'utf8');
const profileUrl = `data:text/javascript;base64,${Buffer.from(profileSource).toString('base64')}`;
const source = (await readFile(new URL('../web/js/board.js', import.meta.url), 'utf8')).replace("'./model-profiles.js'", JSON.stringify(profileUrl));
const { createRunController, runSummary, parseImport, conversationSelection, mount } = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
const tick = () => new Promise(resolve => setImmediate(resolve));
function deferred() { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; }
function timers() {
  const pending = new Map(); let sequence = 0;
  return { schedule(fn) { pending.set(++sequence, fn); return sequence; }, unschedule(id) { pending.delete(id); },
    async next() { const [id, fn] = pending.entries().next().value || []; if (fn) { pending.delete(id); fn(); await tick(); } }, get size() { return pending.size; } };
}

test('launches one run, polls once at a time and reports completion exactly once', async () => {
  const t = timers(), started = [], finished = [], progress = []; let polls = 0;
  const pendingStart = deferred();
  const controller = createRunController({ createRun: body => { started.push(body); return pendingStart.promise; }, run: async id => { polls++; return { run_id: id, status: 'completed', done: 2, total: 2 }; } },
    { ...t, onFinish: x => finished.push(x), onProgress: x => progress.push(x) });
  const promise = controller.start({ version: 1, conv_ids: ['a', 'b'] });
  assert.equal(controller.state.phase, 'starting');
  await controller.start({ version: 2 }); assert.equal(started.length, 1, 'double submit must not start another server run');
  pendingStart.resolve({ run_id: 'r1', status: 'running', done: 0, total: 2 }); await promise;
  assert.equal(t.size, 1); await t.next();
  assert.equal(polls, 1); assert.equal(controller.state.phase, 'finished'); assert.equal(t.size, 0); assert.equal(finished.length, 1);
  controller.watch('r1'); await tick(); assert.equal(finished.length, 1, 'viewing a finished run cannot repeat finish side effects');
  assert.equal(progress.at(-1).done, 2); controller.destroy();
});

test('poll errors preserve run ID and retry observation without creating another run', async () => {
  const t = timers(); let writes = 0, fail = true;
  const controller = createRunController({ createRun: async () => { writes++; return { run_id: 'r2' }; }, run: async () => {
    if (fail) throw new Error('503'); return { run_id: 'r2', status: 'completed', done: 1, total: 1 };
  } }, t);
  await controller.start({ version: 1 }); await t.next();
  assert.equal(controller.state.phase, 'paused'); assert.equal(controller.state.run.run_id, 'r2'); assert.match(controller.state.error.message, /503/); assert.equal(t.size, 0);
  fail = false; controller.watch(controller.state.run); await tick();
  assert.equal(writes, 1); assert.equal(controller.state.phase, 'finished'); controller.destroy();
});

test('failed writes remain errors and do not fabricate a run or success event', async () => {
  const t = timers(); let completed = 0;
  const controller = createRunController({ createRun: async () => { throw new Error('offline'); }, run: async () => assert.fail('must not poll') },
    { ...t, onFinish: () => completed++ });
  await controller.start({ version: 1 });
  assert.equal(controller.state.phase, 'error'); assert.equal(controller.state.run, null); assert.equal(t.size, 0); assert.equal(completed, 0); controller.destroy();
});

test('existing editor run is observed without duplicate POST and stale run replies are ignored', async () => {
  const old = deferred(), next = deferred(); let writes = 0;
  const controller = createRunController({ createRun: async () => { writes++; }, run: id => id === 'old' ? old.promise : next.promise }, timers());
  controller.watch({ run_id: 'old', version: 1 }); controller.watch({ run_id: 'new', version: 2 });
  next.resolve({ run_id: 'new', status: 'completed', done: 4, total: 4 }); await tick();
  old.resolve({ run_id: 'old', status: 'completed', done: 99, total: 99 }); await tick();
  assert.equal(writes, 0); assert.equal(controller.state.run.run_id, 'new'); assert.equal(controller.state.run.done, 4); controller.destroy();
});

test('stop only stops observation; destroy suppresses pending replies and timers', async () => {
  const pending = deferred(), t = timers(); let finished = 0, calls = 0;
  const controller = createRunController({ run: () => { calls++; return pending.promise; } }, { ...t, onFinish: () => finished++ });
  controller.watch('r3'); controller.stop();
  assert.equal(controller.state.phase, 'paused'); assert.equal(controller.state.run.run_id, 'r3');
  pending.resolve({ run_id: 'r3', status: 'completed' }); await tick();
  assert.equal(finished, 0); assert.equal(controller.state.phase, 'paused'); assert.equal(calls, 1);
  controller.destroy(); controller.watch('r4'); assert.equal(calls, 1); assert.equal(t.size, 0);
});

test('destroy during POST does not resurrect the board when the server reply arrives', async () => {
  const pending = deferred(); let changed = 0;
  const controller = createRunController({ createRun: () => pending.promise }, { ...timers(), onChange: () => changed++ });
  const request = controller.start({ version: 1 }); controller.destroy(); const before = changed;
  pending.resolve({ run_id: 'late', status: 'completed' }); await request;
  assert.equal(changed, before); assert.equal(controller.state.run, null);
});

test('reset clears the selection and suppresses an outstanding poll from the previous data context', async () => {
  const pending = deferred(), t = timers(); let finished = 0;
  const controller = createRunController({ run: () => pending.promise }, { ...t, onFinish: () => finished++ });
  controller.watch('old-context'); controller.reset();
  assert.equal(controller.state.phase, 'idle'); assert.equal(controller.state.run, null);
  pending.resolve({ run_id: 'old-context', status: 'completed' }); await tick();
  assert.equal(controller.state.run, null); assert.equal(finished, 0); assert.equal(t.size, 0);
  controller.destroy();
});

test('malformed server launch response is an error instead of endless polling', async () => {
  const t = timers(); const controller = createRunController({ createRun: async () => ({ status: 'running' }) }, t);
  await controller.start({ version: 1 }); assert.equal(controller.state.phase, 'error'); assert.equal(t.size, 0); controller.destroy();
});

test('distribution and changed count ignore missing scores and retain real zeroes', () => {
  const summary = runSummary({ per_conv: [
    { old_total: null, new_total: 85.38, status: 'done' }, { old_total: 0, new_total: 20, status: 'done' },
    { old_total: 100, new_total: 100, status: 'done' }, { old_total: 60, new_total: null, status: 'error' },
  ] });
  assert.equal(summary.errors, 1); assert.equal(summary.changed, 1); assert.equal(summary.compared, 2);
  assert.deepEqual(summary.before, [1, 0, 0, 1, 1]); assert.deepEqual(summary.after, [0, 1, 0, 0, 2]);
});

test('JSON import validates syntax and shape without silently transforming transcript content', () => {
  assert.throws(() => parseImport('{'), /JSON/); assert.throws(() => parseImport('null'), /Нужен/);
  assert.throws(() => parseImport('"dialogue"'), /Нужен/); assert.throws(() => parseImport('[]'), /нет диалогов/);
  const value = { conversation_id: 'synthetic', transcript: [{ speaker: 'client', text: '<img onerror=alert(1)>' }] };
  assert.deepEqual(parseImport(JSON.stringify(value)), value);
});

test('selection is limited to chosen group and scope', () => {
  const rows = [{ id: 'a', group_id: 'X', total: null }, { id: 'b', group_id: 'X', total: 0 }, { id: 'c', group_id: 'Y', total: 99.99 }];
  assert.deepEqual(conversationSelection(rows, 'X', 'all'), ['a', 'b']);
  assert.deepEqual(conversationSelection(rows, 'X', 'pending'), ['a']);
  assert.deepEqual(conversationSelection(rows, '', 'active', 'c'), ['c']);
});

// Minimal DOM for real mount interactions; this does not prove browser geometry.
class Element {
  constructor(tag) { this.tagName = tag.toUpperCase(); this.children = []; this.parentNode = null; this.dataset = {}; this.style = {}; this.attributes = {}; this.listeners = {}; this.className = ''; this._text = ''; this.hidden = false; this.checked = false; this.files = []; }
  set textContent(value) { this.children = []; this._text = String(value); }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
  get value() { return this._value ?? (this.tagName === 'SELECT' ? this.children[0]?.value || '' : ''); }
  set value(value) { this._value = String(value); }
  get classList() { return { add: (...names) => { this.className = [...new Set([...this.className.split(' ').filter(Boolean), ...names])].join(' '); }, remove: (...names) => { this.className = this.className.split(' ').filter(n => !names.includes(n)).join(' '); } }; }
  append(...children) { for (const child of children) { this.children.push(child); child.parentNode = this; } }
  replaceChildren(...children) { this.children = []; this._text = ''; this.append(...children); if (this.tagName === 'SELECT') this._value = undefined; }
  setAttribute(key, value) { this.attributes[key] = String(value); }
  addEventListener(event, handler) { (this.listeners[event] ||= []).push(handler); }
  dispatch(event) { const payload = { target: this, preventDefault() {} }; return Promise.all((this.listeners[event] || []).map(fn => fn(payload))); }
  click() { return this.dispatch('click'); }
  remove() { if (this.parentNode) this.parentNode.children = this.parentNode.children.filter(c => c !== this); }
  focus() { this.focused = true; }
}
const walk = root => [root, ...root.children.flatMap(walk)];
const findClass = (root, name) => walk(root).find(n => n.className.split(' ').includes(name));
function bus() { const listeners = new Map(); const events = []; return { events, on(name, fn) { if (!listeners.has(name)) listeners.set(name, new Set()); listeners.get(name).add(fn); return () => listeners.get(name).delete(fn); }, emit(name, data) { events.push([name, data]); for (const fn of listeners.get(name) || []) fn(data); } }; }
function ui(api, economics) {
  const host = new Element('div');
  globalThis.document = { head: new Element('head'), createElement: tag => new Element(tag), getElementById: id => id === 'cqa-board' ? host : null, querySelector: () => ({}) };
  const CQA = { config: { mock: false }, state: { checklistVersion: 1, activeConversationId: 'a' }, bus: bus(), api: {
    conversations: async () => [{ id: 'a', group_id: 'TEST', total: 85.38 }], checklists: async () => [{ version: 1, name: 'Базовый' }],
    config: async () => ({ mode: 'fast', location: 'cloud', profile: 'claude', model: { model: 'synthetic-provider' } }), ...api,
  } };
  if (economics) CQA.economics = economics;
  return { host, CQA, destroy: mount(CQA) };
}

test('budget tracks selected channel mix and profile; choosing cascade never starts a run', async t => {
  const updates = [], writes = []; let options, destroyed = 0;
  const { host, destroy } = ui({
    modelGuide: async () => ({ reports: [] }),
    config: async () => ({ location: 'cloud', profile: 'cloud', mode: 'fast' }),
    conversations: async () => [{ id: 'a', channel: 'chat', group_id: 'A', total: 80 }, { id: 'b', channel: 'call', group_id: 'B', total: null }],
    setConfig: async body => { writes.push(body); return body; }, createRun: async body => writes.push(body),
  }, { mountBudget: (_host, value) => { options = value; return { update: value => updates.push(value), destroy: () => destroyed++ }; } });
  t.after(destroy); await tick();
  const latest = () => updates.at(-1) || options;
  assert.deepEqual(latest().plan, { dialogues: 2, channels: { chat: 1, call: 1 }, mode: 'fast' });
  const controls = walk(host).filter(n => n.tagName === 'SELECT');
  const group = controls.find(n => n.children.some(c => c.textContent === 'Все загруженные наборы'));
  group.value = 'B'; await group.dispatch('change');
  assert.deepEqual(latest().plan.channels, { chat: 0, call: 1 }); assert.equal(latest().plan.dialogues, 1);
  const profile = controls.find(n => n.children.some(c => c.value === 'routing'));
  profile.value = 'routing'; await profile.dispatch('change'); assert.equal(latest().profile, 'routing');
  await options.onSwitchCascade(); assert.equal(profile.value, 'cascade'); assert.equal(latest().profile, 'cascade');
  assert.equal(writes.length, 0, 'budget choice is a selection, not a model/config request');
  destroy(); assert.equal(destroyed, 1);
});

test('budget ignores a measurement reply after board teardown', async () => {
  const pending = deferred(); let mounts = 0;
  const { destroy } = ui({ modelGuide: () => pending.promise }, { mountBudget: () => { mounts++; } });
  destroy(); pending.resolve({ reports: [] }); await tick(); assert.equal(mounts, 0);
});

test('budget reloads once on source change and discards the older measurement response', async t => {
  const old = deferred(), fresh = deferred(), payloads = []; let reads = 0;
  const { CQA, destroy } = ui({ modelGuide: () => ++reads === 1 ? old.promise : fresh.promise }, {
    mountBudget: (_host, options) => { payloads.push(options.quality); return { update() {}, destroy() {} }; },
  });
  t.after(destroy); await tick();
  CQA.config.mock = true; CQA.bus.emit('api:mode');
  CQA.bus.emit('api:mode'); assert.equal(reads, 2, 'same-context request notifications do not cause reloads');
  const current = { reports: [], source: 'current' }; fresh.resolve(current); await tick();
  old.resolve({ reports: [], source: 'obsolete' }); await tick(); assert.deepEqual(payloads, [current]);
});

test('late budget mounting inherits an already running board lock', async t => {
  const pending = deferred(), updates = [];
  const { CQA, destroy } = ui({ modelGuide: () => pending.promise, run: async () => ({ run_id: 'in-flight', status: 'running', done: 0, total: 1 }) }, {
    mountBudget: (_host, options) => { updates.push(options); return { update: value => updates.push(value), destroy() {} }; },
  });
  t.after(destroy); await tick(); CQA.bus.emit('run:started', { run_id: 'in-flight' }); await tick();
  pending.resolve({ reports: [] }); await tick(); assert.equal(updates.at(-1).busy, true);
});

function downloads(t) {
  const files = [], blobs = new Map();
  t.mock.method(URL, 'createObjectURL', blob => { const url = `blob:synthetic-${blobs.size}`; blobs.set(url, blob); return url; });
  t.mock.method(URL, 'revokeObjectURL', () => {});
  t.mock.method(Element.prototype, 'click', function () {
    if (this.tagName === 'A') files.push({ filename: this.download, blob: blobs.get(this.href) });
  });
  return files;
}
const exportButton = (host, format) => walk(host).find(n => n.tagName === 'BUTTON' && n.textContent === `Скачать ${format.toUpperCase()}`);
const finishedRun = (id, extra = {}) => ({ run_id: id, version: 1, status: 'completed', per_conv: [], ...extra });
async function observe(CQA, id) { CQA.bus.emit('run:started', { run_id: id }); await tick(); }

test('a slow old export cannot lock the newly selected run or unlock its pending download', async t => {
  const files = downloads(t), old = deferred(), current = deferred(), calls = [];
  const { host, CQA, destroy } = ui({ run: async id => finishedRun(id), exportRun: (id, format) => {
    calls.push([id, format]); return id === 'old' ? old.promise : current.promise;
  } });
  t.after(destroy); await tick(); await observe(CQA, 'old');
  await exportButton(host, 'json').dispatch('click');
  await observe(CQA, 'current');
  assert.equal(exportButton(host, 'csv').disabled, false, 'old request must not block the selected run');
  await exportButton(host, 'csv').dispatch('click');
  old.resolve(new Blob(['old snapshot'])); await tick();
  assert.equal(files.length, 0, 'old snapshot must not download under the current run');
  assert.equal(exportButton(host, 'csv').disabled, true, 'old finally must not release the newer export lock');
  const snapshot = new Blob(['run_id,run_status,total\ncurrent,failed,\n']);
  current.resolve(snapshot); await tick();
  assert.deepEqual(calls, [['old', 'json'], ['current', 'csv']]);
  assert.deepEqual(files, [{ filename: 'current.csv', blob: snapshot }]);
});

test('late export failure from a previous run does not overwrite the selected run notice', async t => {
  const old = deferred();
  const { host, CQA, destroy } = ui({ run: async id => finishedRun(id), exportRun: () => old.promise });
  t.after(destroy); await tick(); await observe(CQA, 'old');
  await exportButton(host, 'json').dispatch('click'); await observe(CQA, 'current');
  old.reject(new Error('old export failed')); await tick();
  assert.doesNotMatch(findClass(host, 'cqa-board__notice').textContent, /old export failed/);
  assert.equal(exportButton(host, 'json').disabled, false);
});

test('switching away and back to the same run invalidates its earlier export request', async t => {
  const files = downloads(t), old = deferred();
  const { host, CQA, destroy } = ui({ run: async id => finishedRun(id), exportRun: () => old.promise });
  t.after(destroy); await tick(); await observe(CQA, 'same-id');
  await exportButton(host, 'json').dispatch('click');
  await observe(CQA, 'another'); await observe(CQA, 'same-id');
  old.resolve(new Blob(['stale request'])); await tick();
  assert.equal(files.length, 0, 'matching IDs alone do not make an old UI request current');
});

test('switching server data to demo resets the selected run and cancels a pending export', async t => {
  const files = downloads(t), pending = deferred();
  const { host, CQA, destroy } = ui({ run: async id => finishedRun(id), exportRun: () => pending.promise });
  t.after(destroy); await tick(); await observe(CQA, 'server-run');
  await exportButton(host, 'json').dispatch('click');
  CQA.config.mock = true; CQA.bus.emit('api:mode', { mock: true });
  pending.resolve(new Blob(['server data'])); await tick();
  assert.equal(files.length, 0, 'server download must not arrive in the demo workspace');
  assert.equal(findClass(host, 'cqa-board__results').hidden, true, 'old server results are not demo results');
  CQA.config.mock = false; CQA.bus.emit('api:mode', { mock: false });
  assert.equal(exportButton(host, 'json').disabled, true, 'returning to HTTP must not resurrect the old export selection');
});

test('unchanged mode notifications preserve the selected export and duplicate clicks do not start requests', async t => {
  const files = downloads(t), pending = deferred(); let calls = 0;
  const { host, CQA, destroy } = ui({ run: async id => finishedRun(id), exportRun: () => { calls++; return pending.promise; } });
  t.after(destroy); await tick(); await observe(CQA, 'selected');
  await exportButton(host, 'json').dispatch('click');
  CQA.bus.emit('api:mode', { mock: false });
  await exportButton(host, 'csv').dispatch('click');
  assert.equal(calls, 1); assert.equal(findClass(host, 'cqa-board__results').hidden, false);
  const snapshot = new Blob(['selected snapshot']); pending.resolve(snapshot); await tick();
  assert.deepEqual(files, [{ filename: 'selected.json', blob: snapshot }]);
});

test('offline transition cancels a pending download but retains the explicitly selected snapshot', async t => {
  const files = downloads(t), pending = deferred(); let calls = 0;
  const { host, CQA, destroy } = ui({ run: async id => finishedRun(id), exportRun: () => { calls++; return pending.promise; } });
  t.after(destroy); await tick(); await observe(CQA, 'saved');
  await exportButton(host, 'json').dispatch('click');
  CQA.config.offline = true; CQA.bus.emit('api:mode', { mock: false, offline: true });
  assert.equal(exportButton(host, 'csv').disabled, true);
  await exportButton(host, 'csv').dispatch('click'); assert.equal(calls, 1);
  CQA.config.offline = false; CQA.bus.emit('api:mode', { mock: false, offline: false });
  pending.resolve(new Blob(['cancelled snapshot'])); await tick();
  assert.equal(files.length, 0); assert.equal(findClass(host, 'cqa-board__results').hidden, false);
  assert.equal(exportButton(host, 'json').disabled, false);
});

test('changing the API source resets its run even when the replacement server uses the same run ID', async t => {
  const files = downloads(t), pending = deferred(); let calls = 0;
  const { host, CQA, destroy } = ui({ run: async id => finishedRun(id), exportRun: () => {
    calls++; return calls === 1 ? pending.promise : Promise.resolve(new Blob(['new source']));
  } });
  t.after(destroy); await tick(); await observe(CQA, 'same-id');
  await exportButton(host, 'json').dispatch('click');
  CQA.config.apiBase = './other-api'; CQA.bus.emit('api:mode', { mock: false });
  assert.equal(findClass(host, 'cqa-board__results').hidden, true);
  await observe(CQA, 'same-id'); await exportButton(host, 'csv').dispatch('click'); await tick();
  pending.resolve(new Blob(['old source'])); await tick();
  assert.equal(files.length, 1); assert.equal(files[0].filename, 'same-id.csv');
  assert.equal(await files[0].blob.text(), 'new source');
  assert.equal(CQA.bus.events.filter(([name]) => name === 'run:finished').length, 2, 'finish events belong to their data source');
});

test('starting a new run cancels the old export before asynchronous settings are saved', async t => {
  const files = downloads(t), pending = deferred(), settings = deferred(); let exports = 0;
  const { host, CQA, destroy } = ui({ run: async id => finishedRun(id), exportRun: () => { exports++; return pending.promise; },
    setConfig: () => settings.promise, createRun: async () => finishedRun('new-run') });
  t.after(destroy); await tick(); await observe(CQA, 'old-run');
  await exportButton(host, 'json').dispatch('click');
  const starting = findClass(host, 'cqa-board__form').dispatch('submit');
  pending.resolve(new Blob(['old snapshot'])); await tick();
  assert.equal(files.length, 0, 'preparing the next run must invalidate the previous export');
  assert.equal(exportButton(host, 'csv').disabled, true);
  await exportButton(host, 'csv').dispatch('click'); assert.equal(exports, 1);
  settings.resolve({ profile: 'mock' }); await starting;
});

test('a data context reset during settings save cannot launch a run after switching back', async t => {
  const settings = deferred(); let creates = 0;
  const { host, CQA, destroy } = ui({ setConfig: () => settings.promise, createRun: async () => { creates++; return finishedRun('unwanted'); } });
  t.after(destroy); await tick();
  const starting = findClass(host, 'cqa-board__form').dispatch('submit');
  CQA.config.mock = true; CQA.bus.emit('api:mode', { mock: true });
  CQA.config.mock = false; CQA.bus.emit('api:mode', { mock: false });
  settings.resolve({ profile: 'mock' }); await starting;
  assert.equal(creates, 0); assert.equal(findClass(host, 'cqa-board__results').hidden, true);
  assert.equal(findClass(host, 'cqa-board__button--primary').disabled, false);
});

test('an active export failure identifies its run and allows retry without downloading partial data', async t => {
  const files = downloads(t); let calls = 0;
  const { host, CQA, destroy } = ui({ run: async id => finishedRun(id), exportRun: async () => {
    calls++; if (calls === 1) throw new Error('snapshot unavailable'); return new Blob(['retried snapshot']);
  } });
  t.after(destroy); await tick(); await observe(CQA, 'selected');
  await exportButton(host, 'json').dispatch('click'); await tick();
  assert.equal(files.length, 0);
  assert.match(findClass(host, 'cqa-board__notice').textContent, /selected.*snapshot unavailable/);
  assert.equal(findClass(host, 'cqa-board__notice').attributes.role, 'alert');
  assert.equal(exportButton(host, 'json').disabled, false);
  await exportButton(host, 'json').dispatch('click'); await tick();
  assert.equal(files.length, 1); assert.equal(calls, 2);
});

test('running, cancelled and incomplete runs cannot export previous totals through a synthetic click', async t => {
  let exports = 0;
  const { host, CQA, destroy } = ui({ run: async id => finishedRun(id, { status: id,
    per_conv: [{ id: 'a', status: 'incomplete', old_total: 100, new_total: 100, score: { status: 'incomplete', missing: ['C8'] } }] }),
    exportRun: async () => { exports++; return new Blob(['unexpected']); } });
  t.after(destroy); await tick();
  for (const status of ['running', 'cancelled', 'incomplete']) {
    await observe(CQA, status);
    assert.equal(exportButton(host, 'json').disabled, true);
    assert.match(findClass(host, 'cqa-board__tile').textContent, /Не рассчитан/);
    await exportButton(host, 'json').dispatch('click'); await exportButton(host, 'csv').dispatch('click');
  }
  assert.equal(exports, 0);
});

test('failed-run downloads preserve the HTTP snapshot bytes and never reuse a prior score', async t => {
  const files = downloads(t), calls = [];
  const snapshots = {
    json: new Blob([JSON.stringify({ run_id: 'failed-run', checklist_version: 2, run_status: 'failed', per_conv: [{ status: 'error', score: null, answers: [] }] })]),
    csv: new Blob(['run_id,checklist_version,run_status,status,total\nfailed-run,2,failed,error,\n']),
  };
  const { host, CQA, destroy } = ui({ run: async id => finishedRun(id, { version: 2, status: 'failed',
    per_conv: [{ id: 'a', status: 'error', old_total: 100, new_total: null, error: 'synthetic failure' }] }),
    exportRun: async (id, format) => { calls.push([id, format]); return snapshots[format]; } });
  t.after(destroy); await tick(); await observe(CQA, 'failed-run');
  assert.match(findClass(host, 'cqa-board__tile').textContent, /Не рассчитан/);
  for (const format of ['json', 'csv']) { await exportButton(host, format).dispatch('click'); await tick(); }
  assert.deepEqual(calls, [['failed-run', 'json'], ['failed-run', 'csv']]);
  assert.deepEqual(files.map(f => f.blob), [snapshots.json, snapshots.csv]);
  assert.match(findClass(host, 'cqa-board__exports').textContent, /failed-run.*v2.*ошиб/i);
});

test('cleanup suppresses a pending export without a download or a late error message', async t => {
  const files = downloads(t), pending = deferred();
  const { host, CQA, destroy } = ui({ run: async id => finishedRun(id), exportRun: () => pending.promise });
  await tick(); await observe(CQA, 'old'); await exportButton(host, 'json').dispatch('click'); destroy();
  const before = host.textContent;
  pending.resolve(new Blob(['late'])); await tick();
  assert.equal(files.length, 0); assert.equal(host.textContent, before);
});

test('mounted board saves provider choice before launch and keeps write failures visible', async () => {
  const calls = [];
  const { host, CQA, destroy } = ui({ setConfig: async body => { calls.push(['config', body]); return { profile: 'chosen-server-profile' }; },
    createRun: async body => { calls.push(['run', body]); throw new Error('503 unavailable'); } });
  await tick();
  walk(host).find(node => Object.hasOwn(node.dataset, 'boardLive')).checked = true;
  await findClass(host, 'cqa-board__form').dispatch('submit');
  assert.equal(calls[0][0], 'config'); assert.equal(calls[1][1].profile, 'chosen-server-profile'); assert.deepEqual(calls[1][1].conv_ids, ['a']);
  assert.equal(calls[1][1].live, true); assert.equal(calls[1][1].force, false, 'fresh missing pairs must not discard saved unchanged evaluations');
  assert.equal(calls[1][1].activate, true, 'activation belongs to the run so old totals are captured first');
  assert.match(findClass(host, 'cqa-board__notice').textContent, /503 unavailable/);
  assert.equal(findClass(host, 'cqa-board__notice').attributes.role, 'alert');
  assert.equal(CQA.bus.events.filter(([name]) => name === 'run:finished').length, 0);
  assert.equal(findClass(host, 'cqa-board__button--primary').disabled, false); destroy();
});

test('mounted board does not start evaluation if saving model settings fails', async () => {
  let writes = 0;
  const { host, destroy } = ui({ setConfig: async () => { throw new Error('settings error'); }, createRun: async () => { writes++; } });
  await tick(); await findClass(host, 'cqa-board__form').dispatch('submit');
  assert.equal(writes, 0); assert.match(findClass(host, 'cqa-board__notice').textContent, /settings error/); destroy();
});

test('mounted board observes editor run and publishes real totals as text on completion', async () => {
  let writes = 0;
  const { host, CQA, destroy } = ui({ createRun: async () => { writes++; }, run: async id => ({ run_id: id, version: 2, activated: true, status: 'completed', done: 1, total: 1, cost_kzt: 0, elapsed: 1.2,
    per_conv: [{ id: '<synthetic>', old_total: 85.38, new_total: 99.99, status: 'done' }] }) });
  await tick(); CQA.bus.emit('run:started', { run_id: 'from-editor', version: 2 }); await tick();
  assert.equal(writes, 0); assert.equal(CQA.bus.events.filter(([n]) => n === 'run:finished').length, 1);
  assert.deepEqual(CQA.bus.events.find(([n]) => n === 'checklist:activated')?.[1], { version: 2 });
  assert.match(findClass(host, 'cqa-board__tile').textContent, /85,38.*99,99/);
  assert.match(findClass(host, 'cqa-board__tile-id').textContent, /<synthetic>/);
  assert.match(findClass(host, 'cqa-board__stats').textContent, /0 ₸/); destroy();
});

test('failed server run does not announce checklist activation', async () => {
  const { host, CQA, destroy } = ui({ run: async id => ({ run_id: id, version: 2, status: 'failed', activated: false,
    done: 1, total: 1, error: 'Provider unavailable', per_conv: [{ id: 'a', status: 'error', error: 'Provider unavailable', new_total: null }] }) });
  await tick(); CQA.bus.emit('run:started', { run_id: 'failed-run', version: 2 }); await tick();
  assert.equal(CQA.bus.events.filter(([n]) => n === 'checklist:activated').length, 0);
  assert.match(findClass(host, 'cqa-board__result-note').textContent, /Provider unavailable/);
  assert.equal(findClass(host, 'cqa-board__tile').dataset.state, 'error'); destroy();
});

test('JSON upload failure stays visible, retains selected file, and does not announce refreshed data', async () => {
  const payload = [{ conversation_id: 'synthetic-upload', transcript: [{ speaker: 'client', text: 'Synthetic only.' }] }];
  let calls = 0;
  const { host, CQA, destroy } = ui({ ingest: async (group, body) => { calls++; assert.equal(group, 'UPLOAD'); assert.deepEqual(body, payload); throw new Error('Import rejected'); } });
  await tick();
  const file = walk(host).find(node => node.type === 'file'); file.files = [{ size: 200, text: async () => JSON.stringify(payload) }]; file.value = 'data.json';
  const upload = walk(host).find(node => node.tagName === 'BUTTON' && node.textContent === 'Загрузить JSON');
  await upload.dispatch('click'); await tick();
  assert.equal(calls, 1); assert.equal(file.value, 'data.json');
  assert.match(findClass(host, 'cqa-board__notice').textContent, /Import rejected/);
  assert.equal(CQA.bus.events.filter(([name]) => name === 'conversations:refresh').length, 0); destroy();
});

test('confirmed JSON import disables duplicate upload without arming a sticky full rerun', async () => {
  const pending = deferred(); let writes = 0;
  const { host, CQA, destroy } = ui({ ingest: async () => { writes++; return pending.promise; } });
  await tick();
  const file = walk(host).find(node => node.type === 'file'); file.files = [{ size: 20, text: async () => '[{"conversation_id":"synthetic"}]' }];
  const upload = walk(host).find(node => node.tagName === 'BUTTON' && node.textContent === 'Загрузить JSON');
  await upload.dispatch('click'); await upload.dispatch('click'); await tick(); assert.equal(writes, 1);
  pending.resolve({ count: 1, group_id: 'TEST' }); await tick();
  assert.equal(walk(host).find(node => Object.hasOwn(node.dataset, 'boardLive')).checked, false);
  assert.match(findClass(host, 'cqa-board__notice').textContent, /Загружено диалогов: 1/);
  assert.equal(CQA.bus.events.filter(([name]) => name === 'conversations:refresh').length, 1); destroy();
});

const liveControl = host => walk(host).find(n => Object.hasOwn(n.dataset, 'boardLive'));
const submit = host => findClass(host, 'cqa-board__form').dispatch('submit');
const choose = async (host, label, value) => {
  const field = walk(host).find(n => n.tagName === 'LABEL' && n.children[0]?.textContent === label);
  const control = field.children.find(n => n.tagName === 'SELECT');
  control.value = value; await control.dispatch('change');
};
const base = { version: 1, active: true, name: 'Base', criteria: [{ id: 'C1', hash: 'h1', weight: 10 }, { id: 'C2', hash: 'h2', weight: 90 }] };
const weights = { ...base, version: 2, active: false, parent_version: 1, name: 'Weights', criteria: [{ id: 'C1', hash: 'h1', weight: 20 }, { id: 'C2', hash: 'h2', weight: 80 }] };
const replacement = { ...weights, version: 3, parent_version: 2, name: 'Replacement', criteria: [{ id: 'C1', hash: 'h1', weight: 20 }, { id: 'C2', hash: 'new-h2', weight: 80 }] };

test('read-only selection never starts a run or preview and shows no invented count or estimate', async () => {
  let writes = 0;
  const { host, CQA, destroy } = ui({ checklists: async () => [base, weights, replacement],
    createRun: async () => { writes++; assert.fail('planning cannot start evaluation'); },
    setConfig: async () => { writes++; assert.fail('selection cannot write settings'); },
  });
  await tick();
  assert.equal(liveControl(host).checked, false);
  await choose(host, 'Чек-лист', 2); await choose(host, 'Режим оценки', 'accurate');
  await choose(host, 'Модель', 'local'); CQA.bus.emit('checklist:selected', 3); await tick();
  assert.equal(writes, 0);
  const hint = findClass(host, 'cqa-board__plan-hint').textContent;
  assert.match(hint, /до запуска.*недоступ/i);
  assert.doesNotMatch(hint, /\d+\s*(пар|₸|с\b)/);
  assert.match(hint, /времени.*стоимости.*нет/i);
  destroy();
});

test('descendant apply clears/disables live, preserves reuse for weights and a single changed criterion', async () => {
  const calls = [];
  const { host, CQA, destroy } = ui({ checklists: async () => [base, weights, replacement],
    createRun: async body => { calls.push(body); throw new Error('synthetic stop'); },
  });
  await tick(); liveControl(host).checked = true;
  await choose(host, 'Чек-лист', 2);
  assert.equal(liveControl(host).checked, false); assert.equal(liveControl(host).disabled, true);
  await submit(host);
  CQA.bus.emit('checklist:selected', 3); await submit(host);
  assert.deepEqual(calls.map(c => [c.version, c.force, c.live, c.preview]), [[2, false, false, false], [3, false, false, false]]);
  assert.match(findClass(host, 'cqa-board__plan-policy').textContent, /неизменённые.*переиспольз/i);
  assert.match(findClass(host, 'cqa-board__plan-policy').textContent, /вес/i);
  destroy();
});

test('pending new dialogues always request live missing-pair evaluation, including a descendant', async () => {
  const calls = [];
  const { host, destroy } = ui({ conversations: async () => [{ id: 'new', group_id: 'CONTROL', total: null, status: 'pending' }],
    checklists: async () => [base, weights], createRun: async body => { calls.push(body); throw new Error('synthetic stop'); },
  });
  await tick(); assert.equal(liveControl(host).checked, false);
  await choose(host, 'Чек-лист', 2); await submit(host);
  assert.equal(calls[0].live, true); assert.equal(calls[0].force, false); assert.equal(calls[0].preview, false);
  assert.match(findClass(host, 'cqa-board__plan-policy').textContent, /новые диалоги.*сейчас.*не из кэша/i);
  destroy();
});

test('upload then descendant apply cannot turn live into force or leak freshness to another group', async () => {
  const calls = []; let imported = false;
  const oldRow = { id: 'a', group_id: 'OLD', total: 85.38, status: 'evaluated' };
  const { host, destroy } = ui({
    conversations: async () => imported ? [oldRow, { id: 'new', group_id: 'NEW', total: null, status: 'pending' }] : [oldRow],
    checklists: async () => [base, weights], ingest: async () => { imported = true; return { count: 1, group_id: 'NEW' }; },
    createRun: async body => { calls.push(body); throw new Error('synthetic stop'); },
  });
  await tick();
  walk(host).find(n => n.type === 'file').files = [{ size: 20, text: async () => '[{"conversation_id":"new"}]' }];
  await walk(host).find(n => n.tagName === 'BUTTON' && n.textContent === 'Загрузить JSON').dispatch('click'); await tick();
  await choose(host, 'Чек-лист', 2); await submit(host);
  assert.deepEqual(calls[0].conv_ids, ['new']); assert.equal(calls[0].live, true); assert.equal(calls[0].force, false);
  await choose(host, 'Набор', 'OLD'); await submit(host);
  assert.deepEqual(calls[1].conv_ids, ['a']); assert.equal(calls[1].live, false); assert.equal(calls[1].force, false);
  destroy();
});

test('only the explicit full rerun action requests force and it does not persist to normal apply', async () => {
  const calls = [];
  const { host, destroy } = ui({ checklists: async () => [base, weights], createRun: async body => { calls.push(body); throw new Error('synthetic stop'); } });
  await tick(); await choose(host, 'Чек-лист', 2);
  const full = walk(host).find(n => n.tagName === 'BUTTON' && n.textContent === 'Пересчитать всё заново');
  assert.ok(full); assert.equal(full.type, 'button'); assert.ok(!full.className.includes('--primary'));
  await full.dispatch('click'); await tick();
  assert.equal(calls[0].force, true); assert.equal(calls[0].live, true); assert.equal(calls[0].preview, false);
  assert.equal(calls[0].activate, true); assert.deepEqual(calls[0].conv_ids, ['a']);
  await submit(host); assert.equal(calls[1].force, false); assert.equal(calls[1].live, false);
  destroy();
});

test('post-start plan uses actual server pair counts, preserves zero and never mistakes calls for pairs', async () => {
  const { host, CQA, destroy } = ui({ run: async id => ({ run_id: id, status: 'completed', pairs_total: 80,
    pairs_to_evaluate: id === 'weights' ? 0 : 10, llm_calls: 3, from_cache: 4, done: 10, total: 10, per_conv: [] }) });
  await tick(); CQA.bus.emit('run:started', { run_id: 'replacement' }); await tick();
  let text = findClass(host, 'cqa-board__run-plan').textContent;
  assert.match(text, /при запуске/i); assert.match(text, /10 пар/); assert.match(text, /70 пар/);
  assert.doesNotMatch(text, /3 пар|4 пар/);
  CQA.bus.emit('run:started', { run_id: 'weights' }); await tick();
  text = findClass(host, 'cqa-board__run-plan').textContent;
  assert.match(text, /0 пар/); assert.match(text, /80 пар/);
  destroy();
});

test('missing or invalid server plan numbers remain unavailable instead of being inferred', async () => {
  const cases = [ {}, { pairs_total: 80 }, { pairs_total: 80, pairs_to_evaluate: null },
    { pairs_total: 80, pairs_to_evaluate: -1 }, { pairs_total: 80, pairs_to_evaluate: 90 },
    { pairs_total: '80', pairs_to_evaluate: '10' }, { pairs_total: 80, pairs_to_evaluate: 0.5 } ];
  const { host, CQA, destroy } = ui({ run: async id => ({ run_id: id, status: 'completed', total: 10,
    from_cache: 70, llm_calls: 10, cache_total: 80, ...cases[Number(id)] }) });
  await tick();
  for (let i = 0; i < cases.length; i++) {
    CQA.bus.emit('run:started', { run_id: String(i) }); await tick();
    const text = findClass(host, 'cqa-board__run-plan').textContent;
    assert.match(text, /не (передал|сообщил)/i); assert.doesNotMatch(text, /\d+ пар/);
  }
  destroy();
});

test('incomplete scores count as errors; pending and no-applicable null scores do not', () => {
  const summary = runSummary({ per_conv: [
    { id: 'failed', status: 'failed', old_total: 60, new_total: 100 },
    { id: 'partial', status: 'done', old_total: 80, new_total: 100, score: { status: 'incomplete', missing: ['C2'] } },
    { id: 'incomplete', status: 'incomplete', new_total: null },
    { id: 'pending', status: 'pending', new_total: null },
    { id: 'na', status: 'done', new_total: null, score: { status: 'no_applicable' } },
    { id: 'valid', status: 'done', old_total: 0, new_total: 20 },
  ], errors: [{ conv_id: 'failed', error: '402' }, { conv_id: 'outside', error: 'timeout' }] });
  assert.equal(summary.errors, 4, 'errors must be deduplicated by dialogue');
  assert.equal(summary.compared, 1); assert.equal(summary.changed, 1);
  assert.deepEqual(summary.after, [0, 1, 0, 0, 0], 'partial/failure totals cannot enter the chart');
});

test('board exposes counted errors, distinct incomplete tiles and no misleading partial total', async () => {
  const { host, CQA, destroy } = ui({ run: async id => ({ run_id: id, status: 'completed', done: 4, total: 4, per_conv: [
    { id: 'bad', status: 'error', error: 'HTTP 402', new_total: null },
    { id: 'partial', status: 'done', new_total: 100, score: { status: 'incomplete', missing: ['C2', 'C4'] } },
    { id: 'waiting', status: 'pending', new_total: null },
    { id: 'na', status: 'done', new_total: null, score: { status: 'no_applicable' } },
  ] }) });
  await tick(); CQA.bus.emit('run:started', { run_id: 'errors' }); await tick();
  assert.match(findClass(host, 'cqa-board__stats').textContent, /С ошибками2/);
  const tiles = walk(host).filter(n => n.className.split(' ').includes('cqa-board__tile'));
  assert.deepEqual(tiles.map(n => n.dataset.state), ['error', 'incomplete', 'pending', 'done']);
  assert.match(tiles[1].attributes['aria-label'], /неполн/i);
  assert.doesNotMatch(tiles[1].textContent, /100/);
  assert.match(tiles[1].textContent, /C2.*C4/);
  assert.match(findClass(host, 'cqa-board__result-note').textContent, /2/);
  destroy();
});

test('launch response displays its plan immediately and full rerun cannot duplicate a pending start', async () => {
  const pending = deferred(); const calls = [];
  const { host, destroy } = ui({ createRun: async body => { calls.push(body); return pending.promise; } });
  await tick(); const starting = submit(host);
  const full = walk(host).find(n => n.tagName === 'BUTTON' && n.textContent === 'Пересчитать всё заново');
  assert.equal(full.disabled, true); await full.dispatch('click');
  assert.equal(calls.length, 1); assert.equal(calls[0].force, false);
  pending.resolve({ run_id: 'immediate', status: 'completed', pairs_total: 8, pairs_to_evaluate: 0, per_conv: [] });
  await starting;
  assert.match(findClass(host, 'cqa-board__run-plan').textContent, /к оценке 0 пар.*8 пар/);
  destroy();
});

test('all N/A is not considered a new dialogue, and incomplete completion cannot announce activation', async () => {
  const calls = [];
  const { host, CQA, destroy } = ui({ conversations: async () => [{ id: 'a', group_id: 'TEST', total: null, status: 'no_applicable' }],
    createRun: async body => { calls.push(body); return { run_id: 'partial', status: 'incomplete', version: 2, activated: true,
      per_conv: [{ id: 'a', status: 'incomplete' }] }; },
  });
  await tick(); await submit(host);
  assert.equal(calls[0].live, false); assert.equal(calls[0].force, false);
  assert.equal(CQA.bus.events.filter(([n]) => n === 'checklist:activated').length, 0);
  assert.match(findClass(host, 'cqa-board__results').textContent, /Прогон завершился с ошибкой/);
  destroy();
});


test('all four model choices map explicitly to profiles before a run', async () => {
  for (const [choice, expected] of [['cloud', { location: 'cloud', profile: 'cloud' }], ['cascade', { location: 'cloud', profile: 'cascade' }], ['routing', { location: 'cloud', profile: 'routing' }], ['local', { location: 'local', profile: 'local' }]]) {
    const calls = [];
    const { host, destroy } = ui({ setConfig: async body => { calls.push(body); return body; }, createRun: async body => ({ run_id: 'synthetic-' + choice, status: 'completed', total: 1, done: 1 }) });
    await tick();
    const choiceControl = walk(host).find(node => node.tagName === 'SELECT' && node.children.some(option => option.value === 'cascade'));
    choiceControl.value = choice; await choiceControl.dispatch('change');
    await findClass(host, 'cqa-board__form').dispatch('submit');
    assert.deepEqual(calls, [{ mode: 'fast', ...expected }]); destroy();
  }
});

test('routing config is restored as routing with fast mode and no settings write', async () => {
  let writes = 0;
  const { host, destroy } = ui({ config: async () => ({ location: 'cloud', profile: 'routing', mode: 'fast', model: { model: 'Haiku', escalate: 'Gemini' } }), setConfig: async () => { writes++; } });
  await tick();
  const controls = walk(host).filter(node => node.tagName === 'SELECT');
  assert.equal(controls.find(node => node.children.some(option => option.value === 'routing')).value, 'routing');
  assert.equal(controls.find(node => node.children.some(option => option.value === 'accurate')).disabled, true);
  assert.match(findClass(host, 'cqa-board__form').textContent, /C3: Да\/Нет/);
  assert.equal(writes, 0); destroy();
});

test('restoring cascade config keeps the selection and fast mode without an extra settings write', async () => {
  let writes = 0;
  const { host, destroy } = ui({ config: async () => ({ location: 'cloud', profile: 'cascade', mode: 'fast', model: { model: 'Haiku', escalate: 'Gemini' } }), setConfig: async () => { writes++; } });
  await tick();
  const controls = walk(host).filter(node => node.tagName === 'SELECT');
  assert.equal(controls.find(node => node.children.some(option => option.value === 'cascade')).value, 'cascade');
  assert.equal(controls.find(node => node.children.some(option => option.value === 'accurate')).disabled, true);
  assert.match(findClass(host, 'cqa-board__form').textContent, /Сначала Haiku, при сомнении Gemini/);
  assert.equal(writes, 0); destroy();
});


test('HTTP mock server stays mock on unchanged launch, even without is_mock metadata', async () => {
  const calls = [];
  const { host, destroy } = ui({
    config: async () => ({ mode: 'fast', location: 'cloud', profile: 'mock', model: { model: 'mock' } }),
    setConfig: async body => { calls.push(['config', body]); return body; },
    createRun: async body => { calls.push(['run', body]); return { run_id: 'safe-mock', status: 'completed' }; },
  });
  await tick();
  const picker = walk(host).find(node => node.tagName === 'SELECT' && node.children.some(option => option.value === 'cascade'));
  assert.equal(picker.value, 'configured');
  assert.equal(picker.children.find(option => option.value === 'configured').textContent, 'Демо без модели');
  assert.match(findClass(host, 'cqa-board__form').textContent, /не вызывает реальные модели/);
  await findClass(host, 'cqa-board__form').dispatch('submit');
  assert.deepEqual(calls[0], ['config', { mode: 'fast', location: 'cloud', profile: 'mock' }]);
  assert.equal(calls[1][1].profile, 'mock');
  destroy();
});

test('only explicit cloud selection leaves a configured mock or custom server profile', async () => {
  for (const profile of ['mock', 'claude']) {
    const calls = [];
    let config = { mode: 'fast', location: 'cloud', profile };
    const { host, destroy } = ui({
      config: async () => config,
      setConfig: async body => { calls.push(body); config = body; return body; },
      createRun: async () => ({ run_id: 'selected-cloud', status: 'completed' }),
    });
    await tick();
    const picker = walk(host).find(node => node.tagName === 'SELECT' && node.children.some(option => option.value === 'cascade'));
    assert.equal(picker.value, 'configured');
    picker.value = 'cloud'; await picker.dispatch('change');
    await findClass(host, 'cqa-board__form').dispatch('submit');
    assert.deepEqual(calls, [{ mode: 'fast', location: 'cloud', profile: 'cloud' }]);
    assert.equal(picker.value, 'cloud');
    assert.equal(picker.children.some(option => option.value === 'configured'), false, 'stale mock option must disappear once the actual server profile changes');
    destroy();
  }
});
