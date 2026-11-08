import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { runInNewContext } from 'node:vm';
const source=await readFile(new URL('../web/js/core.js',import.meta.url),'utf8');
test('W11.3: repeated conv:open and nav:open preserve scroll; real tab transitions reset it',()=>{
  const reset=source.slice(source.indexOf('export function resetPanelScroll'),source.indexOf('export function boot')).replace('export ','');
  const setTab=source.slice(source.indexOf('  function setTab('),source.indexOf('  CQA.setTab = setTab;'));
  const handlers=source.slice(source.indexOf("  CQA.bus.on('conv:open',"),source.indexOf("  CQA.bus.on('appeal:start',"));
  const callbacks={},nested={scrollTop:240},panel={scrollTop:90,querySelectorAll:()=>[nested]},workspace={scrollTop:150};
  const CQA={state:{tab:'dialogues'},bus:{on:(n,f)=>callbacks[n]=f,emit(){}}};
  const context={CQA,TAB_NAMES:['dialogues','board'],document:{scrollingElement:{scrollTop:100}},
    root:{querySelector:()=>workspace,querySelectorAll:()=>[]},byId:()=>({...panel,querySelectorAll:panel.querySelectorAll,textContent:'Tab'}),history:{replaceState(){}},announce(){}};
  runInNewContext(reset+setTab+handlers,context);
  callbacks['conv:open']('a');assert.equal(nested.scrollTop,240);assert.equal(workspace.scrollTop,150);
  callbacks['conv:open']({id:'b',version:2});assert.equal(CQA.state.activeConversationId,'b');assert.equal(nested.scrollTop,240);
  callbacks['nav:open']('board');assert.equal(nested.scrollTop,0);assert.equal(workspace.scrollTop,0);
  workspace.scrollTop=300;nested.scrollTop=400;callbacks['nav:open']('board');assert.equal(workspace.scrollTop,300);assert.equal(nested.scrollTop,400);
  callbacks['conv:open']('a');assert.equal(workspace.scrollTop,0);assert.equal(nested.scrollTop,0);
});
