import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const qualityUrl = new URL('../web/js/quality.js', import.meta.url);
const source = (await readFile(qualityUrl, 'utf8')).replaceAll('import.meta.url', JSON.stringify(qualityUrl.href));
const { validateQuality } = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
const payload = JSON.parse(await readFile(new URL('../web/data/quality.json', import.meta.url), 'utf8'));

test('generated metrics remain consumable by the shipped UI across all references', () => {
  const result = validateQuality(payload);
  assert.equal(result.state, 'ready', result.issues.join('; '));
  assert.deepEqual(new Set(result.reports.map(r => r.reference.id)), new Set(['silver', 'gold_v2', 'gold_v2_full']));
  const runIds = new Set(result.reports.map(r => r.run_id));
  for (const run of runIds) {
    assert.equal(result.reports.filter(r => r.run_id === run).length, 3);
  }
  assert.equal(result.comparisons.length, (runIds.size - 1) * 3);
  for (const profile of ['cloud', 'cascade', 'routing']) {
    assert.ok(result.reports.some(r => r.id === payload.profile_catalog[profile].report_id));
  }
  assert.equal(payload.profile_catalog.local.api_cost.amount, 0);
  assert.equal(payload.profile_catalog.local.cloud_fallback, false);
});

test('published aggregates omit conversation content and unverifiable subgroup intervals', () => {
  const forbidden = new Set(['conv_id', 'msg_id', 'message_id', 'quote', 'quotes', 'evidence', 'explanation', 'transcript', 'audio_url']);
  function inspect(value) {
    if (!value || typeof value !== 'object') return;
    for (const [key, child] of Object.entries(value)) {
      assert.ok(!forbidden.has(key), `private field ${key} in aggregate artifact`);
      inspect(child);
    }
  }
  inspect(payload);
  for (const report of payload.reports) {
    for (const group of [...report.criteria, ...report.languages, ...report.channels]) {
      const ci = group.agreement_ci;
      if (group.dialogues < 2 || ci.effective_dialogues < 2) {
        assert.equal(ci.lower, null);
        assert.equal(ci.upper, null);
      }
    }
    if (report.cost_audit.unknown_dialogues > 0) {
      assert.equal(report.cost.basis, 'unknown');
      assert.equal(report.cost.amount, null);
      assert.equal(report.cost.count, 0);
    }
  }
});
