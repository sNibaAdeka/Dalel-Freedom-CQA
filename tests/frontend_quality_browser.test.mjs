/** Optional real Chromium checks. Synthetic in-memory API only, loopback assets only.
 * CQA_QUALITY_PLAYWRIGHT_MODULE=/absolute/path/to/playwright/index.mjs node --test tests/frontend_quality_browser.test.mjs
 * CQA_QUALITY_SCREENSHOT_DIR=/tmp/quality-ui optionally keeps visual artifacts outside the repository.
 */
import assert from 'node:assert/strict';
import test from 'node:test';
import { readFile, mkdir } from 'node:fs/promises';
import { createServer } from 'node:http';
import { pathToFileURL } from 'node:url';
import { join } from 'node:path';

const modulePath = process.env.CQA_QUALITY_PLAYWRIGHT_MODULE;
test('Chromium: synthetic tables, mobile widths, focus, touch targets, contrast and reduced motion', { skip: !modulePath, timeout: 60000 }, async () => {
  const { chromium } = await import(pathToFileURL(modulePath).href);
  const fixture = {
    id: 'synthetic-ui', run_id: 'synthetic-ui-run', measured_at: '2026-01-02T10:00:00Z',
    source: { kind: 'synthetic', label: 'Synthetic browser check' },
    model: { requested: `synthetic-${'x'.repeat(120)}`, resolved: null }, mode: 'fast', guidance_version: 'synthetic-g1', checklist_version: '1', cache: 'mixed',
    reference: { id: 'synthetic-reference', type: 'machine', cells: 8 },
    sample: { dataset: 'synthetic DEV', split: 'holdout', dialogues: 4, dev_reused: true, used_for_tuning: null, untouched: false },
    denominator: 'all_cells', counts: { matched: 5, total: 8, missing: 1, errors: 1, unlabelled: 1 },
    score_mae: { value: 3, count: 2 }, latency: { p50_seconds: 2, p95_seconds: null, count: 4, scope: 'model_only' },
    criteria: [
      { id: 'A', label: 'Synthetic A', counts: { matched: 3, total: 4, missing: 1, errors: 0 } },
      { id: 'B', label: 'Synthetic B', counts: { matched: 2, total: 4, missing: 0, errors: 1, unlabelled: 1 } },
    ],
    confusion: [{ criterion_id: 'A', rows: ['yes'], columns: ['yes', '__missing__'], values: [[3, 1]] }],
  };
  const assets = new Map(await Promise.all([
    ['/quality.js', '../web/js/quality.js', 'text/javascript'],
    ['/css/quality.css', '../web/css/quality.css', 'text/css'],
    ['/app.css', '../web/app.css', 'text/css'],
  ].map(async ([path, file, type]) => [path, { body: await readFile(new URL(file, import.meta.url)), type }])));
  const html = `<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Synthetic quality check</title><link rel="stylesheet" href="/app.css"></head><body><main class="cqa-root"><div id="cqa-quality"></div></main><script type="module">
    import {mount} from '/quality.js';
    window.fixture = ${JSON.stringify(fixture)};
    window.fixtureMode = 'ready'; window.calls = 0;
    window.CQA = {api: {async quality() {
      window.calls++;
      if (window.fixtureMode === 'error') throw new Error('synthetic error');
      if (window.fixtureMode === 'pending') return new Promise(() => {});
      if (window.fixtureMode === 'empty') return {reports: []};
      if (window.fixtureMode === 'invalid') return {reports: [{...window.fixture, denominator:'answered_only'}]};
      return {reports: [window.fixture]};
    }}};
    window.disposeQuality = mount(window.CQA);
  </script></body></html>`;
  const server = createServer((req, res) => {
    if (req.url === '/') { res.writeHead(200, { 'content-type': 'text/html' }); res.end(html); return; }
    if (req.url === '/favicon.ico') { res.writeHead(204); res.end(); return; }
    const asset = assets.get(req.url);
    res.writeHead(asset ? 200 : 404, { 'content-type': asset?.type || 'text/plain' }); res.end(asset?.body || 'Not found');
  });
  let browser;
  try {
    await new Promise((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolve); });
    browser = await chromium.launch({ headless: true, channel: 'chrome' });
    const page = await browser.newPage(), errors = [], external = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/*', route => {
      if (new URL(route.request().url()).hostname !== '127.0.0.1') { external.push(route.request().url()); return route.abort(); }
      return route.continue();
    });
    await page.goto(`http://127.0.0.1:${server.address().port}/`);
    await page.getByText('Замеров: 1.', { exact: false }).waitFor();
    await page.locator('link[data-cqa-quality-style]').evaluate(link => new Promise(resolve => link.sheet ? resolve() : link.addEventListener('load', resolve, { once: true })));
    await page.locator('details').evaluateAll(nodes => nodes.forEach(el => { el.open = true; }));
    const screenshotDir = process.env.CQA_QUALITY_SCREENSHOT_DIR;
    if (screenshotDir) await mkdir(screenshotDir, { recursive: true });
    for (const width of [375, 768, 1024, 1440]) {
      await page.setViewportSize({ width, height: 900 });
      for (const projector of [false, true]) {
        await page.locator('.cqa-root').evaluate((root, enabled) => root.classList.toggle('projector', enabled), projector);
        const measured = await page.evaluate(() => {
          const target = document.querySelector('#cqa-quality');
          const small = [...target.querySelectorAll('button,summary')].filter(el => el.checkVisibility()).map(el => el.getBoundingClientRect()).filter(rect => rect.width < 44 || rect.height < 44).length;
          const rgb = color => color.match(/[\d.]+/g)?.map(Number);
          const luminance = color => rgb(color).slice(0, 3).map(n => { const c = n / 255; return c <= .04045 ? c / 12.92 : ((c + .055) / 1.055) ** 2.4; }).reduce((s, c, i) => s + c * [.2126, .7152, .0722][i], 0);
          const background = el => {
            for (let current = el; current; current = current.parentElement) {
              const color = getComputedStyle(current).backgroundColor, components = rgb(color);
              if (components && (components.length === 3 || components[3] === 1)) return color;
            }
            return 'rgb(255,255,255)';
          };
          const contrasts = [...target.querySelectorAll('button,summary,th,td,p,li,dt,dd,h1,h2')].filter(el => el.checkVisibility()).map(el => {
            const a = luminance(getComputedStyle(el).color), b = luminance(background(el));
            return (Math.max(a, b) + .05) / (Math.min(a, b) + .05);
          });
          return { pageWidth: document.documentElement.scrollWidth, small, minContrast: Math.min(...contrasts) };
        });
        assert.ok(measured.pageWidth <= width, `overflow at ${width}, projector=${projector}`);
        assert.equal(measured.small, 0, `touch targets at ${width}, projector=${projector}`);
        assert.ok(measured.minContrast >= 4.5, `contrast ${measured.minContrast} at ${width}`);
        if (screenshotDir && !projector && [375, 1440].includes(width)) await page.screenshot({ path: join(screenshotDir, `quality-${width}.png`), fullPage: true });
      }
    }
    await page.keyboard.press('Tab');
    assert.equal(await page.locator('button').evaluate(el => el === document.activeElement), true);
    assert.notEqual(await page.locator('button').evaluate(el => getComputedStyle(el).outlineStyle), 'none');
    const originalCalls = await page.evaluate(() => calls);
    await page.keyboard.press('Enter'); await page.waitForFunction(expected => calls === expected, originalCalls + 1);
    await page.getByText('Замеров: 1.', { exact: false }).waitFor();
    await page.locator('summary').first().focus(); await page.keyboard.press('Enter');
    assert.equal(await page.locator('details').first().evaluate(el => el.open), true);
    for (const [mode, message] of [['empty', 'Замеров пока нет'], ['error', 'Не удалось загрузить замеры'], ['invalid', 'Замеры не показаны: данные не соответствуют контракту.']]) {
      await page.evaluate(value => { window.fixtureMode = value; }, mode);
      await page.getByRole('button', { name: 'Обновить замеры' }).click();
      await page.getByText(message, { exact: true }).waitFor();
      assert.equal(await page.locator('table').count(), 0);
    }
    await page.emulateMedia({ reducedMotion: 'reduce' });
    await page.evaluate(() => { window.fixtureMode = 'pending'; });
    await page.getByRole('button', { name: 'Обновить замеры' }).click();
    assert.equal(await page.locator('.cqa-quality__skeleton').evaluate(el => getComputedStyle(el).animationName), 'none');
    assert.equal(await page.locator('.cqa-quality__content').getAttribute('aria-busy'), 'true');
    assert.deepEqual(errors, []); assert.deepEqual(external, []);
  } finally {
    await browser?.close();
    await new Promise(resolve => server.close(resolve));
  }
});
