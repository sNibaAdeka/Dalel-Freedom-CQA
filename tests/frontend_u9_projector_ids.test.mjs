import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

// U-9 (critic, 8 Nov): on the projector the list's right column was sized by the «◆ Грубая ошибка» status,
// so IDs were cut to «p1-kz…». Browser-checked at 1440×900 and 1280×720: «p1-kz-015» 93/93 px after the fix.
test('U-9: projector list rows give the dialogue ID its natural width', async () => {
  const css = await readFile(new URL('../web/css/list.css', import.meta.url), 'utf8');
  assert.match(css, /\.cqa-root\.projector \.cq-list-row-main \{ grid-template-columns:auto minmax\(0,1fr\); column-gap:8px; \}/);
  assert.match(css, /\.cqa-root\.projector \.cq-list-status \{ max-width:none; \}/);
});

test('Minimal projector: glass scene kept, gross violation as ◆ icon, short mode line', async () => {
  const css = await readFile(new URL('../web/css/list.css', import.meta.url), 'utf8');
  const polish = await readFile(new URL('../web/css/workspace-polish.css', import.meta.url), 'utf8');
  const list = await readFile(new URL('../web/js/list.js', import.meta.url), 'utf8');
  const shell = await readFile(new URL('../web/js/shell-state.js', import.meta.url), 'utf8');
  assert.match(css, /\.cqa-root\.projector \.cq-list-status\.is-critical \.cq-list-status-text \{ position:absolute;/);
  assert.match(list, /element\("span", "cq-list-status-text", statusLabels\[status\]\)/);
  assert.match(polish, /\.cqa-root\.gl-theme\.projector \{ --ws-room-shade: color-mix\(in srgb, var\(--gl-base\) 45%, transparent\); \}/);
  assert.match(shell, /health\.model \? `Модель: \$\{health\.model\}`/);
});
