/** W9: source-bound slide metrics + opt-in real Chromium projector/deck checks.
 * CQA_W9_PLAYWRIGHT_MODULE=/absolute/path/to/playwright/index.mjs node --test tests/frontend_w9_demo.test.mjs
 * No backend, credentials or models: the full shell uses its authored demo data.
 */
import assert from 'node:assert/strict';
import test from 'node:test';
import { runInNewContext } from 'node:vm';
import { readFile, mkdir } from 'node:fs/promises';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { join, resolve, extname } from 'node:path';

const root = fileURLToPath(new URL('../', import.meta.url));
const html = await readFile(join(root, 'docs/slides/index.html'), 'utf8');
const quality = JSON.parse(await readFile(join(root, 'web/data/quality.json'), 'utf8'));
const economics = await readFile(join(root, 'docs/BANK_ECONOMICS.md'), 'utf8');
const metric = name => html.match(new RegExp(`data-metric="${name}">([^<]+)<`))?.[1];
const ru = (value, digits = 1) => value.toLocaleString('ru-RU', { minimumFractionDigits: digits, maximumFractionDigits: digits });

test('slide metrics match the machine-reference Gemini DEV holdout and recorded economics', () => {
  const report = quality.reports.find(r => r.id === 'gemini-4-argon-g9-holdout');
  assert.equal(report.reference.type, 'machine');
  assert.equal(report.sample.split, 'holdout');
  assert.equal(report.sample.dev_reused, true);
  const labelled = report.counts.total - report.counts.unlabelled;
  assert.equal(metric('agreement'), `${ru(100 * report.counts.matched / labelled)}%`);
  assert.equal(metric('cells'), `${report.counts.matched}/${labelled}`);
  assert.equal(metric('dialogues'), String(report.sample.dialogues));
  assert.equal(metric('ci'), `${ru(100 * report.agreement_ci.lower)}–${ru(100 * report.agreement_ci.upper)}%`);
  for (const [profile, label] of [['cloud', 'Облако · Gemini'], ['cascade', 'Каскад · Haiku → Gemini'], ['routing', 'Маршрутизация']]) {
    const row = economics.split('\n').find(line => line.startsWith(`| ${label} |`)).split('|');
    const rendered = html.match(new RegExp(`data-cost="${profile}">([^<]+)<`))?.[1];
    assert.equal(rendered, `$${ru(Number(row[4].trim()), 4)}`);
  }
  assert.match(html, /не независимый тест/);
  assert.match(html, /не точность по человеческой разметке/);
  assert.match(html, /не счёт и не ROI/);
});

test('four standalone slides contain no external resource dependencies', () => {
  assert.equal((html.match(/<section class="slide"/g) || []).length, 4);
  assert.doesNotMatch(html, /<(?:script|img|iframe)[^>]+src=|@import|url\(|https?:\/\//i);
  assert.doesNotMatch(html, /<link[^>]+href="(?!data:)/i);
  assert.match(html, /prefers-reduced-motion/);
});

test('offline slide controls: keys, buttons, bounds, hash, and editable-field isolation', () => {
  const slides = Array.from({ length: 4 }, (_, i) => ({ hidden: i !== 0, dataset: { title: `Слайд ${i + 1}` } }));
  const controls = Object.fromEntries(['previous', 'next', 'counter'].map(id => [id, { addEventListener(type, fn) { this[type] = fn; } }]));
  const events = {}, location = { hash: '#3' };
  const document = { querySelectorAll: () => slides, getElementById: id => controls[id], addEventListener: (type, fn) => { events[type] = fn; } };
  runInNewContext(html.match(/<script>([\s\S]*?)<\/script>/)[1], {
    document, location, window: { addEventListener(type, fn) { events[type] = fn; } },
    history: { replaceState(_state, _title, hash) { location.hash = hash; } },
  });
  const current = n => {
    assert.equal(controls.counter.textContent, `${n} / 4`);
    assert.equal(location.hash, `#${n}`);
    assert.deepEqual(slides.map(slide => slide.hidden), slides.map((_, i) => i !== n - 1));
    assert.equal(controls.previous.disabled, n === 1);
    assert.equal(controls.next.disabled, n === 4);
  };
  const key = (key, opts = {}) => {
    let prevented = false;
    events.keydown({ key, target: { closest: () => false }, preventDefault() { prevented = true; }, ...opts });
    return prevented;
  };
  current(3);
  key('Home'); current(1);
  key('ArrowLeft'); current(1);
  key('ArrowRight'); current(2);
  key(' '); current(3);
  key('PageDown'); current(4);
  key('ArrowDown'); current(4);
  key('PageUp'); current(3);
  key('ArrowUp'); current(2);
  controls.previous.click(); current(1);
  controls.next.click(); current(2);
  for (const modifier of ['altKey', 'ctrlKey', 'metaKey']) {
    assert.equal(key('End', { [modifier]: true }), false); current(2);
  }
  assert.equal(key('End', { target: { closest: () => true } }), false); current(2);
  assert.equal(key(' ', { target: { closest: selector => selector === 'button' } }), false); current(2);
  assert.equal(key('Tab'), false);
  key('End'); current(4);
  location.hash = '#999'; events.hashchange(); current(4);
  location.hash = '#invalid'; events.hashchange(); current(1);
});

test('slide text tokens meet 4.5:1 against the darkest surface in the neutral palette', () => {
  const token = name => html.match(new RegExp(`--${name}:\\s*(#[a-f0-9]+)`, 'i'))[1];
  const lum = hex => hex.slice(1, 7).match(/../g).map(v => parseInt(v, 16) / 255)
    .map(v => v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4)
    .reduce((sum, v, i) => sum + v * [.2126, .7152, .0722][i], 0);
  for (const fg of ['ink', 'muted']) for (const bg of ['background', 'surface', 'highlight']) {
    const ratio = (lum(token(bg)) + .05) / (lum(token(fg)) + .05);
    assert.ok(ratio >= 4.5, `${fg}/${bg}: ${ratio}`);
  }
});

const modulePath = process.env.CQA_W9_PLAYWRIGHT_MODULE;
test('Chromium: deck navigation, source text, contrast; P mode score, diamond, long evidence and scrolling', { skip: !modulePath, timeout: 90000 }, async () => {
  const { chromium } = await import(pathToFileURL(modulePath).href);
  let browser;
  const dir = join(root, 'output/playwright/w9');
  await mkdir(dir, { recursive: true });
  try {
    // Virtual loopback origin: every response is fulfilled from files, no listening port.
    const origin = 'http://127.0.0.1:8099';
    browser = await chromium.launch({ channel: 'chrome', headless: true });
    const page = await browser.newPage();
    const errors = [], forbidden = [];
    page.on('pageerror', e => errors.push(e.message));
    await page.route('**/*', async route => {
      const url = new URL(route.request().url());
      if (url.origin !== origin || url.pathname.includes('/api/')) { forbidden.push(url.href); return route.abort(); }
      const path = resolve(root, '.' + url.pathname);
      if (!path.startsWith(root) || !['/web/', '/docs/slides/'].some(prefix => url.pathname.startsWith(prefix))) return route.fulfill({ status: 404, body: '' });
      try {
        const body = await readFile(path);
        const types = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.json': 'application/json', '.jpg': 'image/jpeg' };
        return route.fulfill({ status: 200, contentType: types[extname(path)] || 'application/octet-stream', body });
      } catch { return route.fulfill({ status: 404, body: '' }); }
    });
    await page.emulateMedia({ reducedMotion: 'reduce' });
    await page.goto(`${origin}/docs/slides/index.html`);
    for (const [width, height] of [[375, 812], [768, 1024], [1024, 768], [1280, 720], [1440, 900]]) {
      await page.setViewportSize({ width, height });
      for (let i = 0; i < 4; i++) {
        await page.evaluate(n => { location.hash = `#${n + 1}`; }, i);
        await page.waitForFunction(n => document.querySelectorAll('.slide')[n].hidden === false, i);
        const layout = await page.locator('.slide:not([hidden])').evaluate(slide => {
          const box = slide.getBoundingClientRect();
          const outside = [...slide.querySelectorAll('*')].filter(e => {
            const b = e.getBoundingClientRect(); return b.right > box.right + 1 || b.bottom > box.bottom + 1;
          }).map(e => e.className);
          return { ratio: box.width / box.height, outside, overflow: document.documentElement.scrollWidth > innerWidth };
        });
        assert.equal(layout.overflow, false, `slide ${i + 1} at ${width}`);
        assert.deepEqual(layout.outside, [], `clipped slide ${i + 1} at ${width}`);
        if (width >= 768) assert.ok(Math.abs(layout.ratio - 16 / 9) < .01);
        if (width === 1280) await page.screenshot({ path: join(dir, `slide-${i + 1}.png`) });
      }
    }
    await page.keyboard.press('Home');
    assert.equal(await page.locator('#counter').textContent(), '1 / 4');
    await page.keyboard.press('ArrowLeft');
    assert.equal(await page.locator('#counter').textContent(), '1 / 4');
    await page.keyboard.press('ArrowRight');
    await page.keyboard.press('Space');
    assert.equal(await page.locator('#counter').textContent(), '3 / 4');
    await page.locator('#previous').focus();
    await page.keyboard.press('Space');
    assert.equal(await page.locator('#counter').textContent(), '2 / 4');
    assert.equal(await page.locator('#previous').evaluate(e => getComputedStyle(e).outlineStyle), 'solid');
    await page.locator('#next').click();
    await page.keyboard.press('End');
    assert.equal(await page.locator('#next').isDisabled(), true);
    await page.keyboard.press('ArrowRight');
    assert.equal(await page.locator('#counter').textContent(), '4 / 4');
    // Conservative palette check: darkest possible neutral slide background, including glass.
    const contrast = await page.evaluate(() => {
      const lum = hex => hex.replace('#', '').match(/../g).slice(0, 3).map(x => parseInt(x, 16) / 255).map(x => x <= .04045 ? x / 12.92 : ((x + .055) / 1.055) ** 2.4).reduce((s, x, i) => s + x * [.2126, .7152, .0722][i], 0);
      const css = getComputedStyle(document.documentElement);
      const l = token => lum(css.getPropertyValue(token).trim());
      return ['--ink', '--muted'].map(fg => (l('--background') + .05) / (l(fg) + .05));
    });
    assert.ok(Math.min(...contrast) >= 4.5);

    await page.setViewportSize({ width: 1280, height: 720 });
    await page.goto(`${origin}/web/index.html?mode=demo`);
    await page.waitForFunction(() => window.CQA?.state?.ready);
    await page.locator('.cq-list-row').first().click();
    await page.locator('.cqa-card__total strong').waitFor();
    await page.waitForFunction(() => [...document.querySelectorAll('link[rel=stylesheet]')].every(link => link.sheet));
    await page.keyboard.press('p');
    assert.equal(await page.locator('#cqa-projector-toggle').getAttribute('aria-pressed'), 'true');
    // Keep the shipped shell/list, substitute only a synthetic card with long verbatim evidence.
    await page.evaluate(async () => {
      const CQA = window.CQA;
      const detail = await CQA.api.conversation(CQA.state.activeConversationId);
      const quote = 'Синтетический пример: обещание гарантированного дохода. ' + 'Длинная реплика для проверки переноса текста и прокрутки. '.repeat(10);
      detail.messages.push({ id: 'w9-proof', idx: detail.messages.length, speaker: 'operator', text: quote });
      detail.evals[0].evidence = [{ message_id: 'w9-proof', quote }];
      detail.score.total = 0; detail.score.critical = true;
      window.w9Fixture = detail;
      CQA.api.conversation = async () => structuredClone(window.w9Fixture);
      CQA.bus.emit('conv:open', detail.id);
    });
    await page.getByText('Отмечена грубая ошибка', { exact: true }).waitFor();
    for (const [width, height] of [[1280, 720], [1440, 900]]) {
      await page.setViewportSize({ width, height });
      await page.locator('.cqa-card__results').evaluate(e => { e.scrollTop = 0; });
      const layout = await page.evaluate(() => {
        const rect = sel => { const b = document.querySelector(sel).getBoundingClientRect(); return { top: b.top, bottom: b.bottom, right: b.right, height: b.height, width: b.width }; };
        return { overflow: document.documentElement.scrollWidth > innerWidth, pageHeight: document.documentElement.scrollHeight,
          card: rect('.cqa-card'), score: rect('.cqa-card__total strong'), diamond: rect('.cqa-card__critical svg'),
          font: parseFloat(getComputedStyle(document.querySelector('.cqa-card__total strong')).fontSize),
          quoteFont: parseFloat(getComputedStyle(document.querySelector('.cqa-card__evidence blockquote')).fontSize),
          panes: ['.cqa-card__results', '.cqa-card__transcript-scroll'].map(sel => { const e = document.querySelector(sel); return { ...rect(sel), scroll: getComputedStyle(e).overflowY, overflowX: e.scrollWidth > e.clientWidth + 1 }; }) };
      });
      assert.equal(layout.overflow, false);
      assert.ok(layout.pageHeight <= height + 1, JSON.stringify(layout));
      assert.ok(layout.card.bottom <= height && layout.card.top >= 0, JSON.stringify(layout));
      assert.ok(layout.score.bottom <= layout.card.bottom && layout.score.top >= layout.card.top);
      assert.ok(layout.font >= 80 && layout.quoteFont >= 24);
      assert.ok(layout.diamond.width >= 32 && layout.diamond.height >= 32);
      assert.match(await page.locator('.cqa-card__critical path').first().getAttribute('d'), /M12 2 22 12 12 22 2 12Z/);
      for (const pane of layout.panes) { assert.equal(pane.overflowX, false); assert.equal(pane.scroll, 'auto'); assert.ok(pane.height > 150); }
      await page.screenshot({ path: join(dir, `projector-${width}.png`) });
      // Last criterion and long quote can be reached; a scroll area is not a clipped card.
      await page.locator('.cqa-card__criterion').last().scrollIntoViewIfNeeded();
      const end = await page.locator('.cqa-card__criterion').last().evaluate(e => {
        const b = e.getBoundingClientRect(), p = e.closest('.cqa-card__results').getBoundingClientRect();
        return b.bottom <= p.bottom + 1 && b.bottom <= innerHeight;
      });
      assert.equal(end, true);
      await page.locator('.cqa-card__evidence blockquote').first().scrollIntoViewIfNeeded();
      await page.screenshot({ path: join(dir, `evidence-${width}.png`) });
      await page.locator('.cqa-card__proof-button').first().click();
      await page.locator('.cqa-card__quote-mark').first().waitFor();
    }
    // P remains a shell shortcut, not an accidental toggle while typing.
    await page.locator('.cq-list-search input').fill('p');
    await page.keyboard.press('p');
    assert.equal(await page.locator('#cqa-projector-toggle').getAttribute('aria-pressed'), 'true');
    await page.locator('.cq-list-search input').fill('');
    await page.locator('.cq-list-search input').evaluate(e => e.blur());
    await page.keyboard.press('p');
    assert.equal(await page.locator('#cqa-projector-toggle').getAttribute('aria-pressed'), 'false');
    await page.keyboard.press('p');
    // Preserve "not calculated" without rendering it as an 80px number.
    await page.evaluate(() => { window.w9Fixture.evals = []; window.w9Fixture.score = { total: null, status: 'incomplete', lines: [] }; CQA.bus.emit('conv:open', window.w9Fixture.id); });
    await page.locator('.cqa-card__total--incomplete').waitFor();
    assert.equal(await page.locator('.cqa-card__total strong').evaluate(e => parseFloat(getComputedStyle(e).fontSize)), 28);
    for (const width of [375, 768, 1024]) {
      await page.setViewportSize({ width, height: 900 });
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, `projector overflow at ${width}`);
    }
    assert.deepEqual(errors, []);
    assert.deepEqual(forbidden, []);
  } finally {
    await browser?.close();
  }
});
