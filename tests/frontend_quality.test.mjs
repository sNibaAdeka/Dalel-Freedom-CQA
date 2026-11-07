import assert from 'node:assert/strict';
import test from 'node:test';
import { readFile } from 'node:fs/promises';

const moduleUrl = new URL('../web/js/quality.js', import.meta.url);
const source = (await readFile(moduleUrl, 'utf8')).replaceAll('import.meta.url', JSON.stringify(moduleUrl.href));
const { validateQuality, agreement, formatPercent, reportCaveats, mount } = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);

// Invented aggregates only. No transcript, provider or saved real measurement.
export function fixture() {
  const counts = (matched, missing = 0, errors = 0, unlabelled = 0) => ({ matched, total: 3, missing, errors, unlabelled });
  return {
    id: 'synthetic-report', run_id: 'synthetic-run', measured_at: '2026-01-02T10:00:00Z',
    source: { kind: 'cli', label: 'Synthetic CLI fixture', artifact: 'synthetic-summary.json' },
    model: { requested: 'synthetic-model', resolved: null }, mode: 'fast', guidance_version: 'synthetic-g1',
    checklist_version: 'synthetic-v1', cache: 'unknown',
    reference: { id: 'synthetic-reference', type: 'machine', cells: 12, annotator_agreement: { matched: 9, total: 12 } },
    sample: { dataset: 'synthetic DEV', split: 'holdout', dialogues: 3, dev_reused: true, used_for_tuning: false, untouched: false },
    denominator: 'all_cells', counts: { matched: 8, total: 12, missing: 1, errors: 1, unlabelled: 1 },
    score_mae: { value: 2.5, count: 1 }, latency: { p50_seconds: 4, p95_seconds: 9, count: 3, scope: 'cli_wall' },
    criteria: [
      { id: 'X1', label: 'Synthetic contact', counts: counts(2, 1) },
      { id: 'X2', counts: counts(2, 0, 1) },
      { id: 'X3', counts: counts(2, 0, 0, 1) },
      { id: 'X4', counts: counts(2) },
    ],
    confusion: [
      { criterion_id: 'X1', rows: ['yes', 'no'], columns: ['yes', 'no', '__missing__'], values: [[1, 0, 1], [0, 1, 0]] },
      { criterion_id: 'X3', rows: ['yes', '__unlabelled__'], columns: ['yes', 'no'], values: [[2, 0], [0, 1]] },
    ],
    auto_approved: { counts: { matched: 3, total: 4, missing: 0, errors: 0, unlabelled: 0 } },
  };
}
const wrap = report => ({ schema_version: 1, reports: [report] });
const check = change => { const report = fixture(); change(report); return validateQuality(wrap(report)); };

test('parent reports envelope accepts empty only as an empty reports array', () => {
  assert.equal(validateQuality({ reports: [] }).state, 'empty');
  assert.equal(validateQuality({ schema_version: 1, reports: [] }).state, 'empty');
  for (const value of [null, undefined, [], {}, { reports: null }, { measurements: [] }, { schema_version: 2, reports: [] }]) {
    assert.equal(validateQuality(value).state, 'invalid');
  }
});

test('valid synthetic report is immutable and keeps unknown resolved model', () => {
  const input = wrap(fixture()), original = structuredClone(input);
  const result = validateQuality(input);
  assert.equal(result.state, 'ready');
  assert.deepEqual(result.issues, []);
  assert.deepEqual(input, original);
  assert.equal(result.reports[0].model.resolved, null);
});

test('all planned cells denominator includes missing predictions, errors and unlabelled cells', () => {
  const metric = agreement(fixture().counts);
  assert.equal(metric.all, 8 / 12);
  assert.equal(metric.labelled, 8 / 11);
  assert.equal(metric.referenceCoverage, 11 / 12);
  assert.equal(metric.disagreed, 1);
  assert.equal(formatPercent(8, 12), '66,7%');
  assert.equal(formatPercent(0, 12), '0%');
  assert.equal(formatPercent(0, 0), 'не измерено');
  assert.equal(agreement({ total: 0, matched: 0, missing: 0, errors: 0 }).all, null);
});

test('omitted unlabelled defaults to zero, never guesses an expected denominator', () => {
  const report = fixture(); delete report.criteria; delete report.confusion;
  delete report.counts.unlabelled;
  const result = validateQuality(wrap(report));
  assert.equal(result.state, 'ready');
  assert.equal(result.reports[0].counts.unlabelled, 0);
  assert.equal(result.reports[0].counts.total, 12);
});

const invalidCases = [
  ['matched exceeds available cells', r => { r.counts.matched = 12; }],
  ['negative error count', r => { r.counts.errors = -1; }],
  ['fractional cell count', r => { r.counts.total = 12.5; }],
  ['unsafe integer', r => { r.counts.total = Number.MAX_SAFE_INTEGER + 1; }],
  ['string count', r => { r.counts.total = '12'; }],
  ['non-finite count', r => { r.counts.missing = NaN; }],
  ['answered-only denominator', r => { r.denominator = 'answered_only'; }],
  ['missing run provenance', r => { delete r.run_id; }],
  ['timezone missing', r => { r.measured_at = '2026-01-02T10:00:00'; }],
  ['invalid calendar date', r => { r.measured_at = '2026-02-31T10:00:00Z'; }],
  ['empty source', r => { r.source.label = ''; }],
  ['unrecognised source', r => { r.source.kind = 'magic'; }],
  ['missing resolved-model disclosure', r => { delete r.model.resolved; }],
  ['unknown cache enum', r => { r.cache = 'probably_fresh'; }],
  ['reference smaller than available labels', r => { r.reference.cells = 8; }],
  ['invented reference type', r => { r.reference.type = 'gold'; }],
  ['annotator agreement impossible', r => { r.reference.annotator_agreement.matched = 13; }],
  ['reused holdout called untouched', r => { r.sample.untouched = true; }],
  ['tune called untouched', r => { r.sample = { ...r.sample, split: 'tune', dev_reused: false, untouched: true }; }],
  ['missing reuse provenance', r => { delete r.sample.dev_reused; }],
  ['untouched with unknown tuning', r => { r.sample = { ...r.sample, dev_reused: false, untouched: true, used_for_tuning: null }; }],
  ['cells without dialogues', r => { r.sample.dialogues = 0; }],
  ['MAE has no paired totals', r => { r.score_mae.count = 0; }],
  ['MAE exceeds scale', r => { r.score_mae.value = 101; }],
  ['MAE coverage exceeds sample', r => { r.score_mae.count = 4; }],
  ['p95 below median', r => { r.latency.p95_seconds = 3; }],
  ['latency observations without either percentile', r => { r.latency.p50_seconds = null; r.latency.p95_seconds = null; }],
  ['latency exceeds sample', r => { r.latency.count = 4; }],
  ['CLI labelled as API e2e', r => { r.latency.scope = 'api_e2e'; }],
  ['unknown cost silently zero', r => { r.cost = { amount: 0, currency: 'USD', count: 0, basis: 'unknown' }; }],
  ['known cost with no currency', r => { r.cost = { amount: 1, currency: null, count: 3, basis: 'reported' }; }],
  ['cost without observations', r => { r.cost = { amount: 1, currency: 'USD', count: 0, basis: 'reported' }; }],
  ['criteria lose a row', r => { r.criteria.pop(); }],
  ['duplicate criterion', r => { r.criteria[1].id = 'X1'; }],
  ['language partition loses cells', r => { r.languages = [{ id: 'unknown', counts: { matched: 1, total: 1, missing: 0, errors: 0 } }]; }],
  ['auto-approved contains errors', r => { r.auto_approved.counts.errors = 1; }],
  ['auto-approved exceeds overall matches', r => { r.auto_approved.counts = { matched: 9, total: 9, missing: 0, errors: 0 }; }],
  ['confusion loses a missing prediction', r => { r.confusion[0].values[0][2] = 0; }],
  ['confusion silently swaps axes', r => { r.confusion[0].values = [[1, 1, 0], [0, 1, 0]]; }],
  ['confusion missing criterion', r => { r.confusion[0].criterion_id = 'absent'; }],
  ['confusion not rectangular', r => { r.confusion[0].values[0].pop(); }],
  ['duplicate confusion label', r => { r.confusion[0].columns[1] = 'yes'; }],
];
for (const [name, change] of invalidCases) test(`rejects ${name}`, () => {
  const result = check(change);
  assert.equal(result.state, 'invalid');
  assert.deepEqual(result.reports, []);
  assert.ok(result.issues[0].startsWith('reports[0].'));
});

test('duplicate report ids cannot masquerade as two independent runs', () => {
  assert.equal(validateQuality({ reports: [fixture(), fixture()] }).state, 'invalid');
});

test('explicit zero cost and zero errors are preserved, unknown cost stays null', () => {
  for (const cost of [null, { amount: null, currency: null, count: 0, basis: 'unknown' }, { amount: 0, currency: 'KZT', count: 3, basis: 'reported' }]) {
    assert.equal(check(r => { r.cost = cost; }).state, 'ready');
  }
});

test('unmeasured p95 stays unknown even when p50 is available', () => {
  const result = check(r => { r.latency.p95_seconds = null; });
  assert.equal(result.state, 'ready');
  assert.equal(result.reports[0].latency.p95_seconds, null);
  assert.equal(result.reports[0].latency.p50_seconds, 4);
});

test('partial MAE, reused DEV and CLI scope produce caveats without an untouched claim', () => {
  const text = reportCaveats(validateQuality(wrap(fixture())).reports[0]).join(' ');
  assert.match(text, /DEV.*повторно/);
  assert.match(text, /holdout.*не.*нетронут/i);
  assert.match(text, /MAE.*1 из 3/);
  assert.match(text, /CLI.*API/);
  assert.match(text, /без.*эталон/);
});

test('genuinely declared untouched control has a qualified provenance statement', () => {
  const result = check(r => { r.sample = { ...r.sample, split: 'control', dev_reused: false, used_for_tuning: false, untouched: true }; });
  assert.equal(result.state, 'ready');
  assert.match(reportCaveats(result.reports[0]).join(' '), /По данным источника/);
});

test('unknown tuning history is not interpreted as independent evaluation', () => {
  const result = check(r => { r.sample = { ...r.sample, dev_reused: false, used_for_tuning: null, untouched: null }; });
  assert.equal(result.state, 'ready');
  assert.match(reportCaveats(result.reports[0]).join(' '), /неизвест/);
});

test('all unlabelled is no labelled agreement, all missing predictions is zero all-cells agreement', () => {
  assert.equal(agreement({ total: 4, matched: 0, missing: 0, errors: 0, unlabelled: 4 }).labelled, null);
  assert.equal(agreement({ total: 4, matched: 0, missing: 4, errors: 0 }).all, 0);
});

test('valid complete language/channel partitions and estimated cost remain distinct', () => {
  const result = check(r => {
    r.languages = [{ id: 'unknown', counts: { ...r.counts } }];
    r.channels = [{ id: 'synthetic', counts: { ...r.counts } }];
    r.cost = { amount: 3, currency: 'USD', count: 3, basis: 'estimated' };
  });
  assert.equal(result.state, 'ready');
  assert.equal(result.reports[0].cost.basis, 'estimated');
});

// Small isolated DOM fixture tests content and lifecycle, not browser layout.
class Element {
  constructor(tag) {
    this.tagName = tag.toUpperCase(); this.children = []; this.attributes = new Map(); this.listeners = new Map(); this.className = ''; this.ownText = '';
    this.classList = { add: value => { this.className += ` ${value}`; }, remove: value => { this.className = this.className.split(' ').filter(x => x !== value).join(' '); } };
  }
  set innerHTML(_) { throw new Error('Untrusted HTML insertion is forbidden in the fixture'); }
  set textContent(value) { this.ownText = String(value); this.children = []; }
  get textContent() { return this.ownText + this.children.map(child => child.textContent).join(''); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.ownText = ''; this.children = children; }
  setAttribute(key, value) { this.attributes.set(key, String(value)); }
  getAttribute(key) { return this.attributes.get(key) ?? null; }
  addEventListener(key, fn) { if (!this.listeners.has(key)) this.listeners.set(key, new Set()); this.listeners.get(key).add(fn); }
  removeEventListener(key, fn) { this.listeners.get(key)?.delete(fn); }
  click() { if (!this.disabled) this.listeners.get('click')?.forEach(fn => fn()); }
}
function descendants(root, tag) { return root.children.flatMap(child => [...(child.tagName === tag.toUpperCase() ? [child] : []), ...descendants(child, tag)]); }
function setup(t, quality, economics) {
  const oldDoc = globalThis.document, oldFetch = globalThis.fetch;
  const host = new Element('div'), head = new Element('head'), listeners = new Map();
  globalThis.document = { head, createElement: tag => new Element(tag), getElementById: id => id === 'cqa-quality' ? host : null,
    querySelector: () => head.children.find(child => child.getAttribute('data-cqa-quality-style') !== null) ?? null };
  globalThis.fetch = () => { throw new Error('No direct network access in the module'); };
  const bus = {
    on(name, fn) { if (!listeners.has(name)) listeners.set(name, new Set()); listeners.get(name).add(fn); return () => listeners.get(name).delete(fn); },
    emit(name, value) { listeners.get(name)?.forEach(fn => fn(value)); },
  };
  let calls = 0;
  const CQA = { api: quality ? { quality(...args) { assert.equal(args.length, 0); calls++; return quality(); } } : {}, bus };
  if (economics) CQA.economics = economics;
  const dispose = mount(CQA);
  t.after(() => { dispose(); globalThis.document = oldDoc; globalThis.fetch = oldFetch; });
  return { host, head, bus, CQA, dispose, listeners, calls: () => calls };
}
const settle = () => new Promise(resolve => setImmediate(resolve));
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };

test('calculator uses only a valid loaded report and is torn down before refresh or destroy', async t => {
  let payload = wrap(fixture()), mounts = 0, destroyed = 0;
  const { bus, dispose } = setup(t, async () => payload, {
    mountCalculator: (_host, options) => { mounts++; assert.equal(options.quality, payload); return { destroy: () => destroyed++ }; },
  });
  await settle(); assert.equal(mounts, 1);
  payload = {}; bus.emit('quality:refresh'); assert.equal(destroyed, 1); await settle(); assert.equal(mounts, 1);
  payload = wrap(fixture()); bus.emit('quality:refresh'); await settle(); assert.equal(mounts, 2);
  dispose(); assert.equal(destroyed, 2);
});

test('mount empty: exactly one adapter call, no invented measurement and accessible status', async t => {
  const { host, head, calls } = setup(t, async () => ({ reports: [] }));
  assert.match(host.textContent, /Загрузка замеров/);
  await settle();
  assert.equal(calls(), 1);
  assert.match(host.textContent, /Замеров пока нет/);
  assert.equal(descendants(host, 'table').length, 0);
  assert.equal(descendants(head, 'link').length, 1);
  assert.ok(descendants(host, 'p').some(el => el.getAttribute('role') === 'status'));
  assert.equal(descendants(host, 'button')[0].disabled, false);
});

test('missing adapter is a connection error, not a successful empty dataset', async t => {
  const { host } = setup(t);
  await settle();
  assert.match(host.textContent, /Не удалось загрузить/);
  assert.doesNotMatch(host.textContent, /Замеров пока нет/);
});

test('rendering exposes all denominators, source scope, unknown model and missing p95', async t => {
  const r = fixture(); r.latency.p95_seconds = null;
  const { host } = setup(t, async () => wrap(r));
  await settle();
  for (const pattern of [/66,7%/, /8 \/ 12/, /8 \/ 11/, /1 \/ 3/, /4 с \/ не измерено/, /фактическая: неизвестна/, /Совпадение с машинным эталоном/, /Использование кэша неизвестно/, /СтоимостьНе приравнивается к нулюнеизвестна/]) assert.match(host.textContent, pattern);
  assert.doesNotMatch(host.textContent, /Точность модели/);
  for (const tbl of descendants(host, 'table')) {
    assert.equal(descendants(tbl, 'caption').length, 1);
    assert.ok(descendants(tbl, 'th').every(cell => ['row', 'col'].includes(cell.scope)));
  }
});

test('invalid successful response never renders trusted-looking percentages', async t => {
  const r = fixture(); r.counts.total = 1;
  const { host } = setup(t, async () => wrap(r)); await settle();
  assert.match(host.textContent, /не соответствуют контракту/);
  assert.equal(descendants(host, 'table').length, 0);
});

test('refresh failure clears stale values and does not expose raw provider error', async t => {
  let failNext = false;
  const { host, bus } = setup(t, async () => { if (failNext) throw new Error('private-upstream-debug'); return wrap(fixture()); });
  await settle(); assert.match(host.textContent, /66,7%/);
  failNext = true; bus.emit('quality:refresh');
  assert.doesNotMatch(host.textContent, /66,7%/);
  await settle();
  assert.match(host.textContent, /Не удалось загрузить/);
  assert.doesNotMatch(host.textContent, /private-upstream-debug|66,7%/);
});

test('newer response wins and dispose prevents a pending response from rebuilding the UI', async t => {
  const a = deferred(), b = deferred(), c = deferred(), queue = [a, b, c];
  const { host, bus, dispose } = setup(t, () => queue.shift().promise);
  bus.emit('quality:refresh'); b.resolve(wrap(fixture())); await settle();
  a.resolve({ reports: [] }); await settle(); assert.match(host.textContent, /66,7%/);
  bus.emit('quality:refresh'); dispose(); c.resolve(wrap(fixture())); await settle();
  assert.equal(host.children.length, 0);
});

test('mount is idempotent; events reload only the quality read and cleanup removes listeners', async t => {
  const { host, CQA, bus, calls, dispose, listeners } = setup(t, async () => ({ reports: [] }));
  assert.equal(mount(CQA), dispose); await settle(); assert.equal(calls(), 1);
  bus.emit('tab:changed', 'board'); bus.emit('run:completed'); await settle(); assert.equal(calls(), 1);
  bus.emit('tab:changed', 'quality'); await settle(); assert.equal(calls(), 2);
  bus.emit('api:mode'); await settle(); assert.equal(calls(), 3);
  descendants(host, 'button')[0].click(); await settle(); assert.equal(calls(), 4);
  dispose(); bus.emit('quality:refresh'); await settle(); assert.equal(calls(), 4);
  assert.ok([...listeners.values()].every(set => set.size === 0));
});

test('source strings are rendered as text and cannot add markup or links', async t => {
  const r = fixture(); r.source.artifact = '<img src=x onerror=bad()>javascript:bad()'; r.run_id = '<script>bad()</script>';
  const { host } = setup(t, async () => wrap(r)); await settle();
  assert.match(host.textContent, /<img src=x/);
  assert.match(host.textContent, /<script>bad/);
  assert.equal(descendants(host, 'img').length, 0);
  assert.equal(descendants(host, 'script').length, 0);
  assert.equal(descendants(host, 'a').length, 0);
});

test('no host is a safe no-op with no adapter call', () => {
  assert.equal(globalThis.document, undefined);
  const dispose = mount({ api: { quality: () => { throw new Error('must not be called'); } } });
  assert.equal(typeof dispose, 'function'); dispose();
});

// W8 invented aggregates: analyst extensions have no dialogue text or provider access.
function interval(lower = 0.4, upper = 0.95) {
  return { lower, upper, level: 0.95, method: 'dialogue_cluster_percentile', repetitions: 10000, seed: 17, unit: 'dialogue' };
}
function analystFixture() {
  const rows = [];
  for (const reference of ['silver', 'gold_v2_full']) {
    for (const name of ['gemini', 'cascade', 'haiku']) {
      const r = fixture(); r.id = `${name}-${reference}`; r.run_id = `${name}-run`;
      r.reference.id = reference; r.model = { requested: name === 'gemini' ? 'openai:google/gemini-fixture' : `openai:anthropic/${name}-fixture`, resolved: null };
      r.agreement_ci = interval(); r.all_cells_ci = interval();
      r.languages = [
        { id: 'ru', label: 'RU', dialogues: 2, counts: { total: 8, matched: 5, missing: 1, errors: 1, unlabelled: 0 }, agreement_ci: interval(), all_cells_ci: interval() },
        { id: 'kz', label: 'KZ', dialogues: 1, counts: { total: 4, matched: 3, missing: 0, errors: 0, unlabelled: 1 } },
      ];
      r.channels = [{ id: 'chat', label: 'Чат', dialogues: 3, counts: { ...r.counts }, agreement_ci: interval(), all_cells_ci: interval() }];
      if (name === 'gemini') r.cost = { amount: 0.3, currency: 'USD', count: 3, basis: 'estimated' };
      rows.push(r);
    }
  }
  return {
    schema_version: 1, reports: rows,
    profile_catalog: { cloud: { report_id: 'gemini-silver' }, cascade: { report_id: 'cascade-silver' } },
    comparisons: ['silver', 'gold_v2_full'].flatMap(reference_id => ['cascade', 'haiku'].map(name => ({
      report_id: `${name}-${reference_id}`, baseline_report_id: `gemini-${reference_id}`, reference_id,
      win: 1, loss: 1, compared: 11, dialogues: 3, delta: 0, delta_ci: interval(-0.1, 0.1), p_value: 1, significant: false, conclusion: 'within_noise', exploratory: true,
    }))),
    stability: [{ id: 'repeated', left_run_id: 'cascade-run', right_run_id: 'haiku-run', matched: 10, total: 12, dialogues: 3, agreement_ci: interval(0.6, 1), same_configuration: false, configuration_note: 'Synthetic transport difference' }],
  };
}

test('analyst extensions preserve labelled vs all-cell interval and true group dialogue counts', () => {
  const input = analystFixture(), before = structuredClone(input), result = validateQuality(input);
  assert.equal(result.state, 'ready', result.issues.join(';'));
  assert.deepEqual(input, before);
  assert.equal(result.reports[0].languages[1].dialogues, 1);
  assert.equal(result.reports[0].languages[1].agreement_ci, null);
  assert.equal(result.comparisons[0].delta, 0);
  assert.equal(result.comparisons[0].significant, false);
  assert.equal(result.stability[0].same_configuration, false);
});

for (const [title, change] of [
  ['reversed CI', p => { p.reports[0].agreement_ci.lower = 0.99; }],
  ['wrong interval level', p => { p.reports[0].agreement_ci.level = 0.5; }],
  ['cell bootstrap presented as dialogue bootstrap', p => { p.reports[0].agreement_ci.unit = 'cell'; }],
  ['infinite interval', p => { p.reports[0].agreement_ci.upper = Infinity; }],
  ['negative percentage interval', p => { p.reports[0].agreement_ci.lower = -0.1; }],
  ['group dialogue partition mismatch', p => { p.reports[0].languages[1].dialogues = 2; }],
  ['paired delta cannot disagree with wins and losses', p => { p.comparisons[0].delta = 0.4; }],
  ['missing baseline', p => { p.comparisons[0].baseline_report_id = 'not-measured'; }],
  ['cross-reference paired comparison', p => { p.comparisons[0].baseline_report_id = 'gemini-gold_v2_full'; }],
  ['paired cells exceed supported labels', p => { p.comparisons[0].compared = 12; }],
  ['invented significance', p => { p.comparisons[0].significant = true; p.comparisons[0].conclusion = 'difference_detected'; }],
  ['concealed exploratory inference', p => { p.comparisons[0].exploratory = false; }],
  ['impossible repeated matches', p => { p.stability[0].matched = 13; }],
  ['unknown stability run', p => { p.stability[0].left_run_id = 'missing'; }],
  ['unmeasured recommended model', p => { p.profile_catalog.cloud.report_id = 'missing'; }],
]) test(`rejects analyst ${title}`, () => {
  const payload = analystFixture(); change(payload);
  const result = validateQuality(payload); assert.equal(result.state, 'invalid'); assert.deepEqual(result.reports, []);
});

test('analyst render shows uncertainty, sample sizes, unknown cost and non-equivalence', async t => {
  const { host } = setup(t, async () => analystFixture()); await settle();
  for (const pattern of [/95% ДИ/, /KZ/, /1 диалог/, /11 общих ячеек/, /Различие в пределах шума/, /McNemar p=1/, /наблюдаемое совпадение.*такое же/, /эквивалентность не доказана/, /Стоимость каскада не восстановлена/, /Разные конфигурации \/ транспорт/, /83,3%/, /10 \/ 12/]) assert.match(host.textContent, pattern);
  assert.doesNotMatch(host.textContent, /2,2 раза|2.2 раза|доказана эквивалентность|та же точность/);
  assert.equal(descendants(host, 'select').length, 2);
  for (const select of descendants(host, 'select')) assert.ok(descendants(host, 'label').some(label => label.getAttribute('for') === select.id));
  const full = descendants(host, 'details').find(el => el.className.includes('all-reports'));
  assert.ok(full); assert.notEqual(full.open, true);
});

test('reference and paired-model selectors update locally without network requests', async t => {
  const { host, calls } = setup(t, async () => analystFixture()); await settle();
  const reference = descendants(host, 'select').find(el => el.id === 'cqa-quality-reference');
  reference.value = 'gold_v2_full'; reference.listeners.get('change').forEach(fn => fn());
  const selected = descendants(host, 'select').find(el => el.id === 'cqa-quality-comparison');
  assert.equal(selected.value, 'cascade-gold_v2_full');
  selected.value = 'haiku-gold_v2_full'; selected.listeners.get('change').forEach(fn => fn());
  assert.equal(calls(), 1);
  assert.match(host.textContent, /gold_v2_full/);
});

test('cost advantage appears only for complete comparable measured costs', async t => {
  const payload = analystFixture();
  for (const r of payload.reports.filter(r => r.run_id === 'cascade-run')) r.cost = { amount: 0.15, currency: 'USD', count: 3, basis: 'estimated' };
  const { host } = setup(t, async () => payload); await settle();
  assert.match(host.textContent, /в 2 раза дешевле/);
});

test('partial cost coverage does not become a headline savings ratio', async t => {
  const payload = analystFixture();
  payload.reports[1].cost = { amount: 0.15, currency: 'USD', count: 2, basis: 'estimated' };
  const { host } = setup(t, async () => payload); await settle();
  assert.doesNotMatch(host.textContent, /раза дешевле/);
  assert.match(host.textContent, /Полнота и сопоставимость цен требуют проверки/);
});

test('single-dialogue slices explicitly withhold confidence intervals', async t => {
  const payload = analystFixture(), one = payload.reports[0].languages[1];
  one.agreement_ci = { ...interval(null, null), valid_repetitions: 0, reason: 'insufficient_clusters' };
  const result = validateQuality(payload); assert.equal(result.state, 'ready', result.issues.join(';'));
  const { host } = setup(t, async () => payload); await settle();
  assert.match(host.textContent, /1 диалог/);
  assert.match(host.textContent, /ДИ не оценивается: мало диалогов/);
});

test('one dialogue cannot present a precise confidence interval', () => {
  const payload = analystFixture(); payload.reports[0].languages[1].agreement_ci = interval(1, 1);
  assert.equal(validateQuality(payload).state, 'invalid');
});

test('cost audit presents coverage and bounds without substituting a cost estimate', async t => {
  const payload = analystFixture(); payload.reports[1].cost_audit = { known_dialogues: 2, unknown_dialogues: 1, total_dialogues: 3, reasons: ['combined_stage_tokens'], lower_bound_usd: 0.01, upper_bound_usd: 0.4 };
  const { host } = setup(t, async () => payload); await settle();
  assert.match(host.textContent, /Аудит стоимости: восстановлено 2 \/ 3/);
  assert.match(host.textContent, /это границы, не точечная оценка/);
  assert.doesNotMatch(host.textContent, /раза дешевле/);
});

test('cost audit cannot claim contradictory coverage', () => {
  const payload = analystFixture(); payload.reports[1].cost_audit = { known_dialogues: 2, unknown_dialogues: 2, total_dialogues: 3, reasons: [], lower_bound_usd: null, upper_bound_usd: null };
  assert.equal(validateQuality(payload).state, 'invalid');
});

test('valid eight-decimal paired deltas remain compatible with deterministic producer output', () => {
  const payload = analystFixture(), c = payload.comparisons[0];
  c.win = 0; c.loss = 1; c.delta = -0.09090909;
  assert.equal(validateQuality(payload).state, 'ready');
});

test('matching cell p-value without supporting cluster CI cannot become significant', () => {
  const payload = analystFixture(), c = payload.comparisons[0];
  c.p_value = 0.01; c.significant = true; c.conclusion = 'difference_detected';
  assert.equal(validateQuality(payload).state, 'invalid');
});

for (const [title, effective] of [['negative', -1], ['fractional', 1.5], ['null', null], ['string', '2'], ['nonfinite', Infinity], ['above planned sample', 4], ['zero with labelled cells', 0], ['one falsely precise cluster', 1]]) {
  test(`effective dialogue validation rejects ${title}`, () => {
    const payload = analystFixture(); payload.reports[0].agreement_ci.effective_dialogues = effective;
    assert.equal(validateQuality(payload).state, 'invalid');
  });
}

test('many planned dialogues with only one effective labelled cluster withhold CI', () => {
  const payload = analystFixture(), ci = payload.reports[0].agreement_ci;
  Object.assign(ci, { effective_dialogues: 1, lower: null, upper: null, valid_repetitions: 0, reason: 'insufficient_clusters' });
  const result = validateQuality(payload);
  assert.equal(result.state, 'ready', result.issues.join(';'));
  assert.equal(result.reports[0].sample.dialogues, 3);
  assert.equal(result.reports[0].agreement_ci.effective_dialogues, 1);
  assert.equal(result.reports[0].agreement_ci.lower, null);
});

test('a multi-cluster CI cannot claim the one-cluster unavailability reason', () => {
  const payload = analystFixture();
  Object.assign(payload.reports[0].agreement_ci, { effective_dialogues: 2, lower: null, upper: null, valid_repetitions: 0, reason: 'insufficient_clusters' });
  assert.equal(validateQuality(payload).state, 'invalid');
});

test('effective cluster support is retained for comparisons, stability and groups', () => {
  const payload = analystFixture();
  payload.reports[0].languages[0].agreement_ci.effective_dialogues = 2;
  payload.comparisons[0].delta_ci.effective_dialogues = 3;
  payload.stability[0].agreement_ci.effective_dialogues = 3;
  const result = validateQuality(payload); assert.equal(result.state, 'ready');
  assert.equal(result.reports[0].languages[0].agreement_ci.effective_dialogues, 2);
  assert.equal(result.comparisons[0].delta_ci.effective_dialogues, 3);
  assert.equal(result.stability[0].agreement_ci.effective_dialogues, 3);
  payload.reports[0].languages[0].agreement_ci.effective_dialogues = 3;
  assert.equal(validateQuality(payload).state, 'invalid');
});

test('no labelled cells requires zero effective clusters and null interval bounds', () => {
  const r = fixture();
  for (const key of ['criteria', 'confusion', 'auto_approved']) delete r[key];
  r.counts = { total: 12, matched: 0, missing: 0, errors: 0, unlabelled: 12 };
  r.score_mae = { value: null, count: 0 };
  r.agreement_ci = { ...interval(null, null), effective_dialogues: 0, valid_repetitions: 0, reason: 'no_labelled_cells' };
  assert.equal(validateQuality(wrap(r)).state, 'ready');
  r.agreement_ci.effective_dialogues = 1;
  assert.equal(validateQuality(wrap(r)).state, 'invalid');
});

test('v1 intervals without effective-dialogue metadata remain valid', () => {
  const result = validateQuality(analystFixture());
  assert.equal(result.state, 'ready');
  assert.equal(result.reports[0].agreement_ci.effective_dialogues, null);
});

function fourProfileFixture() {
  const payload = analystFixture();
  for (const reference_id of ['silver', 'gold_v2_full']) {
    const r = structuredClone(payload.reports.find(row => row.id === `cascade-${reference_id}`));
    r.id = `routing-${reference_id}`; r.run_id = 'routing-run';
    payload.reports.push(r);
    const comparison = structuredClone(payload.comparisons.find(row => row.report_id === `cascade-${reference_id}`));
    comparison.report_id = r.id; payload.comparisons.push(comparison);
  }
  payload.profile_catalog.routing = { label: 'Маршрутизация', report_id: 'routing-silver' };
  payload.profile_catalog.local = { label: 'Synthetic local model', api_cost: { amount: 0, currency: 'USD', basis: 'no_external_api', scope: 'local_only' } };
  return payload;
}
function storedCostAudit(overrides = {}) {
  return {
    known_dialogues: 3, unknown_dialogues: 0, total_dialogues: 3, reasons: [], lower_bound_usd: null, upper_bound_usd: null,
    method: 'stored_full_cost', pricing_complete: null, comparable: false, zero_cost_paid_usage_dialogues: 0,
    notes: ['Synthetic source note'], coverage_note: 'Synthetic coverage note', recorded_total_kzt: 75,
    currency_conversion: { usd_kzt: 500, source: 'synthetic conversion assumption', assumption: true }, ...overrides,
  };
}

test('quality shows four profiles and explicit local measurement and operating-cost limits', async t => {
  const { host } = setup(t, async () => fourProfileFixture()); await settle();
  const cards = descendants(host, 'div').find(el => el.className.split(' ').includes('cqa-quality__cards'));
  assert.equal(cards.children.length, 4);
  assert.match(cards.textContent, /Маршрутизация/);
  const local = cards.children[3];
  assert.match(local.textContent, /Локально.*Synthetic local model/);
  assert.match(local.textContent, /Качество и время на этом наборе не проверены/);
  assert.match(local.textContent, /API \/ диалог0 USD/);
  assert.match(local.textContent, /Сервер и эксплуатацияне рассчитаны/);
  assert.match(local.textContent, /Нулевой тариф API не означает нулевую полную стоимость/);
  assert.doesNotMatch(local.textContent, /0%|100%|0 с/);
});

test('routing comparison follows selected reference without additional API reads', async t => {
  const { host, calls } = setup(t, async () => fourProfileFixture()); await settle();
  let compare = descendants(host, 'select').find(el => el.id === 'cqa-quality-comparison');
  assert.ok(compare.children.some(option => option.value === 'routing-silver'));
  compare.value = 'routing-silver'; compare.listeners.get('change').forEach(fn => fn());
  assert.match(host.textContent, /11 общих ячеек/);
  const reference = descendants(host, 'select').find(el => el.id === 'cqa-quality-reference');
  reference.value = 'gold_v2_full'; reference.listeners.get('change').forEach(fn => fn());
  compare = descendants(host, 'select').find(el => el.id === 'cqa-quality-comparison');
  assert.ok(compare.children.some(option => option.value === 'routing-gold_v2_full'));
  assert.equal(calls(), 1);
});

test('a local catalogue without explicit API cost cannot acquire a zero tariff', async t => {
  const payload = fourProfileFixture(); delete payload.profile_catalog.local.api_cost;
  const { host } = setup(t, async () => payload); await settle();
  const local = descendants(host, 'article').find(el => el.className.includes('unmeasured'));
  assert.match(local.textContent, /API \/ диалогне указано/);
  assert.doesNotMatch(local.textContent, /0 USD|Нулевой тариф/);
});

test('a measured local profile uses its own report instead of a fabricated unknown result', async t => {
  const payload = fourProfileFixture(); payload.profile_catalog.local.report_id = 'haiku-silver';
  const { host } = setup(t, async () => payload); await settle();
  const cards = descendants(host, 'div').find(el => el.className.split(' ').includes('cqa-quality__cards'));
  const local = cards.children[3];
  assert.match(local.textContent, /72,7%/);
  assert.doesNotMatch(local.textContent, /Качество и время на этом наборе не проверены/);
});

test('reported complete-cost fields remain visible when model token allocation is absent', async t => {
  const payload = fourProfileFixture();
  for (const r of payload.reports.filter(row => /gemini|cascade|routing/.test(row.run_id))) {
    r.cost = { amount: 0.15, currency: 'USD', count: 3, basis: 'reported' };
    r.cost_audit = storedCostAudit(r.run_id === 'gemini-run' ? { pricing_complete: false, zero_cost_paid_usage_dialogues: 1 } : {});
  }
  const { host } = setup(t, async () => payload); await settle();
  assert.match(host.textContent, /Стоимость — по ценам моделей в каждом вызове; токены по моделям не разделены/);
  assert.match(host.textContent, /1 диалог с нулевой записанной ценой при расходе токенов; сумма занижена/);
  assert.match(host.textContent, /0,05 USD \(источник\)/);
  assert.match(host.textContent, /Пересчёт USD\/KZT: 500/);
  assert.doesNotMatch(host.textContent, /Стоимость каскада не восстановлена|раза дешевле/);
});

test('pricing-completeness and comparability audit gates ratios even with full cost counts', async t => {
  const payload = fourProfileFixture();
  payload.reports[0].cost_audit = storedCostAudit({ pricing_complete: false });
  payload.reports[1].cost = { amount: 0.15, currency: 'USD', count: 3, basis: 'estimated' };
  payload.reports[1].cost_audit = storedCostAudit({ pricing_complete: true, comparable: true });
  const { host } = setup(t, async () => payload); await settle();
  assert.doesNotMatch(host.textContent, /раза дешевле/);
  assert.match(host.textContent, /вывод об экономии не сделан/);
});

test('fully audited comparable complete costs permit a ratio derived from data', async t => {
  const payload = fourProfileFixture();
  payload.reports[0].cost_audit = storedCostAudit({ pricing_complete: true, comparable: true });
  payload.reports[1].cost = { amount: 0.15, currency: 'USD', count: 3, basis: 'estimated' };
  payload.reports[1].cost_audit = storedCostAudit({ pricing_complete: true, comparable: true });
  const { host } = setup(t, async () => payload); await settle();
  assert.match(host.textContent, /в 2 раза дешевле/);
});

for (const [title, change] of [
  ['routing missing report', p => { p.profile_catalog.routing.report_id = 'missing'; }],
  ['local missing report', p => { p.profile_catalog.local.report_id = 'missing'; }],
  ['local negative tariff', p => { p.profile_catalog.local.api_cost.amount = -1; }],
  ['local positive tariff with no-external-API claim', p => { p.profile_catalog.local.api_cost.amount = 1; }],
  ['local tariff implies total costs', p => { p.profile_catalog.local.api_cost.scope = 'total_operating_cost'; }],
  ['local unverified tariff basis', p => { p.profile_catalog.local.api_cost.basis = 'probably_free'; }],
  ['malformed pricing completeness', p => { p.reports[0].cost_audit = storedCostAudit({ pricing_complete: 'yes' }); }],
  ['malformed comparability', p => { p.reports[0].cost_audit = storedCostAudit({ comparable: 'yes' }); }],
  ['zero-price anomalies exceed dialogues', p => { p.reports[0].cost_audit = storedCostAudit({ zero_cost_paid_usage_dialogues: 4 }); }],
  ['invalid conversion denominator', p => { p.reports[0].cost_audit = storedCostAudit({ currency_conversion: { usd_kzt: 0, source: 'synthetic', assumption: true } }); }],
]) test(`four-profile validation rejects ${title}`, () => {
  const payload = fourProfileFixture(); change(payload); assert.equal(validateQuality(payload).state, 'invalid');
});

test('cached routing latency cannot look like live model speed or round into zero', async t => {
  const payload = fourProfileFixture(), routing = payload.reports.find(r => r.id === 'routing-silver');
  routing.cache = 'cached'; routing.latency = { ...routing.latency, p50_seconds: 0.012, p95_seconds: 0.041 };
  const { host } = setup(t, async () => payload); await settle();
  const cards = descendants(host, 'div').find(el => el.className.split(' ').includes('cqa-quality__cards'));
  assert.match(cards.children[2].textContent, /Кэш · p50 \/ p950,012 с \/ 0,041 с/);
  assert.match(cards.children[2].textContent, /Время повтора кэша; живое время не измерено/);
});

test('mixed-cache latency has an explicit limitation and does not enter headline live-speed comparison', async t => {
  const payload = fourProfileFixture(); payload.reports[0].cache = 'fresh'; payload.reports[1].cache = 'mixed';
  const { host } = setup(t, async () => payload); await settle();
  const cards = descendants(host, 'div').find(el => el.className.split(' ').includes('cqa-quality__cards'));
  assert.match(cards.children[1].textContent, /Смешанный кэш · p50 \/ p95/);
  assert.match(cards.children[1].textContent, /живое время отдельно не измерено/);
  const decision = descendants(host, 'p').find(el => el.className === 'cqa-quality__decision');
  assert.doesNotMatch(decision.textContent, /На этих прогонах p95/);
});

test('quality refresh button outranks the global `.cqa-root button {color: inherit}` reset (white ink on brand fill)', async () => {
  const { readFile } = await import('node:fs/promises');
  const css = await readFile(new URL('../web/css/quality.css', import.meta.url), 'utf8');
  const rule = css.match(/^([^{\n]*cqa-quality__refresh)\s*\{[^}]*color:\s*var\(--brand-ink\)/m);
  assert.ok(rule, 'refresh rule sets brand ink');
  // `.cqa-root button` has specificity (0,1,1); a lone class (0,1,0) loses and the label renders dark on dark.
  assert.match(rule[1].trim(), /^\.cqa-quality\s+\.cqa-quality__refresh$/);
});

test('published quality.json: comparison options are distinct labels and slices are Russian', async t => {
  const { readFile } = await import('node:fs/promises');
  const published = JSON.parse(await readFile(new URL('../web/data/quality.json', import.meta.url), 'utf8'));
  const { host } = setup(t, async () => structuredClone(published)); await settle();
  const compare = descendants(host, 'select').find(el => el.id === 'cqa-quality-comparison');
  const labels = compare.children.map(option => option.textContent);
  assert.equal(new Set(labels).size, labels.length, `duplicate options: ${labels.join(' | ')}`);
  assert.ok(labels.includes('Haiku g10') && labels.includes('Haiku g9 (ранний)') && labels.includes('Sonnet 5.5 CLI'));
  assert.doesNotMatch(labels.join(' '), /Haiku 5 5|Sonnet 5 5/);
  assert.match(host.textContent, /Чаты · /);
  assert.match(host.textContent, /Звонки · /);
  assert.doesNotMatch(host.textContent, /AGENT CHAT|VOICE CALL/i);
});
