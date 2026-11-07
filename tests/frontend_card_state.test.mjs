// Run: node --experimental-default-type=module tests/frontend_card_state.test.mjs
// Minimal DOM harness exercises the real card module. Geometry is supplied explicitly;
// this checks state/control flow, not browser rendering or CSS layout.
import assert from 'node:assert/strict';

let narrow = true;
const nodes = root => [root, ...root.children.flatMap(nodes)];
const dataKey = name => name.slice(5).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase());
function matches(node, selector) {
  return selector.split(',').some(part => {
    part = part.trim();
    if (part.startsWith('.')) return node.className.split(/\s+/).includes(part.slice(1));
    if (part.startsWith('#')) return node.id === part.slice(1);
    const attr = part.match(/^\[([^=\]]+)(?:=["']([^"']*)["'])?\]$/);
    if (attr) {
      const value = attr[1].startsWith('data-') ? node.dataset[dataKey(attr[1])] : node.attrs[attr[1]];
      return value !== undefined && (attr[2] === undefined || value === attr[2]);
    }
    return node.tagName.toLowerCase() === part.toLowerCase();
  });
}
class Node {
  constructor(tag) {
    this.tagName = tag.toUpperCase(); this.children = []; this.parentNode = null;
    this.style = {}; this.scrollTop = 0; this.dataset = {}; this.attrs = {}; this.listeners = {};
    this._text = ''; this._class = ''; this.hidden = false; this.connected = false;
    this.classList = {
      add: (...names) => { this._class = [...new Set([...this._class.split(/\s+/).filter(Boolean), ...names])].join(' '); },
      remove: (...names) => { this._class = this._class.split(/\s+/).filter(name => !names.includes(name)).join(' '); },
    };
  }
  set className(value) { this._class = value; } get className() { return this._class; }
  set textContent(value) { this.replaceChildren(); this._text = String(value); }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
  get isConnected() { return this.connected || !!this.parentNode?.isConnected; }
  get clientWidth() { return this.className.includes('cqa-card__results') ? (narrow ? 0 : 500) : 400; }
  append(...children) { for (const child of children) { child.parentNode = this; this.children.push(child); } }
  prepend(...children) { for (const child of children) child.parentNode = this; this.children.unshift(...children); }
  replaceChildren(...children) { for (const child of this.children) child.parentNode = null; this.children = []; this._text = ''; this.append(...children); }
  setAttribute(name, value) { this.attrs[name] = String(value); }
  getAttribute(name) { return this.attrs[name] ?? null; }
  removeAttribute(name) { delete this.attrs[name]; }
  querySelectorAll(selector) { return nodes(this).slice(1).filter(node => matches(node, selector)); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  addEventListener(name, callback) { (this.listeners[name] ||= []).push(callback); }
  removeEventListener() {}
  focus() {} scrollIntoView() {}
  getBoundingClientRect() {
    const messageIndex = this.dataset.messageId?.split(':').at(-1);
    const top = messageIndex === undefined ? 100 : 110 + Number(messageIndex) * 80;
    const height = messageIndex === undefined ? 400 : 60;
    const isNumber = this.className.includes('cqa-card__criterion-number');
    const left = isNumber ? 520 : this.className.includes('cqa-card__results') ? 500 : 0;
    const width = isNumber ? 22 : this.clientWidth;
    return { top, bottom: top + height, left, right: left + width, width, height };
  }
}
const head = new Node('head'); head.connected = true;
const host = new Node('div'); host.id = 'cqa-card'; host.connected = true;
globalThis.document = {
  baseURI: 'http://localhost/', head,
  createElement: tag => new Node(tag), createElementNS: (_, tag) => new Node(tag),
  createTextNode: text => { const node = new Node('#text'); node.textContent = text; return node; },
  getElementById: id => nodes(host).find(node => node.id === id) || null,
  querySelector: selector => head.querySelector(selector) || host.querySelector(selector),
};
globalThis.window = { matchMedia: () => ({ matches: true }), addEventListener() {}, removeEventListener() {} };
globalThis.ResizeObserver = class { observe() {} disconnect() {} };
let nextFrame = 0;
const frames = new Map();
globalThis.requestAnimationFrame = callback => { const id = ++nextFrame; frames.set(id, callback); return id; };
globalThis.cancelAnimationFrame = id => frames.delete(id);
function flushFrames() { while (frames.size) { const queued = [...frames.entries()]; frames.clear(); for (const [, callback] of queued) callback(); } }
function makeBus() {
  const listeners = new Map();
  return {
    on(name, callback) { if (!listeners.has(name)) listeners.set(name, new Set()); listeners.get(name).add(callback); return () => listeners.get(name).delete(callback); },
    emit(name, payload) { for (const callback of listeners.get(name) || []) callback(payload); },
  };
}
const detail = {
  id: 'A', channel: 'chat', language: 'RU', is_mock: true, meta: {},
  messages: [
    { id: 'A:0', idx: 0, speaker: 'operator', text: 'Демонстрационная первая реплика.', ts_raw: '00:00', t_sec: 0 },
    { id: 'A:1', idx: 1, speaker: 'operator', text: 'Демонстрационная вторая реплика.', ts_raw: '00:10', t_sec: 10 },
    { id: 'A:2', idx: 2, speaker: 'system', text: 'Synthetic context event.', ts_raw: '00:20', t_sec: 20 },
  ],
  checklist: { version: 1, criteria: [{ id: 'C8', title: 'Грубые ошибки', critical: true, critical_when: 'yes', weight: 0 }, { id: 'C2', title: 'Пропущенное действие' }] },
  evals: [
    { criterion_id: 'C8', answer: 'yes', critical_hit: true, evidence: [{message_id:'A:2',quote:'Synthetic context event.'}], checked_range: ['A:0', 'A:1'], confidence: .9 },
    { criterion_id: 'C2', answer: 'no', critical_hit: false, evidence: [], checked_range: ['A:0', 'A:1'], confidence: .9 },
  ],
  score: { total: 0, critical: true, sections: {}, lines: [
    { criterion_id: 'C8', answer: 'yes', points: 0, max_points: 0, weight: 0, weight_effective: 0 },
    { criterion_id: 'C2', answer: 'no', points: 0, max_points: 1, weight: 50, weight_effective: 50 },
  ] },
};
detail.evals[0].raw = { cascade: { escalated: true, flagged: ['C8'], first_answers: { C8: 'yes' } } };
detail.evals[1].raw = { cascade: { escalated: false, flagged: [], first_answers: { C2: 'no' } } };
const initialReads = [];
const CQA = { state: {}, config: { role: 'operator' }, bus: makeBus(), api: { conversation: async (id, options) => { initialReads.push(options); return structuredClone(detail); } } };
const { mount, explanationReferences } = await import('../web/js/card.js');
assert.equal(explanationReferences('Основание [A:0], [A:2]; [unknown:9].', detail.messages), 'Основание #1, #3; [unknown:9].');
const cleanup = mount(CQA);
CQA.bus.emit('conv:open', 'A');
await new Promise(resolve => setImmediate(resolve));
flushFrames();
assert.match(host.textContent, /Нарушение выявлено/);
assert.deepEqual(initialReads[0], { trace: true });
const cascadeBadges = host.querySelectorAll('.cqa-card__cascade');
assert.equal(cascadeBadges.length, 2);
assert.equal(cascadeBadges[0].dataset.escalated, 'true');
assert.match(cascadeBadges[0].querySelector('summary').textContent, /Перепроверено сильной моделью/);
assert.match(cascadeBadges[0].querySelector('summary').title, /Первая модель: Да.*C8/);
assert.equal(cascadeBadges[1].dataset.escalated, 'false');
assert.match(cascadeBadges[1].textContent, /Хватило быстрой модели/);
assert.match(host.textContent, /Система: цитата найдена в оригинале · контекст/);
assert.match(host.querySelector('.cqa-card__verified-summary').textContent,/1\/1/);
assert.ok(!host.querySelector('[data-criterion-id="C8"]')?.textContent.includes('0 / 0'));
const finalScore = structuredClone(detail.score);
finalScore.critical = false; finalScore.total = 85.38; finalScore.lines[0].answer = 'no'; finalScore.lines[0].points = 0;
const { submitDecision } = await import('../web/js/review.js');
const decisionReads = [];
await submitDecision({ bus: CQA.bus, api: {
  createDecision: async () => ({ old_total: detail.score.total, new_total: finalScore.total, score: finalScore }),
  conversation: async (id, options) => {
    decisionReads.push(options);
    const fresh = { ...structuredClone(detail), score: structuredClone(finalScore) };
    if (!options?.trace) for (const evaluation of fresh.evals) delete evaluation.raw;
    return fresh;
  },
} }, { appeal_id: 'synthetic-appeal', action: 'change', new_answer: 'no', reason: 'Synthetic review' },
{ conv_id: 'A', criterion_id: 'C8', before: detail });
flushFrames();
assert.deepEqual(decisionReads, [{ trace: true }]);
assert.equal(host.querySelectorAll('.cqa-card__cascade').length, 2, 'full post-decision refresh must retain both cascade badges');
assert.match(host.querySelectorAll('.cqa-card__cascade')[0].textContent, /Первая модель: Да/);
assert.match(host.textContent, /Грубая ошибка в исходной оценке ИИ/, 'historical AI flag must be explicitly labeled after a decision');
assert.equal(CQA.state.conversation.evals[0].critical_hit, true, 'original AI flag must remain unchanged');
assert.equal(CQA.state.conversation.score.critical, false, 'final critical flag must reflect the decision');
const row = nodes(host).find(node => node.dataset.criterionId === 'C8');
assert.match(row.textContent, /После решения: Нет/);
assert.match(row.textContent, /Нарушений нет/);
assert.equal(row.querySelectorAll('.cqa-card__critical').length,0);
assert.equal(host.querySelector('.cqa-card__total').textContent,'85,38/ 100');
CQA.bus.emit('criterion:focus', { conv_id: 'A', criterion_id: 'C2' });
flushFrames();
const results = host.querySelector('.cqa-card__results');
const bracket = host.querySelector('.cqa-card__range-bracket');
assert.equal(results.clientWidth, 0, 'harness simulates the hidden mobile results panel');
assert.equal(host.dataset.pane, 'transcript');
assert.equal(bracket.hidden, false, 'range bracket must be drawn independently of hidden results panel');
assert.equal(bracket.style.top, '14px');
assert.equal(bracket.style.height, '132px');
assert.match(host.querySelector('.cqa-card__range-status').textContent, /реплики 1–2/);
// Preserve the inspected range and scroll positions through another score update.
host.querySelector('.cqa-card__transcript-scroll').scrollTop = 91;
host.querySelector('.cqa-card__results').scrollTop = 203;
const nextScore = {...finalScore, total:99.99, sections:{'Грубые нарушения':null, 'Секция':85.38}, formula_steps:['55.5 / 65 × 100 = 85.38']};
CQA.bus.emit('score:updated', {conv_id:'A',score:nextScore}); flushFrames();
assert.equal(host.querySelector('.cqa-card__total').textContent,'99,99/ 100');
assert.equal(host.querySelector('.cqa-card__transcript-scroll').scrollTop,91);
assert.equal(host.querySelector('.cqa-card__results').scrollTop,203);
assert.equal(host.querySelector('.cqa-card__range-bracket').hidden,false);
assert.match(host.querySelector('.cqa-card__sections').textContent,/Грубые нарушения—Секция85,38/);
assert.match(host.querySelector('.cqa-card__formula').textContent,/55.5 \/ 65 × 100 = 85.38/);
narrow = false;
CQA.bus.emit('criterion:focus',{conv_id:'A',criterion_id:'C2'});flushFrames();
const dots = host.querySelector('.cqa-card__thread').querySelectorAll('circle');
assert.ok(Number(dots[0].getAttribute('cx')) - Number(dots[1].getAttribute('cx')) >= 40, 'thread spans from criterion number to the actual message edge');
assert.equal(dots[0].getAttribute('visibility'),'visible');
CQA.bus.emit('score:updated',{conv_id:'A',score:{...nextScore,total:null,status:'no_applicable'}});flushFrames();
assert.match(host.textContent,/Итог не рассчитывается: нет применимых критериев/);
cleanup();
console.log('PASS: real card mount; historical C8 flag after yes→no; v1 precision, formula and no-applicable; preserved viewport; original AI immutability; mobile range bracket with zero-width results panel.');

// Provider errors use synthetic data and deferred API stubs: no backend/model calls.
const tick = () => new Promise(resolve => setImmediate(resolve));
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return { promise, resolve }; };
const incomplete = () => ({ ...structuredClone(detail), checklist: { version: 7, criteria: [
  { id: 'C1', title: 'Контакт', answer_type: 'binary' },
  { id: 'C2', title: 'Уточнение', answer_type: 'binary', na_allowed: true },
  { id: 'C8', title: 'Грубые ошибки', answer_type: 'binary', critical: true },
] }, evals: [
  { criterion_id: 'C1', answer: null, review_reasons: ['оценка не получена: ошибка модели: HTTP 402 Insufficient credits secret-diagnostic-must-not-render'] },
  { criterion_id: 'C2', answer: 'na', na_reason: 'Синтетический общий вопрос.' },
], score: { total: 100, sections: { misleading: 100 }, formula_steps: ['stale 100'], lines: [] } });
const fresh = () => ({ ...incomplete(), evals: [
  { criterion_id: 'C1', answer: 'yes' }, { criterion_id: 'C2', answer: 'na' }, { criterion_id: 'C8', answer: 'no' },
], score: { total: 88, lines: [], status: 'ok' } });
const seen = [];
CQA.bus.on('card:provider-state', state => seen.push(state));
let reads = [], posts = [], sourceDetail = incomplete(), pendingRun = deferred();
CQA.api = {
  conversation: async (id, options) => { reads.push([id, options]); return { ...structuredClone(sourceDetail), id }; },
  createRun: body => { posts.push(body); return pendingRun.promise; },
  run: async () => { throw new Error('Unexpected poll in this test'); },
};
const stopErrors = mount(CQA);
CQA.bus.emit('conv:open', 'A'); await tick(); flushFrames();
assert.equal(host.querySelector('.cqa-card__total').textContent, 'Не рассчитан');
assert.match(host.textContent, /Критериев без ответа: 2 · C1, C8/);
assert.equal(host.querySelector('.cqa-card__formula'), null, 'do not expose stale successful calculation');
assert.equal(host.querySelector('#cqa-weights'), null, 'weights plugin must not render a misleading total');
assert.match(host.querySelector('[data-criterion-id="C1"]').textContent, /ошибка провайдера/);
assert.equal(host.querySelector('[data-criterion-id="C1"]').querySelector('.cqa-card__appeal'), null, 'there is no answer to appeal');
assert.doesNotMatch(host.querySelector('[data-criterion-id="C8"]').textContent, /Нарушений нет|ошибка провайдера/);
assert.match(host.querySelector('[data-criterion-id="C2"]').textContent, /Исключён из расчёта/);
assert.doesNotMatch(host.textContent, /secret-diagnostic/);
assert.equal(seen.at(-1).provider_failure_count, 1);
const click = node => node.listeners.click.forEach(listener => listener());
let retryButton = host.querySelector('[data-card-retry]');
click(retryButton); click(retryButton);
assert.equal(posts.length, 1); assert.equal(retryButton.disabled, true);
assert.equal(posts[0].version, 7); assert.equal(posts[0].live, true); assert.equal(posts[0].force, true);
assert.deepEqual(posts[0].conv_ids, ['A']);
assert.match(host.textContent, /все критерии этого диалога/);
sourceDetail = fresh(); pendingRun.resolve({ run_id: 'card-r1', status: 'completed', version: 7 }); await tick(); flushFrames();
assert.equal(host.querySelector('.cqa-card__total').textContent, '88/ 100');
assert.deepEqual(reads.at(-1), ['A', { version: 7, trace: true }]);
assert.match(host.textContent, /Результат обновлён/);
assert.equal(seen.at(-1).missing_count, 0);

// A completed transport/run is not a complete evaluation.
sourceDetail = incomplete(); CQA.bus.emit('conv:open', 'A'); await tick();
pendingRun = deferred(); click(host.querySelector('[data-card-retry]'));
pendingRun.resolve({ run_id: 'card-r2', status: 'completed', version: 7 }); await tick();
assert.equal(host.querySelector('.cqa-card__total').textContent, 'Не рассчитан');
assert.match(host.querySelector('.cqa-card__retry-status').textContent, /Критериев без ответа: 2/);
assert.doesNotMatch(host.querySelector('.cqa-card__retry-status').textContent, /Результат обновлён/);

// Switching A → B → A cannot let a late result overwrite a newer visit to A.
pendingRun = deferred(); click(host.querySelector('[data-card-retry]'));
CQA.bus.emit('conv:open', 'B'); await tick();
assert.equal(host.querySelector('.cqa-card__title').textContent, 'B');
CQA.bus.emit('conv:open', 'A'); await tick();
click(host.querySelector('[data-card-retry]')); assert.equal(posts.length, 3, 'reopening A does not bypass the in-flight lock');
sourceDetail = fresh(); pendingRun.resolve({ run_id: 'card-r3', status: 'completed', version: 7 }); await tick();
assert.equal(host.querySelector('.cqa-card__total').textContent, 'Не рассчитан', 'stale retry data does not replace a newer visit');

// A newer authoritative score update invalidates the pending refresh too.
sourceDetail = incomplete(); CQA.bus.emit('conv:open', 'A'); await tick();
pendingRun = deferred(); click(host.querySelector('[data-card-retry]'));
const newer = fresh(); newer.score.total = 77;
CQA.bus.emit('score:updated', newer);
sourceDetail = fresh(); pendingRun.resolve({ run_id: 'card-r4', status: 'completed', version: 7 }); await tick();
assert.equal(host.querySelector('.cqa-card__total').textContent, '77/ 100');
CQA.bus.emit('score:updated', { ...fresh(), checklist: { ...fresh().checklist, version: 6 } });
assert.equal(host.querySelector('.cqa-card__total').textContent, '77/ 100', 'wrong-version external detail is ignored');
stopErrors();
console.log('PASS: missing/provider/N/A UI separation; suppressed stale totals; safe provider hooks; full-dialogue retry; duplicate lock; pinned refresh; incomplete terminal run; A-B-A and newer-score stale guards.');


// No evaluations is a neutral start state; a completed attempt with no answers becomes an error.
sourceDetail = {...incomplete(), evals:[], score:{total:null,status:'incomplete',lines:[]}};
pendingRun = deferred(); posts=[];
const stopPending = mount(CQA);
CQA.bus.emit('conv:open','A'); await tick(); flushFrames();
assert.match(host.querySelector('.cqa-card__pending-summary').textContent,/Ещё не оценён/);
assert.equal(host.querySelector('.cqa-card__missing-summary'),null);
assert.equal(host.querySelectorAll('[data-answer-state="pending"]').length,3);
assert.equal(host.querySelectorAll('[data-card-retry]').length,1,'one primary action for an unassessed dialogue');
assert.equal(host.querySelector('[data-card-retry]').textContent,'Оценить диалог');
click(host.querySelector('[data-card-retry]'));
assert.deepEqual(posts[0].conv_ids,['A']);
pendingRun.resolve({run_id:'empty-attempt',status:'failed',version:7}); await tick(); flushFrames();
assert.equal(host.querySelector('.cqa-card__pending-summary'),null);
assert.match(host.querySelector('.cqa-card__missing-summary').textContent,/Критериев без ответа: 3/);
assert.equal(host.querySelector('[data-card-retry]').textContent,'Переоценить диалог');
stopPending();
console.log('PASS: neutral unassessed state, one pinned evaluation action, failed empty attempt becomes missing-response state.');

// Remount simulates reload: only a scoped run pointer survives, never a transcript or answer.
const { webcrypto } = await import('node:crypto');
const savedCrypto = Object.getOwnPropertyDescriptor(globalThis, 'crypto');
Object.defineProperty(globalThis, 'crypto', {configurable:true,value:webcrypto});
const storedAttempts = new Map();
window.localStorage={getItem:key=>storedAttempts.get(key)||null,setItem:(key,value)=>storedAttempts.set(key,value),removeItem:key=>storedAttempts.delete(key)};
CQA.config={role:'operator',mock:false,apiBase:'./api'};
CQA.state.health={prompt_version:'synthetic-prompt'};
let configuredProfile='cloud', recoveryPosts=0, recoveryReads=[], recoveryOffline=false;
sourceDetail={...incomplete(),evals:[],score:{total:null,status:'incomplete',lines:[]}};
const savedRun={run_id:'run-browser-known',version:7,profile:'cloud',mode:'fast',status:'failed',finished_at:'2026-11-07T20:00:00Z',per_conv:[{id:'A',status:'error'}]};
CQA.api={
  config:async()=>({profile:configuredProfile,mode:'fast',model:{model:'synthetic-provider'}}),
  conversation:async(id,options)=>({...structuredClone(sourceDetail),id}),
  createRun:async()=>{recoveryPosts++;return structuredClone(savedRun);},
  run:async id=>{recoveryReads.push(id);if(recoveryOffline)throw Error('offline');return structuredClone(savedRun);},
  runs:()=>{throw Error('must not scan run history');},
};
async function until(predicate){for(let i=0;i<100;i++){if(predicate())return;await tick();}assert.fail('state did not settle');}
const firstVisit=mount(CQA);CQA.bus.emit('conv:open','A');
await until(()=>host.querySelector('[data-card-retry]')?.textContent==='Оценить диалог');
click(host.querySelector('[data-card-retry]'));
await until(()=>host.querySelector('.cqa-card__missing-summary'));
assert.equal(recoveryPosts,1);assert.equal(storedAttempts.size,1);
assert.doesNotMatch(JSON.stringify([...storedAttempts]),/Демонстрационная|Синтетический|A:0/);
firstVisit();
const afterReload=mount(CQA);CQA.bus.emit('conv:open','A');
await until(()=>host.querySelector('.cqa-card__missing-summary'));
assert.deepEqual(recoveryReads,['run-browser-known']);assert.equal(recoveryPosts,1);
assert.equal(host.querySelector('[data-card-retry]').textContent,'Переоценить диалог');
afterReload();
configuredProfile='local';
const otherProfile=mount(CQA);CQA.bus.emit('conv:open','A');
await until(()=>host.querySelector('.cqa-card__pending-summary'));
assert.equal(recoveryReads.length,1,'another profile must not inherit a failed run');
assert.equal(storedAttempts.size,1,'unrelated scope does not delete the original pointer');
otherProfile();
configuredProfile='cloud';sourceDetail=fresh();recoveryOffline=true;
const oldFullResult=mount(CQA);CQA.bus.emit('conv:open','A');
await until(()=>host.querySelector('[data-card-retry]')?.textContent==='Продолжить проверку прогона');
assert.equal(host.querySelector('.cqa-card__missing-summary'),null,'offline lookup alone is not evidence of failure');
recoveryOffline=false;click(host.querySelector('[data-card-retry]'));
await until(()=>host.querySelector('.cqa-card__missing-summary'));
assert.match(host.querySelector('.cqa-card__missing-summary').textContent,/Обновление|обновление/);
assert.equal(storedAttempts.size,1,'old full scores are not proof the failed run recovered');
assert.equal(host.querySelector('[data-card-retry]').textContent,'Переоценить диалог');
oldFullResult();
sourceDetail.evals=sourceDetail.evals.map(evaluation=>({...evaluation,created_at:'2026-11-07T20:00:01Z'}));
const newerSuccess=mount(CQA);CQA.bus.emit('conv:open','A');
await until(()=>storedAttempts.size===0);
assert.equal(host.querySelector('.cqa-card__total').textContent,'88/ 100');
assert.equal(host.querySelector('.cqa-card__missing-summary'),null);
assert.doesNotMatch(host.querySelector('.cqa-card__retry-status').textContent,/ошибкой|Критериев без ответа/);
assert.equal(recoveryPosts,1);assert.deepEqual(recoveryReads,Array(4).fill('run-browser-known'));
newerSuccess();
if(savedCrypto)Object.defineProperty(globalThis,'crypto',savedCrypto);else delete globalThis.crypto;
console.log('PASS: browser-local run pointer verified after remount; failed zero-answer restored; other profile isolated; newer complete card wins; no model POST on reload.');
