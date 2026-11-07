"""Actual JS partial annotation export remains consumable by the Claude scorer."""
import json
import subprocess
from pathlib import Path


def test_actual_js_partial_export_is_accepted_by_claude_scorer(tmp_path):
    from api.labeling import _checklist
    from cqa.models import ChecklistVersion
    from scripts.score_labels import build_report, load_labels

    root = Path(__file__).resolve().parents[1]
    checklist = ChecklistVersion.model_validate_json((root / 'data/checklists/v1.json').read_text())
    context = {'apiScope': 'http://localhost:8170/api/', 'annotator': 'synthetic-tester',
               'checklist': _checklist(checklist).model_dump(mode='json'),
               'conversation': {'id': 'synthetic-conversation', 'transcript_hash': 'b' * 64}}
    code = """
      const fs = await import('node:fs/promises');
      const source = await fs.readFile(process.argv[1], 'utf8');
      const m = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));
      const ctx = JSON.parse(process.argv[2]);
      const draft = m.setAnswer(m.emptyDraft(ctx), ctx, 'C6', 0, '2026-11-08T00:00:00Z');
      process.stdout.write(m.exportRows(draft, ctx, ['C6']).map(r => JSON.stringify(r)).join('\\n') + '\\n');
    """
    output = subprocess.run(['node', '--input-type=module', '-e', code, str(root / 'web/js/labeling.js'),
                             json.dumps(context)], capture_output=True, text=True, check=True).stdout
    path = tmp_path / 'human.jsonl'
    path.write_text(output)
    rows = [json.loads(line) for line in output.splitlines()]
    assert rows[0]['checklist_hash'] == context['checklist']['checklist_hash']
    assert rows[0]['transcript_hash'] == 'b' * 64
    assert rows[0]['criterion_hash'] == next(c.hash for c in checklist.criteria if c.id == 'C6')
    cells, rejected = load_labels([path], checklist)
    assert not rejected
    assert cells == {('synthetic-tester', 'synthetic-conversation', 'C6'): 0}
    run_path = tmp_path / 'synthetic-run.jsonl'
    run_path.write_text(json.dumps({'conv_id': 'synthetic-conversation', 'evals': [{'criterion_id': 'C6', 'answer': 0}]}))
    report = build_report([path], [run_path], checklist)
    comparison = report['runs'][run_path.name]['vs_consensus']
    assert comparison['overall']['n'] == 1 and comparison['overall']['correct'] == 1
    assert comparison['total_score']['dialogues'] == 0 and comparison['total_score']['mae'] is None
    rows[0]['criterion_hash'] = 'f' * 16
    path.write_text(json.dumps(rows[0]))
    assert load_labels([path], checklist)[1] == {'stale_criterion_hash': 1}
