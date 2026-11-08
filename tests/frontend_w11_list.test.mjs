import test from 'node:test';
import assert from 'node:assert/strict';
import { runInNewContext } from 'node:vm';
import { withPreviousScores } from '../web/js/list.js';
import { listHarness,source } from './helpers/w11-list.mjs';
import { tick,deferred } from './helpers/w11-board.mjs';
const row=(id,extra={})=>({id,total:null,status:'incomplete',checklist_version:2,...extra});
const historical={score:{total:76.47},status:'evaluated',evals:[]};
test('W11.2c: version map contains only previous_score rows; refresh and activation clear it synchronously',async()=>{
  const h=listHarness({conversation:async()=>historical},[row('active',{total:64.71}),row('old')]);
  await h.reload();assert.deepEqual({...h.CQA.state.listScoreVersions},{old:1});
  let pending=h.emit('conversations:refresh');assert.deepEqual({...h.CQA.state.listScoreVersions},{});await pending;
  pending=h.emit('checklist:activated',{version:2});assert.deepEqual({...h.CQA.state.listScoreVersions},{});await pending;
  h.emit('score:updated',{id:'old',checklist:{version:1},score:{total:70}});
  assert.equal(h.CQA.state.listScoreVersions.old,undefined);assert.equal(h.state.rows[1].previous_score,true);
});
test('W11.2d: opening any queue row never changes global editor/board version',()=>{
  const body=source.slice(source.indexOf('  function open(id,'),source.indexOf('  function chooseFirstOnce'));
  for(const previous_score of [true,false]){
    const emitted=[],CQA={state:{checklistVersion:2},bus:{emit:(...a)=>emitted.push(a)}};
    runInNewContext(body+"; open('a');",{CQA,state:{rows:[row('a',{checklist_version:1,previous_score})]},reflectSelection(){}});
    assert.equal(CQA.state.checklistVersion,2);assert.deepEqual(emitted,[['conv:open','a']]);
  }
});
test('W11.4: checklist GET failure preserves successful authoritative queue and makes no historical reads',async()=>{
  let gets=0;
  const h=listHarness({checklists:async()=>{throw Error('503');},conversation:async()=>{gets++;return historical;}},[row('a')]);
  await h.reload();assert.equal(h.state.error,false);assert.equal(h.state.rows.length,1);assert.equal(gets,0);
  assert.equal(h.state.rows[0].total,null);assert.deepEqual({...h.CQA.state.listScoreVersions},{});
});
test('W11.4: superseded historical lookup stops after at most six already-in-flight reads',async()=>{
  const input=Array.from({length:64},(_,i)=>row(String(i))),gate=deferred();let stale=false,gets=0;
  const work=withPreviousScores(input,[{version:1},{version:2}],{conversation:async()=>{gets++;await gate.promise;return historical;}},()=>stale);
  assert.equal(gets,6);stale=true;gate.resolve();await work;assert.equal(gets,6);
});
test('W11.4: superseded reload cannot start historical requests or overwrite the newest queue',async()=>{
  const first=deferred();let queues=0,gets=0;
  const h=listHarness({conversations:()=>++queues===1?first.promise:Promise.resolve([row('new',{total:64.71})]),conversation:async()=>{gets++;return historical;}});
  const old=h.reload();await h.reload();first.resolve([row('old')]);await old;
  assert.equal(gets,0);assert.equal(h.state.rows[0].id,'new');
});
test('W11.4: refresh plus a cross-version score event uses only one historical batch',async()=>{
  const gate=deferred();let gets=0,queues=0;
  const input=Array.from({length:64},(_,i)=>row(String(i)));
  const h=listHarness({conversations:async()=>{queues++;return input;},conversation:async()=>{gets++;await gate.promise;return historical;}},[row('0',{checklist_version:1,total:76.47,previous_score:true})]);
  const work=h.emit('conversations:refresh');await tick();assert.equal(gets,6);
  h.emit('score:updated',{id:'0',checklist:{version:2,active:true},score:{total:64.71}});
  gate.resolve();await work;assert.equal(queues,1);assert.equal(gets,64);
});
test('W11.4: checklist-less scores cannot overwrite a v1-labelled row with v2 or unknown-version data',async()=>{
  for(const score of [{total:64.71,checklist_version:2},{total:64.71}]){
    const gate=deferred(),old=row('a',{checklist_version:1,total:76.47,previous_score:true,cells:[]});
    const h=listHarness({conversations:()=>gate.promise},[old]);
    h.emit('score:updated',{conv_id:'a',score});assert.equal(old.total,76.47);assert.equal(old.checklist_version,1);
    assert.equal(h.state.loading,true);assert.equal(h.CQA.state.listScoreVersions.a,undefined);
    gate.resolve([]);await tick();
  }
});
