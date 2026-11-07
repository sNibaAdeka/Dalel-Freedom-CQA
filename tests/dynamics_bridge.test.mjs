import assert from 'node:assert/strict';
import test from 'node:test';
import { readFile } from 'node:fs/promises';

// The repository ships native browser modules without a package.json. Import their actual source.
const asModule = source => `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`;
const bridgeSource = await readFile(new URL('../dynamics/bridge.js', import.meta.url), 'utf8');
const bridgeUrl = asModule(bridgeSource);
const { readChildMessage, createHostBridge, clampHeight, exactOrigin } = await import(bridgeUrl);
const bootstrapSource = (await readFile(new URL('../dynamics/bootstrap.js', import.meta.url), 'utf8')).replace("'./bridge.js'", JSON.stringify(bridgeUrl));
const { resolveBootstrap, applyBootstrap, buildFrameUrl } = await import(asModule(bootstrapSource));
const origin = 'https://cqa.example.test';
const parentOrigin = 'https://org.crm4.dynamics.com';
const apiBase = 'https://api.example.test/cqa/api';
const deployment = { parentOrigins: [parentOrigin], apiBases: [apiBase], mock: false };
const child = {};
const event = (type, payload, overrides = {}) => ({ source: child, origin, data: { source: 'dalel-cqa', type, payload }, ...overrides });
const dataSearch = fields => '?' + new URLSearchParams({ data: new URLSearchParams(fields).toString() });
const settings = { parentOrigin, apiBase, role: 'reviewer' };

test('trusted events are reduced to the existing protocol fields; zero and null scores survive', () => {
  const fixtures = [
    ['ready', { role: 'operator', mock: false }],
    ['resize', { height: 650 }],
    ['score.updated', { conv_id: 'chat_ru_001', total: 0 }],
    ['score.updated', { conv_id: 'chat_ru_001', total: null }],
    ['appeal.created', { id: 'appeal-1', conv_id: 'chat_ru_001' }],
    ['decision.saved', { id: 'decision-1', appeal_id: 'appeal-1' }],
  ];
  for (const [type, payload] of fixtures) assert.deepEqual(readChildMessage(event(type, payload), child, origin), { type, payload });
});

test('forged window, sibling iframe, wrong port, null origin, and namespace cannot impersonate the child', () => {
  const base = event('ready', { role: 'operator', mock: true });
  for (const forged of [
    { ...base, source: {} }, { ...base, source: null },
    { ...base, origin: 'https://evil.example.test' }, { ...base, origin: `${origin}:8443` },
    { ...base, origin: 'null' }, { ...base, origin: '*' },
    { ...base, data: { ...base.data, source: 'dalel-host' } },
  ]) assert.equal(readChildMessage(forged, child, origin), null);
  assert.equal(readChildMessage({ ...base, source: null }, null, origin), null);
});

test('malformed, privileged and content-bearing messages are rejected rather than logged', () => {
  for (const [type, payload] of [
    ['ready', { role: 'admin', mock: false }], ['ready', { role: 'operator', mock: 'false' }],
    ['resize', { height: '600' }], ['resize', { height: Infinity }], ['resize', { height: -1 }],
    ['score.updated', { conv_id: 'a', total: 101 }], ['score.updated', { conv_id: 'a', total: NaN }],
    ['score.updated', { conv_id: 'a', total: 40, transcript: 'private' }],
    ['appeal.created', { id: '', conv_id: 'a' }], ['decision.saved', { id: 'a', appeal_id: 'b', comment: 'private' }],
    ['open', { id: 'a' }], ['transcript', { text: 'private' }], ['ready', []],
  ]) assert.equal(readChildMessage(event(type, payload), child, origin), null, type);
  for (const data of [null, [], 'ready', { source: 'dalel-cqa', type: 'ready', payload: null },
    { source: 'dalel-cqa', type: 'ready', payload: { role: 'operator', mock: true }, token: 'private' }]) {
    assert.equal(readChildMessage({ ...event('ready', {}), data }, child, origin), null);
  }
});

test('resize is finite, rounded up and bounded to 360–1400px', () => {
  assert.equal(clampHeight(0), 360); assert.equal(clampHeight(600.2), 601);
  assert.equal(clampHeight(Number.MAX_VALUE), 1400);
  for (const height of [NaN, Infinity, -Infinity, -1, '800', null, {}, true]) assert.equal(clampHeight(height), null);
});

test('host sends only role and ID after a genuine ready, and cleans up on dispose', () => {
  const sent = [], received = [], handlers = new Set();
  const iframe = { postMessage: (...args) => sent.push(args) };
  const target = { addEventListener: (_, cb) => handlers.add(cb), removeEventListener: (_, cb) => handlers.delete(cb) };
  const bridge = createHostBridge({ eventTarget: target, getChildWindow: () => iframe, childOrigin: origin,
    role: 'reviewer', convId: 'chat-1', onEvent: value => received.push(value) });
  const dispatch = value => { for (const handler of handlers) handler(value); };
  bridge.setContext({ role: 'operator', convId: 'chat-2' });
  dispatch(event('ready', { role: 'operator', mock: false })); // correct origin, wrong source
  assert.equal(bridge.ready, false); assert.equal(sent.length, 0);
  const ready = event('ready', { role: 'operator', mock: false }, { source: iframe });
  dispatch(ready);
  assert.deepEqual(sent, [
    [{ source: 'dalel-host', type: 'setRole', payload: { role: 'operator' } }, origin],
    [{ source: 'dalel-host', type: 'open', payload: { id: 'chat-2' } }, origin],
  ]);
  bridge.setContext({ role: 'reviewer', convId: '' });
  assert.equal(sent.length, 3); assert.equal(sent[2][0].type, 'setRole');
  assert.throws(() => bridge.setContext({ role: 'admin', convId: 'chat-2' }));
  assert.throws(() => bridge.setContext({ role: 'operator', convId: '<private text>' }));
  bridge.reset(); assert.equal(bridge.ready, false);
  bridge.setContext({ role: 'reviewer', convId: 'chat-3' }); assert.equal(sent.length, 3);
  dispatch(ready); assert.equal(sent.at(-1)[0].payload.id, 'chat-3');
  bridge.dispose(); assert.equal(handlers.size, 0); assert.equal(bridge.ready, false);
  bridge.setContext({ role: 'operator', convId: 'chat-4' }); assert.equal(sent.length, 5);
  assert.equal(received.length, 2);
});

test('exact origin rejects wildcard, credentials, paths and opaque schemes', () => {
  assert.equal(exactOrigin(parentOrigin), parentOrigin);
  for (const value of ['*', 'null', `${parentOrigin}/`, `${parentOrigin}/form`, 'file:///tmp/app', 'javascript:alert(1)', 'https://u:p@org.crm4.dynamics.com']) {
    assert.throws(() => exactOrigin(value));
  }
});

test('bootstrap serializes one Dynamics data parameter and locks transport to deployment values', () => {
  const url = buildFrameUrl(`${origin}/WebResources/new_/web/index.html?mode=demo`, settings, deployment);
  assert.deepEqual([...url.searchParams.keys()], ['data']);
  assert.deepEqual(resolveBootstrap(url.search, deployment), {
    ...settings, mock: false, mockBase: null, fallbackToMock: false, offline: false,
  });
  const win = { location: { search: url.search }, CQA_CONFIG: { modules: ['card'], mock: true, mockBase: './private', fallbackToMock: true } };
  applyBootstrap(win, deployment);
  assert.deepEqual(win.CQA_CONFIG.modules, ['card']); assert.equal(win.CQA_CONFIG.mock, false);
  assert.equal(win.CQA_CONFIG.mockBase, null); assert.equal(win.CQA_CONFIG.fallbackToMock, false);
});

test('query cannot add an API endpoint, trusted parent, auth token, duplicate key or elevated role', () => {
  const badFields = [
    { ...settings, apiBase: 'https://evil.example.test/api' },
    { ...settings, apiBase: 'https://api.example.test/other' },
    { ...settings, apiBase: apiBase + '?token=x' },
    { ...settings, apiBase: 'https://u:p@api.example.test/cqa/api' },
    { ...settings, parentOrigin: '*' }, { ...settings, parentOrigin: 'https://other.crm4.dynamics.com' },
    { ...settings, role: 'admin' }, { ...settings, mock: 'true' }, { ...settings, token: 'x' },
    { ...settings, convId: 'do-not-put-records-in-urls' },
  ];
  for (const fields of badFields) assert.throws(() => resolveBootstrap(dataSearch(fields), deployment));
  for (const search of ['', '?data=', '?data=a&data=b', '?data=' + 'a'.repeat(2049),
    dataSearch([...Object.entries(settings), ['role', 'operator']])]) assert.throws(() => resolveBootstrap(search, deployment));
  assert.throws(() => buildFrameUrl('javascript:alert(1)', settings, deployment));
});

test('URL encoding roundtrips once and never authorizes a second encoded URL', () => {
  const local = { parentOrigins: ['http://127.0.0.1:8050'], apiBases: ['http://127.0.0.1:8047/api'], mock: true };
  const localSettings = { parentOrigin: local.parentOrigins[0], apiBase: local.apiBases[0], role: 'operator' };
  assert.equal(resolveBootstrap(dataSearch(localSettings), local).apiBase, local.apiBases[0]);
  assert.throws(() => resolveBootstrap(dataSearch({ ...settings, apiBase: encodeURIComponent(apiBase) }), deployment));
});
