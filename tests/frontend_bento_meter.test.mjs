import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const qualityUrl = new URL('../web/js/quality.js', import.meta.url);
const qualitySource = (await readFile(qualityUrl, 'utf8')).replaceAll('import.meta.url', JSON.stringify(qualityUrl.href));
const { bentoStats } = await import(`data:text/javascript;base64,${Buffer.from(qualitySource).toString('base64')}`);
const profileSource = await readFile(new URL('../web/js/model-profiles.js', import.meta.url), 'utf8');
const profileUrl = `data:text/javascript;base64,${Buffer.from(profileSource).toString('base64')}`;
const boardSource = (await readFile(new URL('../web/js/board.js', import.meta.url), 'utf8')).replace("'./model-profiles.js'", JSON.stringify(profileUrl));
const { costMeter, stageCosts } = await import(`data:text/javascript;base64,${Buffer.from(boardSource).toString('base64')}`);
const published = JSON.parse(await readFile(new URL('../web/data/quality.json', import.meta.url), 'utf8'));

// Invented aggregates; numbers chosen so each tile has a distinct, checkable value.
function fixture() {
  const report = (id, run_id, label, matched, cost, extra = {}) => ({ id, run_id, source: { label }, reference: { id: 'silver' },
    sample: { dataset: 'DEV', split: 'holdout', dialogues: 21 }, counts: { matched, total: 168, missing: 0, errors: 0, unlabelled: 9 },
    cost: { amount: cost, currency: 'USD', count: 21 }, ...extra });
  return {
    reports: [
      report('cloud-r', 'run-cloud', 'Gemini 4 Argon', 152, 0.9636368),
      report('cascade-r', 'run-cascade', 'Каскад', 152, 0.5246408, { model_usage: { gemini_dialogues: 9, known_dialogues: 21 } }),
      report('haiku-r', 'run-haiku', 'Haiku g9', 140, 0.1155888),
    ],
    profile_catalog: { cloud: { label: 'Gemini 4 Argon', report_id: 'cloud-r' }, cascade: { first_label: 'Haiku 5.5', escalate_label: 'Gemini 4 Argon', report_id: 'cascade-r' } },
    subsamples: [{ id: 'kz-all', references: { silver: { matched: 115, labelled: 117 } } }],
  };
}

test('bento reads every headline number from quality data', () => {
  const stats = bentoStats(fixture(), 'silver');
  assert.equal(stats.primary.value, '95,6%');
  assert.equal(stats.primary.label, 'Совпадение с эталоном');
  assert.equal(stats.primary.note, 'верхняя оценка, 21 отложенный диалог DEV');
  assert.deepEqual(stats.tiles.map(t => [t.id, t.value]), [['kz', '98,3%'], ['price', '$0,046'], ['cascade', '$0,025']]);
});

test('bento omits tiles whose value is absent and shows a published constant baseline', () => {
  const data = fixture(); data.subsamples = []; data.baselines = { constant: { counts: { matched: 132, total: 168, unlabelled: 9 } } };
  const stats = bentoStats(data, 'silver');
  assert.deepEqual(stats.tiles.map(t => [t.id, t.value]), [['constant', '83,0%'], ['price', '$0,046'], ['cascade', '$0,025']]);
  assert.deepEqual(bentoStats({}, 'silver'), { primary: null, tiles: [] });
});

test('bento on the published quality.json matches the agreed headline values', () => {
  const stats = bentoStats(published, 'silver');
  assert.equal(stats.primary.value, '95,6%');
  const byId = Object.fromEntries(stats.tiles.map(t => [t.id, t.value]));
  assert.equal(byId.kz, '98,3%'); assert.equal(byId.price, '$0,046'); assert.equal(byId.cascade, '$0,025');
});

test('cost meter shares sum to 100 and budget text converts tenge to dollars', () => {
  const stages = [{ name: 'A', cost: 1 }, { name: 'B', cost: 1 }, { name: 'C', cost: 1 }];
  const view = costMeter({ total: 3, stages, budgetKzt: '10000', usdKzt: 500 });
  assert.equal(view.segments.reduce((sum, s) => sum + s.share, 0), 100);
  assert.equal(view.budgetText, 'из $20,00 бюджета');
  assert.equal(costMeter({ total: 0.0459, stages: [{ name: 'Gemini', cost: 0.0459 }] }).value, '$0,0459');
  assert.equal(costMeter({ total: 1, stages: [{ name: 'X', cost: 1 }], budgetKzt: '' , usdKzt: 500 }).budgetText, '');
  assert.equal(costMeter({ total: null, stages: [] }), null);
  assert.equal(costMeter({ total: 1, stages: [], actual: true }).label, 'Факт прогона');
});

test('cascade forecast splits into Haiku and Gemini stages that add up to the total', () => {
  const stages = stageCosts({ quality: published, profile: 'cascade', total: 0.025 });
  assert.deepEqual(stages.map(s => s.name), ['Haiku 5.5', 'Gemini 4 Argon']);
  assert.ok(Math.abs(stages.reduce((sum, s) => sum + s.cost, 0) - 0.025) < 1e-12);
  const view = costMeter({ total: 0.025, stages });
  assert.equal(view.segments.reduce((sum, s) => sum + s.share, 0), 100);
  assert.deepEqual(stageCosts({ quality: published, profile: 'cloud', total: 0.0459 }).map(s => s.name), ['Gemini 4 Argon']);
});
