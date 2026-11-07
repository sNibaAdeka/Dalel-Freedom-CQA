import test from 'node:test';
import assert from 'node:assert/strict';
import { cascadeState } from '../web/js/card.js';

test('direct call routing explains that no cheap first pass happened', () => {
  const state = cascadeState({ raw: { routing: { channel: 'call', model: 'provider:strong', rule: 'route_by_channel.call' } } });
  assert.equal(state.routed, true);
  assert.equal(state.escalated, false);
  assert.match(state.label, /Звонок сразу/);
  assert.match(state.detail, /не выполнялся/);
  assert.equal(cascadeState({ raw: { routing: { channel: 'chat' } } }), null);
});

test('a C3 routing policy remains visible even without a doubt flag', () => {
  const state = cascadeState({ criterion_id: 'C3', raw: { cascade: {
    escalated: true, flagged: [], policy: ['C3=no'], first_answers: { C3: 'no' },
  } } });
  assert.equal(state.escalated, true);
  assert.match(state.detail, /правило маршрутизации C3=no/);
  assert.doesNotMatch(state.detail, /не указана/);
});
