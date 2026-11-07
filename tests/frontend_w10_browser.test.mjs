/** Run with CQA_W10_PLAYWRIGHT_MODULE set to an installed Playwright index.mjs.
 * All network responses are local fixtures. No backend or model requests allowed.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile, mkdir } from 'node:fs/promises';
import { resolve, extname, join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
const root=fileURLToPath(new URL('../',import.meta.url));
const modulePath=process.env.CQA_W10_PLAYWRIGHT_MODULE;
test('W10 browser: U1–U8 stage flows, cancellation, recovery, P mode, responsive bounds and focus', {skip:!modulePath,timeout:90000},async t=>{
  const {chromium}=await import(pathToFileURL(modulePath));
  const browser=await chromium.launch({channel:'chrome',headless:true});t.after(()=>browser.close());
  const page=await browser.newPage({viewport:{width:1280,height:720}});
  const errors=[],forbidden=[];page.on('pageerror',e=>errors.push(e.message));
  const origin='http://127.0.0.1:8099';
  await page.route('**/*',async route=>{
    const url=new URL(route.request().url());
    if(url.origin!==origin||url.pathname.includes('/api/')){forbidden.push(url.href);return route.abort();}
    const path=resolve(root,'.'+url.pathname);
    if(!path.startsWith(root+'/'))return route.fulfill({status:404,body:''});
    try {return route.fulfill({status:200,contentType:({'.html':'text/html','.js':'text/javascript','.css':'text/css','.json':'application/json','.svg':'image/svg+xml'})[extname(path)]||'application/octet-stream',body:await readFile(path)});} catch{return route.fulfill({status:404,body:''});}
  });
  await page.emulateMedia({reducedMotion:'reduce'});
  await page.goto(origin+'/web/index.html?mode=demo');
  await page.waitForFunction(()=>window.CQA?.state.ready);
  await page.locator('.cq-list-row').first().click();
  await page.locator('.cqa-card__total strong').waitFor();
  await page.waitForFunction(()=>[...document.querySelectorAll('link[rel=stylesheet]')].every(l=>l.sheet));
  // U8 and U5: drive the real appeal and decision forms using the shipped mock API.
  await page.evaluate(async()=>{
    const c=window.CQA;
    const detail=await c.api.conversation(c.state.activeConversationId);
    detail.messages.unshift({id:'w10-system',speaker:'system',text:'Synthetic system context'});
    detail.evals[0].evidence=[{message_id:'w10-system',quote:'Synthetic system context'}];
    const original=c.api.conversation;
    c.api.conversation=async(id,opts)=>id===detail.id?structuredClone(detail):original(id,opts);
    window.w10OriginalConversation=original;
    c.bus.emit('appeal:start',{conv_id:detail.id,criterion_id:detail.evals[0].criterion_id});
  });
  await page.locator('.cqa-review-dialog[open]').waitFor();
  assert.equal(await page.locator('.cqa-review-message-option--system input:checked').count(),0);
  assert.ok(await page.locator('.cqa-review-message-option--operator input:checked, .cqa-review-message-option--client input:checked').count()>0);
  await page.getByLabel('Ваш комментарий').fill('Синтетическая проверка формы W10');
  await page.getByRole('button',{name:'Подать апелляцию',exact:true}).click();
  await page.locator('.cqa-review-dialog').waitFor({state:'hidden'});
  await page.evaluate(()=>{window.CQA.api.conversation=window.w10OriginalConversation;});
  await page.locator('[data-role=reviewer]').click();
  await page.locator('#cqa-tab-review').click();
  await page.locator('.cqa-review-item').first().click();
  await page.locator('.cqa-review-decision').waitFor();
  await page.locator('.cqa-review-decide--change').click();
  assert.equal(await page.locator('.cqa-review-decision [aria-invalid=true]').count(),0,'first Change does not mark untouched fields');
  await page.locator('.cqa-review-decision textarea').focus();
  await page.locator('.cqa-review-decision textarea').blur();
  assert.equal(await page.locator('.cqa-review-decision textarea[aria-invalid=true]').count(),1);
  // U6: tab transition resets the actual scroll owner.
  await page.locator('.cqa-workspace').evaluate(e=>{e.scrollTop=200;});
  await page.locator('#cqa-tab-board').click();
  assert.equal(await page.locator('.cqa-workspace').evaluate(e=>e.scrollTop),0);
  // U3/U4/U7: remount the actual board with read-only synthetic API adapters.
  await page.evaluate(async()=>{
    const c=window.CQA, m=await import('./js/board.js');m.mount(c)();
    window.w10Writes=[];
    c.config.mock=false;c.state.activeConversationId='w10-0';
    c.api.config=async()=>({profile:'cloud',mode:'fast',model:{votes:1}});
    c.api.conversations=async()=>Array.from({length:38},(_,i)=>({id:`w10-${i}`,group_id:'DEV',channel:'chat',language:'RU',status:i?'incomplete':'evaluated',total:i?null:64.71,checklist_version:2}));
    c.api.checklists=async()=>[{version:1,active:false,criteria:[]},{version:2,active:true,criteria:[]}];
    c.api.metrics=async()=>({cost_kzt:1483.91});
    c.api.runs=async()=>[{run_id:'w10-saved',status:'completed',version:2,done:1,total:1}];
    c.api.run=async id=>({run_id:id,status:'completed',version:2,activated:true,done:1,total:1,per_conv:[]});
    c.api.setConfig=async body=>{window.w10Writes.push(['config',body]);return body;};
    c.api.createRun=async body=>{window.w10Writes.push(['run',body]);return {run_id:'w10-new',status:'completed',done:38,total:38,per_conv:[]};};
    c.api.conversation=async(id,options)=>({id,messages:[],evals:[],status:'evaluated',score:{total:76.47,critical:Number(id.split('-')[1])<4},checklist:{version:typeof options==='number'?options:options?.version||2,criteria:[]}});
    m.mount(c);
  });
  await page.getByRole('heading',{name:'Оценка завершена',exact:true}).waitFor();
  const scope=page.getByRole('combobox',{name:'Диалоги',exact:true});
  assert.equal(await scope.inputValue(),'active');assert.equal(await page.locator('[data-board-activate]').isChecked(),false);
  assert.match(await page.locator('.cqa-board__form').innerText(),/Всего потрачено в этой базе.*накопленный расход недоступен/);
  await scope.selectOption('all');
  page.once('dialog',async d=>{assert.match(d.message(),/Диалогов: 38.*Вызовов модели/);assert.match(d.message(),/Прогноз этого прогона/);await d.dismiss();});
  await page.locator('.cqa-board__button--primary').click();
  assert.equal(await page.evaluate(()=>window.w10Writes.length),0);
  // Editor apply must only prepare the board, never createRun directly.
  await page.locator('#cqa-tab-editor').click();
  await page.locator('#cqa-ed-apply').click();
  await page.waitForFunction(()=>window.CQA.state.tab==='board');
  assert.equal(await page.evaluate(()=>window.w10Writes.length),0);
  assert.equal(await scope.inputValue(),'active');
  // U2/U1: active version contains one score, 37 historical scores remain labelled.
  await page.evaluate(()=>{window.CQA.bus.emit('conversations:refresh');});
  await page.waitForFunction(()=>document.querySelector('[data-stat=evaluated]')?.textContent==='38');
  assert.equal(await page.locator('[data-stat=critical]').textContent(),'3');
  assert.equal(await page.locator('.cq-list-score.is-previous').count(),37);
  await page.locator('#cqa-tab-dialogues').click();
  const dir=join(root,'output/playwright/w10');await mkdir(dir,{recursive:true});
  for(const projector of [false,true]){
    if(projector)await page.keyboard.press('p');
    for(const [width,height] of [[375,812],[768,1024],[1024,768],[1280,720],[1440,900]]){
      await page.setViewportSize({width,height});
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,`${width} P=${projector}`);
      const diamond=page.locator('.cq-list-diamond svg').first();
      await diamond.scrollIntoViewIfNeeded();
      const box=await diamond.boundingBox();assert.ok(box.width>=20&&box.height>=20);
      assert.equal(await diamond.getAttribute('aria-label'),'Грубая ошибка');
      if(width>=1280){
        await page.screenshot({path:join(dir,`list-${width}-${projector?'projector':'normal'}.png`)});
        await page.locator('#cqa-tab-board').click();
        await page.locator('.cqa-board__button--primary').scrollIntoViewIfNeeded();
        const button=page.locator('.cqa-board__button--primary');await page.keyboard.press('Tab');await button.focus();
        const bounds=await button.boundingBox();assert.ok(bounds.height>=44&&bounds.width>=44&&bounds.y>=0&&bounds.y+bounds.height<=height);
        assert.equal(await button.evaluate(e=>getComputedStyle(e).outlineStyle),'solid');
        assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
        await page.screenshot({path:join(dir,`board-${width}-${projector?'projector':'normal'}.png`)});
        await page.locator('#cqa-tab-dialogues').click();
      }
    }
  }
  assert.deepEqual(errors,[]);assert.deepEqual(forbidden,[]);
});
