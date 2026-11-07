// Glass appearance layer for Дәлел. No imports: pure helpers (testable without DOM) + mount().
// Visual language: surface x opacity + backdrop blur/saturate + inset edge light. Semantic colours are never blurred.
export const STORAGE_KEY = 'dalel.glass.v1';
export const DEFAULT_PRESET = 'frosted';
export const MIN_OPACITY = 0.5;
export const MAX_OPACITY = 0.96;
export const PROJECTOR_OPACITY = 0.96;
export const TINT_ALPHA = 0.12;
export const LIMITS = Object.freeze({
  blur: [0, 40], opacity: [MIN_OPACITY, MAX_OPACITY], saturation: [0.5, 2], edge: [0, 1],
});
export const PRESETS = Object.freeze({
  clear: Object.freeze({ label: 'Прозрачное', blur: 10, opacity: 0.54, saturation: 1.1, edge: 0.8 }),
  frosted: Object.freeze({ label: 'Матовое', blur: 22, opacity: 0.66, saturation: 1.15, edge: 0.65 }),
  solid: Object.freeze({ label: 'Плотное', blur: 32, opacity: 0.9, saturation: 1, edge: 0.45 }),
});
const KEYS = ['blur', 'opacity', 'saturation', 'edge'];

const bounded = (value, [min, max], fallback) => {
  const number = typeof value === 'string' && value.trim() === '' ? NaN : Number(value);
  return Number.isFinite(number) ? Math.min(max, Math.max(min, number)) : fallback;
};

export function clampSettings(input = {}) {
  const base = PRESETS[DEFAULT_PRESET];
  const source = input && typeof input === 'object' ? input : {};
  const out = {};
  for (const key of KEYS) out[key] = bounded(source[key], LIMITS[key], base[key]);
  return out;
}

export function presetName(settings) {
  const s = clampSettings(settings);
  return Object.keys(PRESETS).find(name => KEYS.every(key => Math.abs(PRESETS[name][key] - s[key]) < 1e-6)) || null;
}

// Projector mode: denser, less see-through and a lighter edge; never below the user's own opacity.
export function effectiveSettings(settings, projector = false) {
  const s = clampSettings(settings);
  if (!projector) return s;
  return { ...s, opacity: Math.max(s.opacity, PROJECTOR_OPACITY), blur: Math.min(s.blur, 24), edge: s.edge * 0.6 };
}

export function cssVars(settings, projector = false) {
  const s = effectiveSettings(settings, projector);
  const pct = value => `${Math.round(value * 1000) / 10}%`;
  return {
    '--gl-blur': `${Math.round(s.blur * 10) / 10}px`,
    '--gl-sat': pct(s.saturation),
    '--gl-alpha': pct(s.opacity),
    '--gl-alpha-inner': pct(0.16 + s.opacity * 0.5),
    '--gl-edge': pct(s.edge),
  };
}

export function loadSettings(storage) {
  try {
    const raw = (storage ?? globalThis.localStorage)?.getItem(STORAGE_KEY);
    if (!raw) return clampSettings(PRESETS[DEFAULT_PRESET]);
    return clampSettings(JSON.parse(raw));
  } catch { return clampSettings(PRESETS[DEFAULT_PRESET]); }
}

export function saveSettings(settings, storage) {
  try { (storage ?? globalThis.localStorage).setItem(STORAGE_KEY, JSON.stringify(clampSettings(settings))); return true; }
  catch { return false; }
}

// ---- WCAG 2.x contrast ----
export function hexToRgb(hex) {
  const m = /^#?([0-9a-f]{6})([0-9a-f]{2})?$/i.exec(String(hex).trim());
  if (!m) throw new TypeError(`Bad colour: ${hex}`);
  const n = parseInt(m[1], 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}
export function relativeLuminance(rgb) {
  const [r, g, b] = rgb.map(v => { const c = v / 255; return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4; });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}
export function contrastRatio(a, b) {
  const [hi, lo] = [relativeLuminance(a), relativeLuminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}
export function composite(top, alpha, bottom) { return top.map((v, i) => v * alpha + bottom[i] * (1 - alpha)); }

// Background under text on the glass: mesh -> surface at opacity -> tint layer.
export function glassBackground({ mesh, surface, tint }, opacity, tintAlpha = TINT_ALPHA) {
  return composite(tint, tintAlpha, composite(surface, opacity, mesh));
}
// Lowest contrast of `ink` over every mesh stop (worst part of the background).
export function worstContrast(ink, tokens, opacity) {
  return Math.min(...tokens.mesh.map(stop => contrastRatio(ink, glassBackground({ ...tokens, mesh: stop }, opacity))));
}

// ---- DOM ----
const SVG = 'http://www.w3.org/2000/svg';
const ICONS = {
  dialogues: ['M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z'],
  editor: ['m3 17 2 2 4-4', 'm3 7 2 2 4-4', 'M13 6h8', 'M13 12h8', 'M13 18h8'],
  review: ['M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z', 'm9 12 2 2 4-4'],
  board: ['M3 3v16a2 2 0 0 0 2 2h16', 'M18 17V9', 'M13 17V5', 'M8 17v-3'],
  quality: ['m9 11 3 3L22 4', 'M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11'],
  layers: ['m12 3 10 6-10 6L2 9l10-6', 'm2 15 10 6 10-6', 'm2 12 10 6 10-6'],
  view: ['M4 21v-7', 'M4 10V3', 'M12 21v-9', 'M12 8V3', 'M20 21v-5', 'M20 12V3', 'M1 14h6', 'M9 8h6', 'M17 16h6'],
  projector: ['M3 4h18v12H3z', 'M12 16v5', 'M8 21h8'],
  server: ['M3 4h18v6H3z', 'M3 14h18v6H3z', 'M7 7h.01', 'M7 17h.01'],
  home: ['m10 17 5-5-5-5'],
  close: ['M18 6 6 18', 'm6 6 12 12'],
};
function icon(name) {
  const svg = document.createElementNS(SVG, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24'); svg.setAttribute('fill', 'none'); svg.setAttribute('stroke', 'currentColor');
  svg.setAttribute('stroke-width', '1.8'); svg.setAttribute('stroke-linecap', 'round'); svg.setAttribute('stroke-linejoin', 'round');
  svg.setAttribute('aria-hidden', 'true'); svg.setAttribute('focusable', 'false'); svg.classList.add('gl-icon');
  for (const d of ICONS[name]) { const p = document.createElementNS(SVG, 'path'); p.setAttribute('d', d); svg.append(p); }
  return svg;
}
function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) { if (value === false || value == null) continue; if (key === 'class') node.className = value; else node.setAttribute(key, value === true ? '' : value); }
  node.append(...children);
  return node;
}

const SLIDERS = [
  { key: 'blur', label: 'Размытие', min: 0, max: 40, step: 1, format: v => `${Math.round(v)} px` },
  { key: 'opacity', label: 'Плотность', min: MIN_OPACITY, max: MAX_OPACITY, step: 0.01, format: v => `${Math.round(v * 100)}%` },
  { key: 'saturation', label: 'Насыщенность', min: 0.5, max: 2, step: 0.05, format: v => `${Math.round(v * 100)}%` },
  { key: 'edge', label: 'Кромка', min: 0, max: 1, step: 0.05, format: v => `${Math.round(v * 100)}%` },
];

export function mount(root, { bus, storage } = {}) {
  if (!root || root.dataset.glassMounted) return null;
  root.dataset.glassMounted = '1';
  root.classList.add('gl-theme');
  let settings = loadSettings(storage);
  const apply = () => {
    const vars = cssVars(settings, root.classList.contains('projector'));
    for (const [name, value] of Object.entries(vars)) root.style.setProperty(name, value);
    syncControls();
  };

  // Tab icons (labels stay as the accessible name).
  root.querySelectorAll('[role="tab"][data-tab]').forEach(tab => {
    if (!tab.querySelector('.gl-icon') && ICONS[tab.dataset.tab]) {
      const label = el('span', { class: 'gl-tab-label' }, tab.textContent.trim());
      tab.replaceChildren(icon(tab.dataset.tab), label);
    }
  });

  // Appearance popover.
  const popover = el('div', { class: 'gl-popover', id: 'gl-popover', role: 'dialog', 'aria-label': 'Вид интерфейса', hidden: true });
  const title = el('h2', { class: 'gl-popover-title' }, 'Вид интерфейса');
  const closeButton = el('button', { type: 'button', class: 'gl-icon-button', 'aria-label': 'Закрыть настройки вида' }, icon('close'));
  const presetsBox = el('div', { class: 'gl-presets', role: 'group', 'aria-label': 'Пресеты стекла' });
  const presetButtons = Object.entries(PRESETS).map(([name, preset]) => {
    const button = el('button', { type: 'button', class: 'gl-preset', 'data-preset': name, 'aria-pressed': 'false' }, preset.label);
    button.addEventListener('click', () => { settings = clampSettings(preset); commit(); });
    return button;
  });
  presetsBox.append(...presetButtons);
  const sliderRows = SLIDERS.map(def => {
    const input = el('input', { type: 'range', min: def.min, max: def.max, step: def.step, 'data-key': def.key, 'aria-label': def.label });
    const output = el('output', { 'aria-hidden': 'true' });
    input.addEventListener('input', () => { settings = clampSettings({ ...settings, [def.key]: input.value }); commit(); });
    return { def, input, output, row: el('label', { class: 'gl-slider' }, el('span', { class: 'gl-slider-head' }, el('span', {}, def.label), output), input) };
  });
  const intro = el('p', { class: 'gl-popover-intro' }, 'Настройте свет и глубину рабочего пространства.');
  const sample = el('div', { class: 'gl-material-sample', 'aria-hidden': 'true' }, icon('layers'), el('span', {}, 'Ваше стекло'));
  const note = el('p', { class: 'gl-popover-note' }, 'В режиме проектора фон становится плотнее. Выводы и доказательства всегда остаются чёткими.');
  const reset = el('button', { type: 'button', class: 'gl-reset' }, 'Сбросить вид');
  reset.addEventListener('click', () => { settings = clampSettings(PRESETS[DEFAULT_PRESET]); commit(); });
  popover.append(el('div', { class: 'gl-popover-head' }, title, closeButton), intro, sample, presetsBox, ...sliderRows.map(r => r.row), note, reset);
  root.append(popover);

  function syncControls() {
    const active = presetName(settings);
    presetButtons.forEach(button => button.setAttribute('aria-pressed', String(button.dataset.preset === active)));
    for (const { def, input, output } of sliderRows) { input.value = settings[def.key]; output.textContent = def.format(settings[def.key]); }
  }
  function commit() { saveSettings(settings, storage); apply(); }

  const triggers = [];
  function makeTrigger(className, withLabel) {
    const button = el('button', { type: 'button', class: className, 'aria-haspopup': 'dialog', 'aria-expanded': 'false', 'aria-controls': 'gl-popover', title: 'Вид: стекло' }, icon('view'));
    if (withLabel) button.append(el('span', {}, 'Вид'));
    else button.setAttribute('aria-label', 'Вид: стекло');
    button.addEventListener('click', () => toggle(button));
    triggers.push(button);
    return button;
  }
  let lastTrigger = null;
  function setOpen(open, trigger) {
    popover.hidden = !open;
    root.classList.toggle('gl-popover-open', open);
    triggers.forEach(t => t.setAttribute('aria-expanded', String(open && t === (trigger || lastTrigger))));
    if (open) { lastTrigger = trigger || lastTrigger; closeButton.focus({ preventScroll: true }); }
  }
  function toggle(trigger) { setOpen(popover.hidden, trigger); }
  closeButton.addEventListener('click', () => { setOpen(false); lastTrigger?.focus(); });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && !popover.hidden) { event.stopPropagation(); setOpen(false); lastTrigger?.focus(); }
  }, true);
  document.addEventListener('pointerdown', event => {
    if (popover.hidden) return;
    if (popover.contains(event.target) || triggers.some(t => t.contains(event.target))) return;
    setOpen(false);
  });

  const controls = root.querySelector('.cqa-header-controls');
  if (controls) controls.insertBefore(makeTrigger('cqa-button gl-view-toggle', true), controls.querySelector('#cqa-projector-toggle'));

  // Five real sections, mirroring the floating dock without creating another tablist.
  // Proxy the original buttons so keyboard routing and module lifecycle stay in core.
  const rail = el('nav', { class: 'gl-rail', 'aria-label': 'Быстрые разделы' });
  const sectionButtons = [...root.querySelectorAll('.cqa-tabs [role="tab"][data-tab]')];
  const railButtons = sectionButtons.map((tab, index) => {
    const label = tab.textContent.trim();
    const button = el('button', { type: 'button', class: 'gl-rail-button', 'aria-label': label, title: label, 'data-gl-nav': tab.dataset.tab }, icon(tab.dataset.tab));
    button.append(el('span', { class: 'gl-rail-tooltip', 'aria-hidden': 'true' }, label));
    button.addEventListener('click', () => tab.click());
    button.addEventListener('keydown', event => {
      if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      const next = event.key === 'Home' ? 0 : event.key === 'End' ? sectionButtons.length - 1 : (index + (event.key === 'ArrowDown' ? 1 : -1) + sectionButtons.length) % sectionButtons.length;
      railButtons[next].focus(); sectionButtons[next].click();
    });
    return button;
  });
  rail.append(...railButtons, el('span', { class: 'gl-rail-divider', 'aria-hidden': 'true' }), makeTrigger('gl-rail-button', false));
  root.append(rail);
  const syncSection = tab => {
    railButtons.forEach(button => {
      const active = button.dataset.glNav === tab;
      if (active) button.setAttribute('aria-current', 'page'); else button.removeAttribute('aria-current');
    });
  };
  syncSection(sectionButtons.find(tab => tab.getAttribute('aria-selected') === 'true')?.dataset.tab);
  bus?.on?.('tab:changed', syncSection);

  const syncProjector = () => { apply(); };
  if (typeof MutationObserver !== 'undefined') new MutationObserver(syncProjector).observe(root, { attributes: true, attributeFilter: ['class'] });
  bus?.on?.('projector:changed', syncProjector);
  syncProjector();
  return { get settings() { return { ...settings }; }, apply, setOpen };
}
