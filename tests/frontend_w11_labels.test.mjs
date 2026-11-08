import test from 'node:test';
import assert from 'node:assert/strict';
import { mountBoard, tick, walk, Element } from './helpers/w11-board.mjs';
import * as economics from '../web/js/bank-economics.js';
import { readFile } from 'node:fs/promises';
import { forecastText,runForecast } from '../web/js/model-profiles.js';
const quality=JSON.parse(await readFile(new URL('../web/data/quality.json',import.meta.url)));
test('W11.4: mean-price forecast and channel budget have distinct labels and real fixture values',()=>{
  const forecast=forecastText(runForecast({quality,profile:'cloud',dialogues:1}));
  assert.match(forecast,/Прогноз этого прогона по средней цене диалога/);assert.match(forecast,/0,0459/);
  const ui=mountBoard();const host=new Element('section');
  // Budget uses only local fixtures; its controls are never submitted.
  Element.prototype.removeEventListener=()=>{};
  const budget=economics.mountBudget(host,{quality,plan:{dialogues:1,channels:{chat:1,call:0},mode:'fast'},profile:'cloud',storage:null});
  assert.match(host.textContent,/Бюджет по составу чатов и звонков/);assert.match(host.textContent,/22,35/);
  assert.doesNotMatch(host.textContent,/Прогноз этого прогона/);budget.destroy();ui.destroy();
});
test('W11.4: restored zero-call run explains cache/database result beside separate stored costs',async t=>{
  const run={run_id:'saved',status:'completed',cost_kzt:0,llm_calls:0,done:38,total:38,per_conv:[]};
  const ui=mountBoard({runs:async()=>[run],run:async()=>run,metrics:async()=>({cost_kzt:733.68})});t.after(ui.destroy);await tick();
  assert.match(ui.host.textContent,/Сохранённый прогон saved/);
  assert.match(ui.host.textContent,/из кэша\/базы, без вызовов модели/);
  assert.match(ui.host.textContent,/Стоимость сохранённых оценок активной версии: 733,68/);
  assert.equal(ui.events.some(([n])=>n==='run:progress'||n==='run:completed'),false);
});
test('W11.5: inactive v2 launch is explicit, defaults off for open dialogue and submits activate:false',async t=>{
  const writes=[];
  const ui=mountBoard({checklists:async()=>[{version:1,active:true},{version:2,active:false,parent_version:1}],
    createRun:async body=>{writes.push(body);return {run_id:'stage',version:2,status:'completed',per_conv:[]};}}, {checklistVersion:2});
  t.after(ui.destroy);await tick();
  const button=ui.find('cqa-board__button--primary'),activate=walk(ui.host).find(n=>Object.hasOwn(n.dataset,'boardActivate'));
  assert.equal(button.textContent,'Оценить по v2');assert.equal(activate.checked,false);
  assert.match(ui.host.textContent,/v2 не станет активной; v1 остаётся официальной/);
  assert.equal(button.attributes['aria-describedby'],'cqa-board-activation-hint');
  await ui.find('cqa-board__form').dispatch('submit');assert.equal(writes.length,1);assert.equal(writes[0].activate,false);assert.equal(writes[0].version,2);
  activate.checked=true;await activate.dispatch('change');assert.equal(button.textContent,'Оценить и применить');
  assert.match(ui.host.textContent,/v2 станет активной после успешного расчёта/);
});
