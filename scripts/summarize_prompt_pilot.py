"""Recompute the complete, frozen 10-dialogue pilot report from raw events.

Usage: python scripts/summarize_prompt_pilot.py reports/NEW_RUN
Private DEV data, silver-g6-candidate.jsonl and a predeclared evaluation_plan.json
are required locally. No transcripts or reference annotations are bundled here.
Every planned cell remains in the denominator, including missing/failed answers.
"""
from pathlib import Path
import csv
import hashlib
import json
import math
import statistics
import sys
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from cqa.ingest import ingest_file
from scripts.codex_prompt_pilot import parse_events
from scripts.eval_accuracy import load_run, load_gold, measure, norm, total

run_dir = Path(sys.argv[1])
reference = ROOT / 'eval/gold/silver-g6-candidate.jsonl'
plan = json.loads((run_dir / 'evaluation_plan.json').read_text())
assert hashlib.sha256(reference.read_bytes()).hexdigest() == plan['reference_sha256']
meta, rows = load_run(run_dir / 'run.jsonl')
selected = plan['selection']
assert len(rows) == len(selected) == 10, 'Wait for all rows; no partial result presentation'
assert len({r['conv_id'] for r in rows}) == len(rows)
assert {r['conv_id'] for r in rows} == set(selected)
criteria = json.loads((ROOT / 'data/checklists/v1.json').read_text())['criteria']
gold = load_gold(str(reference))
ref = {(r['conv_id'], r['criterion_id']): r for r in map(json.loads, reference.read_text().splitlines())}
convs = {c['id']: c for c in ingest_file(ROOT / 'data/raw/dev_records.json')}
for row in rows:
    assert len({e['criterion_id'] for e in row['evals']}) == len(row['evals'])
result = measure(rows, lambda a,b: gold.get((a,b)), criteria, set())
assert result['n'] == 80
def stat(count):
    ok,n = count
    return {'matched': ok, 'cells': n, 'pct': round(ok/n*100, 4) if n else None}
def ratio(n,d):
    return round(n/d, 6) if d else None
matches, differences, score_rows = [], [], []
na = {'na_to_na':0, 'na_to_applicable':0, 'applicable_to_na':0, 'applicable_to_applicable':0, 'missing':0}
c8 = {'tp':0, 'fp':0, 'fn':0, 'tn':0, 'missing':0}
raw_quotes = exact_quotes = quote_cells = exact_quote_cells = 0
invalid_quotes = []
for row in rows:
    cid = row['conv_id']
    by_id = {e['criterion_id']:e for e in row['evals']}
    for criterion in criteria:
        k = (cid,criterion['id']); r = ref[k]; ev = by_id.get(k[1],{})
        predicted = norm(ev.get('answer')); expected = gold[k]
        item = {'conv_id':cid,'criterion_id':k[1],'reference':expected,'prediction':predicted,
                'match':predicted==expected,'reference_uncertain':r['uncertain'],
                'reference_needs_adjudication':r['needs_adjudication'],
                'needs_review':ev.get('needs_review',True),
                'model_needs_review':ev.get('model_needs_review',ev.get('needs_review',True)),
                'reference_rationale':r['rationale'],'model_explanation':ev.get('explanation')}
        matches.append(item)
        if not item['match']: differences.append(item)
        if criterion['na_allowed']:
            if predicted is None: na['missing']+=1
            else: na[('na' if expected=='na' else 'applicable')+'_to_'+('na' if predicted=='na' else 'applicable')]+=1
        if k[1]=='C8':
            if predicted is None:
                c8['missing']+=1
                if expected=='yes': c8['fn']+=1
            else: c8['tp' if expected=='yes' and predicted=='yes' else 'fp' if expected!='yes' and predicted=='yes' else 'fn' if expected=='yes' else 'tn']+=1
    pred_total=total({k:norm(v.get('answer')) for k,v in by_id.items()},criteria,set())
    ref_total=total({c['id']:gold[(cid,c['id'])] for c in criteria},criteria,set())
    score_rows.append({'conv_id':cid,'reference_total':ref_total,'prediction_total':pred_total,
                       'absolute_error':abs(pred_total-ref_total) if pred_total is not None and ref_total is not None else None})
    event_path=run_dir/f'{cid}.events.jsonl'
    if row['status']=='completed' and event_path.exists():
        response,_ = parse_events(event_path.read_text())
        messages={m['id']:m['text'] for m in convs[cid]['messages']}
        for e in response['criteria']:
            quotes=e.get('evidence') or []
            valid=0
            for q in quotes:
                raw_quotes+=1
                exact=isinstance(q.get('quote'),str) and bool(q['quote']) and q.get('message_id') in messages and q['quote'] in messages[q['message_id']]
                exact_quotes+=int(exact);valid+=int(exact)
                if not exact: invalid_quotes.append({'conv_id':cid,'criterion_id':e['criterion_id'],**q})
            if quotes: quote_cells+=1;exact_quote_cells+=int(valid==len(quotes))
assert sum(x['match'] for x in matches)==result['ok']
times=sorted(r['wall_s'] for r in rows if r['status']=='completed' and r.get('wall_s') is not None)
reviewed=[x for x in matches if x['reference_needs_adjudication']]
non_reviewed=[x for x in matches if not x['reference_needs_adjudication']]
metrics={
    'kind':'live_prompt_agreement_with_machine_reference_not_human_accuracy',
    'created_utc':datetime.now(timezone.utc).isoformat(timespec='seconds'),
    'requested_model':meta['requested_model'], 'resolved_model':None,
    'scope':'Product batch prompt + evidence validator via experimental Codex CLI; not product API/retries/arbitration E2E',
    'dataset':'10 previously used DEV dialogues; not hidden control or untouched holdout',
    'languages':{k:sum(r['language']==k for r in rows) for k in ['RU','KZ','EN']},
    'channels':{k:sum(r['channel']==k for r in rows) for k in ['chat','call']},
    'completed_dialogues':sum(r['status']=='completed' for r in rows),'planned_dialogues':10,
    'overall':stat((result['ok'],result['n'])),
    'valid_answer_coverage':stat((80-len(result['missing']),80)),
    'per_criterion':{k:stat(v) for k,v in result['per'].items()},
    'per_language':{k:stat(v) for k,v in result['lang'].items()},
    'per_channel':{k:stat(v) for k,v in result['chan'].items()},
    'macro_criterion_match_pct':round(statistics.mean(v[0]/v[1]*100 for v in result['per'].values()),4),
    'total_score_mae':statistics.mean(result['maes']) if result['maes'] else None,
    'total_score_compared_dialogues':len(result['maes']),
    'total_score_max_error':max(result['maes']) if result['maes'] else None,
    'c8':{**c8,'precision':ratio(c8['tp'],c8['tp']+c8['fp']),'recall':ratio(c8['tp'],c8['tp']+c8['fn'])},
    'na':{**na,'agreement_pct':round(100*(na['na_to_na']+na['applicable_to_applicable'])/sum(na.values()),4)},
    'flagged_for_review':stat((result['flagged'][1],80)),
    'model_flagged_for_review':stat((sum(x['model_needs_review'] for x in matches),80)),
    'review_routing_basis':meta.get('review_routing_basis', 'validation errors and confidence < 0.7'),
    'review_reasons_count':sum(len(e.get('review_reasons', [])) for r in rows for e in r['evals']),
    'reference_queue_subset':stat((sum(x['match'] for x in reviewed),len(reviewed))),
    'reference_non_queue_subset':stat((sum(x['match'] for x in non_reviewed),len(non_reviewed))),
    'missing_cells':result['missing'], 'differences':len(differences),
    'evidence':{'raw_quotes':raw_quotes,'exact_quotes_in_original_message':exact_quotes,
                'raw_exact_quote_pct':round(100*exact_quotes/raw_quotes,4) if raw_quotes else None,
                'cells_with_quotes':quote_cells,'cells_with_all_quotes_exact':exact_quote_cells,
                'post_validator_quotes':sum(len(e.get('evidence',[])) for r in rows for e in r['evals'])},
    'latency':{'observations':len(times),'mean_s':statistics.mean(times) if times else None,
               'p50_s':statistics.median(times) if times else None,
               'p95_s':times[math.ceil(.95*len(times))-1] if times else None,
               'p95_method':'nearest rank on at most 10 observations',
               'includes':'CLI launch, provider response and local validation; not production API latency'},
    'provider_calls':sum(r.get('calls',0) for r in rows),
    'usage':{k:sum(r.get('usage',{}).get(k,0) for r in rows) for k in ['input_tokens','cached_input_tokens','output_tokens','reasoning_output_tokens']},
    'cost_usd':None,'cost_basis':'Subscription CLI did not report a monetary price; unknown is not zero',
    'reference_sha256':plan['reference_sha256'],
    'run_sha256':hashlib.sha256((run_dir/'run.jsonl').read_bytes()).hexdigest(),
    'limitations':['Machine reference has unresolved labels; all 80 included in headline.',
                   'AI annotators and evaluated model can share correlated errors.',
                   'DEV already informed development; selected sample is small.',
                   'No production E2E, no independently verified billing, no calibration claim.'],
}
(run_dir/'metrics.json').write_text(json.dumps(metrics,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
for filename,items in [('differences.json',differences),('scores.json',score_rows),('raw_quote_issues.json',invalid_quotes)]:
    (run_dir/filename).write_text(json.dumps(items,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
with (run_dir/'cell_comparison.csv').open('w',encoding='utf-8-sig',newline='') as f:
    w=csv.DictWriter(f,fieldnames=list(matches[0]));w.writeheader();w.writerows(matches)
print(json.dumps({k:metrics[k] for k in ['overall','valid_answer_coverage','per_criterion','total_score_mae','c8','na','flagged_for_review','evidence','latency','provider_calls','usage','differences']},ensure_ascii=False,indent=2))
