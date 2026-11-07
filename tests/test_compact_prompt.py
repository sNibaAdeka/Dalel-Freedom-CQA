"""Synthetic prompt contracts; no model, reference labels or DEV data."""
import hashlib
import json

import pytest

from cqa import evaluator as E, prompts as P


@pytest.fixture(autouse=True)
def official_guidance(monkeypatch):
    monkeypatch.delenv('CQA_READINGS', raising=False)
    monkeypatch.setenv('CQA_GUIDANCE_FILE', str(P.GUIDANCE_FILE))
    P._overlay_cache.clear()


def test_legacy_prompt_is_byte_for_byte_unchanged():
    conv = {'id': 'legacy-fixture', 'channel': 'chat', 'language': 'RU', 'messages': [
        {'id': 'legacy-fixture:0', 'idx': 0, 'speaker': 'operator', 'text': 'Здравствуйте!', 'ts_raw': '00:00'}]}
    crit = {'id': 'custom-check', 'title': 'Greeting', 'description': 'Operator greets the client.',
            'answer_type': 'binary', 'na_allowed': False, 'guidance': ['Greeting must be literal.']}
    prompt = P.build_batch(conv, [crit], ['Synthetic structure fact.'])
    assert hashlib.sha256(json.dumps(prompt, ensure_ascii=False).encode()).hexdigest() == (
        '3bf9427762989f284eb858475b6fe958ff4c27f96dd18867e091792d43a61ff4')
    assert prompt == P.build_batch(conv, [crit], ['Synthetic structure fact.'], compact=False)


@pytest.mark.parametrize('channel,language', [('chat', 'RU'), ('chat', 'KZ'), ('chat', 'EN'), ('call', 'RU')])
def test_compact_keeps_entire_transcript_system_checklist_and_guidance(v1_raw, channel, language):
    criteria = E.as_checklist(v1_raw)['criteria']
    conv = {'id': 'synthetic-long', 'channel': channel, 'language': language, 'messages': [
        {'id': f'synthetic-long:{i}', 'idx': i, 'speaker': role, 'ts_raw': f'00:{i:02d}', 't_sec': i,
         'text': f'BEGIN-{i} ' + 'Қазақша. English. Русский. ' * 400 + f'END-{i}'}
        for i, role in enumerate(['client', 'bot', 'system', 'operator', 'client', 'operator'])]}
    facts = ['Synthetic fact to verify in transcript.']
    system, prompt = P.build_batch(conv, criteria, facts, compact=True)
    legacy_system, legacy = P.build_batch(conv, criteria, facts)
    assert system == legacy_system == P.SYSTEM
    assert P.render_transcript(conv) in prompt and P.facts_block(facts) in prompt
    for message in conv['messages']:
        assert message['text'] in prompt
    for criterion in criteria:
        assert P.criterion_block(criterion) in prompt
        for line in P.guidance_for(criterion)[0]:
            assert line in prompt
    # The full input prefix is unchanged; only output instructions differ.
    assert prompt.split(P.COMPACT_OUTPUT_RULES)[0] == legacy.split('Оцени КАЖДЫЙ критерий')[0]
    assert 'analysis=""' in prompt
    assert 'Все требуемые детали сохраняй, даже если их много' in prompt
    assert 'в обоих полях' in prompt
    assert f'1–{P.COMPACT_QUOTE_MAX_CHARS}' in prompt
    assert '2–6 коротких предложений' not in prompt


def test_compact_respects_explicit_readings_and_replacement_criteria(v1_raw):
    criteria = E.as_checklist(v1_raw)['criteria']
    c7 = next(c for c in criteria if c['id'] == 'C7')
    conv = {'id': 'synthetic-empty', 'channel': 'chat', 'language': 'RU', 'messages': []}
    _, prompt = P.build_batch(conv, [c7], readings={'C7': 'lenient'}, compact=True)
    assert P.criterion_block(c7, {'C7': 'lenient'}) in prompt
    replaced = {**c7, 'description': 'Да — оператор назвал учебный раздел.',
                'hash': 'synthetic-replacement', 'guidance': ['Только учебный раздел.']}
    _, prompt = P.build_batch(conv, [replaced], compact=True)
    assert 'Только учебный раздел.' in prompt
    assert 'Историческая экспериментальная трактовка' not in prompt
    assert '(итог ИЛИ предложение дальнейшей помощи) И прощание' not in prompt


def test_empty_analysis_with_concrete_reason_and_literal_evidence_passes_validator():
    conv = {'id': 'synthetic', 'messages': [
        {'id': 'synthetic:0', 'idx': 0, 'speaker': 'operator', 'text': 'Здравствуйте!'}]}
    item = {'criterion_id': 'synthetic-check', 'analysis': '', 'answer': 'yes',
            'evidence': [{'message_id': 'synthetic:0', 'quote': 'Здравствуйте!'}],
            'checked_range': None, 'na_reason': None, 'suggestion': None,
            'explanation': 'Приветствие — Да. Оператор поздоровался [synthetic:0].', 'confidence': .9}
    result = E.validate_item(item, conv, {'id': 'synthetic-check', 'answer_type': 'binary', 'na_allowed': False})
    assert result['answer'] == 'yes' and not result['errors']
    assert result['analysis'] == '' and result['explanation'] == item['explanation']
    assert result['evidence'][0]['quote'] == item['evidence'][0]['quote']
