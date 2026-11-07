import json
from pathlib import Path

import pytest

from cqa.models import ChecklistVersion
from scripts.annotation_agreement import compare, load_labels, main, statistics


@pytest.fixture
def checklist():
    return ChecklistVersion.model_validate_json((Path(__file__).resolve().parents[1] / 'data/checklists/v1.json').read_text())


def write(path, rows):
    path.write_text('\n'.join(json.dumps(r) for r in rows), encoding='utf-8')
    return path


def row(**changes):
    return {'conv_id': 'x', 'criterion_id': 'C1', 'answer': 'yes', 'annotator': 'Alice', 'checklist_version': 1, **changes}


def test_kappa_known_examples_and_undefined_cases():
    assert statistics([('yes', 'yes'), ('yes', 'no'), ('no', 'yes'), ('no', 'no')]) == {
        'compared': 4, 'agreed': 2, 'agreement_pct': 50, 'cohen_kappa': 0}
    assert statistics([('yes', 'no'), ('no', 'yes')])['cohen_kappa'] == -1
    assert statistics([('yes', 'yes'), ('no', 'no')])['cohen_kappa'] == 1
    assert statistics([('na', 'na')])['cohen_kappa'] is None
    assert statistics([])['agreement_pct'] is None


def test_coverage_does_not_hide_missing_cells(tmp_path, checklist):
    left = load_labels(write(tmp_path/'a', [row(), row(conv_id='missing')]), checklist)
    right = load_labels(write(tmp_path/'b', [row(annotator='Bob', answer='no')]), checklist)
    report = compare(left, right, checklist)
    assert report['coverage']['shared_pct_of_union'] == 50
    assert report['only_a'] == [{'conv_id': 'missing', 'criterion_id': 'C1'}]
    assert report['per_criterion']['C2']['compared'] == 0
    assert report['disagreements'][0]['answer_b'] == 'no'


@pytest.mark.parametrize('change', [
    {'criterion_id': 'unknown'}, {'annotator': ''}, {'answer': None}, {'answer': 'na'},
    {'answer': 0}, {'checklist_version': 2}, {'checklist_version': True},
    {'criterion_hash': 'stale'}, {'answer': float('nan')}, {'conv_id': ''},
])
def test_invalid_rows_rejected(tmp_path, checklist, change):
    with pytest.raises(ValueError):
        load_labels(write(tmp_path/'a', [row(**change)]), checklist)


def test_duplicates_and_mixed_annotators_rejected(tmp_path, checklist):
    for rows in ([row(), row()], [row(), row(conv_id='z', annotator='Bob')]):
        with pytest.raises(ValueError):
            load_labels(write(tmp_path/'a', rows), checklist)


def test_scale_zero_and_na_are_distinct(tmp_path, checklist):
    labels = load_labels(write(tmp_path/'a', [row(criterion_id='C4', answer=0), row(criterion_id='C2', answer='N/A')]), checklist)
    assert labels[('x','C4')]['answer'] == 0
    assert labels[('x','C2')]['answer'] == 'na'
    with pytest.raises(ValueError, match='different annotators'):
        compare(labels, labels, checklist)


def test_cli_writes_aggregate_not_transcripts(tmp_path):
    a = write(tmp_path/'a', [row()])
    b = write(tmp_path/'b', [row(annotator='Bob')])
    out = tmp_path/'report.json'
    assert main([str(a), str(b), '--out', str(out)]) == 0
    report = json.loads(out.read_text())
    assert report['overall']['agreement_pct'] == 100
    assert report['overall']['cohen_kappa'] is None
    assert report['kind'] == 'inter_annotator_agreement'


@pytest.mark.parametrize('field', ['transcript_hash', 'checklist_hash'])
def test_same_id_with_changed_annotation_inputs_is_not_compared(tmp_path, checklist, field):
    left = load_labels(write(tmp_path/'a', [row(**{field: 'old'})]), checklist)
    right = load_labels(write(tmp_path/'b', [row(annotator='Bob', **{field: 'changed'})]), checklist)
    with pytest.raises(ValueError, match=field):
        compare(left, right, checklist)
