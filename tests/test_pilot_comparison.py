import copy

import pytest

from scripts.compare_prompt_pilots import compare


def fixture():
    return {
        'evaluation_plan': {'selection': ['synthetic-a'], 'expected_cells': 8, 'reference_sha256': 'frozen'},
        'manifest': {'sources': {'data/raw/dev_records.json': 'same-data', 'data/checklists/v1.json': 'same-rules'}},
        'metrics': {'overall': {'matched': 7, 'cells': 8, 'pct': 87.5}, 'completed_dialogues': 1,
                    'valid_answer_coverage': {'matched': 8}, 'total_score_mae': 10,
                    'evidence': {'raw_quotes': 8, 'raw_exact_quote_pct': 100},
                    'latency': {'p50_s': 50}, 'usage': {'output_tokens': 2000}},
    }


@pytest.mark.parametrize('key,value', [('reference_sha256', 'relabelled'), ('selection', ['easier']), ('expected_cells', 7)])
def test_rejects_changing_reference_sample_or_denominator(key, value):
    old, new = fixture(), fixture()
    new['evaluation_plan'][key] = value
    with pytest.raises(ValueError, match='Cannot compare'):
        compare(old, new)


def test_faster_with_worse_quality_does_not_pass():
    old, new = fixture(), fixture()
    new['metrics']['latency']['p50_s'] = 25
    new['metrics']['overall'] = {'matched': 6, 'cells': 8, 'pct': 75}
    result = compare(old, new)
    assert result['p50_speedup'] == 2
    assert not result['all_development_gates_pass']
    assert not result['development_gates']['agreement_not_lower']


def test_source_changes_and_inexact_quotes_are_not_hidden():
    old, new = fixture(), fixture()
    new['manifest']['sources']['data/raw/dev_records.json'] = 'changed'
    with pytest.raises(ValueError, match='source mismatch'):
        compare(old, new)
    new = copy.deepcopy(old)
    new['metrics']['latency']['p50_s'] = 25
    new['metrics']['evidence']['raw_exact_quote_pct'] = 90
    assert not compare(old, new)['all_development_gates_pass']
