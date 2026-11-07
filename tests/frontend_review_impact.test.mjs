/* node --test tests/frontend_review_impact.test.mjs
 * Optional real Chromium hook (synthetic data, local assets, no API writes):
 * CQA_REVIEW_IMPACT_BROWSER_URL=http://127.0.0.1:8047 CQA_BROWSER_SESSION=cqa-impact node --test tests/frontend_review_impact.test.mjs
 * Open that named playwright-cli session first from this checkout, or set CQA_BROWSER_CWD to its launch directory.
 * Artifacts go to an OS temporary directory. */
import assert from 'node:assert/strict';
import test from 'node:test';
import { readFile, mkdtemp, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';

const source = await readFile(new URL('../web/js/review-impact.js', import.meta.url), 'utf8');
const { buildReviewImpact, reviewAnswers, createReviewImpact } = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
function fixture() {
  const criteria = [
    { id: 'C1', title: 'Контакт', answer_type: 'binary', weight: 20, na_allowed: false },
    { id: 'C2', title: 'Понимание вопроса', answer_type: 'binary', weight: 30, na_allowed: true },
    { id: 'C3', title: 'Решение вопроса', answer_type: 'scale', scale: [0, 10, 20], weight: 50, na_allowed: true },
    { id: 'C8', title: 'Грубые нарушения', answer_type: 'binary', weight: 0, critical: true, critical_when: 'yes' },
  ];
  const answers = ['yes', 'na', 10, 'yes'];
  return { id: 'impact-synthetic', channel: 'chat', language: 'RU', is_mock: true, meta: {},
    messages: [{ id: 'impact:0', speaker: 'operator', text: 'Здравствуйте! Уточню ваш вопрос и помогу с решением.' }],
    checklist: { version: 1, rounding: { total: 2, mode: 'half_up' }, criteria },
    evals: criteria.map((criterion, i) => ({ criterion_id: criterion.id, answer: answers[i], confidence: .9, needs_review: i === 1 || i === 2,
      evidence: [{ message_id: 'impact:0', quote: 'Уточню ваш вопрос' }], explanation: 'Синтетический пример для проверки интерфейса.' })),
    score: { status: 'complete', checklist_version: 1, total: 64.29, critical: true, sections: {},
      lines: criteria.map((criterion, i) => ({ criterion_id: criterion.id, answer: answers[i], weight: criterion.weight,
        max_points: criterion.weight, points: i === 0 ? 20 : i === 2 ? 25 : null, source: 'model', is_na: answers[i] === 'na' })) },
    appeals: [{ id: 'appeal-1', conv_id: 'impact-synthetic', criterion_id: 'C1', status: 'open' }], decisions: [],
  };
}
const row = (preview, id) => preview.ranked.find(item => item.criterionId === id);
function freeze(value) {
  if (value && typeof value === 'object') { Object.freeze(value); Object.values(value).forEach(freeze); }
  return value;
}

test('ranks absolute effects, handles N/A in both directions and respects exact scale steps', () => {
  const preview = buildReviewImpact(fixture());
  assert.equal(preview.available, true);
  assert.deepEqual(preview.ranked.map(item => item.criterionId), ['C3', 'C1', 'C2']);
  assert.deepEqual(row(preview, 'C3').scenarios.map(item => item.answer), [0, 20, 'na']);
  assert.deepEqual(row(preview, 'C2').scenarios.map(item => [item.answer, item.total, item.denominator]), [['no', 45, 100], ['yes', 75, 100]]);
  assert.equal(row(preview, 'C3').scenarios.find(item => item.answer === 'na').total, 100);
  assert.equal(row(preview, 'C3').scenarios.find(item => item.answer === 0).delta, -35.72);
  assert.equal(row(preview, 'C3').maxImpact, 35.72, 'negative rounded change can be larger than positive change');
  assert.deepEqual(reviewAnswers({ answer_type: 'scale', scale: [7, 3, 7], na_allowed: true }), [3, 7, 'na']);
});

test('critical zero-weight C8 stays separate; human-cleared flags use the official answer', () => {
  const detail = fixture();
  const preview = buildReviewImpact(detail);
  assert.equal(preview.critical[0].state, 'hit');
  assert.equal(preview.critical[0].weight, 0);
  assert.ok(!row(preview, 'C8'));
  detail.score.lines[3] = { ...detail.score.lines[3], source: 'decision', decision_id: 'clear-8', answer: 'no' };
  detail.score.critical = false;
  assert.equal(buildReviewImpact(detail).critical[0].state, 'clear');
  assert.equal(detail.evals[3].answer, 'yes');
});

test('missing, partial, failed, stale, mismatched and malformed numeric data never becomes zero impact', () => {
  const variants = [
    detail => { delete detail.score; }, detail => { detail.score.total = null; },
    detail => { detail.score.total = '64.29'; }, detail => { detail.score.total = NaN; },
    detail => { detail.score.status = 'incomplete'; }, detail => { detail.status = 'failed'; },
    detail => { detail.score.lines.pop(); }, detail => { detail.checklist.criteria[1].weight = null; },
    detail => { detail.score.lines[1].weight = '30'; }, detail => { detail.score.lines[2].answer = 5; },
    detail => { detail.score.lines[2].answer = null; }, detail => { detail.score.lines[0].missing = true; },
    detail => { detail.evals = []; }, detail => { detail.evals[0].error = 'provider failed'; },
    detail => { detail.checklist.criteria[0].hash = 'new'; detail.evals[0].criterion_hash = 'old'; },
    detail => { detail.score.total = 12; }, detail => { detail.score.checklist_version = 9; },
    detail => { detail.score.lines[1] = detail.score.lines[0]; }, detail => { detail.checklist.criteria[2].scale = []; },
    detail => { detail.checklist.rounding.mode = 'unknown'; }, detail => { detail.checklist.rounding.total = -1; },
    detail => { detail.score.lines[1].is_na = false; }, detail => { detail.checklist.criteria[1].na_allowed = false; },
  ];
  for (const mutate of variants) {
    const detail = fixture(); mutate(detail);
    const result = buildReviewImpact(detail);
    assert.equal(result.available, false, String(mutate));
    assert.ok(result.reason);
    assert.ok(result.ranked.every(item => item.maxImpact === null && item.scenarios.length === 0), String(mutate));
  }
  assert.equal(buildReviewImpact(null).available, false);
  assert.equal(buildReviewImpact({}).officialTotal, null);
});

test('a real zero total is available; removing the last applicable criterion yields no total', () => {
  const detail = fixture();
  detail.checklist.criteria = [detail.checklist.criteria[2], detail.checklist.criteria[3]];
  detail.evals = [detail.evals[2], detail.evals[3]];
  detail.score.lines = [detail.score.lines[2], detail.score.lines[3]];
  detail.evals[0].answer = 0; detail.score.lines[0].answer = 0; detail.score.total = 0;
  const preview = buildReviewImpact(detail);
  assert.equal(preview.available, true);
  assert.equal(preview.officialTotal, 0);
  const scenario = row(preview, 'C3').scenarios.find(item => item.answer === 'na');
  assert.deepEqual(scenario, { answer: 'na', total: null, delta: null, denominator: 0, denominatorChanged: true });
  detail.score.total = null; detail.score.status = 'no_applicable';
  assert.match(buildReviewImpact(detail).reason, /Нет применимых/);
});

test('closed appeals and settled human decisions do not inherit model uncertainty', () => {
  const detail = fixture();
  detail.appeals[0].status = 'decided';
  detail.score.lines[2].source = 'decision'; detail.score.lines[2].decision_id = 'd1';
  assert.deepEqual(buildReviewImpact(detail).ranked.map(item => item.criterionId), ['C2']);
  detail.appeals.push({ id: 'reopen', criterion_id: 'C3', status: 'open' });
  assert.equal(row(buildReviewImpact(detail), 'C3').reasons[0], 'Открытый спор');
  detail.decisions.push({ id: 'd2', appeal_id: 'reopen' });
  assert.ok(!row(buildReviewImpact(detail), 'C3'));
});

test('uncertainty selects items but never multiplies effects; immutable snapshots remain untouched', () => {
  const detail = fixture(); detail.appeals = [];
  detail.evals[0].confidence = .2;
  const original = JSON.stringify(detail);
  const first = buildReviewImpact(freeze(detail));
  const changed = structuredClone(detail); changed.evals[0].confidence = .6;
  assert.equal(row(first, 'C1').maxImpact, row(buildReviewImpact(changed), 'C1').maxImpact);
  assert.equal(JSON.stringify(detail), original);
  changed.evals[0].confidence = .99; changed.evals[0].uncertain = true;
  assert.ok(row(buildReviewImpact(changed), 'C1'));
});

// Tiny DOM records actual click handlers and text construction; geometry is verified by the optional Chromium hook.
class Element {
  constructor(tag) { this.tagName = tag.toUpperCase(); this.children = []; this.dataset = {}; this.attributes = {}; this.listeners = {}; this.className = ''; this.classList = { add: name => { this.className += ` ${name}`; } }; }
  append(...nodes) { this.children.push(...nodes); }
  setAttribute(key, value) { this.attributes[key] = value; }
  addEventListener(name, handler) { this.listeners[name] = handler; }
  set textContent(value) { this.text = String(value); this.children = []; }
  get textContent() { return (this.text || '') + this.children.map(child => child.textContent).join(''); }
}
const descendants = node => [node, ...node.children.flatMap(descendants)];
test('DOM uses text-only content, focus callback only, concise disclosures and quiet collapsed state', () => {
  globalThis.document = { createElement: tag => new Element(tag), createElementNS: (_, tag) => new Element(tag) };
  try {
    const detail = fixture(); detail.checklist.criteria[2].title = '<img src=x onerror=alert(1)>';
    const before = JSON.stringify(detail), actions = [];
    const view = createReviewImpact(freeze(detail), { onFocus: id => actions.push(id) });
    const nodes = descendants(view);
    const action = nodes.find(node => node.dataset.reviewImpactId === 'C3');
    action.listeners.click();
    assert.deepEqual(actions, ['C3']);
    assert.ok(!nodes.some(node => node.tagName === 'IMG'));
    assert.ok(view.textContent.includes('<img src=x onerror=alert(1)>'));
    assert.match(view.textContent, /Сохранённый балл не меняется/);
    assert.match(view.textContent, /не вероятность ошибки и не официальная оценка/);
    assert.match(view.textContent, /N\/A исключается из знаменателя/);
    assert.equal(JSON.stringify(detail), before);
    const quiet = fixture(); quiet.appeals = []; quiet.evals.forEach(item => { item.needs_review = false; });
    quiet.evals[3].answer = 'no'; quiet.score.lines[3].answer = 'no'; quiet.score.critical = false;
    const quietView = createReviewImpact(quiet);
    assert.equal(quietView.tagName, 'DETAILS'); assert.ok(!quietView.open);
    assert.equal(descendants(quietView).filter(node => node.tagName === 'BUTTON').length, 0);
    assert.match(quietView.textContent, /нет сигналов/);
    const missing = createReviewImpact({});
    assert.match(missing.textContent, /Расчёт недоступен/);
    assert.ok(!missing.textContent.includes('до 0'));
  } finally { delete globalThis.document; }
});

const browserURL = process.env.CQA_REVIEW_IMPACT_BROWSER_URL;
test('real Chromium: responsive targets, focus/evidence, existing appeal flow and score immutability', { skip: !browserURL, timeout: 90000 }, async () => {
  const origin = new URL(browserURL).origin;
  assert.ok(['localhost', '127.0.0.1', '[::1]'].includes(new URL(origin).hostname));
  const dir = await mkdtemp(join(tmpdir(), 'cqa-review-impact-'));
  const script = `async page => {
    const origin = ${JSON.stringify(origin)};
    await page.route('**/*', route => {
      if(!route.request().url().startsWith(origin+'/')) return route.abort();
      if(route.request().url() === origin+'/review-impact-fixture') return route.fulfill({contentType:'text/html',body:'<!doctype html><html><head><meta name="viewport" content="width=device-width, initial-scale=1"><link rel="stylesheet" href="/app.css"></head><body style="margin:0"><main class="cqa-root"><div id="cqa-card"></div></main></body></html>'});
      return route.continue();
    });
    await page.goto(origin+'/review-impact-fixture');
    await page.emulateMedia({reducedMotion:'reduce'});
    await page.evaluate(async fixture => {
      const callbacks=new Map(), events=[];
      const bus={on(name,fn){if(!callbacks.has(name))callbacks.set(name,new Set());callbacks.get(name).add(fn);return()=>callbacks.get(name).delete(fn);},emit(name,data){events.push({name,data});for(const fn of callbacks.get(name)||[])fn(data);}};
      window.impactFixture=fixture; window.impactEvents=events; window.impactBefore=JSON.stringify(fixture);
      window.CQA={state:{},config:{role:'operator'},bus,api:{conversation:async()=>structuredClone(fixture),appeals:async()=>[],createAppeal:async()=>{throw Error('Test must never save');}}};
      const card=await import('/js/card.js'),review=await import('/js/review.js');
      card.mount(CQA);review.mount(CQA);CQA.bus.emit('conv:open',fixture.id);
    }, ${JSON.stringify(fixture())});
    await page.locator('[data-review-impact="ready"]').waitFor({state:'attached'});
    const widths=[];
    for(const width of [375,768,1024,1440]) {
      await page.setViewportSize({width,height:900});
      if(width<=768)await page.getByRole('tab',{name:'Результат',exact:true}).click();
      await page.locator('#cqa-review-impact').scrollIntoViewIfNeeded();
      widths.push(await page.evaluate(()=>{
        const box=document.querySelector('#cqa-review-impact');
        const controls=[...box.querySelectorAll('button,summary')].filter(node=>node.checkVisibility());
        return {width:innerWidth,overflow:document.documentElement.scrollWidth>innerWidth,small:controls.filter(node=>{const rect=node.getBoundingClientRect();return rect.width<44||rect.height<44;}).map(node=>node.className),motion:getComputedStyle(box.querySelector('button')).transitionDuration};
      }));
    }
    await page.setViewportSize({width:375,height:900});
    await page.getByRole('tab',{name:'Результат',exact:true}).click();
    await page.locator('[data-review-impact-id="C3"]').focus();
    await page.keyboard.press('Enter');
    const focused=await page.evaluate(()=>({id:document.activeElement.closest('[data-criterion-id]')?.dataset.criterionId,pane:document.querySelector('#cqa-card').dataset.pane,marks:document.querySelectorAll('.cqa-card__quote-mark').length}));
    await page.locator('[data-criterion-id="C3"] [data-card-appeal]').click();
    await page.locator('dialog[open]').waitFor();
    const appealCriterion=await page.locator('dialog[open] select').first().inputValue();
    await page.keyboard.press('Escape');
    const unchanged=await page.evaluate(()=>({score:JSON.stringify(CQA.state.conversation.score)===JSON.stringify(impactFixture.score),evals:JSON.stringify(CQA.state.conversation.evals)===JSON.stringify(impactFixture.evals),writes:impactEvents.filter(event=>['score:updated','decision:saved','appeal:created'].includes(event.name)).length}));
    await page.evaluate(()=>CQA.bus.emit('appeal:created',{id:'new-appeal',conv_id:impactFixture.id,criterion_id:'C2',status:'open'}));
    const refreshed=await page.locator('#cqa-review-impact').textContent();
    await page.evaluate(()=>CQA.bus.emit('score:updated',{conv_id:impactFixture.id,score:{...impactFixture.score,total:null,status:'incomplete'}}));
    const unavailable=await page.locator('[data-review-impact="unavailable"]').textContent();
    return {widths,focused,appealCriterion,unchanged,refreshed,unavailable};
  }`;
  const scriptPath = join(dir, 'browser.js'); await writeFile(scriptPath, script);
  const { stdout } = await promisify(execFile)('playwright-cli', [`-s=${process.env.CQA_BROWSER_SESSION || 'cqa-impact'}`, '--raw', 'run-code', `--filename=${scriptPath}`], { cwd: process.env.CQA_BROWSER_CWD || process.cwd(), timeout: 80000, maxBuffer: 1024 * 1024 });
  await writeFile(join(dir, 'report.json'), stdout);
  assert.ok(!stdout.startsWith('### Error'), stdout);
  const report = JSON.parse(stdout);
  for (const result of report.widths) { assert.equal(result.overflow, false, JSON.stringify(result)); assert.deepEqual(result.small, []); assert.equal(result.motion, '0s'); }
  assert.deepEqual(report.focused, { id: 'C3', pane: 'result', marks: 1 });
  assert.equal(report.appealCriterion, 'C3');
  assert.deepEqual(report.unchanged, { score: true, evals: true, writes: 0 });
  assert.match(report.refreshed, /Открытый спор/); assert.match(report.unavailable, /Расчёт недоступен/);
  console.log('Review-impact browser report:', join(dir, 'report.json'));
});
