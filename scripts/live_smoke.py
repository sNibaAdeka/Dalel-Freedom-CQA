"""Probe a real provider, then process RU/KZ/EN chats and a call through the API.

Local outputs contain private transcripts: keep the default ignored results/ location.
The report measures execution, evidence validity and cost, NOT accuracy against human labels.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from api.app import create_app
from api.state import AppState
from cqa import ingest, llm
from fastapi.testclient import TestClient


def select_smoke(convs: list[dict]) -> list[dict]:
    selected = []
    for channel, language in [('chat', 'RU'), ('chat', 'KZ'), ('chat', 'EN'), ('call', 'RU')]:
        found = next((c for c in convs if c['channel'] == channel and c['language'] == language), None)
        if found is None:
            raise ValueError(f'Нужен диалог {channel}/{language} для smoke-теста')
        selected.append(found)
    return selected


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', type=Path, required=True)
    parser.add_argument('--profile', default='claude')
    parser.add_argument('--mode', choices=['fast', 'accurate'], default='fast')
    parser.add_argument('--full', action='store_true', help='all supplied conversations after successful probe')
    parser.add_argument('--out', type=Path)
    args = parser.parse_args(argv)
    llm.load_dotenv()
    spec = llm.preset(args.mode, args.profile)['model']
    if llm.parse_model(spec)[0] == 'mock':
        parser.error('Для этой проверки нужен реальный провайдер, mock запрещён')
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    out = args.out or ROOT / 'results' / 'live-smoke' / stamp
    out.mkdir(parents=True, exist_ok=True)
    report = {'at_utc':stamp, 'profile':args.profile, 'mode':args.mode, 'requested_model':spec,
              'status':'started', 'quality_accuracy':None, 'quality_note':'Human reference labels not supplied'}
    calls = []
    state = None
    llm.add_listener(calls.append)
    try:
        # Do not fan out dozens of criterion calls when auth/quota/provider is unavailable.
        answer, usage = llm.complete_json('Return only a JSON object with ok=true.', 'Connection probe.',
                                          model=spec, no_cache=True, retries=0)
        if answer != {'ok':True} or usage.get('cache_hit'):
            raise RuntimeError('Живой preflight не вернул ожидаемый JSON')
        report['probe_usage'] = usage
        convs = ingest.ingest_file(args.data, group_id='DEV' if args.full else 'LIVE_SMOKE')
        chosen = convs if args.full else select_smoke(convs)
        state = AppState(db_path=out/'run.sqlite', autoload=False, calibrate_audio=False, llm_call=None)
        state.update_settings(mode=args.mode, profile=args.profile)
        state.ingest(chosen, 'live-smoke-file')
        with TestClient(create_app(state)) as client:
            response = client.post('/api/runs', json={'mode':args.mode, 'profile':args.profile,
                               'conv_ids':[c['id'] for c in chosen], 'live':True, 'wait':True})
            response.raise_for_status()
            run = response.json()
            details = [client.get(f"/api/conversations/{c['id']}?trace=1").json() for c in chosen]
            (out/'details.json').write_text(json.dumps(details,ensure_ascii=False,indent=2),encoding='utf-8')
            report.update(run=run, conversations=len(chosen),
                          evaluations=sum(len(d['evals']) for d in details),
                          needs_review=sum(bool(e['needs_review']) for d in details for e in d['evals']),
                          verified_quotes=sum(bool(q['valid']) for d in details for e in d['evals'] for q in e['evidence']))
            report['status'] = 'passed' if run['status']=='completed' and not run.get('errors') else 'failed'
            if any(e.get('provider') == 'mock' for d in details for e in d['evals']):
                report.update(status='failed', error='Unexpected mock evaluation')
    except Exception as exc:
        report.update(status='blocked', error=f'{type(exc).__name__}: {str(exc)[:600]}')
    finally:
        if state is not None:
            llm.remove_listener(state._on_llm_call)
        llm.remove_listener(calls.append)
        report['provider_calls'] = len([c for c in calls if not c.get('cache_hit')])
        report['reported_cost_usd'] = sum(c.get('cost_usd',0) for c in calls)
        report['resolved_models'] = sorted({m for c in calls for m in c.get('resolved_models',[])})
        (out/'calls.json').write_text(json.dumps(calls,ensure_ascii=False,indent=2),encoding='utf-8')
        (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'status':report['status'],'report':str(out/'report.json'),
                      'error':report.get('error'),'provider_calls':report['provider_calls']},ensure_ascii=False))
    return 0 if report['status']=='passed' else 2


if __name__ == '__main__':
    raise SystemExit(main())
