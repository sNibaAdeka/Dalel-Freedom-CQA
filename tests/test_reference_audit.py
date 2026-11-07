import json
from pathlib import Path

import pytest

from cqa.models import ChecklistVersion
from scripts.reference_audit import build_report, validate_review


@pytest.fixture
def checklist():
    return ChecklistVersion.model_validate_json((Path(__file__).resolve().parents[1] / 'data/checklists/v1.json').read_text())


def annotation(checklist):
    return dict(conv_id='fixture', criterion_id='C1', answer='yes',
                checklist_version=checklist.version, criterion_hash=checklist.criteria[0].hash,
                policy_version='g6', annotation_source='ai_policy_review', annotator='reviewer',
                rationale='Greeting and name in first operator turn.',
                evidence=[{'message_id': 'fixture:0', 'quote': 'Hello, I am Sam.'}], checked_range=[],
                uncertain=False, uncertainty_reason=None)


CONV = {'id': 'fixture', 'language': 'EN', 'channel': 'chat',
        'messages': [{'id': 'fixture:0', 'speaker': 'operator', 'text': 'Hello, I am Sam.'}]}


def test_fabricated_quotes_and_changed_criteria_are_rejected(checklist):
    row = annotation(checklist)
    assert validate_review(row, {'fixture': CONV}, checklist)['answer'] == 'yes'
    row['evidence'][0]['quote'] = 'Goodbye'
    with pytest.raises(ValueError, match='exact substring'):
        validate_review(row, {'fixture': CONV}, checklist)
    row = {**annotation(checklist), 'criterion_hash': 'stale'}
    with pytest.raises(ValueError, match='hash mismatch'):
        validate_review(row, {'fixture': CONV}, checklist)


def test_uncertainty_and_invalid_na_cannot_be_silently_accepted(checklist):
    row = {**annotation(checklist), 'uncertain': True}
    with pytest.raises(ValueError, match='requires a reason'):
        validate_review(row, {'fixture': CONV}, checklist)
    row = {**annotation(checklist), 'answer': 'na'}
    with pytest.raises(ValueError, match='not allowed'):
        validate_review(row, {'fixture': CONV}, checklist)


def test_changed_consensus_stays_in_queue_and_is_never_called_accuracy(checklist):
    key = ('fixture', 'C1')
    a = b = {key: {'answer': 'yes'}}
    review = {key: {**annotation(checklist), 'answer': 'no'}}
    report, candidates, queue = build_report([CONV], checklist, a, b, review)
    assert report['review']['changed_old_consensus'] == 1
    assert len(queue) == 1 and candidates[0]['needs_adjudication']
    assert report['product_model_accuracy'] is None and report['human_gold_cells'] == 0
    assert report['review']['compared_to_old_consensus']['agreement_pct'] == 0
