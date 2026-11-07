import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

const source = await readFile(new URL('../web/js/list.js', import.meta.url), 'utf8');
const {cellDescription, evaluationState, formatScore} = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
const checklist = JSON.parse(await readFile(new URL('../data/checklists/v1.json', import.meta.url), 'utf8'));
const criterion = id => checklist.criteria.find(item => item.id === id);

test('queue preserves official score precision and a zero total', () => {
  assert.equal(formatScore(85.38, checklist.rounding.total), '85,38');
  assert.equal(formatScore(99.99, checklist.rounding.total), '99,99');
  assert.equal(formatScore(80), '80');
  assert.equal(formatScore(0), '0');
  assert.equal(formatScore(null), '—');
});

test('C8 no is neutral and yes is a violation without a critical_hit hint', () => {
  const clear = cellDescription({criterion_id:'C8',answer:'no',critical_hit:true}, 7, criterion('C8'));
  assert.equal(clear.kind, 'clear');
  assert.match(clear.label, /Нарушений нет/);
  assert.doesNotMatch(clear.label, /грубая ошибка/);
  const violation = cellDescription({criterion_id:'C8',answer:'yes',critical_hit:false}, 7, criterion('C8'));
  assert.equal(violation.kind, 'critical');
  assert.match(violation.label, /Нарушение выявлено/);
});

test('scale tones use the scale maximum rather than its scoring weight', () => {
  for (const [id, value, kind, label] of [
    ['C4',20,'yes','20/20'], ['C4',15,'partial','15/20'], ['C4',0,'no','0/20'],
    ['C6',10,'yes','10/10'], ['C6',7,'partial','7/10'], ['C6',0,'no','0/10'],
  ]) {
    const cell = cellDescription({criterion_id:id,answer:value}, 0, criterion(id));
    assert.equal(cell.kind, kind); assert.ok(cell.label.includes(label));
  }
});

test('no applicable, not evaluated and an actual zero remain distinct', () => {
  assert.equal(evaluationState({total:null,status:'no_applicable'}), 'no_applicable');
  assert.equal(evaluationState({total:null}), 'pending');
  assert.equal(evaluationState({total:0}), 'approved');
  assert.equal(evaluationState({total:80,appealed:true}), 'appealed');
});

test('critical trigger follows a replacement criterion instead of a hardcoded id', () => {
  const replacement = {id:'X9',critical:true,critical_when:'no'};
  assert.equal(cellDescription({criterion_id:'X9',answer:'no'},0,replacement).kind, 'critical');
  assert.equal(cellDescription({criterion_id:'X9',answer:'yes'},0,replacement).kind, 'clear');
});
