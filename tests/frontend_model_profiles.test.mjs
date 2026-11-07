import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
const source = await readFile(new URL('../web/js/model-profiles.js', import.meta.url), 'utf8');
const { profileSettings, profileChoice, modelHint, profileGuide } = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
const data = JSON.parse(await readFile(new URL('../web/data/quality.json', import.meta.url), 'utf8'));
// Synthetic costs exercise presentation without treating archived estimates as evidence.
function comparisonFixture() {
  const fixture = structuredClone(data);
  const cloud = fixture.reports.find(r => r.id === fixture.profile_catalog.cloud.report_id);
  const cascade = fixture.reports.find(r => r.id === fixture.profile_catalog.cascade.report_id);
  cloud.cost = { amount: 22, currency: 'USD', count: cloud.sample.dialogues, basis: 'estimated' };
  cascade.cost = { amount: 10, currency: 'USD', count: cascade.sample.dialogues, basis: 'estimated' };
  for (const report of [cloud, cascade]) report.cost_audit = { pricing_complete: true, comparable: true };
  return fixture;
}

test('profile mappings preserve cascade across cloud location restoration', () => {
  assert.deepEqual(profileSettings('cascade'), { location: 'cloud', profile: 'cascade' });
  assert.deepEqual(profileSettings('routing'), { location: 'cloud', profile: 'routing' });
  assert.deepEqual(profileSettings('local'), { location: 'local', profile: 'local' });
  assert.deepEqual(profileSettings('cloud'), { location: 'cloud', profile: 'cloud' });
  assert.equal(profileChoice({ location: 'cloud', profile: 'cascade' }), 'cascade');
  assert.equal(profileChoice({ location: 'cloud', profile: 'routing' }), 'routing');
  assert.equal(profileChoice({ location: 'local', profile: 'local' }), 'local');
});

test('server hints expose escalation and distinguish mock from a real configured model', () => {
  assert.equal(modelHint({ model: { model: 'A', escalate: 'B' } }), 'Сначала A, при сомнении B.');
  assert.match(modelHint({ is_mock: true, model: { model: 'A' } }), /не вызывает реальные модели/);
  assert.match(modelHint({ model: { model: 'A', votes: 3 } }), /ответов на критерий: 3/);
});

test('comparisons derive cost ratio and labelled denominator, with explicit DEV caveats', () => {
  const fixture = comparisonFixture();
  const cloud = fixture.reports.find(r => r.id === fixture.profile_catalog.cloud.report_id);
  const guide = profileGuide(fixture);
  assert.match(guide.summary, /Gemini 4 Argon/);
  assert.match(guide.summary, /qwen3.5:9b/);
  assert.match(guide.comparison, /2,2 раза дешевле по расчётной стоимости/);
  assert.ok(guide.comparison.includes(`${cloud.counts.matched}/${cloud.counts.total - cloud.counts.unlabelled}`));
  assert.match(guide.comparison, /не гарантия/);
  assert.match(guide.limitation, /использовался для выбора решения/);
});

test('changing saved costs changes displayed comparison rather than retaining a marketing constant', () => {
  const changed = comparisonFixture();
  const cloud = changed.reports.find(r => r.id === changed.profile_catalog.cloud.report_id);
  const cascade = changed.reports.find(r => r.id === changed.profile_catalog.cascade.report_id);
  cloud.cost.amount = cascade.cost.amount * 3;
  assert.match(profileGuide(changed).comparison, /в 3 раза дешевле/);
  cascade.counts.matched--;
  assert.doesNotMatch(profileGuide(changed).comparison, /одинаковое совпадение/);
});

test('different references, samples and currencies cannot produce unsupported comparisons', () => {
  for (const mutate of [r => { r.reference.id = 'different'; }, r => { r.sample.dialogues = 20; }]) {
    const changed = comparisonFixture();
    mutate(changed.reports.find(r => r.id === changed.profile_catalog.cascade.report_id));
    assert.match(profileGuide(changed).comparison, /недоступно/);
  }
  const changed = comparisonFixture();
  changed.reports.find(r => r.id === changed.profile_catalog.cascade.report_id).cost.currency = 'KZT';
  assert.doesNotMatch(profileGuide(changed).comparison, /дешевле/);
});

test('partial or unknown cost coverage cannot support a savings claim', () => {
  for (const mutate of [cost => { cost.count--; }, cost => {
    cost.amount = null; cost.count = 0; cost.currency = null; cost.basis = 'unknown';
  }]) {
    const changed = comparisonFixture();
    mutate(changed.reports.find(r => r.id === changed.profile_catalog.cascade.report_id).cost);
    assert.doesNotMatch(profileGuide(changed).comparison, /дешевле/);
  }
});

test('missing or negative pricing audit cannot support a savings ratio', () => {
  for (const audit of [undefined, {}, { pricing_complete: null, comparable: false }, { pricing_complete: true, comparable: false }, { pricing_complete: false, comparable: true }]) {
    const changed = comparisonFixture();
    changed.reports.find(r => r.id === changed.profile_catalog.cascade.report_id).cost_audit = audit;
    assert.doesNotMatch(profileGuide(changed).comparison, /дешевле/);
  }
});

test('bundled profile catalog resolves reports and respects measured cost coverage', () => {
  const guide = profileGuide(data);
  assert.ok(guide);
  for (const profile of ['cloud', 'cascade']) {
    const report = data.reports.find(r => r.id === data.profile_catalog[profile].report_id);
    assert.ok(report, `${profile} report is present`);
    if (report.cost.basis === 'unknown' || report.cost.count !== report.sample.dialogues || report.cost_audit?.pricing_complete !== true || report.cost_audit?.comparable !== true) {
      assert.doesNotMatch(guide.comparison, /дешевле/);
    }
  }
});

test('missing references and failed predictions do not become accuracy claims', () => {
  assert.equal(profileGuide({ reports: [] }), null);
  for (const key of ['missing', 'errors']) {
    const changed = structuredClone(data);
    changed.reports.find(r => r.id === changed.profile_catalog.cascade.report_id).counts[key] = 1;
    assert.doesNotMatch(profileGuide(changed).comparison, /одинаковое совпадение/);
  }
  const changed = structuredClone(data);
  changed.reports = [];
  assert.match(profileGuide(changed).comparison, /недоступно/);
});


test('configured mock and custom profiles cannot silently become paid cloud profiles', () => {
  for (const profile of ['mock', 'claude', 'bank-private']) {
    const config = { location: 'cloud', profile };
    assert.equal(profileChoice(config), 'configured');
    assert.deepEqual(profileSettings(profileChoice(config), config), config);
  }
  assert.throws(() => profileSettings('configured'), /недоступен/);
  assert.match(modelHint({ profile: 'mock' }), /не вызывает реальные модели/);
});
