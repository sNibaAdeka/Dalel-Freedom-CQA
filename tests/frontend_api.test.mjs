import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile, mkdtemp, rm } from 'node:fs/promises';
import { execFileSync } from 'node:child_process';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

const source = await readFile(new URL('../web/js/api.js', import.meta.url), 'utf8');
const { createApi, scoreFor, makeChecklist, criterionHash } = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
globalThis.document = { baseURI: 'http://cqa.test/' };
function context(config = {}) {
  const events = [];
  const CQA = {config:{mock:true, ...config},state:{},bus:{emit:(...args)=>events.push(args)}};
  return {CQA,events,api:createApi(CQA)};
}

test('65 synthetic details have consistent totals and exact operator evidence', async () => {
  const {api}=context();
  const rows=await api.conversations();
  assert.equal(rows.length,65);
  assert.equal(rows.filter(r=>r.channel==='call').length,5);
  for(const row of rows){
    const d=await api.conversation(row.id);
    assert.equal(d.is_mock,true);
    assert.equal(d.evals.length,8);
    assert.ok(Math.abs(d.score.total-d.score.total_raw)<=0.005000001);
    assert.ok(Math.abs(d.score.lines.reduce((n,x)=>n+x.weight_effective,0)-100)<1e-6);
    for(const ev of d.evals) for(const e of ev.evidence){
      const m=d.messages.find(x=>x.id===e.message_id);
      assert.equal(m.speaker,'operator');
      assert.equal(m.text.slice(e.start,e.end),e.quote);
    }
  }
});

test('filters, missing IDs and returned values do not mutate source', async () => {
  const {api}=context();
  assert.equal((await api.conversations({lang:'KZ'})).length,15);
  assert.equal((await api.conversations({channel:'call'})).length,5);
  await assert.rejects(api.conversation('missing'),/Диалог не найден/);
  const row=(await api.conversations())[0];
  const d=await api.conversation(row.id);d.messages[0].text='changed';
  assert.notEqual((await api.conversation(row.id)).messages[0].text,'changed');
});

test('decision validates input, preserves original evaluations and prevents double submit', async () => {
  const {api}=context();const row=(await api.conversations())[0];
  const before=await api.conversation(row.id);
  await assert.rejects(api.createAppeal({conv_id:row.id,criterion_id:'C8',comment:''}));
  const appeal=await api.createAppeal({conv_id:row.id,criterion_id:'C8',comment:'Проверить критерий',message_ids:[]});
  await assert.rejects(api.createDecision({appeal_id:appeal.id,action:'change',new_answer:'no',reason:''}));
  await assert.rejects(api.createDecision({appeal_id:appeal.id,action:'change',new_answer:'na',reason:'Тест'}));
  const result=await api.createDecision({appeal_id:appeal.id,action:'change',new_answer:'no',reason:'Проверено вручную'});
  const after=await api.conversation(row.id);
  assert.deepEqual(after.evals,before.evals);
  assert.equal(after.decisions.length,1);
  assert.equal(after.score.critical,false);
  assert.equal(result.new_total,after.score.total);
  await assert.rejects(api.createDecision({appeal_id:appeal.id,action:'confirm',reason:'Повтор'}));
});

test('live errors and offline mode never fabricate successful writes', async () => {
  const original=globalThis.fetch;
  try{
    let calls=0;
    globalThis.fetch=async()=>{calls++;return {ok:false,status:503}};
    const {api,CQA}=context({mock:false});
    await assert.rejects(api.conversations(),/503/);
    await assert.rejects(api.createAppeal({comment:'Проверка'}),/503/);
    assert.equal(CQA.config.mock,false);
    assert.equal(calls,2);
    CQA.config.offline=true;
    await assert.rejects(api.conversations(),/Офлайн/);
    assert.equal(calls,2);
  }finally{globalThis.fetch=original;}
});

test('private/synthetic fixture mode shares the generator response contract', async () => {
  const original=globalThis.fetch;
  const fixtureDirectory=await mkdtemp(join(tmpdir(),'cqa-api-fixtures-'));
  try{
    execFileSync('python3',[fileURLToPath(new URL('../scripts/make_ui_mocks.py',import.meta.url)),'--synthetic','--out',fixtureDirectory],{stdio:'pipe'});
    globalThis.fetch=async(url)=>{
      const filename=new URL(url).pathname.split('/').at(-1);
      const text=await readFile(join(fixtureDirectory,filename),'utf8');
      return {ok:true,status:200,json:async()=>JSON.parse(text)};
    };
    const {api}=context({mockBase:'./mocks/'});
    const rows=await api.conversations();assert.equal(rows.length,65);
    assert.equal((await api.checklists()).length,2);
    for(const row of rows){
      const d=await api.conversation(row.id);
      assert.equal(d.id,row.id);assert.equal(d.evals.length,8);assert.equal(d.is_mock,true);
    }
    const call=rows.find(r=>r.channel==='call');
    const audio=await api.audio(call.id);assert.equal(audio.src,null);assert.ok(Object.keys(audio.offsets).length);
  }finally{globalThis.fetch=original;await rm(fixtureDirectory,{recursive:true,force:true});}
});


test('official checklist and exact hashes match the backend definition', async () => {
  const official=JSON.parse(await readFile(new URL('../data/checklists/v1.json',import.meta.url),'utf8'));
  const v1=makeChecklist();
  assert.deepEqual(v1.criteria,official.criteria);
  for(const c of v1.criteria)assert.equal(criterionHash(c),c.hash,c.id);
  const v2=makeChecklist(2);
  assert.deepEqual(v1.criteria.filter((c,i)=>c.hash!==v2.criteria[i].hash).map(c=>c.id),['C4']);
});

test('all 5184 official answer combinations match an independent rational oracle', () => {
  const cases=JSON.parse(execFileSync('python3',[fileURLToPath(new URL('./frontend_v1_oracle.py',import.meta.url))],{encoding:'utf8',maxBuffer:4*1024*1024}));
  const checklist=makeChecklist();assert.equal(cases.length,5184);
  for(const item of cases){const evs=Object.entries(item.answers).map(([criterion_id,answer])=>({criterion_id,answer,critical_hit:false}));const score=scoreFor('oracle',checklist,evs);assert.equal(score.total,item.total,JSON.stringify(item.answers));assert.equal(score.critical,item.critical);}
});

test('PDF example, non-100 weights, zero applicability and fractional rounding', () => {
  const checklist=makeChecklist(),answers={C1:'yes',C2:'na',C3:'na',C4:15,C5:'yes',C6:7,C7:'yes',C8:'no'};
  const evs=Object.entries(answers).map(([criterion_id,answer])=>({criterion_id,answer}));
  const score=scoreFor('pdf',checklist,evs);
  assert.equal(score.total,85.38);assert.equal(score.lines[5].points,10.5);assert.equal(score.critical,false);assert.equal(Object.keys(score.sections).length,4);assert.equal(score.sections['Грубые нарушения'],null);
  const none=structuredClone(checklist);none.criteria=none.criteria.filter(c=>c.na_allowed);const noScore=scoreFor('none',none,none.criteria.map(c=>({criterion_id:c.id,answer:'na'})));assert.equal(noScore.total,null);assert.equal(noScore.status,'no_applicable');
  const custom=structuredClone(checklist);custom.criteria=[{...custom.criteria[0],weight:.005},{...custom.criteria[4],weight:.995}];
  assert.equal(scoreFor('fraction',custom,[{criterion_id:'C1',answer:'yes'},{criterion_id:'C5',answer:'no'}]).total,.5);
});

test('weight-only changes reuse every evaluation; changed wording only re-evaluates one',async()=>{
  const {api}=context();const row=(await api.conversations())[1],before=await api.conversation(row.id),v1=await api.checklist(1);
  const weighted=structuredClone(v1);weighted.criteria[0].weight=27.5;weighted.criteria[0].max_points=27.5;
  const w=await api.createChecklist({...weighted,base_version:1});assert.deepEqual(w.diff.needs_reeval,[]);assert.deepEqual(w.diff.rescore_only,['C1']);assert.ok(w.warnings.length);
  const first=await api.run((await api.createRun({version:w.version,conv_ids:[row.id],live:true})).run_id);assert.equal(first.from_cache,8);assert.equal(first.simulated_evaluations,0);assert.equal(first.llm_calls,0);
  const rewrite=structuredClone(w);rewrite.criteria[3].description='Новое проверяемое условие.';
  const changed=await api.createChecklist({...rewrite,base_version:w.version});assert.deepEqual(changed.diff.needs_reeval,['C4']);
  const next=await api.run((await api.createRun({version:changed.version,conv_ids:[row.id],activate:true,live:true})).run_id);assert.equal(next.from_cache,7);assert.equal(next.simulated_evaluations,1);assert.equal(next.llm_calls,0);
  const old=await api.conversation(row.id,{version:1});assert.deepEqual(old.evals,before.evals);assert.equal(old.score.total,before.score.total);
  const updated=await api.conversation(row.id);assert.equal(updated.checklist.version,changed.version);assert.equal((await api.conversations()).find(r=>r.id===row.id).total,updated.score.total);
});

test('checklist validation, imported conversations and mode-specific cache are real mock transitions',async()=>{
  const {api}=context();const bad=await api.checklist(1);bad.criteria[3].scale=[];await assert.rejects(api.createChecklist(bad),/шкал/);
  await assert.rejects(api.ingest('CONTROL'),/демо/);
  const payload=[{conversation_id:'upload-demo',channel:'agent chat',language:'EN',transcript:[{speaker:'operator',text:'A new synthetic response.',timestamp:'00:00'}]}];
  const result=await api.ingest('UPLOADED',payload);assert.equal(result.count,1);
  assert.equal((await api.conversation('upload-demo')).score.total,null);
  const run=await api.run((await api.createRun({version:1,conv_ids:['upload-demo'],mode:'fast'})).run_id);assert.equal(run.done,1);assert.equal(run.status,'complete');assert.equal(run.simulated_evaluations,8);
  const accurate=await api.run((await api.createRun({version:1,conv_ids:['upload-demo'],mode:'accurate'})).run_id);assert.equal(accurate.from_cache,0);assert.equal(accurate.simulated_evaluations,8);
  const preview=await api.run((await api.createRun({version:2,conv_ids:['upload-demo'],preview:true})).run_id);assert.equal(preview.done,1);assert.equal((await api.conversation('upload-demo')).checklist.version,1);
  const cfg=await api.setConfig({location:'local'});assert.equal(cfg.profile,'local');
});


test('activation happens only on completion and old-version appeals cannot change new wording', async()=>{
  const {api}=context(),row=(await api.conversations())[1];
  const appeal=await api.createAppeal({conv_id:row.id,criterion_id:'C4',comment:'Проверить старую оценку',checklist_version:1});
  const started=await api.createRun({version:2,conv_ids:[row.id],activate:true});
  assert.equal((await api.checklists()).find(c=>c.active).version,1);
  const finished=await api.run(started.run_id);assert.equal(finished.activated,true);assert.equal((await api.checklists()).find(c=>c.active).version,2);
  const before=await api.conversation(row.id),result=await api.createDecision({appeal_id:appeal.id,action:'change',new_answer:0,reason:'Решение о старой формулировке'});
  assert.equal(result.new_total,before.score.total);assert.equal(result.warnings.length,1);
  const old=await api.conversation(row.id,1);assert.equal(old.score.lines.find(l=>l.criterion_id==='C4').answer,0);
  assert.deepEqual((await api.conversation(row.id)).evals,before.evals);
});


test('replacing a binary criterion with a scale evaluates only the changed domain',async()=>{
  const {api}=context(),row=(await api.conversations())[2],draft=await api.checklist(1);
  Object.assign(draft.criteria[1],{answer_type:'scale',scale:[0,2,4],na_allowed:false,na_rule:'',description:'Оценить уточнение запроса по новой шкале.'});
  const created=await api.createChecklist({...draft,base_version:1});assert.deepEqual(created.diff.needs_reeval,['C2']);
  const result=await api.run((await api.createRun({version:created.version,conv_ids:[row.id],activate:true})).run_id);
  assert.equal(result.from_cache,7);assert.equal(result.simulated_evaluations,1);
  const changed=await api.conversation(row.id);assert.ok([0,2,4].includes(changed.evals.find(e=>e.criterion_id==='C2').answer));
  assert.ok(changed.score.total>=0&&changed.score.total<=100);
});

test('quality reports never invent demo measurements or silently switch live to mock', async () => {
  const original = globalThis.fetch;
  try {
    let urls = [];
    globalThis.fetch = async url => { urls.push(String(url)); return { ok: false, status: 404 }; };
    assert.deepEqual(await context().api.quality(), { reports: [] });
    assert.equal(urls.length, 0);
    const { api, CQA } = context({ mock: false, fallbackToMock: true });
    assert.deepEqual(await api.quality(), { reports: [] });
    assert.equal(urls.length, 2);
    assert.ok(urls[0].endsWith('/data/quality.json'));
    assert.ok(urls[1].endsWith('/api/metrics/quality'));
    assert.equal(CQA.config.mock, false);
    urls = [];
    globalThis.fetch = async url => { urls.push(String(url)); return { ok: false, status: 503 }; };
    await assert.rejects(api.quality(), /503/);
    assert.equal(urls.length, 1);
    assert.equal(CQA.config.mock, false);
    urls = [];
    globalThis.fetch = async url => { urls.push(String(url)); return { ok: true, status: 200, json: async () => ({ reports: [{ id: 'x' }] }) }; };
    assert.deepEqual(await api.quality(), { reports: [{ id: 'x' }] });
    assert.equal(urls.length, 1, 'static report found: the missing API route is never requested');
    CQA.config.offline = true;
    await assert.rejects(api.quality(), /Офлайн/);
    assert.equal(urls.length, 1);
  } finally { globalThis.fetch = original; }
});

test('quality can load a published same-origin report but rejects cross-origin fallback', async () => {
  const original = globalThis.fetch;
  try {
    const expected = { reports: [{ id: 'synthetic-contract-test' }] };
    globalThis.fetch = async url => String(url).includes('/metrics/quality')
      ? { ok: false, status: 404 }
      : { ok: true, status: 200, json: async () => expected };
    assert.deepEqual(await context({mock:false}).api.quality(), expected);
    await assert.rejects(context({mock:false, qualityReportUrl:'https://elsewhere.invalid/quality.json'}).api.quality(), /том же сервере/);
  } finally { globalThis.fetch = original; }
});


test('model guide reads only the same-origin static report and never calls an evaluator in mock', async () => {
  const original = globalThis.fetch, requests = [];
  try {
    globalThis.fetch = async (url, options) => { requests.push({ url: String(url), method: options.method || 'GET' }); return { ok: true, json: async () => ({ profile_catalog: {}, reports: [] }) }; };
    const { api } = context(); await api.modelGuide();
    assert.deepEqual(requests, [{ url: 'http://cqa.test/data/quality.json', method: 'GET' }]);
    await assert.rejects(context({ qualityReportUrl: 'https://elsewhere.test/data.json' }).api.modelGuide(), /том же сервере/);
    assert.equal(requests.length, 1);
  } finally { globalThis.fetch = original; }
});
