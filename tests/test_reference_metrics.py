import json
from pathlib import Path

import pytest

from scripts.eval_accuracy import total, reference_variants, measure, load_gold, complete_mean


def test_reference_is_not_silently_rewritten():
    v=reference_variants()
    assert all(not x['exclude'] and not x['overrides'] for x in v.values())
    assert reference_variants(True)['b']['exclude']==['C7']


def test_reference_score_uses_official_rounding_and_requires_all_weighted_answers():
    v1=json.loads((Path(__file__).resolve().parents[1]/'data/checklists/v1.json').read_text())
    answers={'C1':'yes','C2':'na','C3':'na','C4':15,'C5':'yes','C6':7,'C7':'yes','C8':'no'}
    assert total(answers,v1['criteria'],set())==85.38
    answers['C1'] = 'no'
    assert total(answers,v1['criteria'],set()) == 70.0
    answers.pop('C4')
    assert total(answers,v1['criteria'],set()) is None


def test_missing_prediction_is_reported_separately_and_has_no_score():
    # a2 semantics (eval-llm 3107501): a failed model call (limit/network) is not an answer -- it is excluded from
    # agreement, listed in `missing` and reported; the dialogue gets no total, so MAE is never computed from a guess.
    criteria = [{'id': 'C1', 'weight': 10}, {'id': 'C2', 'weight': 15}]
    rows = [{'conv_id':'x', 'language':'RU', 'channel':'chat', 'evals':[{'criterion_id':'C1','answer':'yes'}]}]
    result = measure(rows, lambda conv_id, criterion_id: 'yes', criteria, set())
    assert result['n'] == 1
    assert result['acc'] == 1.0
    assert result['missing'] == [('x', 'C2')]
    assert result['auto'] == [1, 1] and result['flagged'] == [0, 0]
    assert result['maes'] == []
    assert measure([], lambda *args: None, criteria, set())['acc'] is None


def test_unadjudicated_duplicate_reference_is_rejected(tmp_path):
    path = tmp_path / 'reference.jsonl'
    row = {'conv_id':'x','criterion_id':'C1','answer':'yes'}
    path.write_text(json.dumps(row) + '\n' + json.dumps({**row, 'answer':'no'}))
    with pytest.raises(ValueError, match='duplicate reference cell'):
        load_gold(str(path))


def test_unknown_cost_or_time_is_not_fabricated_as_zero():
    assert complete_mean([{}, {'cost': 2}], 'cost') is None
    assert complete_mean([], 'cost') is None
    assert complete_mean([{'cost': 0}, {'cost': 2}], 'cost') == 1
