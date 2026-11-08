import assert from 'node:assert/strict';
import test from 'node:test';
import { readFile } from 'node:fs/promises';

const source = await readFile(new URL('../web/js/editor.js', import.meta.url), 'utf8');
const editor = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);

const checklist = scale => ({ criteria: [
  { id: 'C1', title: 'Приветствие', description: 'Да — поздоровался.', answer_type: 'binary', weight: 50 },
  { id: 'N1', title: 'Вежливость', description: 'Число от 0 до 10.', answer_type: 'numeric', scale, weight: 50 },
] });

test('«Число» is offered in the editor and a valid range saves', () => {
  assert.ok(editor.ANSWER_TYPES.includes('numeric'));
  assert.equal(editor.TYPE_LABELS.numeric, 'Число');
  assert.ok(!source.includes("type === 'numeric' && ui.answer_type !== 'numeric') continue"), 'the type button must not be hidden');
  const result = editor.validate(checklist([0, 10]));
  assert.equal(result.ok, true, JSON.stringify(result.errors));
  assert.ok(!result.issues.some(issue => issue.field === 'answer_type'));
});

test('numeric range is still validated: min < max and max > 0', () => {
  for (const scale of [[10], [10, 10], [10, 0], [0, 0], [], [-1, 5]]) {
    const result = editor.validate(checklist(scale));
    assert.equal(result.ok, false, `range ${JSON.stringify(scale)} must be rejected`);
    assert.ok(result.errors.some(issue => issue.field === 'scale'));
  }
});
