import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { runInNewContext } from 'node:vm';
import { mountBoard,tick } from './helpers/w11-board.mjs';
test('W11.2a: result tile opens its run version even while list map and global selection differ',async t=>{
  const ui=mountBoard({createRun:async body=>({run_id:'v2',version:body.version,status:'completed',activated:false,per_conv:[{id:'a',status:'done',old_total:76.47,new_total:64.71}]}),
    conversation:async()=>({id:'a',checklist:{version:2,active:false},messages:[],score:{total:64.71}}),
    checklists:async()=>[{version:1,active:true},{version:2,active:false}]}, {checklistVersion:2,listScoreVersions:{a:1}});
  t.after(ui.destroy);await tick();await ui.find('cqa-board__form').dispatch('submit');await tick();
  ui.CQA.state.checklistVersion=1;
  await ui.find('cqa-board__tile').dispatch('click');
  assert.deepEqual(ui.events.find(([n])=>n==='conv:open'),['conv:open',{id:'a',version:2}]);
  assert.equal(ui.CQA.state.checklistVersion,1);assert.equal(ui.events.some(([n])=>n==='checklist:activated'),false);
});
test('W11.2b: active detail replaces old card version, inactive version and other dialogues are ignored',async()=>{
  const source=await readFile(new URL('../web/js/card.js',import.meta.url),'utf8');
  const body=source.slice(source.indexOf("  on('score:updated',"),source.indexOf("  window.addEventListener('resize'"));
  let callback;const rendered=[];const current={id:'a',checklist:{version:1},score:{total:76.47}};
  const context={on:(_,f)=>callback=f,current,generation:0,recoveryScope:{version:1},render:d=>rendered.push(d)};
  runInNewContext(body,context);
  callback({id:'a',messages:[],checklist:{version:2,active:false},score:{total:64.71}});assert.equal(rendered.length,0);
  callback({id:'b',messages:[],checklist:{version:2,active:true},score:{total:64.71}});assert.equal(rendered.length,0);
  callback({id:'a',messages:[],checklist:{version:2,active:true},score:{total:64.71}});
  assert.equal(rendered.length,1);assert.equal(rendered[0].checklist.version,2);assert.equal(rendered[0].score.total,64.71);
  assert.equal(context.recoveryScope,null,'retry recovery pointer of the old version is not reused');
});
