import assert from 'node:assert/strict';
import test from 'node:test';
import { readFile } from 'node:fs/promises';
const source = await readFile(new URL('../web/js/shell-state.js', import.meta.url), 'utf8');
const { keyboardAction, modeCopy, runFailureNotice } = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);

test('physical keyboard shortcuts work with Russian layout and missing key values', () => {
  assert.deepEqual(keyboardAction({code:'KeyP',key:'з'}),{type:'projector'});
  assert.deepEqual(keyboardAction({code:'Digit4',key:'4'}),{type:'tab',tab:'board'});
  assert.deepEqual(keyboardAction({code:'Digit5',key:'5'}),{type:'tab',tab:'quality'});
  assert.equal(keyboardAction({}),null);
  assert.deepEqual(keyboardAction({key:'P'}),{type:'projector'});
});
test('last run warning distinguishes incomplete answers from review and healthy HTTP status', () => {
  assert.equal(runFailureNotice({ status: 'completed', per_conv: [{ status: 'needs_review' }] }), null);
  assert.equal(runFailureNotice({ status: 'running', per_conv: [] }), null);
  assert.match(runFailureNotice({ status: 'completed', per_conv: [{ status: 'incomplete' }, { status: 'error' }] }).description, /2/);
  assert.ok(runFailureNotice({ status: 'failed', errors: 1 }));
  assert.ok(runFailureNotice({ status: 'completed', errors: [{ conv_id: 'synthetic', error: 'provider failed' }] }));
  assert.equal(runFailureNotice(null), null);
});
test('browser modifiers and auto-repeat never trigger projector or navigation', () => {
  for (const field of ['ctrlKey','metaKey','altKey','repeat','defaultPrevented']) assert.equal(keyboardAction({code:'KeyP',[field]:true}),null);
});
test('configured demo conversations preserve review and checklist shortcuts', () => {
  const demos=['demo-ru-001','demo-kz-001'];
  assert.deepEqual(keyboardAction({code:'Digit2'},demos),{type:'conversation',id:demos[1]});
  assert.deepEqual(keyboardAction({code:'Digit4'},demos),{type:'tab',tab:'editor'});
  assert.deepEqual(keyboardAction({code:'Digit1'},['missing-second']),{type:'tab',tab:'dialogues'});
});
test('failed live connection and mock server cannot look like successful real evaluation', () => {
  assert.equal(modeCopy({mock:false},null,true).mode,'unavailable');
  assert.equal(modeCopy({mock:false},{provider:'mock',ok:true}).mode,'mock');
  assert.equal(modeCopy({mock:false},null).mode,'connecting');
  assert.match(modeCopy({mock:true},null).description,/Оценки и стоимость условные/);
  assert.match(modeCopy({mock:false},{ok:true,runs:false}).description,/недоступен/);
});
