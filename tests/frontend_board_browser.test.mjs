/** Optional, isolated Chromium check. All APIs are synthetic functions; no backend or model is contacted.
 * CQA_BOARD_PLAYWRIGHT_MODULE=/absolute/path/to/playwright/index.mjs node --test tests/frontend_board*.mjs
 * CQA_BOARD_BROWSER_EXECUTABLE may select an already installed Chromium executable. No downloads.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile, mkdtemp } from 'node:fs/promises';
import { createServer } from 'node:http';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

const modulePath = process.env.CQA_BOARD_PLAYWRIGHT_MODULE;
test('isolated board: request semantics, hatched failures, keyboard, responsive targets and contrast',
  { skip: !modulePath, timeout: 60000 }, async t => {
    const { chromium } = await import(pathToFileURL(modulePath).href);
    const assets = new Map(await Promise.all(['web/js/board.js', 'web/js/model-profiles.js', 'web/css/board.css', 'web/app.css'].map(async path =>
      [`/${path}`, await readFile(new URL(`../${path}`, import.meta.url), 'utf8')])));
    const html = `<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
      <title>Synthetic board acceptance</title><link rel="stylesheet" href="/web/app.css"></head>
      <body class="cqa-root" style="margin:0"><main id="cqa-board"></main><script type="module">
      import { mount } from '/web/js/board.js';
      const listeners = new Map(); window.calls = [];
      window.CQA = { config: {mock: false}, state: {checklistVersion: 1}, bus: {
        on(name, fn) { if (!listeners.has(name)) listeners.set(name, new Set()); listeners.get(name).add(fn); return () => listeners.get(name).delete(fn); },
        emit(name, data) { for (const fn of listeners.get(name) || []) fn(data); },
      }, api: {
        conversations: async () => [{ id: 'new-synthetic', status: 'pending', total: null, group_id: 'SYNTHETIC' }],
        checklists: async () => [{version: 1, name: 'Base', active: true}, {version: 2, name: 'New criterion', parent_version: 1}],
        config: async () => ({mode: 'fast', location: 'cloud', profile: 'synthetic', model: {model: 'fixture only'}}),
        setConfig: async body => ({...body, profile: 'synthetic'}),
        createRun: async body => { window.calls.push(body); return { run_id: 'synthetic-' + window.calls.length,
          status: 'completed', done: 3, total: 3, pairs_total: 24, pairs_to_evaluate: 8, from_cache: 16, llm_calls: 0,
          per_conv: [
            {id: 'synthetic-failed', status: 'error', error: 'Synthetic provider failure', old_total: 85, new_total: null},
            {id: 'synthetic-incomplete', status: 'done', old_total: 50, new_total: 100, score: {status: 'incomplete', missing: ['C2', 'C3']}},
            {id: 'synthetic-success', status: 'done', old_total: 60, new_total: 80},
          ] }; },
      }}; window.destroyBoard = mount(window.CQA);
      </script></body></html>`;
    const server = createServer((request, response) => {
      const path = new URL(request.url, 'http://localhost').pathname;
      if (path === '/') { response.writeHead(200, {'Content-Type': 'text/html'}); response.end(html); return; }
      if (!assets.has(path)) { response.writeHead(404); response.end(); return; }
      response.writeHead(200, {'Content-Type': path.endsWith('.js') ? 'text/javascript' : 'text/css'}); response.end(assets.get(path));
    });
    await new Promise((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolve); });
    t.after(() => new Promise(resolve => server.close(resolve)));
    const browser = await chromium.launch({ headless: true, ...(process.env.CQA_BOARD_BROWSER_EXECUTABLE ? {executablePath: process.env.CQA_BOARD_BROWSER_EXECUTABLE} : {}) });
    t.after(() => browser.close());
    const page = await browser.newPage();
    const errors = []; page.on('pageerror', error => errors.push(error.message));
    const baseUrl = `http://127.0.0.1:${server.address().port}`;
    await page.route('**/*', route => new URL(route.request().url()).origin === baseUrl ? route.continue() : route.abort());
    await page.goto(baseUrl);
    await page.waitForFunction(() => document.querySelector('button[type=submit]')?.disabled === false);
    await page.getByRole('combobox', {name: /^Чек-лист/}).selectOption('2');
    assert.equal(await page.locator('[data-board-live]').isChecked(), false);
    assert.equal(await page.locator('[data-board-live]').isDisabled(), true);
    assert.equal(await page.evaluate(() => window.calls.length), 0, 'selection cannot launch a preview');
    await page.getByRole('button', {name: 'Оценить и применить', exact: true}).click();
    await page.locator('.cqa-board__tile').first().waitFor();
    assert.deepEqual(await page.evaluate(() => window.calls.map(({live, force, preview}) => ({live, force, preview}))), [{live: true, force: false, preview: false}]);
    assert.match(await page.locator('.cqa-board__run-plan').innerText(), /8 пар.*16 пар/);
    const partial = page.locator('[data-conv-id="synthetic-incomplete"]');
    assert.match(await partial.innerText(), /Не рассчитан/); assert.doesNotMatch(await partial.innerText(), /100/);
    await page.evaluate(() => document.querySelectorAll('details').forEach(el => { el.open = true; }));
    await page.emulateMedia({reducedMotion: 'reduce'});
    const artifacts = await mkdtemp(join(tmpdir(), 'cqa-board-browser-'));
    const checks = [];
    for (const width of [375, 768, 1024, 1440]) {
      await page.setViewportSize({width, height: 1000});
      const metrics = await page.evaluate(() => {
        const shown = el => el.checkVisibility() && !el.closest('[hidden]');
        const targets = [...document.querySelectorAll('button,input,select,summary')].filter(shown);
        const small = targets.flatMap(el => {
          const target = el.matches('input[type=checkbox]') ? el.closest('label') : el;
          const rect = target.getBoundingClientRect();
          return rect.width < 44 || rect.height < 44 ? [{text: target.textContent, w: rect.width, h: rect.height}] : [];
        });
        const tile = document.querySelector('[data-state=incomplete]');
        return {width: innerWidth, scrollWidth: document.documentElement.scrollWidth, small,
          hatch: getComputedStyle(tile).backgroundImage, animation: getComputedStyle(document.querySelector('[data-state=done]')).animationName,
          primary: [...document.querySelectorAll('.cqa-board__button--primary')].filter(shown).length};
      });
      assert.ok(metrics.scrollWidth <= width, `overflow at ${width}: ${metrics.scrollWidth}`);
      assert.deepEqual(metrics.small, [], `44px targets at ${width}`);
      assert.match(metrics.hatch, /repeating-linear-gradient/); assert.equal(metrics.animation, 'none'); assert.equal(metrics.primary, 1);
      checks.push(metrics);
      await page.screenshot({path: join(artifacts, `board-${width}.png`), fullPage: true});
    }
    await page.getByRole('button', {name: 'Пересчитать всё заново', exact: true}).focus();
    await page.keyboard.press('Tab'); await page.keyboard.press('Shift+Tab');
    const focus = await page.evaluate(() => ({visible: document.activeElement.matches(':focus-visible'), width: getComputedStyle(document.activeElement).outlineWidth}));
    assert.equal(focus.visible, true); assert.ok(parseFloat(focus.width) >= 2);
    await page.keyboard.press('Enter');
    await page.waitForFunction(() => window.calls.length === 2);
    assert.deepEqual(await page.evaluate(() => [window.calls[1].force, window.calls[1].live]), [true, true]);
    const contrasts = await page.evaluate(() => {
      const probe = document.createElement('span'); document.querySelector('.cqa-board').append(probe);
      const rgb = color => {
        probe.style.color = color;
        // Canvas resolves rgb, hex and color-mix into sRGB channel values.
        const canvas = document.createElement('canvas'), ctx = canvas.getContext('2d');
        ctx.fillStyle = getComputedStyle(probe).color; ctx.fillRect(0, 0, 1, 1); return [...ctx.getImageData(0, 0, 1, 1).data].slice(0, 3);
      };
      const luminance = color => rgb(color).map(v => v / 255).map(v => v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4)
        .reduce((sum, v, i) => sum + v * [.2126, .7152, .0722][i], 0);
      const pairs = [['--muted', '--surface'], ['--muted', '--surface-subtle'], ['--brand-ink', '--brand'],
        ['--no', '--surface'], ['--no', '--board-error-stripe'], ['--muted', '--board-error-stripe'], ['--ink', '--board-error-stripe']];
      const ratios = pairs.map(([fg, bg]) => { const a = luminance('var(' + fg + ')'), b = luminance('var(' + bg + ')'); return {fg, bg, ratio: (Math.max(a, b) + .05) / (Math.min(a, b) + .05)}; });
      probe.remove(); return ratios;
    });
    for (const item of contrasts) assert.ok(item.ratio >= 4.5, `${item.fg}/${item.bg}: ${item.ratio}`);
    assert.deepEqual(errors, []);
    t.diagnostic(JSON.stringify({artifacts, widths: checks.map(m => m.width), minContrast: Math.min(...contrasts.map(m => m.ratio))}));
  });
