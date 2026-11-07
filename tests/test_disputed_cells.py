"""Deterministic triage mechanics and source privacy; all inputs are synthetic."""
import json

import pytest

from scripts.disputed_cells import make_queue, rank_cells, read_cells, render_report


def criterion(cid='C1', weight=10, critical=False):
    return {'id': cid, 'hash': 'a' * 16, 'answer_type': 'binary', 'na_allowed': False,
            'weight': weight, 'critical': critical}


def run(name, cells, family=None):
    return {'name': name, 'family': family or name, 'cells': cells}


def test_missing_unknown_invalid_are_not_semantic_disagreements(tmp_path):
    path = tmp_path / 'run.jsonl'
    path.write_text(json.dumps({'conv_id': 'd', 'evals': [
        {'criterion_id': 'C1', 'answer': 'yes', 'explanation': 'PRIVATE'},
        {'criterion_id': 'C2', 'answer': None}, {'criterion_id': 'C3', 'answer': 'unknown'},
        {'criterion_id': 'C4', 'answer': True}]}) + '\n')
    assert read_cells(path) == {('d', 'C1'): 'yes'}
    assert rank_cells([run('a', read_cells(path)), run('b', {})], {}, [criterion()], {'d'}) == []
    assert rank_cells([run('a', {('d', 'C1'): 'yes'}), run('b', {('d', 'C1'): 'na'})], {}, [criterion()], {'d'}) == []


def test_conflicting_duplicates_fail_instead_of_selecting_last(tmp_path):
    path = tmp_path / 'gold.jsonl'
    path.write_text('\n'.join(json.dumps({'conv_id': 'd', 'criterion_id': 'C1', 'answer': a}) for a in ['yes', 'no']))
    with pytest.raises(ValueError, match='conflicting duplicate'):
        read_cells(path)


def test_errored_model_predictions_are_missing_even_with_valid_answers(tmp_path):
    path = tmp_path / 'run.jsonl'
    path.write_text(json.dumps({'conv_id': 'd', 'evals': [
        {'criterion_id': 'C1', 'answer': 'yes', 'errors': ['invalid_evidence']},
        {'criterion_id': 'C2', 'answer': 'no', 'errors': []},
        {'criterion_id': 'C3', 'answer': 'yes'}]}) + '\n')
    assert read_cells(path) == {('d', 'C2'): 'no', ('d', 'C3'): 'yes'}
    assert rank_cells([run('errored', read_cells(path)), run('other', {('d', 'C1'): 'no'})],
                      {}, [criterion()], {'d'}) == []


def test_repeats_of_a_family_do_not_multiply_votes():
    a, b = {('d', 'C1'): 'yes'}, {('d', 'C1'): 'no'}
    base = rank_cells([run('a', a, 'Haiku'), run('b', b, 'Other')], {}, [criterion()], {'d'})[0]
    repeated = rank_cells([run('a', a, 'Haiku'), run('a2', a, 'Haiku'), run('b', b, 'Other')], {}, [criterion()], {'d'})[0]
    assert base['gini'] == repeated['gini'] == .5
    assert base['priority'] == repeated['priority'] == 5
    assert repeated['family_count'] == 2 and repeated['run_count'] == 3


def test_zero_weight_critical_flag_keeps_a_priority_and_correct_reason():
    criteria = [criterion('C1', 20), criterion('C8', 0, True)]
    runs = [run('a', {('d', 'C8'): 'yes'}), run('b', {('d', 'C8'): 'no'})]
    cell = rank_cells(runs, {}, criteria, {'d'})[0]
    assert cell['official_weight'] == 0
    assert cell['effective_weight'] == 20
    assert cell['priority'] == 10
    assert any('критический' in r for r in cell['why'])


def test_rare_critical_dispute_has_explicit_safety_first_tier():
    criteria = [criterion('C1', 20), criterion('C8', 0, True)]
    a = {(f'd{i}', cid): 'yes' for i in range(20) for cid in ('C1', 'C8')}
    b = {key: ('no' if key[1] == 'C1' or key[0] == 'd0' else 'yes') for key in a}
    rows = rank_cells([run('a', a), run('b', b)], {}, criteria, {f'd{i}' for i in range(20)})
    assert rows[0]['criterion_id'] == 'C8'
    assert rows[0]['priority'] < rows[1]['priority'], 'critical tier is explicit, not a fake inflated score'


def test_unanimous_models_against_reference_are_kept_but_reference_only_is_not():
    refs = {'silver': {('d', 'C1'): 'no', ('reference-only', 'C1'): 'yes'}}
    cells = rank_cells([run('a', {('d', 'C1'): 'yes'})], refs, [criterion()], {'d', 'reference-only'})
    assert len(cells) == 1
    assert cells[0]['gini'] == 0 and cells[0]['spread'] == 1
    assert cells[0]['priority'] == 10


def test_frequency_weights_repeated_disputes_and_excludes_non_dev_ids():
    a = {('d1', 'C1'): 'yes', ('d2', 'C1'): 'yes', ('outsider', 'C1'): 'no'}
    b = {('d1', 'C1'): 'no', ('d2', 'C1'): 'yes', ('outsider', 'C1'): 'yes'}
    cell = rank_cells([run('a', a), run('b', b)], {}, [criterion()], {'d1', 'd2'})[0]
    assert cell['frequency'] == .5
    assert cell['priority'] == 2.5
    assert cell['criterion_disputed'] == 1 and cell['criterion_observed'] == 2


def test_determinism_blind_queue_and_report_have_no_transcript_fields():
    criteria = [criterion()]
    runs = [run('a', {('d2', 'C1'): 'yes', ('d1', 'C1'): 'yes'}),
            run('b', {('d1', 'C1'): 'no', ('d2', 'C1'): 'no'})]
    first = rank_cells(runs, {}, criteria, {'d1', 'd2'})
    second = rank_cells(runs, {}, criteria, {'d2', 'd1'})
    checklist = {'checklist_id': 'c', 'version': 1}
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    assert first[0]['conv_id'] == 'd1'
    queue = make_queue(first, checklist, 1)
    assert queue == {'schema_version': 1, 'checklist_id': 'c', 'checklist_version': 1,
                     'cells': [{'conv_id': 'd1', 'criterion_id': 'C1', 'criterion_hash': 'a' * 16}]}
    assert not any(word in json.dumps(queue) for word in ['answer', 'model', 'reference', 'explanation', 'quote'])
    assert render_report(first, checklist, [], 2) == render_report(second, checklist, [], 2)
