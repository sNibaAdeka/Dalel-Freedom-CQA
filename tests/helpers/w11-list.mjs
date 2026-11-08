// Execute the actual queue reload/event handlers with rendering stubs.
// This harness tests asynchronous state and requests, not DOM geometry.
import { readFile } from 'node:fs/promises';
import { runInNewContext } from 'node:vm';
import { withPreviousScores } from '../../web/js/list.js';
export const source=await readFile(new URL('../../web/js/list.js',import.meta.url),'utf8');
export function listHarness(api={}, rows=[]) {
  const state={rows,visible:rows,loaded:true,request:0,loading:false};
  const CQA={state:{checklistVersion:2,listScoreVersions:{stale:1}},api:{conversations:async()=>rows,checklists:async()=>[{version:1},{version:2}],...api}};
  const handlers=new Map(),notices=[];
  const context={state,CQA,controller:{signal:{aborted:false}},withPreviousScores,refreshButton:{},
    rowsNode:{setAttribute(){},replaceChildren(){}},setNotice:s=>notices.push(s),updateCount(){},summary(){},
    applyFilters(){},reflectSelection(){},rowElement:r=>r,subscribe:(n,f)=>handlers.set(n,f)};
  const reload=source.slice(source.indexOf('  async function reload()'),source.indexOf('  form.addEventListener("submit"'));
  const events=source.slice(source.indexOf('  subscribe("score:updated",'),source.indexOf('  const api = {',source.indexOf('  subscribe("score:updated",')));
  runInNewContext(reload+events+'; this.reload = reload;',context);
  return {state,CQA,notices,reload:context.reload,emit:(n,p)=>handlers.get(n)?.(p)};
}
