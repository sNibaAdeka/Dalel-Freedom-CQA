"""Compare two measured runs only when sample, reference and source data match.

Input directories must contain manifest.json, evaluation_plan.json, metrics.json,
and run.jsonl. This reports DEV progress; it never rewrites labels or predictions.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def read_run(path: Path) -> dict:
    data = {name: json.loads((path / f'{name}.json').read_text())
            for name in ('manifest', 'evaluation_plan', 'metrics')}
    metrics, plan, manifest = data['metrics'], data['evaluation_plan'], data['manifest']
    if hashlib.sha256((path / 'run.jsonl').read_bytes()).hexdigest() != metrics['run_sha256']:
        raise ValueError(f'{path}: metrics do not describe the stored run')
    records = [json.loads(line) for line in (path / 'run.jsonl').read_text().splitlines() if line.strip()]
    rows = [row for row in records if 'conv_id' in row]
    expected = plan['selection']
    if len(set(expected)) != len(expected) or len(rows) != len(expected):
        raise ValueError('Incomplete or duplicate selection')
    if sorted(row['conv_id'] for row in rows) != sorted(expected) or manifest['selection'] != expected:
        raise ValueError('Run and plan selections differ')
    if metrics['overall']['cells'] != plan['expected_cells']:
        raise ValueError('Primary denominator differs from plan')
    if metrics['reference_sha256'] != plan['reference_sha256']:
        raise ValueError('Metrics and plan references differ')
    if metrics['completed_dialogues'] != sum(row['status'] == 'completed' for row in rows):
        raise ValueError('Completion count differs from stored run')
    return data


def compare(before: dict, after: dict) -> dict:
    old_plan, new_plan = before['evaluation_plan'], after['evaluation_plan']
    for key in ('selection', 'reference_sha256', 'expected_cells'):
        if old_plan[key] != new_plan[key]:
            raise ValueError(f'Cannot compare runs with different {key}')
    # Prompt/evaluator hashes may change; data and checklist must not.
    for suffix in ('dev_records.json', 'checklists/v1.json'):
        values = [[v for k, v in run['manifest']['sources'].items() if k.endswith(suffix)]
                  for run in (before, after)]
        if len(values[0]) != 1 or values[0] != values[1]:
            raise ValueError(f'Data/checklist source mismatch: {suffix}')
    a, b = before['metrics'], after['metrics']
    def pct_delta(x, y):
        return (y - x) / x * 100 if x and y is not None else None
    speed = (a['latency']['p50_s'] / b['latency']['p50_s']
             if a['latency']['p50_s'] and b['latency']['p50_s'] else None)
    gates = {
        'complete_sample': b['completed_dialogues'] == len(new_plan['selection']),
        'all_answers_valid': b['valid_answer_coverage']['matched'] == new_plan['expected_cells'],
        'all_raw_quotes_exact': bool(b['evidence']['raw_quotes']) and b['evidence']['raw_exact_quote_pct'] == 100,
        'agreement_not_lower': b['overall']['matched'] >= a['overall']['matched'],
        'score_mae_not_higher': b['total_score_mae'] is not None and a['total_score_mae'] is not None and b['total_score_mae'] <= a['total_score_mae'],
        'lower_p50': speed is not None and speed > 1,
    }
    return {
        'kind': 'paired_dev_progress_not_holdout_accuracy',
        'before': a, 'after': b,
        'delta_agreement_percentage_points': b['overall']['pct'] - a['overall']['pct'],
        'p50_speedup': speed,
        'p50_change_pct': pct_delta(a['latency']['p50_s'], b['latency']['p50_s']),
        'output_tokens_change_pct': pct_delta(a['usage']['output_tokens'], b['usage']['output_tokens']),
        'development_gates': gates, 'all_development_gates_pass': all(gates.values()),
        'limitations': 'Same small DEV sample and machine reference; joint changes and provider variation '
                       'prevent causal attribution. Passing these gates is not production or human validation.',
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('before', type=Path)
    parser.add_argument('after', type=Path)
    parser.add_argument('--out', type=Path)
    args = parser.parse_args(argv)
    result = compare(read_run(args.before), read_run(args.after))
    output = json.dumps(result, ensure_ascii=False, indent=2) + '\n'
    if args.out:
        if args.out.exists():
            parser.error('Comparison output already exists; preserve previous reports')
        args.out.write_text(output, encoding='utf-8')
    else:
        print(output, end='')


if __name__ == '__main__':
    main()
