/** Opt-in browser test, synthetic localhost only; no Dynamics tenant connection.
 * Build/serve dynamics/README.md's cross-origin example, then open a dedicated browser:
 * playwright-cli -s=dalel-t50 open http://127.0.0.1:8050/dynamics/embed-demo.html
 * DALEL_DYNAMICS_BROWSER_URL=http://127.0.0.1:8050/dynamics/embed-demo.html node --test tests/dynamics_browser.test.mjs
 * Artifacts are written to an isolated OS temp directory, printed on completion.
 */
import assert from 'node:assert/strict';
import test from 'node:test';
import { execFile } from 'node:child_process';
import { mkdtemp, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { promisify } from 'node:util';
const run = promisify(execFile);
const url = process.env.DALEL_DYNAMICS_BROWSER_URL;
const session = process.env.DALEL_DYNAMICS_BROWSER_SESSION || 'dalel-t50';

test('real cross-origin iframe: ready/open/role, private-field rejection, resize and responsive host', { skip: !url, timeout: 90000 }, async () => {
  assert.ok(['127.0.0.1', 'localhost', '[::1]'].includes(new URL(url).hostname));
  const output = await mkdtemp(join(tmpdir(), 'dalel-t50-browser-'));
  const script = `async page => {
    await page.goto(${JSON.stringify(url)});
    await page.waitForFunction(() => document.querySelector('#connection-state').textContent.startsWith('Подключено'));
    const frame = await (await page.$('#cqa-frame')).contentFrame();
    const frameOrigin = await frame.evaluate(() => location.origin);
    if (!['127.0.0.1', 'localhost', '[::1]'].includes(await frame.evaluate(() => location.hostname))) throw new Error('Local synthetic iframe required');
    await frame.waitForFunction(() => window.CQA?.state.activeConversationId === 'demo-ru-001');
    const initial = await frame.evaluate(() => ({ id: CQA.state.activeConversationId, mock: CQA.config.mock, parentOrigin: CQA.config.parentOrigin, apiBase: CQA.config.apiBase }));
    if (!initial.mock) throw new Error('Synthetic demo mode required for this test');
    await page.getByLabel('Роль в интерфейсе', { exact: true }).selectOption('reviewer');
    await page.getByLabel('ID диалога', { exact: true }).fill('demo-ru-002');
    await page.getByRole('button', { name: 'Применить', exact: true }).click();
    await frame.waitForFunction(() => CQA.state.role === 'reviewer' && CQA.state.activeConversationId === 'demo-ru-002');
    const changed = await frame.evaluate(() => ({ role: CQA.state.role, id: CQA.state.activeConversationId }));
    const before = await page.locator('#event-count').innerText();
    // Actual postMessage from the wrong window/origin, even with the correct namespace.
    await page.evaluate(() => window.postMessage({ source: 'dalel-cqa', type: 'score.updated', payload: { conv_id: 'forged', total: 99 } }, location.origin));
    // Actual trusted child: unexpected content fields must still be rejected by the host.
    await frame.evaluate(() => parent.postMessage({ source: 'dalel-cqa', type: 'score.updated', payload: { conv_id: 'demo-ru-002', total: 50, transcript: 'PRIVATE_TEST_ONLY' } }, CQA.config.parentOrigin));
    // A supported notification, deliberately simulated in-browser (not a persisted scoring run).
    await frame.evaluate(() => CQA.bus.emit('score:updated', { conv_id: 'demo-ru-002', score: { total: 0 } }));
    await page.waitForFunction(() => document.querySelector('#event-log').textContent.includes('total: 0'));
    const after = await page.locator('#event-count').innerText();
    const log = await page.locator('#event-log').innerText();
    await frame.evaluate(() => parent.postMessage({ source: 'dalel-cqa', type: 'resize', payload: { height: 999999 } }, CQA.config.parentOrigin));
    await page.waitForFunction(() => document.querySelector('iframe').style.height === '1400px');
    const height = await page.locator('iframe').evaluate(e => e.style.height);
    await page.getByLabel('ID диалога', { exact: true }).fill('<not-an-id>');
    await page.getByRole('button', { name: 'Применить', exact: true }).click();
    const invalid = await page.locator('#conv-id').evaluate(e => ({ invalid: e.getAttribute('aria-invalid'), focused: document.activeElement === e }));
    await page.getByLabel('ID диалога', { exact: true }).fill('demo-ru-002');
    await page.getByRole('button', { name: 'Применить', exact: true }).click();
    await page.emulateMedia({ reducedMotion: 'reduce' });
    const sizes = [];
    for (const width of [375, 768, 1024, 1440]) {
      await page.setViewportSize({ width, height: 900 });
      const metrics = await page.evaluate(() => {
        const targets = [...document.querySelectorAll('button,select,input')].map(e => { const b=e.getBoundingClientRect(); return {w:b.width,h:b.height}; });
        return {width:innerWidth,scrollWidth:document.documentElement.scrollWidth,targets,primary:document.querySelectorAll('.primary').length,reduced:getComputedStyle(document.querySelector('.skeleton')).animationName};
      });
      sizes.push({...metrics,childOverflow:await frame.evaluate(() => document.documentElement.scrollWidth > innerWidth)});
      if (width===375 || width===1440) await page.screenshot({path:${JSON.stringify(output)}+'/host-'+width+'.png',fullPage:true});
    }
    return {initial,changed,hostOrigin:await page.evaluate(() => location.origin),frameOrigin,before,after,log,height,invalid,sizes};
  }`;
  const path = join(output, 'acceptance.js');
  await writeFile(path, script);
  const { stdout } = await run('playwright-cli', [`-s=${session}`, '--raw', 'run-code', `--filename=${path}`], { cwd: output, maxBuffer: 4 * 1024 * 1024, timeout: 80000 });
  await writeFile(join(output, 'result.json'), stdout);
  assert.ok(!stdout.startsWith('### Error'), stdout);
  const result = JSON.parse(stdout);
  assert.notEqual(result.hostOrigin, result.frameOrigin, 'Use the documented cross-origin example');
  assert.equal(result.initial.parentOrigin, result.hostOrigin);
  assert.equal(result.initial.mock, true);
  assert.deepEqual(result.changed, { role: 'reviewer', id: 'demo-ru-002' });
  assert.equal(Number(result.after.split(': ')[1]) - Number(result.before.split(': ')[1]), 1);
  assert.doesNotMatch(result.log, /PRIVATE_TEST_ONLY|forged/);
  assert.equal(result.height, '1400px');
  assert.deepEqual(result.invalid, { invalid: 'true', focused: true });
  for (const size of result.sizes) {
    assert.ok(size.scrollWidth <= size.width, `Host overflow at ${size.width}`);
    assert.equal(size.childOverflow, false, `Child overflow at ${size.width}`);
    assert.ok(size.targets.every(t => t.w >= 44 && t.h >= 44), `Small targets at ${size.width}`);
    assert.equal(size.primary, 1); assert.equal(size.reduced, 'none');
  }
  console.log('T50 browser artifacts:', output);
});
