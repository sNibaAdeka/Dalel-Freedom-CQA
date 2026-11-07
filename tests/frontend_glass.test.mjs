import assert from 'node:assert/strict';
import test from 'node:test';
import { readFile } from 'node:fs/promises';

const read = path => readFile(new URL(path, import.meta.url), 'utf8');
const glassSource = await read('../web/js/glass.js');
const css = await read('../web/css/glass.css');
const appCss = await read('../web/app.css');
const polish = await read('../web/css/workspace-polish.css');
const G = await import(`data:text/javascript;base64,${Buffer.from(glassSource).toString('base64')}`);

const rootBlock = css.match(/:root\s*\{([^}]*)\}/)[1];
const token = name => { const m = new RegExp(`--${name}:\\s*(#[0-9a-fA-F]{6})`).exec(rootBlock); assert.ok(m, `token --${name}`); return G.hexToRgb(m[1]); };
const appToken = name => { const m = new RegExp(`--${name}:\\s*(#[0-9a-fA-F]{6})`).exec(appCss); assert.ok(m, `app token --${name}`); return G.hexToRgb(m[1]); };
const tokens = {
  mesh: ['gl-mesh-cream', 'gl-mesh-sand', 'gl-mesh-green', 'gl-mesh-mint', 'gl-base'].map(token),
  surface: token('gl-surface'), tint: token('gl-tint'),
};
const ink = token('gl-ink'), muted = token('gl-muted');
const darkestStop = tokens.mesh.reduce((a, b) => (G.relativeLuminance(a) < G.relativeLuminance(b) ? a : b));

test('presets: default is frosted, projector is denser than every preset', () => {
  assert.equal(G.DEFAULT_PRESET, 'frosted');
  assert.deepEqual(Object.values(G.PRESETS).map(p => p.label), ['Прозрачное', 'Матовое', 'Плотное']);
  for (const preset of Object.values(G.PRESETS)) {
    assert.ok(G.effectiveSettings(preset, true).opacity >= preset.opacity);
    assert.ok(G.effectiveSettings(preset, true).opacity >= G.PROJECTOR_OPACITY);
  }
  assert.ok(G.PRESETS.clear.opacity < G.PRESETS.frosted.opacity && G.PRESETS.frosted.opacity < G.PRESETS.solid.opacity);
});

test('sliders are clamped and junk falls back to defaults', () => {
  const s = G.clampSettings({ blur: 999, opacity: 0, saturation: -4, edge: 'x' });
  assert.equal(s.blur, 40); assert.equal(s.opacity, G.MIN_OPACITY); assert.equal(s.saturation, 0.5); assert.equal(s.edge, G.PRESETS.frosted.edge);
  assert.deepEqual(G.clampSettings(null), G.clampSettings({}));
  assert.equal(G.clampSettings({ opacity: 5 }).opacity, G.MAX_OPACITY);
  assert.equal(G.presetName(G.PRESETS.frosted), 'frosted');
  assert.equal(G.presetName({ ...G.PRESETS.frosted, blur: 3 }), null);
});

test('storage is read and written strictly inside try/catch', () => {
  const broken = { getItem() { throw new Error('denied'); }, setItem() { throw new Error('denied'); } };
  assert.deepEqual(G.loadSettings(broken), G.clampSettings(G.PRESETS.frosted));
  assert.equal(G.saveSettings(G.PRESETS.clear, broken), false);
  const bag = new Map(); const ok = { getItem: k => bag.get(k) ?? null, setItem: (k, v) => bag.set(k, v) };
  assert.equal(G.saveSettings(G.PRESETS.frosted, ok), true);
  assert.equal(G.presetName(G.loadSettings(ok)), 'frosted');
  bag.set(G.STORAGE_KEY, '{not json'); assert.deepEqual(G.loadSettings(ok), G.clampSettings(G.PRESETS.frosted));
});

test('WCAG formula sanity', () => {
  assert.ok(Math.abs(G.contrastRatio([0, 0, 0], [255, 255, 255]) - 21) < 1e-9);
  assert.ok(Math.abs(G.contrastRatio([119, 119, 119], [255, 255, 255]) - 4.48) < 0.02);
});

const cases = [['MIN_OPACITY floor', G.MIN_OPACITY], ...Object.entries(G.PRESETS).map(([k, p]) => [`preset ${k}`, p.opacity]), ['projector', G.PROJECTOR_OPACITY]];
test('text contrast >= 4.5:1 on the worst mesh stop for every preset (ink and muted)', () => {
  const rows = [];
  for (const [name, opacity] of cases) {
    const inkRatio = G.worstContrast(ink, tokens, opacity), mutedRatio = G.worstContrast(muted, tokens, opacity);
    rows.push(`${name} (opacity ${opacity}): ink ${inkRatio.toFixed(2)}, muted ${mutedRatio.toFixed(2)}`);
    assert.ok(inkRatio >= 4.5, `${name} ink ${inkRatio}`); assert.ok(mutedRatio >= 4.5, `${name} muted ${mutedRatio}`);
  }
  console.log(`# worst mesh stop luminance ${G.relativeLuminance(darkestStop).toFixed(3)}\n# ${rows.join('\n# ')}`);
});

test('slider extremes cannot break readability', () => {
  for (const opacity of [G.MIN_OPACITY, G.clampSettings({ opacity: 0.01 }).opacity]) assert.ok(G.worstContrast(muted, tokens, opacity) >= 4.5);
});

test('selected tab and primary buttons keep AA contrast; CTA green differs from the "Yes" green', () => {
  assert.ok(G.contrastRatio(appToken('brand-ink'), appToken('brand')) >= 4.5);
  assert.ok(G.contrastRatio(appToken('yes'), appToken('yes-bg')) >= 4.5);
  assert.notEqual(appToken('brand').join(), appToken('yes').join());
});

test('css: raw colours only as :root tokens, no emoji, no images', () => {
  const rest = css.replace(rootBlock, '').replace(/\/\*[\s\S]*?\*\//g, '');
  assert.equal(/#[0-9a-fA-F]{3,8}\b/.test(rest), false, 'raw hex in component rules');
  assert.equal(/rgba?\(|hsla?\(/.test(rest), false);
  assert.equal(/\p{Extended_Pictographic}/u.test(css + glassSource), false);
  assert.equal(/url\(/.test(css), false);
});

test('css: only transform/opacity motion, 150-300ms, reduced-motion and no-backdrop fallback', () => {
  for (const m of css.matchAll(/transition:\s*([^;]+);/g)) for (const part of m[1].split(',')) assert.match(part.trim(), /^(none|transform|opacity)\b/, `transition ${part}`);
  assert.match(css, /--gl-t:\s*200ms/);
  assert.match(css, /@media \(prefers-reduced-motion: reduce\)/);
  assert.match(css, /@supports not \(\(backdrop-filter: blur\(1px\)\) or \(-webkit-backdrop-filter: blur\(1px\)\)\)/);
  assert.match(css, /backdrop-filter: blur\(var\(--gl-blur\)\) saturate\(var\(--gl-sat\)\)/);
  assert.match(css, /opacity: \.12/);
  for (const frame of css.match(/@keyframes[^{]+\{[\s\S]*?\}\s*\}/g) || []) assert.equal(/\b(width|height|top|left)\s*:/.test(frame), false);
});

test('css: geometry from the brief (62px bar r20, scene r27, panel r17) and 44px targets', () => {
  assert.match(css, /--gl-bar-h:\s*62px/); assert.match(css, /--gl-radius-bar:\s*20px/);
  assert.match(css, /--gl-radius-scene:\s*27px/); assert.match(css, /--gl-radius-panel:\s*17px/);
  assert.match(css, /\.gl-icon-button[^{]*\{[^}]*width: 44px; height: 44px/);
  assert.match(css, /\.gl-preset[^{]*\{[^}]*min-height: 44px/);
  assert.match(css, /input\[type="range"\][^{]*\{[^}]*height: 44px/);
});


test('photo can be black and text still passes at the minimum material opacity', () => {
  // The room is decorative and replaceable: prove the bound for any image pixel,
  // not just sampled pixels in this particular JPEG. Tint is conservatively omitted.
  const shadeHex = /--ws-room-shade:\s*(#[0-9a-f]{8})/i.exec(polish)[1];
  const shadeAlpha = parseInt(shadeHex.slice(-2), 16) / 255;
  const darkestPhoto = G.composite(G.hexToRgb(shadeHex), shadeAlpha, [0, 0, 0]);
  const backdrop = G.composite(tokens.surface, G.MIN_OPACITY, darkestPhoto);
  const ratio = G.contrastRatio(muted, backdrop);
  console.log(`# arbitrary black photo + shade + minimum opacity: muted ${ratio.toFixed(2)}`);
  assert.ok(ratio >= 4.5, `minimum photo contrast ${ratio}`);
});

test('decorative asset is same-origin and has a solid fallback; polish uses semantic tokens', () => {
  const root = polish.match(/:root\s*\{([^}]*)\}/)[1];
  const rules = polish.replace(root, '').replace(/\/\*[\s\S]*?\*\//g, '');
  assert.equal(/#[0-9a-fA-F]{3,8}\b/.test(rules), false);
  assert.equal(/url\(['"]?https?:\/\//.test(polish), false);
  assert.match(polish, /url\('..\/assets\/workspace-room\.jpg'\)/);
  assert.match(polish, /background-color: var\(--ws-room-fallback\)/);
  assert.match(polish, /prefers-reduced-motion: reduce/);
});


test('neutral palette keeps both inverse CTA and positive/status text readable', () => {
  const get = name => G.hexToRgb(new RegExp(`--${name}:\\s*(#[0-9a-fA-F]{6})`).exec(polish)[1]);
  for (const [foreground, background] of [['ws-brand-ink', 'ws-brand'], ['ws-brand', 'ws-brand-soft'], ['ws-positive', 'ws-positive-bg'], ['ws-nav-ink', 'ws-nav-active']]) {
    assert.ok(G.contrastRatio(get(foreground), get(background)) >= 4.5, `${foreground} on ${background}`);
  }
});
