/* Opt-in Chromium acceptance. Serve web/ as static assets only; all API methods are local stubs.
 * Open a named playwright-cli session on about:blank from an OS temporary directory.
 * CQA_PROVIDER_BROWSER_URL=http://127.0.0.1:8098 CQA_BROWSER_SESSION=cqa-provider
 * CQA_BROWSER_CWD=/tmp/cqa-provider-browser node --test tests/frontend_provider_browser.test.mjs
 */
import assert from 'node:assert/strict';
import test from 'node:test';
import { execFile } from 'node:child_process';
import { mkdtemp, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { promisify } from 'node:util';

const base = process.env.CQA_PROVIDER_BROWSER_URL;
test('Chromium: missing/provider states, responsive controls, contrast, keyboard retry and incomplete completion', { skip: !base, timeout: 60000 }, async () => {
  const origin = new URL(base).origin;
  assert.ok(['localhost', '127.0.0.1', '[::1]'].includes(new URL(origin).hostname));
  const dir = await mkdtemp(join(tmpdir(), 'cqa-provider-'));
  const script = `async page => {
    const origin = ${JSON.stringify(origin)};
    const forbidden = [], errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/*', route => {
      const url = route.request().url();
      if(url === origin+'/provider-fixture') return route.fulfill({contentType:'text/html',body:'<!doctype html><html lang="ru"><head><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/app.css"></head><body style="margin:0"><main class="cqa-root"><div id="cqa-card"></div></main></body></html>'});
      if(url.startsWith(origin+'/') && /\\.(?:css|js)(?:\\?|$)/.test(url)) return route.continue();
      forbidden.push(url); return route.abort();
    });
    await page.goto(origin+'/provider-fixture');
    await page.emulateMedia({reducedMotion:'reduce'});
    await page.evaluate(async () => {
      const fixture = {id:'synthetic-provider-demo',language:'RU',channel:'chat',is_mock:true,
        messages:[{id:'m1',speaker:'operator',text:'Здравствуйте. Это синтетический пример проверки интерфейса.'}],
        checklist:{version:7,criteria:[{id:'C1',title:'Приветствие',answer_type:'binary'},
          {id:'C2',title:'Уточнение',answer_type:'binary',na_allowed:true}, {id:'C8',title:'Грубые ошибки',answer_type:'binary',critical:true}]},
        evals:[{criterion_id:'C1',answer:null,review_reasons:['оценка не получена: ошибка модели: HTTP 402 Insufficient credits']},
          {criterion_id:'C2',answer:'na',na_reason:'Общий вопрос без уточнений.'}],score:{total:100,status:'incomplete',lines:[]}};
      const listeners = new Map(); window.requests = []; window.reads = [];
      window.CQA={state:{},config:{role:'operator'},bus:{on(name,fn){if(!listeners.has(name))listeners.set(name,new Set());listeners.get(name).add(fn);return()=>listeners.get(name).delete(fn);},emit(name,value){for(const fn of listeners.get(name)||[])fn(value);}},
        api:{conversation:async(id,options)=>{window.reads.push({id,options});return structuredClone(fixture);},
          createRun:body=>{window.requests.push(body);return new Promise(resolve=>{window.finishRetry=resolve;});},run:async()=>{throw Error('unexpected poll');}}};
      const card=await import('/js/card.js');window.cardCleanup=card.mount(CQA);CQA.bus.emit('conv:open',fixture.id);
    });
    await page.locator('[data-answer-state="missing"]').first().waitFor({state:'attached'});
    const widths=[];
    for(const width of [375,768,1024,1440]) {
      await page.setViewportSize({width,height:900});
      if(width<=768) await page.getByRole('tab',{name:'Результат',exact:true}).click();
      const result=await page.evaluate(()=>{
        const controls=[...document.querySelectorAll('[data-card-retry]')].filter(node=>node.checkVisibility());
        return {width:innerWidth,overflow:document.documentElement.scrollWidth>innerWidth,
          targets:controls.map(node=>{const rect=node.getBoundingClientRect();return {w:rect.width,h:rect.height};}),
          total:document.querySelector('.cqa-card__total').textContent,
          motion:controls.map(node=>getComputedStyle(node).transitionDuration)};
      }); widths.push(result);
    }
    await page.setViewportSize({width:375,height:900});
    await page.getByRole('tab',{name:'Результат',exact:true}).click();
    const control=page.locator('.cqa-card__retry--primary');
    await control.focus();
    await page.keyboard.press('Tab');
    await page.keyboard.press('Shift+Tab');
    const focus=await control.evaluate(node=>({visible:node.matches(':focus-visible'),outline:getComputedStyle(node).outlineWidth}));
    const contrast=await page.evaluate(()=>{
      const luminance=color=>{const values=color.match(/[\\d.]+/g).slice(0,3).map(Number).map(v=>{v/=255;return v<=.04045?v/12.92:((v+.055)/1.055)**2.4;});return .2126*values[0]+.7152*values[1]+.0722*values[2];};
      return [...document.querySelectorAll('.cqa-card__missing-title,.cqa-card__missing-description,.cqa-card__retry-scope,.cqa-card__answer--missing,.cqa-card__retry')].map(node=>{
        let bg=node;while(bg.parentElement&&getComputedStyle(bg).backgroundColor==='rgba(0, 0, 0, 0)')bg=bg.parentElement;
        const a=luminance(getComputedStyle(node).color),b=luminance(getComputedStyle(bg).backgroundColor);
        return {className:node.className,ratio:(Math.max(a,b)+.05)/(Math.min(a,b)+.05)};
      });
    });
    await page.screenshot({path:${JSON.stringify(join(dir, 'provider-375.png'))},fullPage:true});
    await page.keyboard.press('Enter');
    await page.evaluate(()=>document.querySelector('.cqa-card__retry--primary').click());
    const pending=await page.evaluate(()=>({posts:window.requests.length,body:window.requests[0],disabled:[...document.querySelectorAll('[data-card-retry]')].every(node=>node.disabled)}));
    await page.evaluate(()=>window.finishRetry({run_id:'synthetic-run',version:7,status:'completed'}));
    await page.waitForFunction(()=>document.querySelector('.cqa-card__retry-status').textContent.includes('Критериев без ответа: 2'));
    const finished=await page.evaluate(()=>({total:document.querySelector('.cqa-card__total').textContent,
      status:document.querySelector('.cqa-card__retry-status').textContent,read:window.reads.at(-1),
      focus:document.activeElement.className,posts:window.requests.length}));
    await page.setViewportSize({width:1440,height:900});
    await page.screenshot({path:${JSON.stringify(join(dir, 'provider-1440.png'))},fullPage:true});
    await page.evaluate(()=>window.cardCleanup());
    return {widths,focus,contrast,pending,finished,forbidden,errors};
  }`;
  const scriptPath = join(dir, 'acceptance.js');
  await writeFile(scriptPath, script);
  const result = await promisify(execFile)('playwright-cli', ['--raw', `-s=${process.env.CQA_BROWSER_SESSION || 'cqa-provider'}`, 'run-code', `--filename=${scriptPath}`],
    { cwd: process.env.CQA_BROWSER_CWD || dir, maxBuffer: 2 * 1024 * 1024, timeout: 55000 });
  const report = JSON.parse(result.stdout);
  await writeFile(join(dir, 'report.json'), JSON.stringify(report, null, 2));
  for(const row of report.widths) {
    assert.equal(row.overflow, false, JSON.stringify(row)); assert.equal(row.total, 'Не рассчитан');
    assert.ok(row.targets.length > 0); for(const target of row.targets) assert.ok(target.w >= 44 && target.h >= 44, JSON.stringify(target));
    assert.ok(row.motion.every(duration => duration === '0s'));
  }
  assert.equal(report.focus.visible, true); assert.equal(report.focus.outline, '3px');
  for(const item of report.contrast) assert.ok(item.ratio >= 4.5, JSON.stringify(item));
  assert.equal(report.pending.posts, 1); assert.equal(report.pending.disabled, true);
  assert.deepEqual(report.pending.body, {conv_ids:['synthetic-provider-demo'],version:7,live:true,force:true,activate:false,preview:false});
  assert.equal(report.finished.total, 'Не рассчитан'); assert.match(report.finished.status, /Критериев без ответа: 2/);
  assert.equal(report.finished.posts, 1); assert.equal(report.finished.focus, 'cqa-card__retry-status');
  assert.deepEqual(report.finished.read.options, {version:7});
  assert.deepEqual(report.forbidden, []); assert.deepEqual(report.errors, []);
  console.log('Provider browser artifacts:', dir);
});
