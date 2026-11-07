import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

// Load the browser ES module without changing the repository's package mode.
// Module initialization does not touch DOM; mount() owns all browser access.
const providerSource = await readFile(new URL('../web/js/provider-state.js', import.meta.url), 'utf8');
const providerUrl = `data:text/javascript;base64,${Buffer.from(providerSource).toString('base64')}`;
const source = (await readFile(new URL('../web/js/card.js', import.meta.url), 'utf8'))
  .replace("'./provider-state.js'", JSON.stringify(providerUrl));
const { verifyEvidence, cascadeState } = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
const messages = [
  { id: 'test:0', speaker: 'operator', text: 'Мы не гарантируем доходность.' },
  { id: 'test:1', speaker: 'client', text: 'Здравствуйте!' },
  { id: 'test:2', speaker: 'operator', text: '🙂 Точный ответ.' },
  { id: 'test:3', speaker: 'operator', text: 'Да. Да.' },
];

test('an exact operator quote with correct boundaries is verified', () => {
  const quote = messages[0].text;
  const result = verifyEvidence({ message_id: 'test:0', quote, start: 0, end: quote.length }, messages);
  assert.equal(result.valid, true);
  assert.equal(result.message.id, 'test:0');
});

test('removing a negation cannot produce a verified quote, even with valid=true', () => {
  assert.equal(verifyEvidence({ message_id: 'test:0', quote: 'Мы гарантируем доходность.', start: 0, end: messages[0].text.length, valid: true }, messages).valid, false);
});

test('empty quotes are not evidence', () => {
  assert.equal(verifyEvidence({ message_id: 'test:0', quote: '', start: 0, end: 0 }, messages).valid, false);
});

test('whitespace-only quotes are not evidence', () => {
  assert.equal(verifyEvidence({ message_id: 'test:0', quote: ' ', start: 2, end: 3 }, messages).valid, false);
});

test('an exact client quote is source context, not an operator action', () => {
  const proof = verifyEvidence({ message_id: 'test:1', quote: messages[1].text, start: 0, end: messages[1].text.length }, messages);
  assert.equal(proof.valid, true); assert.equal(proof.context, true); assert.equal(proof.message.speaker, 'client');
});

test('system and bot quotes can be verified as context without changing their role', () => {
  for (const speaker of ['system', 'bot']) {
    const message = {id: speaker, speaker, text: 'Synthetic context event.'};
    const proof = verifyEvidence({message_id: speaker, quote: message.text}, [message]);
    assert.equal(proof.valid, true); assert.equal(proof.context, true); assert.equal(proof.message.speaker, speaker);
  }
});

test('unknown source roles are rejected', () => {
  assert.equal(verifyEvidence({message_id:'x',quote:'text'},[{id:'x',speaker:'unknown',text:'text'}]).valid,false);
});

test('evidence offsets use JavaScript UTF-16 positions after surrogate pairs', () => {
  const result = verifyEvidence({ message_id: 'test:2', quote: 'Точный ответ.', start: 3, end: 16 }, messages);
  assert.equal(result.valid, true);
  assert.equal(result.message.text.slice(result.start, result.end), result.quote);
});

test('code-point offsets that slice the wrong text are rejected', () => {
  assert.equal(verifyEvidence({ message_id: 'test:2', quote: 'Точный ответ.', start: 2, end: 15 }, messages).valid, false);
});

test('missing offsets can be recovered only from a unique exact occurrence', () => {
  const result = verifyEvidence({ message_id: 'test:2', quote: 'Точный ответ.' }, messages);
  assert.equal(result.valid, true);
  assert.equal(result.start, 3);
  assert.equal(result.end, 16);
});

test('ambiguous repeated quotes require explicit offsets', () => {
  assert.equal(verifyEvidence({ message_id: 'test:3', quote: 'Да.' }, messages).valid, false);
  assert.equal(verifyEvidence({ message_id: 'test:3', quote: 'Да.', start: 4, end: 7 }, messages).valid, true);
});

test('nonexistent message IDs cannot be verified', () => {
  assert.equal(verifyEvidence({ message_id: 'unknown', quote: messages[0].text }, messages).valid, false);
});

test('out-of-bounds or partial boundaries are rejected rather than repaired', () => {
  for (const bounds of [{ start: -1, end: 2 }, { start: 0, end: 100 }, { start: 0, end: null }, { start: 0.5, end: 2 }]) {
    assert.equal(verifyEvidence({ message_id: 'test:0', quote: 'Мы', ...bounds }, messages).valid, false);
  }
});

test('non-string quotes cannot be coerced into valid evidence', () => {
  assert.equal(verifyEvidence({ message_id: 'test:0', quote: { toString: () => messages[0].text }, start: 0, end: messages[0].text.length }, messages).valid, false);
});


test('cascade trace distinguishes a strong recheck from a sufficient first pass', () => {
  const trace = { escalated: true, flagged: ['C4', 'C6'], first_answers: { C1: 'no', C4: 10 } };
  const result = cascadeState({ criterion_id: 'C1', raw: { cascade: trace } });
  assert.equal(result.label, 'Перепроверено сильной моделью');
  assert.match(result.detail, /Первая модель: Нет/);
  assert.match(result.detail, /C4, C6/);
  const fast = cascadeState({ criterion_id: 'C4', raw: { cascade: { ...trace, escalated: false, flagged: [] } } });
  assert.equal(fast.label, 'Хватило быстрой модели');
  assert.match(fast.detail, /Первая модель: 10/);
  assert.match(fast.detail, /не отмечены на проверку/);
});

test('cascade badges do not infer success from absent or malformed traces', () => {
  for (const value of [null, {}, { raw: {} }, { raw: { cascade: { escalated: 'true' } } }]) assert.equal(cascadeState(value), null);
  const fallback = cascadeState({ criterion_id: 'C2', model: 'a → b (ответ первой модели: вторая не ответила)', raw: { cascade: { escalated: true, first_answers: { C2: 'na' } } } });
  assert.match(fallback.label, /Сильная модель не ответила/);
  assert.match(fallback.detail, /N\/A/);
});
