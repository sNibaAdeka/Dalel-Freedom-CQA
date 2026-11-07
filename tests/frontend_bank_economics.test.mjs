import assert from 'node:assert/strict';
import test from 'node:test';
import { readFile, mkdtemp, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';

const moduleUrl = new URL('../web/js/bank-economics.js', import.meta.url);
const source = (await readFile(moduleUrl, 'utf8')).replaceAll('import.meta.url', JSON.stringify(moduleUrl.href));
const { buildEconomics, calculateEconomics, estimatePlanBudget, validateInputs, loadInputs, saveInputs, mountCalculator, mountBudget, STORAGE_KEY } = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);

// Invented aggregate measurements, never bank transcripts or provider calls.
function quality() {
  const stats = (n, cost, seconds, needs) => ({ dialogues: n, cost_per_dialogue_kzt: cost, cost_per_dialogue_usd: cost / 500,
    cost_per_pair_kzt: cost / 4, cost_per_pair_usd: cost / 2000, mean_seconds_per_dialogue: seconds,
    mean_seconds_per_pair: seconds / 4, review_count: needs, review_known_dialogues: n, currency_fx: { usd_kzt: 500 },
    cost_provenance: { method: 'stored_full_cost', comparable: false, coverage_note: 'Историческая полнота цен не подтверждена.' }, latency_provenance: { projection_allowed: true, cache_state: 'fresh' } });
  const q = { profile_catalog: { local: { cloud_fallback: false, api_cost: { amount: 0, currency: 'USD', scope: 'local_only' } } }, reports: [] };
  for (const [i, id] of ['cloud', 'cascade', 'routing'].entries()) {
    q.profile_catalog[id] = { report_id: id };
    q.reports.push({ id, mode: 'fast', sample: { dialogues: 4, dataset: 'synthetic DEV' }, reference: { id: 'synthetic-silver', type: 'machine' },
      counts: { matched: 14, total: 16, unlabelled: 1 }, agreement_ci: { level: .95, lower: .8, upper: .99 },
      economics: { ...stats(4, 10 + i, 8, 1), channels: { chat: stats(3, 5 + i, 6, 0), call: stats(1, 25 + i, 14, 1) } } });
  }
  return q;
}
const model = () => buildEconomics(quality());
const scenario = { monthly_chats: 80, monthly_calls: 20, qa_hourly_kzt: 3000, manual_minutes: 6, monthly_budget_kzt: 1000, spent_kzt: 200 };
function storage() { const data = new Map(); return { getItem: key => data.get(key) ?? null, setItem: (key, value) => data.set(key, value) }; }
function mixedQuality() {
  const q = quality(), overall = q.reports[0].economics;
  overall.cost_provenance = { comparable: false, pricing_complete: false, coverage_note: 'Шесть нулевых цен в чатах.' };
  overall.latency_provenance = { projection_allowed: false, note: 'Общий замер свежести неизвестен.' };
  overall.channels.chat.cost_provenance = { ...overall.cost_provenance };
  overall.channels.chat.latency_provenance = { ...overall.latency_provenance };
  overall.channels.call.cost_provenance = { comparable: true, pricing_complete: true, coverage_note: 'Цены звонков записаны.' };
  overall.channels.call.latency_provenance = { projection_allowed: true, cache_state: 'fresh', note: 'Свежий замер звонков.' };
  return q;
}

test('four profiles retain measured agreement/CI and local has API zero only', () => {
  const value = model(); assert.deepEqual(value.profiles.map(r => r.id), ['cloud', 'cascade', 'routing', 'local']);
  assert.equal(value.profiles[0].agreement, 14 / 15); assert.deepEqual(value.profiles[0].interval, [.8, .99]);
  const local = value.profiles[3]; assert.equal(local.cost_usd, 0); assert.equal(local.seconds, null); assert.equal(local.agreement, null);
  const invalidLocal = quality(); invalidLocal.profile_catalog.local.cloud_fallback = true;
  assert.equal(buildEconomics(invalidLocal).profiles[3].cost_usd, null);
});

test('scenario weights channel means by entered volumes, not sample average', () => {
  const result = calculateEconomics(scenario, model()), cloud = result.profiles[0];
  assert.equal(result.total, 100); assert.equal(result.call_share, .2); assert.equal(result.remaining_kzt, 800);
  assert.equal(cloud.monthly_cost_kzt, 900); assert.ok(Math.abs(cloud.monthly_cost_usd - 1.8) < 1e-12);
  assert.equal(cloud.sequential_hours_10000, 7.6 * 10000 / 3600);
  assert.equal(cloud.review_fraction, .2); assert.equal(cloud.qa_hours_saved, 8);
  assert.equal(cloud.qa_value_saved_kzt, 24000); assert.equal(cloud.net_before_other_costs_kzt, 23100); assert.equal(cloud.over_budget, true);
});

test('unknown channel measurements cannot use overall mean to promise savings', () => {
  const q = quality(); delete q.reports[0].economics.channels.call;
  const result = calculateEconomics(scenario, buildEconomics(q)).profiles[0];
  assert.equal(result.monthly_cost_kzt, null); assert.equal(result.sequential_hours_10000, null); assert.equal(result.qa_hours_saved, null);
  const chatOnly = calculateEconomics({ ...scenario, monthly_calls: 0 }, buildEconomics(q)).profiles[0];
  assert.equal(chatOnly.monthly_cost_kzt, 400);
});

test('missing final review evidence and Gemini escalation never become saved QA hours', () => {
  const q = quality(); const raw = q.reports[0].economics;
  raw.review_count = undefined; raw.review_known_dialogues = undefined; raw.review_fraction = .1; raw.escalation_fraction = .1;
  raw.channels.chat.review_known_dialogues = 2;
  const result = calculateEconomics(scenario, buildEconomics(q)).profiles[0];
  assert.equal(result.qa_hours_saved, null); assert.equal(result.net_before_other_costs_kzt, null);
});

test('cached or unproven latency cannot be extrapolated to fresh model work', () => {
  for (const value of [undefined, { projection_allowed: false, cache_state: 'cached' }]) {
    const q = quality(); const raw = q.reports[0].economics;
    raw.latency_provenance = value;
    for (const channel of Object.values(raw.channels)) channel.latency_provenance = value;
    const m = buildEconomics(q);
    assert.equal(calculateEconomics(scenario, m).profiles[0].sequential_hours_10000, null);
    assert.equal(estimatePlanBudget({ pairs_to_evaluate: 8 }, 'cloud', m).sequential_seconds, null);
  }
});

test('calls-only scenario and budget use call provenance, not contradictory chat warnings', () => {
  const m = buildEconomics(mixedQuality());
  const callsOnly = calculateEconomics({ ...scenario, monthly_chats: 0, monthly_calls: 100 }, m).profiles[0];
  assert.equal(callsOnly.monthly_cost_kzt, 2500); assert.equal(callsOnly.sequential_hours_10000, 14 * 10000 / 3600);
  assert.equal(callsOnly.cost_provenance.comparable, true); assert.equal(callsOnly.cost_provenance.pricing_complete, true);
  assert.equal(callsOnly.latency_provenance.projection_allowed, true);
  assert.doesNotMatch(callsOnly.cost_provenance.coverage_note, /нулевых|чатах/);
  assert.doesNotMatch(callsOnly.latency_provenance.note, /Общий|неизвестен/);
  const budget = estimatePlanBudget({ dialogues: 100, channels: { chat: 0, call: 100 }, mode: 'fast' }, 'cloud', m);
  assert.equal(budget.sequential_seconds, 1400); assert.deepEqual(budget.cost_provenance, callsOnly.cost_provenance);
  const mixed = calculateEconomics(scenario, m).profiles[0];
  assert.equal(mixed.sequential_hours_10000, null); assert.equal(mixed.cost_provenance.comparable, false);
  assert.match(mixed.cost_provenance.coverage_note, /нулевых/); assert.equal(mixed.latency_provenance.projection_allowed, false);
});

test('a required channel without evidence keeps scenario provenance unknown', () => {
  const q = mixedQuality(); delete q.reports[0].economics.channels.call;
  const result = calculateEconomics({ ...scenario, monthly_chats: 0, monthly_calls: 100 }, buildEconomics(q)).profiles[0];
  assert.equal(result.monthly_cost_kzt, null); assert.equal(result.sequential_hours_10000, null);
  assert.equal(result.cost_provenance.pricing_complete, null); assert.equal(result.cost_provenance.comparable, false);
  assert.match(result.cost_provenance.coverage_note, /Звонки: полнота записанных цен не подтверждена/);
});

test('empty assumptions and zero volumes are distinct; spent is never implicitly zero', () => {
  const empty = calculateEconomics({}, model()); assert.equal(empty.total, null); assert.equal(empty.remaining_kzt, null);
  assert.ok(empty.profiles.every(r => r.monthly_cost_kzt === null));
  const zero = calculateEconomics({ ...scenario, monthly_chats: 0, monthly_calls: 0 }, model());
  assert.equal(zero.call_share, null); assert.equal(zero.profiles[0].monthly_cost_kzt, 0); assert.equal(zero.profiles[0].sequential_hours_10000, null);
  assert.equal(calculateEconomics({ monthly_budget_kzt: 100 }, model()).remaining_kzt, null);
  assert.equal(calculateEconomics({ monthly_budget_kzt: 100, spent_kzt: 120 }, model()).remaining_kzt, -20);
});

test('invalid values do not propagate negative, infinite or fractional volumes', () => {
  for (const bad of [-1, Infinity, NaN, 'hello', true, 1.5]) assert.ok(validateInputs({ monthly_chats: bad }).errors.monthly_chats);
  assert.equal(validateInputs({ manual_minutes: 1.5 }).values.manual_minutes, 1.5);
  assert.equal(validateInputs({ monthly_chats: ' ' }).values.monthly_chats, null);
  const result = calculateEconomics({ ...scenario, monthly_chats: Number.MAX_VALUE }, model()); assert.equal(result.total, null);
});

test('plan pair allocation is explicit and excludes provider-call count', () => {
  const result = estimatePlanBudget({ pairs_to_evaluate: 8, total: 10, estimated_calls: 500, mode: 'fast:cloud' }, 'cloud', model(), scenario);
  assert.equal(result.cost_kzt, 20); assert.equal(result.cost_usd, .04); assert.equal(result.sequential_seconds, 16); assert.equal(result.basis, 'mean_pair_allocation');
  assert.equal(estimatePlanBudget({ estimated_calls: 500 }, 'cloud', model()).cost_kzt, null);
});

test('parent dialogue/channel fallback uses weighted cost; unknown mode has no inherited fast estimate', () => {
  const weighted = estimatePlanBudget({ dialogues: 100, channels: { chat: 80, call: 20 }, mode: 'fast' }, 'cloud', model(), scenario);
  assert.equal(weighted.cost_kzt, 900); assert.equal(weighted.basis, 'channel_weighted_dialogues'); assert.equal(weighted.over_budget, true);
  const fallback = estimatePlanBudget({ dialogues: 100, mode: 'fast' }, 'cloud', model()); assert.equal(fallback.cost_kzt, 1000); assert.equal(fallback.basis, 'mean_dialogue_fallback');
  const accurate = estimatePlanBudget({ dialogues: 100, mode: 'accurate' }, 'cloud', model()); assert.equal(accurate.cost_kzt, null); assert.equal(accurate.mode_comparable, false);
  const zero = estimatePlanBudget({ pairs_to_evaluate: 0, total: 100 }, 'cloud', model()); assert.equal(zero.cost_kzt, 0); assert.equal(zero.sequential_seconds, 0);
});

test('local budget promises only API zero; missing size remains unknown', () => {
  const local = estimatePlanBudget({ dialogues: 50, mode: 'fast' }, 'local', model()); assert.equal(local.cost_usd, 0); assert.equal(local.sequential_seconds, null);
  assert.equal(estimatePlanBudget({}, 'local', model()).cost_usd, null);
});

test('input storage tolerates missing, blocked and corrupt localStorage', () => {
  const s = storage(); assert.equal(loadInputs(s).monthly_chats, ''); assert.equal(saveInputs(s, scenario), true);
  assert.equal(loadInputs(s).spent_kzt, 200);
  s.setItem(STORAGE_KEY, '{broken'); assert.equal(loadInputs(s).monthly_budget_kzt, '');
  const blocked = { getItem() { throw Error('denied'); }, setItem() { throw Error('denied'); } };
  assert.equal(loadInputs(blocked).spent_kzt, ''); assert.equal(saveInputs(blocked, scenario), false); assert.equal(saveInputs(undefined, scenario), false);
});

class Element {
  constructor(tag) { this.tagName = tag.toUpperCase(); this.children = []; this.attributes = new Map(); this.listeners = new Map(); this.ownText = ''; }
  set innerHTML(_) { throw Error('Unsafe HTML'); }
  set textContent(value) { this.ownText = String(value); this.children = []; }
  get textContent() { return this.ownText + this.children.map(child => child.textContent).join(''); }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.ownText = ''; this.children = items; }
  setAttribute(key, value) { this.attributes.set(key, String(value)); }
  getAttribute(key) { return this.attributes.get(key) ?? null; }
  addEventListener(event, callback) { if (!this.listeners.has(event)) this.listeners.set(event, new Set()); this.listeners.get(event).add(callback); }
  removeEventListener(event, callback) { this.listeners.get(event)?.delete(callback); }
  dispatch(event) { if (!this.disabled) this.listeners.get(event)?.forEach(callback => callback({ target: this })); }
}
const all = (root, tag) => root.children.flatMap(child => [...(child.tagName === tag.toUpperCase() ? [child] : []), ...all(child, tag)]);
function dom(t) {
  const previous = globalThis.document, head = new Element('head');
  globalThis.document = { createElement: tag => new Element(tag), head, querySelector: () => head.children[0] ?? null };
  t.after(() => { globalThis.document = previous; }); return { host: new Element('div'), head };
}

test('calculator mounts collapsed with all profiles, visible labels, no invented defaults', t => {
  const { host } = dom(t), handle = mountCalculator(host, { quality: quality(), storage: storage() }); t.after(() => handle.destroy());
  assert.equal(all(host, 'details').length, 1); assert.notEqual(all(host, 'details')[0].open, true); assert.equal(all(host, 'input').length, 6);
  assert.ok(all(host, 'input').every(input => input.value === '' && input.type === 'number'));
  for (const label of all(host, 'label')) assert.ok(all(host, 'input').some(input => input.id === label.htmlFor));
  assert.equal(all(host, 'section').length, 4); assert.match(host.textContent, /Эскалация к Gemini не равна ручной проверке/);
  assert.match(host.textContent, /не подтверждённый ROI/); assert.match(host.textContent, /токены по моделям не разделены/);
  assert.equal(all(host, 'button').length, 0);
});

test('same-page calculator and budget share explicit inputs even when storage is blocked', t => {
  const { host } = dom(t), other = new Element('div'), s = { getItem() { throw Error('denied'); }, setItem() { throw Error('denied'); } };
  const calc = mountCalculator(host, { quality: quality(), storage: s }), budget = mountBudget(other, { quality: quality(), plan: { dialogues: 100 }, storage: s });
  t.after(() => { calc.destroy(); budget.destroy(); });
  const fields = all(host, 'input'); fields[4].value = '1000'; fields[4].dispatch('input'); fields[5].value = '200'; fields[5].dispatch('input');
  assert.match(other.textContent, /остаток 800/); assert.equal(all(other, 'input')[0].value, '1000');
  all(other, 'input')[1].value = '350'; all(other, 'input')[1].dispatch('input');
  assert.equal(calc.getInputs().spent_kzt, '350'); assert.equal(fields[5].value, '350');
});

test('invalid form values remain visible with an error and aria-invalid', t => {
  const { host } = dom(t), handle = mountCalculator(host, { quality: quality(), storage: storage() }); t.after(() => handle.destroy());
  const input = all(host, 'input')[0]; input.value = '-1'; input.dispatch('input');
  assert.equal(input.value, '-1'); assert.equal(input.getAttribute('aria-invalid'), 'true'); assert.match(host.textContent, /Введите целое число/);
});

test('calls-only card does not render overall unknown-freshness or zero-chat-price warning', t => {
  const { host } = dom(t), s = storage(); saveInputs(s, { ...scenario, monthly_chats: 0, monthly_calls: 100 });
  const handle = mountCalculator(host, { quality: mixedQuality(), storage: s }); t.after(() => handle.destroy());
  const cloud = all(host, 'section')[0];
  assert.doesNotMatch(cloud.textContent, /Шесть нулевых цен|Общий замер свежести неизвестен|Нет сопоставимого замера свежего расчёта/);
  assert.match(cloud.textContent, /38,89 ч/);
});

test('budget switching only invokes parent callback, respects busy state and can recover failure', async t => {
  const { host } = dom(t), s = storage(); saveInputs(s, scenario); let calls = 0;
  const handle = mountBudget(host, { quality: quality(), plan: { dialogues: 100, mode: 'fast' }, profile: 'cloud', storage: s, busy: true,
    onSwitchCascade: async () => { calls++; throw Error('private error'); } }); t.after(() => handle.destroy());
  const button = all(host, 'button')[0]; assert.equal(button.hidden, false); assert.equal(button.disabled, true); button.dispatch('click'); assert.equal(calls, 0);
  assert.ok(all(host, 'input').every(input => input.disabled)); handle.update({ busy: false }); button.dispatch('click');
  assert.equal(calls, 1); assert.equal(button.disabled, true); await new Promise(resolve => setImmediate(resolve));
  assert.equal(button.disabled, false); assert.match(host.textContent, /Не удалось переключить профиль/); assert.doesNotMatch(host.textContent, /private error/);
  handle.update({ profile: 'cascade' }); assert.equal(button.hidden, true);
});

test('destroy removes listeners/subscriptions and late async switching cannot refill host', async t => {
  const { host } = dom(t), s = storage(); saveInputs(s, scenario); let finish;
  const handle = mountBudget(host, { quality: quality(), plan: { dialogues: 100 }, storage: s, onSwitchCascade: () => new Promise(resolve => { finish = resolve; }) });
  const button = all(host, 'button')[0]; button.dispatch('click'); handle.destroy(); finish(); await new Promise(resolve => setImmediate(resolve));
  assert.equal(host.children.length, 0); saveInputs(s, scenario); assert.equal(host.children.length, 0); assert.equal(button.listeners.get('click').size, 0);
});

test('no host mounts do not access browser storage or network', () => {
  assert.equal(typeof mountCalculator(null, { storage: null }).destroy, 'function'); assert.equal(typeof mountBudget(null, { storage: null }).destroy, 'function');
  assert.doesNotMatch(source, /\bfetch\s*\(|createRun\s*\(/);
});

test('styles use semantic tokens, 44px controls, keyboard focus, responsive and reduced-motion', async () => {
  const css = await readFile(new URL('../web/css/bank-economics.css', import.meta.url), 'utf8');
  assert.doesNotMatch(css, /#[\da-f]{3,8}\b|\brgba?\(/i); assert.match(css, /min-height: 44px/); assert.match(css, /:focus-visible/);
  assert.match(css, /min-width: 768px/); assert.match(css, /projector/); assert.match(css, /prefers-reduced-motion/);
});

test('offline documentation generator is deterministic and uses only aggregate inputs', async t => {
  const dir = await mkdtemp(path.join(tmpdir(), 'bank-economics-test-')); t.after(() => rm(dir, { recursive: true, force: true }));
  const q = path.join(dir, 'quality.json'), config = path.join(dir, 'models.json'), output = path.join(dir, 'out.md');
  const aggregate = quality(); aggregate.reports[0].sample.dialogues = 10;
  await writeFile(q, JSON.stringify(aggregate)); await writeFile(config, JSON.stringify({ usd_kzt: 500 }));
  const script = new URL('../scripts/bank_economics.py', import.meta.url).pathname;
  const args = [script, '--quality', q, '--models', config, '--output', output];
  const run = () => spawnSync(process.env.CQA_PYTHON ?? 'python3', args, { encoding: 'utf8', env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1' } });
  const first = run(); assert.equal(first.status, 0, first.stderr); const bytes = await readFile(output, 'utf8');
  const second = run(); assert.equal(second.status, 0, second.stderr); assert.equal(await readFile(output, 'utf8'), bytes);
  assert.match(bytes, /14\/15 \(93.3%\)/); assert.match(bytes, /80–99%/); assert.match(bytes, /SHA256 quality/); assert.match(bytes, /на ручную проверку/i); assert.match(bytes, /Облако · Gemini \| 10 \|/);
  assert.doesNotMatch(bytes, /2,2 раза|22\.9|10\.37/);
});

test('documentation preserves per-report FX when current config changes and marks the mismatch', async t => {
  const dir = await mkdtemp(path.join(tmpdir(), 'bank-economics-fx-')); t.after(() => rm(dir, { recursive: true, force: true }));
  const q = path.join(dir, 'quality.json'), config = path.join(dir, 'models.json'), output = path.join(dir, 'out.md');
  const aggregate = quality(); aggregate.reports[1].economics.currency_fx.usd_kzt = 600;
  aggregate.reports[1].economics.cost_per_dialogue_usd = 11 / 600;
  await writeFile(q, JSON.stringify(aggregate)); await writeFile(config, JSON.stringify({ usd_kzt: 1000 }));
  const script = new URL('../scripts/bank_economics.py', import.meta.url).pathname;
  const result = spawnSync(process.env.CQA_PYTHON ?? 'python3', [script, '--quality', q, '--models', config, '--output', output], { encoding: 'utf8', env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1' } });
  assert.equal(result.status, 0, result.stderr); const text = await readFile(output, 'utf8');
  assert.match(text, /Курс в метаданных замеров: 500, 600 ₸\/\$/);
  assert.match(text, /Текущая конфигурация: 1000 ₸\/\$\. НЕ СОВПАДАЕТ/);
  assert.match(text, /Сохранённые суммы не пересчитываются/);
  assert.match(text, /Облако · Gemini \| 4 \| 10 \| 0\.02 \| 500 \|/);
  assert.match(text, /Каскад · Haiku → Gemini \| 4 \| 11 \| 0\.0183 \| 600 \|/);
});
