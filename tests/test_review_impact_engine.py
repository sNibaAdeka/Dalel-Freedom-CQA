"""Cross-check the UI's hypothetical totals against the authoritative Python engine.

Synthetic answers only. This guards against a duplicated frontend formula drifting
on N/A denominators, scale steps, fractional weights or rounding rules.
"""
import json
import random
import shutil
import subprocess
from pathlib import Path

import pytest

from cqa.engine import score
from cqa.models import ChecklistVersion, CriterionEval


def test_frontend_scenarios_match_real_scoring_engine(v1_raw):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required for cross-runtime scoring verification')
    rng = random.Random(701)
    cases = []
    for index in range(24):
        raw = json.loads(json.dumps(v1_raw))
        raw['rounding'] = {'total': index % 4, 'mode': 'half_even' if index % 2 else 'half_up'}
        if index % 3 == 0:
            raw['criteria'][0]['weight'] = 12.125
            raw['criteria'][5]['weight'] = 7.75
        checklist = ChecklistVersion.model_validate(raw)
        options = {c.id: (list(c.scale) if c.answer_type == 'scale' else ['yes', 'no'])
                   + (['na'] if c.na_allowed else []) for c in checklist.criteria}
        answers = {cid: rng.choice(allowed) for cid, allowed in options.items()}
        def evaluations(values):
            return [CriterionEval(conv_id='synthetic-impact', criterion_id=c.id, criterion_hash=c.hash,
                                  answer=values[c.id], confidence=.5, needs_review=True)
                    for c in checklist.criteria]
        evs = evaluations(answers)
        expected = {}
        for criterion in checklist.criteria:
            if criterion.critical:
                continue
            expected[criterion.id] = []
            for answer in options[criterion.id]:
                if answer == answers[criterion.id]:
                    continue
                changed = {**answers, criterion.id: answer}
                expected[criterion.id].append({'answer': answer, 'total': score(checklist, evaluations(changed)).total})
        cases.append({'detail': {'checklist': checklist.model_dump(mode='json'),
                                 'evals': [ev.model_dump(mode='json') for ev in evs],
                                 'score': score(checklist, evs).model_dump(mode='json')}, 'expected': expected})
    module = (Path(__file__).resolve().parents[1] / 'web/js/review-impact.js').as_uri()
    script = f"""
      import fs from 'node:fs';
      const {{buildReviewImpact}} = await import({json.dumps(module)});
      const cases = JSON.parse(fs.readFileSync(0, 'utf8'));
      process.stdout.write(JSON.stringify(cases.map(item => buildReviewImpact(item.detail))));
    """
    result = subprocess.run([node, '--experimental-default-type=module', '--input-type=module', '-e', script],
                            input=json.dumps(cases), capture_output=True, text=True, check=True, timeout=30)
    actual = json.loads(result.stdout)
    assert len(actual) == len(cases)
    for case, preview in zip(cases, actual):
        assert preview['available'], preview['reason']
        by_id = {item['criterionId']: item for item in preview['ranked']}
        assert set(by_id) == set(case['expected'])
        for cid, expected in case['expected'].items():
            observed = [{'answer': item['answer'], 'total': item['total']} for item in by_id[cid]['scenarios']]
            assert sorted(observed, key=lambda item: str(item['answer'])) == sorted(expected, key=lambda item: str(item['answer']))
