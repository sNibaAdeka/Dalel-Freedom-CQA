import assert from 'node:assert/strict';
import test from 'node:test';
import { readFile } from 'node:fs/promises';
import { webcrypto } from 'node:crypto';
const source = await readFile(new URL('../web/js/provider-state.js', import.meta.url), 'utf8');
const { attemptScope, createAttemptStore, matchesAttempt, createDialogueRetry, attemptResolved } = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
const fixture = () => ({id:'synthetic-A',messages:[{id:'A:0',speaker:'client',text:'Private synthetic transcript',ts_raw:'00:00'}],
  checklist:{version:3,criteria:[{id:'C1',answer_type:'binary',hash:'wording-v1'}]},evals:[],score:{status:'incomplete',lines:[]}});
const settings = {profile:'cloud',mode:'fast',model:{model:'synthetic-model'},prompt_version:'template-1'};
const scope = await attemptScope(fixture(),settings,'./api','https://example.invalid/product/',webcrypto);
const context = {conv_id:'synthetic-A',version:3,attemptScope:scope};
const failed = () => ({run_id:'run-known',version:3,profile:'cloud',mode:'fast',status:'failed',per_conv:[{id:'synthetic-A',status:'error'}]});
const memory = () => {const values=new Map();return {values,getItem:key=>values.get(key)||null,setItem:(key,value)=>values.set(key,value),removeItem:key=>values.delete(key)};};
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};};

test('recovery scope isolates API server, version, profile, model, prompt and input; storage contains no transcript or conversation ID',async()=>{
  const storage=memory(),store=createAttemptStore(storage);
  assert.equal(store.write(scope,'run-known'),true);
  assert.equal(store.read(scope),'run-known');
  assert.doesNotMatch(JSON.stringify([...storage.values]),/Private synthetic transcript|synthetic-A|wording-v1/);
  const variants=[
    await attemptScope(fixture(),settings,'./api','https://other.invalid/product/',webcrypto),
    await attemptScope({...fixture(),checklist:{...fixture().checklist,version:4}},settings,'./api','https://example.invalid/product/',webcrypto),
    await attemptScope(fixture(),{...settings,profile:'local'},'./api','https://example.invalid/product/',webcrypto),
    await attemptScope(fixture(),{...settings,model:{model:'another'}},'./api','https://example.invalid/product/',webcrypto),
    await attemptScope(fixture(),{...settings,prompt_version:'template-2'},'./api','https://example.invalid/product/',webcrypto),
    await attemptScope({...fixture(),messages:[{...fixture().messages[0],text:'Changed input'}]},settings,'./api','https://example.invalid/product/',webcrypto),
  ];
  for(const other of variants)assert.equal(store.read(other),null);
  assert.equal(await attemptScope(fixture(),settings,'https://user:secret@example.invalid/api','https://example.invalid/',webcrypto),null);
  assert.equal(await attemptScope(fixture(),settings,'./api?token=secret','https://example.invalid/',webcrypto),null);
  assert.equal(await attemptScope(fixture(),settings,'./api#secret','https://example.invalid/',webcrypto),null);
  assert.equal((await attemptScope(fixture(),settings,'https://EXAMPLE.invalid:443/product/../api/','https://example.invalid/',webcrypto)).server,'https://example.invalid/api/');
  assert.equal(await attemptScope(fixture(),settings,'./api','https://example.invalid/',{}),null);
});

test('storage corruption or denial is nonfatal and late completion cannot clear a newer pointer',()=>{
  const storage=memory(),store=createAttemptStore(storage);
  store.write(scope,'run-old');store.write(scope,'run-new');store.forget(scope,'run-old');
  assert.equal(store.read(scope),'run-new');store.forget(scope,'run-new');assert.equal(store.read(scope),null);
  assert.equal(store.write(scope,'../foreign'),false);
  const denied=createAttemptStore({getItem(){throw Error('denied');},setItem(){throw Error('quota');},removeItem(){throw Error('denied');}});
  assert.equal(denied.read(scope),null);assert.equal(denied.write(scope,'run-x'),false);assert.doesNotThrow(()=>denied.forget(scope));
});

test('exact known failed run survives a new controller after reload without POST or history scan',async()=>{
  const storage=memory(),store=createAttemptStore(storage),reads=[],received=[];
  let posts=0;
  const api={createRun:async()=>{posts++;return failed();},run:async id=>{reads.push(id);return failed();},conversation:async()=>fixture(),runs:()=>{throw Error('history scan forbidden');}};
  const first=createDialogueRetry(api,{onProgress:job=>store.write(job.context.attemptScope,job.run.run_id)});
  await first.start(context);first.destroy();assert.equal(posts,1);
  const second=createDialogueRetry(api,{onComplete:(detail,job)=>received.push({detail,job})});
  await second.recover(context,store.read(scope));
  assert.deepEqual(reads,['run-known']);assert.equal(posts,1);assert.equal(second.get(context).run.status,'failed');
  assert.equal(received[0].detail.evals.length,0);assert.equal(second.get(context).phase,'finished');second.destroy();
});

test('wrong run/version/profile/mode/dialogue and missing run never become proof or fetch conversation',async()=>{
  const variants=[{...failed(),run_id:'other'},{...failed(),version:4},{...failed(),profile:'local'},
    {...failed(),mode:'accurate'}, {...failed(),per_conv:[{id:'B'}]}, {...failed(),per_conv:[{id:'synthetic-A'},{id:'B'}]}, {...failed(),status:'unknown'}];
  for(const snapshot of variants){
    assert.equal(matchesAttempt(snapshot,context,'run-known'),false);
    const retry=createDialogueRetry({run:async()=>snapshot,conversation:async()=>{throw Error('must not fetch');}});
    await retry.recover(context,'run-known');assert.equal(retry.get(context).phase,'forgotten');assert.equal(retry.get(context).run,null);retry.destroy();
  }
  const retry=createDialogueRetry({run:async()=>{throw Object.assign(Error('missing'),{status:404});}});
  await retry.recover(context,'run-known');assert.equal(retry.get(context).phase,'forgotten');retry.destroy();
});

test('network uncertainty stays resumable and prevents a duplicate run',async()=>{
  let attempts=0,posts=0;
  const retry=createDialogueRetry({run:async()=>{if(++attempts===1)throw Error('offline');return failed();},conversation:async()=>fixture(),createRun:async()=>{posts++;return failed();}});
  await retry.recover(context,'run-known');assert.equal(retry.get(context).phase,'paused');assert.equal(retry.get(context).run,null);
  await retry.start(context);assert.equal(posts,0);
  await retry.resume(context);assert.equal(retry.get(context).phase,'finished');assert.equal(attempts,2);retry.destroy();
});

test('running recovery polls the same ID and fresh complete result supersedes a prior failure',async()=>{
  let scheduled,reads=0,complete;
  const retry=createDialogueRetry({run:async()=>{reads++;return reads===1?{...failed(),status:'running'}:{...failed(),status:'completed',per_conv:[{id:'synthetic-A',status:'done'}]};},
    conversation:async()=>({...fixture(),evals:[{criterion_id:'C1',answer:'yes'}],score:{total:100}})},
    {schedule:fn=>{scheduled=fn;return 1;},unschedule(){},onComplete:detail=>{complete=detail;}});
  await retry.recover(context,'run-known');assert.equal(retry.get(context).phase,'running');scheduled();
  await new Promise(resolve=>setImmediate(resolve));assert.equal(complete.score.total,100);assert.equal(reads,2);retry.destroy();
});

test('destroyed recovery ignores late transport responses',async()=>{
  const response=deferred();let completions=0,reads=0;
  const retry=createDialogueRetry({run:()=>response.promise,conversation:async()=>{reads++;return fixture();}}, {onComplete:()=>completions++});
  const work=retry.recover(context,'run-known');retry.destroy();response.resolve(failed());await work;
  assert.equal(reads,0);assert.equal(completions,0);
});


test('old full scores do not erase a failed forced run; exact success or strictly newer per-criterion timestamps are required',()=>{
  const complete={...fixture(),evals:[{criterion_id:'C1',answer:'yes'}],score:{total:100}};
  const run={...failed(),finished_at:'2026-11-07T20:00:00Z'};
  assert.equal(attemptResolved(complete,run),false);
  assert.equal(attemptResolved({...complete,checklist:{version:3}},run),false);
  assert.equal(attemptResolved({...complete,checklist:{version:3,criteria:[]}},run),false);
  for(const created_at of ['2026-11-07T19:59:59Z','2026-11-07T20:00:00Z','invalid']){
    assert.equal(attemptResolved({...complete,evals:[{...complete.evals[0],created_at}]},run),false);
  }
  assert.equal(attemptResolved({...complete,evals:[{...complete.evals[0],created_at:'2026-11-07T20:00:01Z'}]},run),true);
  assert.equal(attemptResolved(complete,{...run,status:'completed'}),true);
  assert.equal(attemptResolved(fixture(),{...run,status:'completed'}),false);
});
