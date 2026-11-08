import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

// «Речь оператора»: the card's informational line for calls (module init does not touch the DOM).
const providerSource = await readFile(new URL('../web/js/provider-state.js', import.meta.url), 'utf8');
const providerUrl = `data:text/javascript;base64,${Buffer.from(providerSource).toString('base64')}`;
const source = (await readFile(new URL('../web/js/card.js', import.meta.url), 'utf8'))
  .replace("'./provider-state.js'", JSON.stringify(providerUrl));
const { speechLine } = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);

test('call metrics render as one quiet line with the not-in-score note', () => {
  const line = speechLine({ tempo_wpm: 124, pauses_over_3s: 4, pause_threshold_sec: 3, longest_pause_sec: 4.6, silence_share: null,
    note: 'по транскрипту (запись ещё не обработана), на балл не влияет' });
  assert.equal(line.text, 'Темп: 124 слов/мин · Паузы > 3 с: 4 · Самая длинная: 4,6 с');
  assert.match(line.note, /на балл не влияет/);
});

test('audio silence share is appended; missing tempo shows a dash; chats show nothing', () => {
  const line = speechLine({ tempo_wpm: null, pauses_over_3s: 0, longest_pause_sec: null, silence_share: 0.192 });
  assert.equal(line.text, 'Темп: — · Паузы > 3 с: 0 · Самая длинная: — · Тишина в записи: 19%');
  assert.equal(line.note, 'по записи и транскрипту, на балл не влияет');
  assert.equal(speechLine(null), null);
  assert.equal(speechLine(undefined), null);
});
