/** Real browser acceptance, opt-in and synthetic localhost data only.
 * Open the authorized session first:
 * playwright-cli -s=cqa-wave3-ui open http://127.0.0.1:8047/?mode=live
 * CQA_BROWSER_ACCEPTANCE_URL=http://127.0.0.1:8047/?mode=live node --test tests/frontend_browser_acceptance.test.mjs
 * Requires a seeded backend with >=5 synthetic conversations and a scored card.
 * Regular Node runs skip this test. No DOM or geometry substitutes are used.
 */
import assert from 'node:assert/strict';
import { execFile } from 'node:child_process';
import { mkdir, writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { promisify } from 'node:util';
import test from 'node:test';

const url = process.env.CQA_BROWSER_ACCEPTANCE_URL;
const session = process.env.CQA_BROWSER_SESSION || 'cqa-wave3-ui';
const cwd = fileURLToPath(new URL('../', import.meta.url));
const run = promisify(execFile);

test('real Chromium: responsive shell, projector queue, card panes, targets and primary actions', {skip: !url, timeout: 120000}, async () => {
  assert.ok(['localhost', '127.0.0.1', '[::1]'].includes(new URL(url).hostname), 'Acceptance uses a synthetic localhost server only');
  const dir = 'reports/qa/cqa-wave3-ui';
  await mkdir(new URL(`../${dir}/`, import.meta.url), {recursive: true});
  const browserScript = `async page => {
    // This named Chromium session can outlive local asset edits; verify the files currently served.
    const network = await page.context().newCDPSession(page);
    await network.send('Network.clearBrowserCache');
    await network.detach();
    await page.goto(${JSON.stringify(url)});
    await page.waitForFunction(() => window.CQA?.state?.ready === true);
    await page.getByRole('tab', {name:'Диалоги', exact:true}).click();
    await page.locator('.cq-list-row').first().click();
    await page.locator('.cqa-card__criterion').first().waitFor({state:'attached'});
    await page.emulateMedia({reducedMotion:'reduce'});
    const results=[];
    for(const [width,height] of [[375,812],[768,1024],[1024,768],[1280,720],[1440,900],[1920,1080]]) {
      await page.setViewportSize({width,height});
      for(const projector of [false,true]) {
        const toggle=page.getByRole('button',{name:'Проектор',exact:true});
        if((await toggle.getAttribute('aria-pressed')==='true')!==projector)await toggle.click();
        for(const [tab,label] of [['dialogues','Диалоги'],['editor','Чек-лист'],['review','На проверке'],['board','Оценка']]) {
          await page.getByRole('tab',{name:new RegExp('^'+label)}).click();
          if(tab==='dialogues' && width<=768)await page.locator('.cq-list-rows').scrollIntoViewIfNeeded();
          await page.evaluate(()=>new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r))));
          const metrics=await page.evaluate(()=>{
            const shown=e=>e.checkVisibility() && getComputedStyle(e).visibility!=='hidden' && !e.closest('[hidden]') && !e.classList.contains('cqa-skip');
            const queue=document.querySelector('.cq-list-rows'),q=queue.getBoundingClientRect();
            const rows=[...document.querySelectorAll('.cq-list-row')];
            const visibleRows=rows.filter(e=>{const b=e.getBoundingClientRect();return shown(e)&&b.top>=Math.max(0,q.top)-1&&b.bottom<=Math.min(innerHeight,q.bottom)+1;}).length;
            const small=[...document.querySelectorAll('button,input,select,textarea,summary,a[href]')].filter(shown).flatMap(e=>{
              const target=e.matches('input[type=checkbox],input[type=radio]')?e.closest('label')||e:e;
              const b=target.getBoundingClientRect();return b.width<43.9||b.height<43.9?[{tag:e.tagName,class:e.className,w:b.width,h:b.height}]:[];
            });
            return {documentWidth:document.documentElement.scrollWidth,visibleRows,rowCount:rows.length,small,primary:[...document.querySelectorAll('[class*=primary]')].filter(shown).length};
          });
          let panes=null;
          if(tab==='dialogues' && width<=768){
            await page.getByRole('tab',{name:'Результат',exact:true}).click();
            const resultVisible=await page.locator('.cqa-card__results').isVisible();
            const transcriptHidden=!(await page.locator('.cqa-card__transcript-panel').isVisible());
            await page.getByRole('tab',{name:'Результат',exact:true}).press('ArrowLeft');
            panes={resultVisible,transcriptHidden,transcriptVisible:await page.locator('.cqa-card__transcript-panel').isVisible(),resultHidden:!(await page.locator('.cqa-card__results').isVisible())};
          }
          results.push({width,height,projector,tab,panes,...metrics});
          if(tab==='dialogues') {
            await page.evaluate(()=>{window.scrollTo(0,0);document.querySelector('#cqa-workspace').scrollTop=0;});
            await page.screenshot({path:${JSON.stringify(dir)}+'/acceptance-'+width+'-'+projector+'.png'});
          }
        }
      }
    }
    return {url:page.url(),userAgent:await page.evaluate(()=>navigator.userAgent),results};
  }`;
  const scriptPath = fileURLToPath(new URL(`../${dir}/acceptance.js`, import.meta.url));
  await writeFile(scriptPath, browserScript);
  const {stdout} = await run('playwright-cli', [`-s=${session}`,'--raw','run-code',`--filename=${scriptPath}`], {cwd, maxBuffer: 8*1024*1024, timeout: 110000});
  await writeFile(new URL(`../${dir}/acceptance.json`, import.meta.url), stdout);
  assert.ok(!stdout.startsWith('### Error'), stdout);
  const report = JSON.parse(stdout);
  assert.equal(report.results.length,48);
  for(const item of report.results){
    const context = `${item.width}x${item.height}, projector=${item.projector}, ${item.tab}`;
    assert.ok(item.documentWidth<=item.width, `Horizontal page overflow: ${context}`);
    assert.deepEqual(item.small,[],`Targets below 44px: ${context}`);
    assert.ok(item.primary<=1,`Multiple primary actions: ${context}`);
    if(item.tab==='dialogues'&&item.projector){
      assert.ok(item.rowCount>=5,'Seed at least five synthetic rows');
      assert.ok(item.visibleRows>=5,`Five complete projector rows: ${context}; got ${item.visibleRows}`);
    }
    if(item.panes)assert.ok(Object.values(item.panes).every(Boolean),`Mobile pane isolation / keyboard switch: ${context}`);
  }
});
