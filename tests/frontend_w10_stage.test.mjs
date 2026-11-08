import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { runInNewContext } from 'node:vm';
const read = name => readFile(new URL(`../web/${name}`, import.meta.url), 'utf8');
const load = source => import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
const profilesSource = await read('js/model-profiles.js');
const profiles = await load(profilesSource);
const boardSource = await read('js/board.js');
const board = await load(boardSource.replace("'./model-profiles.js'", JSON.stringify(`data:text/javascript;base64,${Buffer.from(profilesSource).toString('base64')}`)));
const listSource = await read('js/list.js');
const list = await load(listSource);
const reviewSource = await read('js/review.js');
const review = await load(reviewSource);
const tick = () => new Promise(resolve => setImmediate(resolve));

test('U1: list diamond uses the card SVG, accessible name, and explicit dimensions in P mode', async () => {
  const path = 'M12 2 22 12 12 22 2 12Z M12 7v6m0 3v.1';
  assert.ok(listSource.includes(path)); assert.ok((await read('js/card.js')).includes(path));
  assert.match(listSource, /role="img" aria-label="Грубая ошибка"/);
  assert.match(await read('css/list.css'), /cq-list-diamond svg[^}]*width:20px; height:20px/);
});

test('U2: one v2 score and 37 v1 scores remain 38 evaluated, with honest auto/critical counts', async () => {
  const input = Array.from({length: 38}, (_, i) => ({id: String(i), checklist_version: 2, status: i ? 'incomplete' : 'evaluated', total: i ? null : 64.71}));
  let reads = 0;
  const rows = await list.withPreviousScores(input, [{version: 1}, {version: 2}], {conversation: async (id, version) => {
    reads++; assert.equal(version, 1);
    return {status: 'evaluated', score: {total: 76.47, critical: Number(id) < 4}, needs_review: false, evals: []};
  }});
  assert.equal(reads, 37);
  assert.equal(rows.filter(r => !['pending','running','error'].includes(list.evaluationState(r))).length, 38);
  assert.equal(rows.filter(r => list.evaluationState(r) === 'approved').length, 35);
  assert.equal(rows.filter(r => r.critical).length, 3);
  assert.equal(rows.filter(r => r.previous_score && r.checklist_version === 1).length, 37);
  assert.equal(input[1].total, null, 'never rewrite source/official evaluations');
});

test('U2: latest available older version wins; missing/error/running scores are not fabricated', async () => {
  const input = ['incomplete','running','error'].map((status, i) => ({id: String(i), status, total: null, checklist_version: 4}));
  const calls = [];
  const output = await list.withPreviousScores(input, [1,2,3,4].map(version => ({version})), {conversation: async (id, version) => {
    calls.push([id,version]); return {status:'evaluated',score:{total: version === 3 ? null : 80}};
  }});
  assert.deepEqual(calls, [['0',3],['0',2]]); assert.equal(output[0].checklist_version,2);
  assert.equal(output[1].total,null); assert.equal(output[2].total,null);
  const failed = await list.withPreviousScores([input[0]], [{version:1}], {conversation: async () => { throw Error('503'); }});
  assert.equal(failed[0].total,null);
});

test('U3: forecast uses selected profile and honest missing measurements; does not call any provider', async () => {
  const quality = JSON.parse(await read('data/quality.json'));
  for (const profile of ['cloud','cascade','routing']) {
    const report = quality.reports.find(r => r.id === quality.profile_catalog[profile].report_id);
    const forecast = profiles.runForecast({quality,profile,dialogues:38});
    assert.equal(forecast.cost, (report.economics?.cost_per_dialogue_usd ?? report.cost.amount/report.cost.count)*38);
    assert.equal(forecast.calls, profile === 'cloud' ? 38 : 76);
    assert.match(profiles.forecastText(forecast), /Диалогов: 38.*Вызовов модели/);
  }
  const unknown = profiles.runForecast({profile:'unknown',dialogues:65});
  assert.equal(unknown.cost,null); assert.equal(unknown.seconds,null); assert.equal(unknown.calls,null);
  assert.match(profiles.forecastText(unknown), /прогноза нет/);
  assert.equal(profiles.runForecast({quality,profile:'cloud',mode:'accurate',dialogues:2,criteria:8,votes:1}).calls,16);
});

// Tiny DOM executes actual board event handlers; geometry is covered by the browser suite.
class Element {
  constructor(tag) { this.tagName=tag.toUpperCase(); this.children=[]; this.dataset={}; this.style={}; this.listeners={}; this.attributes={}; this.className=''; this._text=''; this.checked=false; this.files=[]; }
  set textContent(v) {this.children=[];this._text=String(v);} get textContent(){return this._text+this.children.map(c=>c.textContent).join('');}
  get value(){return this._value??(this.tagName==='SELECT'?this.children[0]?.value||'':'');} set value(v){this._value=String(v);}
  get classList(){return {add:()=>{},remove:()=>{}};}
  append(...children){this.children.push(...children);children.forEach(c=>c.parentNode=this);}
  replaceChildren(...children){this.children=[];this._text='';this.append(...children);if(this.tagName==='SELECT')this._value=undefined;}
  setAttribute(k,v){this.attributes[k]=v;} addEventListener(k,fn){(this.listeners[k]||=[]).push(fn);}
  dispatch(k){return Promise.all((this.listeners[k]||[]).map(fn=>fn({preventDefault(){},target:this})));}
  focus(){} remove(){this.parentNode.children=this.parentNode.children.filter(c=>c!==this);}
}
const walk = root => [root,...root.children.flatMap(walk)];
function mount(overrides={}) {
  const host=new Element('div'), listeners=new Map(), events=[];
  globalThis.document={head:new Element('head'),createElement:t=>new Element(t),getElementById:id=>id==='cqa-board'?host:null,querySelector:()=>({})};
  const CQA={config:{mock:false},state:{activeConversationId:'a',checklistVersion:1},bus:{on(n,f){listeners.set(n,f);return ()=>listeners.delete(n);},emit(n,p){events.push([n,p]);listeners.get(n)?.(p);}},api:{
    conversations:async()=>[{id:'a',total:80},{id:'b',total:90}],checklists:async()=>[{version:1}],config:async()=>({profile:'cloud',mode:'fast'}),...overrides}};
  const destroy=board.mount(CQA);
  return {host,CQA,events,destroy,find:cls=>walk(host).find(n=>n.className.split(' ').includes(cls))};
}

test('U3: open-dialogue default, no activation; batch cancellation prevents config and run writes even with cache enabled', async t => {
  const calls=[]; const ui=mount({setConfig:async b=>{calls.push(['config',b]);return b;},createRun:async b=>{calls.push(['run',b]);throw Error('synthetic stop');}});
  t.after(ui.destroy); await tick();
  const scope=walk(ui.host).find(n=>n.tagName==='SELECT'&&n.children.some(c=>c.value==='active'));
  const activate=walk(ui.host).find(n=>Object.hasOwn(n.dataset,'boardActivate'));
  assert.equal(scope.value,'active'); assert.equal(activate.checked,false);
  await ui.find('cqa-board__form').dispatch('submit');
  assert.deepEqual(calls[1][1].conv_ids,['a']); assert.equal(calls[1][1].activate,false);
  calls.length=0; scope.value='all'; await scope.dispatch('change');
  const old=globalThis.confirm; t.after(()=>{globalThis.confirm=old;});
  globalThis.confirm=text=>{assert.match(text,/Диалогов: 2.*Вызовов модели/);assert.match(text,/прогноза нет/);return false;};
  await ui.find('cqa-board__form').dispatch('submit'); assert.equal(calls.length,0);
  globalThis.confirm=()=>true; await ui.find('cqa-board__form').dispatch('submit'); assert.deepEqual(calls[1][1].conv_ids,['a','b']);
  scope.value='active'; activate.checked=true; await scope.dispatch('change'); assert.equal(activate.checked,false);
});

test('U4: forecast and database accounting are separate; current-version aggregate is not falsely called total spend', async t => {
  const ui=mount({metrics:async()=>({cost_kzt:1483.91})});t.after(ui.destroy);await tick();
  assert.match(ui.host.textContent,/Прогноз этого прогона/);
  assert.match(ui.host.textContent,/Всего потрачено в этой базе: накопленный расход недоступен/);
  assert.match(ui.host.textContent,/Стоимость сохранённых оценок активной версии: 1\s483,91/);
  assert.doesNotMatch(await read('js/bank-economics.js'), /Оценка прогона по записанным расходам/);
});

test('U5: first empty Change enters editing; validation only on save or field blur', () => {
  const fragment=reviewSource.slice(reviewSource.indexOf('    let editing = false;'),reviewSource.indexOf("    confirm.node.addEventListener('click'"));
  let saves=0,focus=0;const handlers={};
  const context={reason:{addEventListener:(k,fn)=>handlers[k]=fn},answer:{read:()=>null,focus:()=>focus++,wrap:{addEventListener:(k,fn)=>handlers[k]=fn}},validateAnswer:review.validateAnswer,criterion:{answer_type:'binary'},controls:{},appeal:{},detail:{},current:'no',checklist:{},submitDecisionForm:()=>{saves++;return Promise.resolve();},fail:()=>{}};
  runInNewContext(fragment+"; decide('change');",context); assert.equal(focus,1);assert.equal(saves,0);
  runInNewContext("decide('change');",context); assert.equal(saves,1);
  assert.equal(typeof handlers.blur,'function');assert.equal(typeof handlers.focusout,'function');
});

test('U6: tab navigation resets workspace, panel, nested panes and mobile document', async () => {
  const source=await read('js/core.js');
  const fragment=source.slice(source.indexOf('export function resetPanelScroll'),source.indexOf('export function boot')).replace('export ','');
  const workspace={scrollTop:150},nested={scrollTop:240},panel={scrollTop:90,querySelectorAll:()=>[nested]},document={scrollingElement:{scrollTop:100}};
  runInNewContext(fragment+"; resetPanelScroll(root,panel);",{document,panel,root:{querySelector:()=>workspace}});
  for(const n of [workspace,panel,nested,document.scrollingElement]) assert.equal(n.scrollTop,0);
  assert.match(source,/resetPanelScroll\(root, byId\(`cqa-panel-\$\{tab\}`\)\)/);
});

test('U7: reload restores latest non-preview run via GET and does not replay activation', async t => {
  let reads=0;
  const ui=mount({runs:async()=>[{run_id:'preview',preview:true,created_at:'2026-11-09'},{run_id:'saved',status:'completed',version:2}],run:async id=>{reads++;assert.equal(id,'saved');return {run_id:id,status:'completed',activated:true,version:2,done:1,total:1,per_conv:[]};}});
  t.after(ui.destroy);await tick();await tick();
  assert.match(ui.host.textContent,/Оценка завершена/);assert.equal(reads,1);
  assert.equal(ui.events.some(([name])=>name==='checklist:activated'),false);
  assert.equal(board.latestRun([]),null);
});

test('U7: delayed history cannot replace a newly started run', async t => {
  let resolve; const history=new Promise(r=>resolve=r);
  const ui=mount({runs:()=>history,createRun:async()=>({run_id:'new',status:'completed',done:1,total:1}),run:async()=>assert.fail('stale history must not be watched')});
  t.after(ui.destroy);await tick();await ui.find('cqa-board__form').dispatch('submit');
  resolve([{run_id:'old',status:'completed'}]);await tick();assert.match(ui.host.textContent,/Прогон new/);
});

test('U8: system/bot evidence never preselected; first operator/client is the fallback; no fake evidence if absent', () => {
  const messages=[{id:'s',speaker:'system'},{id:'b',speaker:'bot'},{id:'c',speaker:'client'},{id:'o',speaker:'operator'}];
  assert.deepEqual(review.initialEvidenceIds(messages,[{message_id:'s'}]),['c']);
  assert.deepEqual(review.initialEvidenceIds(messages,[{message_id:'s'},{message_id:'o'}]),['o']);
  assert.deepEqual(review.initialEvidenceIds(messages.slice(0,2),[]),[]);
});

test('U2: cross-version score events reload the authoritative list instead of mislabelling a fallback', async () => {
  const source = await read('js/list.js');
  const body = source.slice(source.indexOf('  subscribe("score:updated",'), source.indexOf('  subscribe("conversations:updated",'));
  const row = {id:'a',checklist_version:1,total:76.47,previous_score:true};
  let callback,reloads=0;
  runInNewContext(body,{subscribe:(_name,fn)=>{callback=fn;},CQA:{state:{listScoreVersions:{a:1}}},state:{rows:[row]},reload:()=>{reloads++;}});
  callback({id:'a',checklist:{version:2},score:{total:64.71}});
  assert.equal(reloads,1);assert.equal(row.total,76.47);assert.equal(row.checklist_version,1);
});

test('U3: selecting a paid profile from a mock server still requires batch confirmation', async t => {
  const calls=[];const ui=mount({config:async()=>({profile:'mock',mode:'fast'}),setConfig:async b=>{calls.push(b);return b;},createRun:async b=>{calls.push(b);throw Error('stop');}});
  t.after(ui.destroy);await tick();
  const selectors=walk(ui.host).filter(n=>n.tagName==='SELECT');
  const scope=selectors.find(n=>n.children.some(c=>c.value==='active'));
  const profile=selectors.find(n=>n.children.some(c=>c.value==='cloud'));
  scope.value='all';await scope.dispatch('change');profile.value='cloud';await profile.dispatch('change');
  const old=globalThis.confirm;t.after(()=>{globalThis.confirm=old;});
  let asked=0;globalThis.confirm=()=>{asked++;return false;};
  await ui.find('cqa-board__form').dispatch('submit');assert.equal(asked,1);assert.equal(calls.length,0);
});

test('W10 changed text tokens maintain 4.5:1, with inherited focus, targets and reduced motion', async () => {
  const css=await read('app.css'),glass=await read('css/glass.css');
  const token=(source,name)=>source.match(new RegExp(`--${name}:\\s*(#[0-9a-f]{6})`,'i'))[1];
  const lum=hex=>hex.slice(1).match(/../g).map(v=>parseInt(v,16)/255).map(v=>v<=.04045?v/12.92:((v+.055)/1.055)**2.4).reduce((s,v,i)=>s+v*[.2126,.7152,.0722][i],0);
  for(const fg of [token(css,'muted'),token(css,'critical'),token(glass,'gl-muted')]){
    for(const bg of [token(css,'surface'),token(glass,'gl-base')])assert.ok((lum(bg)+.05)/(lum(fg)+.05)>=4.5);
  }
  assert.match(css,/:focus-visible[^}]*outline: 3px/);
  assert.match(css,/min-height: 44px/);
  assert.match(css,/prefers-reduced-motion/);
  assert.doesNotMatch((await read('css/list.css')).split('/* W10:')[1],/#[0-9a-f]{3,8}\b/i);
});
