import test from 'node:test';
import assert from 'node:assert/strict';
import { createRunController } from '../web/js/board.js';
import { mountBoard, tick, deferred } from './helpers/w11-board.mjs';
function clock() {
  const jobs = new Map(); let id = 0;
  return { schedule(fn) { jobs.set(++id, fn); return id; }, unschedule(id) { jobs.delete(id); },
    async next() { const entry = jobs.entries().next().value; if (entry) { jobs.delete(entry[0]); entry[1](); await tick(); } },
    get size() { return jobs.size; } };
}
const saved = {run_id:'orphan',status:'running',version:2,elapsed:0.01,done:0,total:1,per_conv:[]};
test('W11.1: frozen persisted running run is bounded, nonblocking, and can be replaced', async () => {
  const time=clock(), events=[]; let gets=0, posts=0;
  const c=createRunController({run:async()=>{gets++;return {...saved};},createRun:async()=>{posts++;return {run_id:'new',status:'completed'};}},
    {...time,onProgress:r=>events.push(r),onFinish:r=>events.push(r)});
  c.watch(saved,{restore:true}); assert.equal(c.state.phase,'restoring');
  await tick(); for(let i=0;i<10;i++)await time.next();
  assert.equal(c.state.phase,'interrupted'); assert.equal(gets,3); assert.equal(time.size,0); assert.equal(events.length,0);
  await c.start({version:2}); assert.equal(posts,1); assert.equal(c.state.run.run_id,'new'); c.destroy();
});
test('W11.1: advancing elapsed is a live heartbeat, but restore observation still has a fixed limit',async()=>{
  const time=clock();let gets=0;
  const c=createRunController({run:async()=>({...saved,elapsed:++gets})},time);
  c.watch(saved,{restore:true});await tick();for(let i=0;i<10;i++)await time.next();
  assert.equal(gets,6);assert.equal(c.state.phase,'paused');assert.equal(time.size,0);c.destroy();
});
test('W11.1: unknown saved run is interrupted; transport failure stays retryable, not declared dead',async()=>{
  for(const status of [404,503]){
    const c=createRunController({run:async()=>{throw Object.assign(Error('offline'),{status});}},clock());
    c.watch(saved,{restore:true});await tick();assert.equal(c.state.phase,status===404?'interrupted':'paused');c.destroy();
  }
});
test('W11.1: late restore reply cannot overwrite a new launch; restore never replays historic failure banners',async()=>{
  const old=deferred(),events=[];
  const c=createRunController({run:()=>old.promise,createRun:async()=>({run_id:'new',status:'completed'})},
    {...clock(),onProgress:r=>events.push(r.run_id),onFinish:r=>events.push(r.run_id)});
  c.watch(saved,{restore:true});await c.start({version:2});old.resolve({...saved,status:'failed'});await tick();
  assert.equal(c.state.run.run_id,'new');assert.deepEqual(events,['new','new']);c.destroy();
});
test('W11.1: real board keeps submit enabled during restoration and explains interrupted history',async t=>{
  const reply=deferred();const ui=mountBoard({runs:async()=>[saved],run:()=>reply.promise});t.after(ui.destroy);await tick();
  assert.equal(ui.find('cqa-board__button--primary').disabled,false);
  assert.match(ui.host.textContent,/Проверяем сохранённый прогон/);
  reply.reject(Object.assign(Error('missing'),{status:404}));await tick();
  assert.equal(ui.find('cqa-board__button--primary').disabled,false);
  assert.match(ui.host.textContent,/Прогон прерван \(сервер перезапускался\) — можно запустить заново/);
});
