"""Audit two AI annotation passes and an optional, independently produced policy review.

This produces a machine reference candidate, never a human gold standard. Real
transcripts and all generated outputs must stay in the ignored reports/ directory.
Missing cells, changed policy decisions and uncertainty remain visible.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import statistics as stats
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cqa.ingest import ingest_file
from cqa.models import ChecklistVersion, normalize_answer
from scripts.annotation_agreement import statistics


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def valid_answer(value, criterion):
    answer = normalize_answer(value)
    if answer == 'na':
        valid = criterion.na_allowed
    elif criterion.answer_type == 'binary':
        valid = answer in ('yes', 'no')
    else:
        valid = isinstance(answer, (int, float)) and answer in criterion.scale
    if not valid:
        raise ValueError(f'{criterion.id}: answer is not allowed: {value!r}')
    return answer


def load_pass(directory: Path, criteria: dict, expected: set) -> tuple[dict, list]:
    rows, sources = {}, []
    for path in sorted(directory.glob('batch_*.json')):
        sources.append({'name': str(path), 'sha256': digest(path)})
        for conv in json.loads(path.read_text(encoding='utf-8'))['dialogues']:
            for cid, annotation in conv['answers'].items():
                key = (conv['conversation_id'], cid)
                if key in rows or key not in expected:
                    raise ValueError(f'{path.name}: duplicate or unknown cell {key}')
                rows[key] = {**annotation, 'answer': valid_answer(annotation.get('answer'), criteria[cid])}
    if rows.keys() != expected:
        raise ValueError(f'{directory}: missing {len(expected - rows.keys())} cells')
    return rows, sources


def validate_review(row: dict, convs: dict, checklist: ChecklistVersion) -> dict:
    criteria = {c.id: c for c in checklist.criteria}
    key = (row.get('conv_id'), row.get('criterion_id'))
    if key[0] not in convs or key[1] not in criteria:
        raise ValueError(f'unknown review cell: {key}')
    criterion = criteria[key[1]]
    if row.get('checklist_version') != checklist.version or row.get('criterion_hash') != criterion.hash:
        raise ValueError(f'{key}: checklist version/hash mismatch')
    if row.get('policy_version') != 'g6' or row.get('annotation_source') != 'ai_policy_review':
        raise ValueError(f'{key}: missing machine-review provenance')
    if not row.get('annotator') or not str(row.get('rationale') or '').strip():
        raise ValueError(f'{key}: annotator and rationale are required')
    if not isinstance(row.get('uncertain'), bool):
        raise ValueError(f'{key}: uncertain must be a boolean')
    if row['uncertain'] and not row.get('uncertainty_reason'):
        raise ValueError(f'{key}: uncertainty requires a reason')
    messages = {m['id']: m for m in convs[key[0]]['messages']}
    evidence = row.get('evidence')
    ranges = row.get('checked_range')
    if not isinstance(evidence, list) or not isinstance(ranges, list) or not (evidence or ranges):
        raise ValueError(f'{key}: evidence or checked message IDs required')
    for item in evidence:
        if not isinstance(item, dict) or item.get('message_id') not in messages:
            raise ValueError(f'{key}: unknown evidence message')
        quote = item.get('quote')
        if not isinstance(quote, str) or not quote or quote not in messages[item['message_id']]['text']:
            raise ValueError(f'{key}: quote is not an exact substring of the cited message')
    for message_id in ranges:
        if message_id not in messages:
            raise ValueError(f'{key}: unknown checked message: {message_id}')
    return {**row, 'answer': valid_answer(row.get('answer'), criterion)}


def build_report(conversations: list, checklist: ChecklistVersion, left: dict, right: dict, review: dict):
    convs = {c['id']: c for c in conversations}
    keys = sorted(left)
    overall = statistics([(left[k]['answer'], right[k]['answer']) for k in keys])
    per = {c.id: statistics([(left[k]['answer'], right[k]['answer']) for k in keys if k[1] == c.id])
           for c in checklist.criteria}
    agreed = {k for k in keys if left[k]['answer'] == right[k]['answer']}
    queue, candidates = [], []
    for key in sorted(review):
        row = review[key]
        reasons = []
        if key not in agreed:
            reasons.append('old_passes_disagree')
        elif row['answer'] != left[key]['answer']:
            reasons.append('policy_review_changes_old_consensus')
        if row['uncertain']:
            reasons.append('reviewer_uncertain')
        candidate = {**row, 'reference_kind': 'machine_candidate_not_human_gold',
                     'needs_adjudication': bool(reasons), 'review_reasons': reasons,
                     'previous_answers': {'A': left[key]['answer'], 'B': right[key]['answer']}}
        candidates.append(candidate)
        if reasons:
            queue.append(candidate)
    sizes = [len(c['messages']) for c in conversations]
    changed = [r for r in candidates if 'policy_review_changes_old_consensus' in r['review_reasons']]
    report = {
        'kind': 'reference_audit_not_model_accuracy',
        'at_utc': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'checklist_version': checklist.version, 'policy_version': 'g6',
        'limitations': [
            'All three annotation passes are AI-produced, not human or organizer gold.',
            'Passes can share correlated errors; agreement and kappa do not establish correctness.',
            'Third review uses policy g6; differences from old passes can be policy changes.',
            'Exact quotations verify provenance, not whether the interpretation is correct.',
            'DEV has already informed development; it is not an untouched test set.',
        ],
        'dataset': {'conversations': len(conversations), 'cells': len(keys),
                    'languages': dict(Counter(c['language'] for c in conversations)),
                    'channels': dict(Counter(c['channel'] for c in conversations)),
                    'messages': sum(sizes),
                    'roles': dict(Counter(m['speaker'] for c in conversations for m in c['messages'])),
                    'messages_per_dialogue': {'min': min(sizes), 'median': stats.median(sizes), 'max': max(sizes)}},
        'old_pass_agreement': {'overall': overall, 'per_criterion': per,
                               'disputed_cells': len(keys) - len(agreed), 'silver_cells': len(agreed)},
        'review': {'cells': len(review), 'complete': len(review) == len(keys),
                   'reviewed_conversations': len({k[0] for k in review}),
                   'changed_old_consensus': len(changed),
                   'changed_by_criterion': dict(Counter(r['criterion_id'] for r in changed)),
                   'uncertain_cells': sum(r['uncertain'] for r in candidates),
                   'adjudication_queue_cells': len(queue),
                   'queue_by_reason': dict(Counter(x for r in queue for x in r['review_reasons'])),
                   'queue_by_criterion': dict(Counter(r['criterion_id'] for r in queue)),
                   'compared_to_old_consensus': statistics([(left[k]['answer'], review[k]['answer']) for k in sorted(agreed & review.keys())]),
                   'per_criterion_vs_old_consensus': {c.id: statistics([(left[k]['answer'], review[k]['answer'])
                       for k in sorted(agreed & review.keys()) if k[1] == c.id]) for c in checklist.criteria}},
        'human_gold_cells': 0, 'organizer_test_accuracy': None, 'product_model_accuracy': None,
    }
    for field in ('language', 'channel'):
        report['old_pass_agreement']['per_' + field] = {
            value: statistics([(left[k]['answer'], right[k]['answer']) for k in keys if convs[k[0]][field] == value])
            for value in sorted({c[field] for c in conversations})}
    return report, candidates, queue


def write_jsonl(path: Path, rows: list):
    path.write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in rows), encoding='utf-8')


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--data', type=Path, required=True)
    p.add_argument('--pass-a', type=Path, required=True)
    p.add_argument('--pass-b', type=Path, required=True)
    p.add_argument('--review', type=Path, nargs='*', default=[])
    p.add_argument('--checklist', type=Path, default=ROOT / 'data/checklists/v1.json')
    p.add_argument('--out', type=Path, default=ROOT / 'reports/reference-audit')
    args = p.parse_args(argv)
    try:
        checklist = ChecklistVersion.model_validate_json(args.checklist.read_text(encoding='utf-8'))
        conversations = ingest_file(args.data, group_id='DEV')
        convs = {c['id']: c for c in conversations}
        criteria = {c.id: c for c in checklist.criteria}
        expected = {(conv, cid) for conv in convs for cid in criteria}
        left, a_sources = load_pass(args.pass_a, criteria, expected)
        right, b_sources = load_pass(args.pass_b, criteria, expected)
        review = {}
        for path in args.review:
            for line in path.read_text(encoding='utf-8-sig').splitlines():
                if not line.strip():
                    continue
                row = validate_review(json.loads(line), convs, checklist)
                key = (row['conv_id'], row['criterion_id'])
                if key in review:
                    raise ValueError(f'duplicate review cell: {key}')
                review[key] = row
        if args.review and review.keys() != expected:
            raise ValueError(f'policy review must cover every cell; missing {len(expected - review.keys())}')
        report, candidates, queue = build_report(conversations, checklist, left, right, review)
        report['sources'] = {'dataset': {'name': str(args.data), 'sha256': digest(args.data)},
                             'checklist': {'name': str(args.checklist), 'sha256': digest(args.checklist)},
                             'policy': {'name': 'docs/ANNOTATION_POLICY.md',
                                        'sha256': digest(ROOT / 'docs/ANNOTATION_POLICY.md')},
                             'pass_a': a_sources, 'pass_b': b_sources,
                             'review': [{'name': str(x), 'sha256': digest(x)} for x in args.review]}
    except (ValueError, OSError, KeyError, TypeError) as exc:
        p.error(str(exc))
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / 'reference_metrics.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    write_jsonl(args.out / 'silver-g6-candidate.jsonl', candidates)
    write_jsonl(args.out / 'adjudication-queue.jsonl', queue)
    with (args.out / 'agreement_by_criterion.csv').open('w', newline='', encoding='utf-8-sig') as f:
        writer = csv.DictWriter(f, fieldnames=['criterion_id', 'compared', 'agreed', 'agreement_pct', 'cohen_kappa'])
        writer.writeheader()
        for cid, values in report['old_pass_agreement']['per_criterion'].items():
            writer.writerow({'criterion_id': cid, **values})
    print(json.dumps({'report': str(args.out / 'reference_metrics.json'),
                      'old_agreement': report['old_pass_agreement']['overall'], 'review': report['review']}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
