import assert from 'node:assert/strict';
import test from 'node:test';
import { readFile } from 'node:fs/promises';

const source = await readFile(new URL('../web/js/editor.js', import.meta.url), 'utf8');
const editor = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
const V1 = JSON.parse(await readFile(new URL('../data/checklists/v1.json', import.meta.url), 'utf8'));
const copy = value => structuredClone(value);

test('official v1 preserves C6 scale versus weight, C8 polarity, N/A and two decimal digits', () => {
  const validated = editor.validate(V1);
  assert.equal(validated.ok, true);
  assert.equal(validated.weightSum, 100);
  const body = editor.buildBody(V1, { parent_version: 1, change_note: 'Новая версия' });
  assert.deepEqual(body.rounding, { total: 2, mode: 'half_up' });
  assert.deepEqual(body.criteria.find(c => c.id === 'C6').scale, [0,4,7,10]);
  assert.equal(body.criteria.find(c => c.id === 'C6').max_points, 15);
  assert.equal(body.criteria.find(c => c.id === 'C8').critical_when, 'yes');
  assert.deepEqual(body.criteria.filter(c => c.na_allowed).map(c => c.id), ['C2','C3','C4','C7']);
  assert.equal(body.version, undefined);
  assert.ok(body.criteria.every(c => !Object.hasOwn(c, 'hash')));
  assert.equal(editor.formatNumber(85.38), '85,38');
  assert.equal(editor.formatNumber(99.99), '99,99');
});

test('non-100 weights warn, normalize exactly and retain the zero-weight critical flag', () => {
  const changed = copy(V1); changed.criteria[0].weight = 13;
  const result = editor.validate(changed);
  assert.equal(result.ok, true); assert.equal(result.weightSum, 103);
  assert.ok(result.warnings.some(issue => issue.field === 'weights'));
  const normalized = editor.normalizeWeights(changed.criteria);
  assert.equal(Math.round(normalized.reduce((sum,c) => sum+c.weight,0)*100),10000);
  assert.equal(normalized.find(c => c.id === 'C8').weight,0);
  assert.equal(changed.criteria[0].weight,13,'normalization must not mutate the input');
});

test('invalid scales, duplicate IDs and N/A without a rule block saving', () => {
  for (const mutate of [c => { c.criteria[3].scale=[0,5,5,20]; }, c => { c.criteria[3].scale=[0,20,10]; }, c => { c.criteria[1].na_rule=''; }, c => { c.criteria[1].id='C1'; }, c => { c.criteria[7].weight=4; }]) {
    const bad=copy(V1);mutate(bad);assert.equal(editor.validate(bad).ok,false);
  }
});

test('weight, section and derived fields reuse answers while prompt changes reevaluate only the changed criterion', () => {
  const weights=copy(V1);weights.criteria[0].weight=12;weights.criteria[0].max_points=12;weights.criteria[0].hash='server-derived';
  let diff=editor.classifyDiff(V1,weights);
  assert.deepEqual(diff.needs_reeval,[]);assert.deepEqual(diff.rescore_only,['C1']);
  const wording=copy(V1);wording.criteria[3].description='Синтетическое новое условие.';
  diff=editor.classifyDiff(V1,wording);assert.deepEqual(diff.needs_reeval,['C4']);
  const scale=copy(V1);scale.criteria[5].scale=[0,5,10];
  assert.deepEqual(editor.classifyDiff(V1,scale).needs_reeval,['C6']);
});

test('import rejects malformed JSON but keeps editable validation errors and drops stale hashes', () => {
  assert.equal(editor.parseImport('{bad').ok,false);
  const good=editor.parseImport(JSON.stringify(V1));
  assert.equal(good.ok,true);assert.equal(good.validation.ok,true);
  assert.ok(good.checklist.criteria.every(c=>!Object.hasOwn(c,'hash')));
  const changed=copy(V1);changed.criteria[1].na_rule='';
  const result=editor.parseImport(changed);assert.equal(result.ok,true);assert.equal(result.validation.ok,false);
});

test('run summaries recognize terminal failures and do not invent measurements', () => {
  assert.equal(editor.runSummary({status:'complete',done:3,total:3}).finished,true);
  const fail=editor.runSummary({status:'failed',done:1,total:3});
  assert.equal(fail.failed,true);assert.equal(fail.cost_kzt,null);assert.equal(fail.llm_calls,null);
  const pending=editor.runSummary({status:'running',done:1,total:3});assert.equal(pending.finished,false);
});

// Minimal DOM for state/event tests, intentionally not a browser layout emulator.
class Element {
  constructor(tag='div', text='') { this.tagName=tag.toUpperCase();this.children=[];this.parentNode=null;this.attrs={};this.dataset={};this.className='';this._text=text;this.listeners=new Map();this.hidden=false;this.value='';this.style={setProperty:(k,v)=>{this.style[k]=v;}}; }
  get textContent(){return this._text+this.children.map(c=>c.textContent).join('');}
  set textContent(value){this._text=String(value);this.replaceChildren();}
  get id(){return this.attrs.id||'';}set id(v){this.attrs.id=v;}
  get classList(){return {add:(...v)=>{this.className=[...new Set([...this.className.split(' ').filter(Boolean),...v])].join(' ');},remove:(...v)=>{this.className=this.className.split(' ').filter(x=>!v.includes(x)).join(' ');},contains:v=>this.className.split(' ').includes(v),toggle:(v,force)=>{const on=force??!this.className.split(' ').includes(v);if(on)this.classList.add(v);else this.classList.remove(v);return on;}};}
  append(...nodes){for(const node of nodes.flat()){if(!node)continue;node.parentNode=this;this.children.push(node);}}
  replaceChildren(...nodes){for(const node of this.children)node.parentNode=null;this.children=[];this.append(...nodes);}
  setAttribute(k,v){this.attrs[k]=String(v);if(k==='class')this.className=String(v);if(k.startsWith('data-'))this.dataset[k.slice(5).replace(/-([a-z])/g,(_,x)=>x.toUpperCase())]=String(v);if(k==='hidden')this.hidden=true;if(k==='disabled')this.disabled=true;if(k==='readonly')this.readOnly=true;}
  getAttribute(k){if(k==='class')return this.className;return this.attrs[k]??null;}
  removeAttribute(k){delete this.attrs[k];if(k==='hidden')this.hidden=false;if(k==='disabled')this.disabled=false;}
  hasAttribute(k){return k==='hidden'?this.hidden:Object.hasOwn(this.attrs,k);}
  matches(selector){
    if(selector==='[hidden]')return this.hidden;
    if(selector===':scope')return true;
    if(selector.includes(','))return selector.split(',').some(s=>this.matches(s.trim()));
    const tag=selector.match(/^[a-z][\w-]*/i)?.[0];if(tag&&this.tagName!==tag.toUpperCase())return false;
    const id=selector.match(/#([\w-]+)/)?.[1];if(id&&this.id!==id)return false;
    for(const m of selector.matchAll(/\.([\w-]+)/g))if(!this.classList.contains(m[1]))return false;
    for(const m of selector.matchAll(/\[([^=\]]+)(?:=["']?([^\]"']*)["']?)?\]/g)){
      const key=m[1];const value=key.startsWith('data-')?this.dataset[key.slice(5).replace(/-([a-z])/g,(_,x)=>x.toUpperCase())]:this.getAttribute(key);
      if(value===null||value===undefined||(m[2]!==undefined&&String(value)!==m[2]))return false;
    }
    return true;
  }
  querySelectorAll(selector){
    if(selector.startsWith(':scope > '))return this.children.filter(c=>c.matches(selector.slice(9)));
    const parts=selector.split(/\s+/);const match=node=>{if(!node.matches(parts.at(-1)))return false;let parent=node.parentNode;for(let i=parts.length-2;i>=0;i--){while(parent&&!parent.matches(parts[i]))parent=parent.parentNode;if(!parent)return false;parent=parent.parentNode;}return true;};
    const all=[];const walk=node=>{for(const child of node.children){if(match(child))all.push(child);walk(child);}};walk(this);return all;
  }
  querySelector(s){return this.querySelectorAll(s)[0]||null;}
  closest(s){let n=this;while(n){if(n.matches(s))return n;n=n.parentNode;}return null;}
  contains(node){for(let n=node;n;n=n.parentNode)if(n===this)return true;return false;}
  addEventListener(k,fn){if(!this.listeners.has(k))this.listeners.set(k,new Set());this.listeners.get(k).add(fn);}
  removeEventListener(k,fn){this.listeners.get(k)?.delete(fn);}
  focus(){document.activeElement=this;}
  getClientRects(){return this.hidden?[]:[{}];}
  get isConnected(){return !!this.parentNode;}
  scrollIntoView(){}
  setSelectionRange(){}
}
function dispatch(target,type,extra={}){const event={target,type,preventDefault(){this.defaultPrevented=true;},stopPropagation(){this.stopped=true;},...extra};for(let n=target;n;n=n.parentNode){for(const fn of n.listeners.get(type)||[])fn(event);if(event.stopped)break;}}
const tick=()=>new Promise(resolve=>setImmediate(resolve));
async function settle(){await tick();await tick();await tick();}
function harness(setup) {
  const head=new Element('head'),body=new Element('body'),host=new Element();host.id='cqa-editor';body.append(host);
  const style=new Element('link');style.dataset.cqaEditorStyle='';head.append(style);
  const store=new Map(),events=[],subscriptions=new Map();
  const saved={document:globalThis.document,window:globalThis.window,requestAnimationFrame:globalThis.requestAnimationFrame,cancelAnimationFrame:globalThis.cancelAnimationFrame};
  globalThis.document={head,body,activeElement:null,createElement:tag=>new Element(tag),createElementNS:(_,tag)=>new Element(tag),createTextNode:text=>new Element('#text',text),querySelector:s=>head.querySelector(s)||body.querySelector(s),getElementById:id=>body.querySelector('#'+id),addEventListener(){},removeEventListener(){}};
  globalThis.window={matchMedia:()=>({matches:true}),localStorage:{getItem:k=>store.get(k)||null,setItem:(k,v)=>store.set(k,v),removeItem:k=>store.delete(k)},addEventListener(){},removeEventListener(){}};
  globalThis.requestAnimationFrame=fn=>setTimeout(fn,0);globalThis.cancelAnimationFrame=clearTimeout;
  const versions=[{...copy(V1),active:true}],calls=[];
  const CQA={config:{mock:true},state:{modules:{board:false}},bus:{on:(k,fn)=>{if(!subscriptions.has(k))subscriptions.set(k,new Set());subscriptions.get(k).add(fn);return()=>subscriptions.get(k).delete(fn);},emit:(k,v)=>{events.push([k,v]);for(const fn of subscriptions.get(k)||[])fn(v);}},api:{checklists:async()=>copy(versions),checklist:async n=>copy(versions.find(v=>v.version===n)),conversations:async()=>[{id:'synthetic-1',group_id:'TEST'}],createChecklist:async body=>{calls.push(['save',body]);const v={...copy(body),version:2};versions.push(v);return v;},activateChecklist:async n=>{calls.push(['activate',n]);return{version:n};},createRun:async body=>{calls.push(['run',body]);return{run_id:'run-test'};},run:async()=>({status:'complete',done:1,total:1,is_mock:true,per_conv:[]})}};
  setup?.(CQA);
  const controller=editor.mount(CQA);
  return {host,store,events,calls,CQA,controller,click:action=>{const target=host.querySelector(`[data-action="${action}"]`);assert.ok(target,`missing ${action}`);dispatch(target,'click');},restore:()=>{controller.destroy();Object.assign(globalThis,saved);}};
}

test('budget profile switch blocks apply until settings settle and then pins the selected profile', async () => {
  const updates = []; let options, finishSwitch, switches = 0, configReads = 0;
  const h = harness(CQA => {
    CQA.api.modelGuide = async () => ({ reports: [] });
    CQA.api.config = async () => { configReads++; return { profile: 'routing', mode: 'fast' }; };
    CQA.api.conversations = async () => [{ id: 'a', channel: 'chat', group_id: 'TEST' }, { id: 'b', channel: 'call', group_id: 'TEST' }];
    CQA.api.setConfig = body => { switches++; assert.deepEqual(body, { location: 'cloud', profile: 'cascade', mode: 'fast' }); return new Promise(resolve => { finishSwitch = resolve; }); };
    CQA.economics = { mountBudget: (_host, value) => { options = value; return { update: value => updates.push(value), destroy() {} }; } };
  });
  try {
    await settle(); const latest = () => updates.at(-1) || options;
    assert.deepEqual(latest().plan, { dialogues: 2, channels: { chat: 1, call: 1 }, mode: 'fast' });
    assert.equal(latest().profile, 'routing');
    const switchPromise = options.onSwitchCascade();
    h.CQA.bus.emit('tab:changed', 'board'); h.CQA.bus.emit('tab:changed', 'editor');
    assert.equal(configReads, 1, 'returning to editor cannot replace a pending profile write with an older config read');
    assert.equal(latest().busy, true); h.click('apply'); await settle();
    assert.equal(h.calls.filter(([name]) => name === 'run').length, 0);
    finishSwitch({ profile: 'cascade', mode: 'fast' }); await switchPromise; await settle();
    assert.equal(latest().profile, 'cascade'); assert.equal(switches, 1);
    assert.equal(h.calls.filter(([name]) => name === 'run').length, 0);
    h.click('apply'); await settle();
    assert.deepEqual(h.calls.find(([name]) => name === 'run')[1], { version: 1, activate: true, profile: 'cascade', mode: 'fast' });
  } finally { h.restore(); }
});

test('failed budget profile change releases apply lock and preserves the previous profile', async () => {
  let options;
  const h = harness(CQA => {
    CQA.api.modelGuide = async () => ({ reports: [] }); CQA.api.config = async () => ({ profile: 'routing', mode: 'fast' });
    CQA.api.setConfig = async () => { throw new Error('offline'); };
    CQA.economics = { mountBudget: (_host, value) => { options = value; return { update() {}, destroy() {} }; } };
  });
  try {
    await settle(); await assert.rejects(options.onSwitchCascade(), /offline/);
    assert.equal(h.controller.state().busy, '');
    h.click('apply'); await settle(); assert.equal(h.calls.find(([name]) => name === 'run')[1].profile, 'routing');
  } finally { h.restore(); }
});

test('mock config mode notifications do not recursively reload budget; real source changes do', async () => {
  let reads = 0, mounts = 0, destroyed = 0;
  const h = harness(CQA => {
    CQA.api.modelGuide = async () => ({ reports: [] });
    CQA.api.config = async () => {
      reads++; if (reads > 4) throw new Error('recursive config request');
      CQA.bus.emit('api:mode', { mock: CQA.config.mock });
      return { profile: 'routing', mode: 'fast' };
    };
    CQA.economics = { mountBudget: () => { mounts++; return { update() {}, destroy() { destroyed++; } }; } };
  });
  try {
    await settle(); assert.equal(reads, 1); assert.equal(mounts, 1);
    h.CQA.bus.emit('api:mode'); await settle(); assert.equal(reads, 1);
    h.CQA.config.mock = false; h.CQA.bus.emit('api:mode'); await settle();
    assert.equal(reads, 2); assert.equal(mounts, 2); assert.equal(destroyed, 1);
  } finally { h.restore(); }
});

test('preview pins the same model profile and mode shown by the budget', async () => {
  const h = harness(CQA => {
    CQA.api.modelGuide = async () => ({ reports: [] }); CQA.api.config = async () => ({ profile: 'routing', mode: 'fast' });
    CQA.economics = { mountBudget: () => ({ update() {}, destroy() {} }) };
  });
  try {
    await settle(); h.click('preview'); await settle();
    assert.deepEqual(h.calls.find(([name]) => name === 'run')[1], { version: 1, preview: true, conv_ids: ['synthetic-1'], profile: 'routing', mode: 'fast' });
  } finally { h.restore(); }
});

test('standalone editor persists a draft, preserves it after failure, saves a new version and applies once', async () => {
  const h=harness();
  try {
    await settle();assert.equal(h.controller.state().view,1);
    assert.match(h.host.textContent,/Управление диалогом/);
    h.click('new-from');await settle();
    let state=h.controller.state();assert.equal(state.view,'draft');assert.ok(h.store.has(editor.STORAGE_KEY));
    const ui=state.draft.criteria.find(c=>c.id==='C1');
    const field=h.host.querySelector(`[data-field="weight"][data-uid="${ui.uid}"]`);
    // Collapsed rows retain state; expand through the public UI before editing.
    if(!field){const toggle=h.host.querySelector(`[data-action="toggle"][data-uid="${ui.uid}"]`);assert.ok(toggle);dispatch(toggle,'click');}
    const weight=h.host.querySelector(`[data-field="weight"][data-uid="${ui.uid}"]`);assert.ok(weight);
    weight.value='13';dispatch(weight,'input');
    const note=h.host.querySelector('#cqa-ed-note');assert.ok(note);note.value='Синтетическое изменение веса';dispatch(note,'input');
    await new Promise(resolve=>setTimeout(resolve,330));
    const persisted=JSON.parse(h.store.get(editor.STORAGE_KEY));assert.equal(persisted.criteria.find(c=>c.id==='C1').weight,'13');
    const save=h.CQA.api.createChecklist;h.CQA.api.createChecklist=async()=>{throw new Error('503');};
    h.click('save');await settle();state=h.controller.state();assert.ok(state.draft);assert.match(state.saveError,/503/);assert.equal(h.events.filter(([n])=>n==='checklist:saved').length,0);
    h.CQA.api.createChecklist=save;h.click('save');await settle();
    assert.equal(h.controller.state().view,2);assert.match(h.host.querySelector('.cqa-ed__subtitle').textContent,/Сохранена · не активна/);assert.doesNotMatch(h.host.querySelector('.cqa-ed__subtitle').textContent,/null/);assert.equal(h.controller.state().draft,null);assert.equal(h.store.has(editor.STORAGE_KEY),false);
    assert.equal(h.calls.find(([n])=>n==='save')[1].criteria.find(c=>c.id==='C1').weight,13);
    h.click('apply');await settle();
    assert.equal(h.calls.filter(([n])=>n==='run').length,1);
    assert.deepEqual(h.calls.find(([n])=>n==='run')[1],{version:2,activate:true});
    assert.equal(h.calls.filter(([n])=>n==='activate').length,0,'the run must capture its baseline before activation');
    assert.equal(h.events.filter(([n])=>n==='checklist:activated').length,0);
    assert.deepEqual(h.events.find(([n])=>n==='run:started')[1],{run_id:'run-test',version:2});
    assert.equal(h.events.some(([n,p])=>n==='nav:open'&&p==='board'), false, 'standalone editor owns observation');
    h.CQA.bus.emit('checklist:activated',{version:2});
    h.CQA.bus.emit('run:finished',{run_id:'run-test',version:2,status:'complete',done:1,total:1,activated:true});
    assert.equal(h.controller.state().sessionActive,2);
    assert.equal(h.controller.state().run.external,false);
  } finally { h.restore(); }
});

test('failed apply does not activate a new checklist or claim it became active', async () => {
  const h=harness();
  try {
    await settle();
    h.CQA.api.createRun=async()=>{throw new Error('503');};
    h.click('apply');await settle();
    assert.equal(h.calls.filter(([n])=>n==='activate').length,0);
    assert.equal(h.events.filter(([n])=>n==='checklist:activated'||n==='run:started').length,0);
    assert.equal(h.controller.state().sessionActive,null);
    assert.match(h.controller.state().applyError,/503/);
  } finally {h.restore();}
});

test('preview does not activate the checklist and emits terminal progress', async () => {
  const h=harness();
  try {
    await settle();h.click('preview');await settle();
    assert.equal(h.calls.filter(([n])=>n==='activate').length,0);
    assert.deepEqual(h.calls.find(([n])=>n==='run')[1],{version:1,preview:true,conv_ids:['synthetic-1']});
    assert.ok(h.events.some(([n,p])=>n==='run:progress'&&p.finished===true&&p.preview===true));
  } finally {h.restore();}
});

test('uploaded groups refresh in the real editor, preserve selection and ignore stale responses', async () => {
  const h=harness();
  try {
    await settle();
    const initial=[{id:'synthetic-1',group_id:'TEST'},{id:'training-1',group_id:'TRAIN'}];
    h.CQA.api.conversations=async()=>initial;
    h.CQA.bus.emit('conversations:refresh');await settle();
    let select=h.host.querySelector('#cqa-ed-group');assert.ok(select);
    select.value='TEST';dispatch(select,'change');
    let finishOld;
    h.CQA.api.conversations=()=>new Promise(resolve=>{finishOld=resolve;});
    h.CQA.bus.emit('conversations:refresh');
    assert.equal(h.controller.state().groupsLoading,true);
    h.click('apply');await settle();
    assert.equal(h.calls.filter(([name])=>name==='run').length,0,'do not apply a stale set while refreshing');
    const uploaded=[...initial,{id:'control-1',group_id:'CONTROL'},{id:'control-2',group_id:'CONTROL'}];
    h.CQA.api.conversations=async()=>uploaded;
    h.CQA.bus.emit('conversations:refresh');await settle();
    assert.equal(h.controller.state().group,'TEST','keep an existing selected group');
    select=h.host.querySelector('#cqa-ed-group');
    assert.ok(select.children.some(node=>node.value==='CONTROL'),'new upload is selectable');
    finishOld([{id:'obsolete-1',group_id:'OBSOLETE'}]);await settle();
    assert.ok(h.controller.state().groups.some(group=>group.id==='CONTROL'),'late old response cannot replace fresh groups');
    assert.equal(h.controller.state().group,'TEST');
    select=h.host.querySelector('#cqa-ed-group');select.value='CONTROL';dispatch(select,'change');
    h.click('apply');await settle();
    assert.deepEqual(h.calls.find(([name])=>name==='run')[1],{version:1,activate:true,conv_ids:['control-1','control-2']});
    h.CQA.api.conversations=async()=>{throw new Error('503');};
    h.CQA.bus.emit('conversations:refresh');await settle();
    assert.match(h.controller.state().groupsError,/503/);
    h.click('apply');await settle();assert.equal(h.calls.filter(([name])=>name==='run').length,1);
    h.CQA.api.conversations=async()=>initial;
    h.click('refresh-groups');await settle();
    assert.equal(h.controller.state().groupsError,'');
    assert.equal(h.controller.state().group,'','removed selection must not keep stale conversation IDs');
  } finally {h.restore();}
});


test('preview prefers current-version answers, excludes unknown versions and fills only the remaining slots', () => {
  const row = (id, version = 1, answer = 'yes') => ({id, checklist_version:version, cells:[{criterion_id:'C1',answer}]});
  const rows = [row('new',1,null),row('old',2),...['a','b','c','d','e'].map(id=>row(id))];
  assert.deepEqual(editor.selectPreview(rows,1), {ids:['a','b','c','d','e'],unscored:0});
  assert.deepEqual(editor.selectPreview(rows.slice(0,4),1), {ids:['a','b','new','old'],unscored:2});
  assert.deepEqual(editor.selectPreview([row('a'),row('a'),{id:''}],1), {ids:['a'],unscored:0});
});

test('preview sends its scored sample and explains unscored additions before starting', async () => {
  const h=harness();
  try {
    await settle();
    h.CQA.api.conversations=async()=>[{id:'new',group_id:'TEST'},{id:'scored',group_id:'TEST',checklist_version:1,cells:[{answer:'yes'}]}];
    h.CQA.bus.emit('conversations:refresh');await settle();
    assert.match(h.host.querySelector('#cqa-ed-preview-hint').textContent,/У 1 нет оценок/);
    h.click('preview');await settle();
    assert.deepEqual(h.calls.find(([n])=>n==='run')[1],{version:1,preview:true,conv_ids:['scored','new']});
  } finally {h.restore();}
});


test('W10 assembled editor delegates apply to board planning without creating a run', async () => {
  const h = harness(CQA => { CQA.state.modules.board = true; CQA.state.activeConversationId = 'synthetic-1'; });
  try {
    await settle(); h.click('apply'); await settle();
    assert.equal(h.calls.some(([name]) => name === 'run' || name === 'activate'), false);
    assert.deepEqual(h.events.find(([name]) => name === 'board:prepare')[1], { version: 1, group: '' });
    assert.ok(h.events.some(([name, value]) => name === 'nav:open' && value === 'board'));
  } finally { h.restore(); }
});
