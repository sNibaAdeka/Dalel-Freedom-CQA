"""Offline, transcript-free DEV annotation priorities; no model or network calls.

The score is a triage heuristic, not an estimate of achievable accuracy gain.
Raw inputs stay in their original ignored directories. Only IDs, answer values,
aggregate counts and file digests are emitted; blind UI receives IDs/hashes only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = Path(__file__).resolve().parents[1]
RUNS = (
    ('gemini', 'Gemini', 'w-int/results/eval_runs/or-hold-google-gemini-4-argon-g9.jsonl'),
    ('cascade', 'Cascade', 'w-int/results/eval_runs/or-hold-cascade-g9.jsonl'),
    ('sonnet-api', 'Sonnet', 'w-int/results/eval_runs/or-hold-anthropic-claude-sonnet-5-5-g9.jsonl'),
    ('qwen', 'Qwen', 'w-int/results/eval_runs/or-hold-qwen-qwen3-8-27b-g9.jsonl'),
    ('deepseek', 'DeepSeek', 'w-int/results/eval_runs/or-hold-deepseek-deepseek-v4-pro-g9.jsonl'),
    ('terra', 'Terra', 'w-int/results/eval_runs/or-hold-openai-gpt-6-1-terra-g9.jsonl'),
    ('haiku-g9', 'Haiku', 'w-int/results/eval_runs/hk-g9-all.jsonl'),
    ('haiku-g10', 'Haiku', 'w-int/results/eval_runs/hk-g10-all.jsonl'),
    ('haiku-g9-repeat', 'Haiku', 'w-m1/results/eval_runs/m1-hold-haiku-m1-c4.jsonl'),
    ('sonnet-cli', 'Sonnet', 'w-m1/results/eval_runs/m1-sonnet-g9-all.jsonl'),
)
REFERENCES = (
    ('silver', 'w-m1/eval/gold/silver.jsonl'),
    ('gold_v2', 'agents/reports/gold_v2.jsonl'),
    ('gold_v2_full', 'agents/reports/gold_v2_full.jsonl'),
)


def answer(value):
    """Only actual answers are observations; null/unknown/invalid are missing."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ('yes', 'no', 'na'):
            return normalized
        if normalized in ('n/a', 'not_applicable'):
            return 'na'
    return None


def read_cells(path: Path) -> dict:
    cells = {}
    for number, line in enumerate(path.read_text(encoding='utf-8-sig').splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        conv = row.get('conv_id')
        if not isinstance(conv, str) or not conv:
            continue
        for cell in row.get('evals', [row]):
            if cell.get('errors'):
                continue
            cid, value = cell.get('criterion_id'), answer(cell.get('answer'))
            if not isinstance(cid, str) or value is None:
                continue
            key = (conv, cid)
            if key in cells and cells[key] != value:
                raise ValueError(f'{path.name}:{number}: conflicting duplicate cell {conv}/{cid}')
            cells[key] = value
    return cells


def valid_answer(criterion, value):
    if value == 'na':
        return criterion.get('na_allowed') is True
    if criterion['answer_type'] == 'binary':
        return value in ('yes', 'no')
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    scale = criterion.get('scale') or []
    if criterion['answer_type'] == 'scale':
        return value in scale
    return bool(scale) and (min(scale) if len(scale) > 1 else 0) <= value <= max(scale)


def rank_cells(runs, references, criteria, dev_ids):
    """Each family has total mass one, shared across its available repeat/config runs."""
    by_id = {c['id']: c for c in criteria}
    keys = sorted({key for run in runs for key in run['cells'] if key[0] in dev_ids and key[1] in by_id})
    candidates, seen, disputed = [], Counter(), Counter()
    max_weight = max((c['weight'] for c in criteria), default=0)
    for key in keys:
        criterion = by_id[key[1]]
        observed = {run['name']: run['cells'][key] for run in runs
                    if key in run['cells'] and valid_answer(criterion, run['cells'][key])}
        if not observed:
            continue
        refs = {name: cells[key] for name, cells in references.items()
                if key in cells and valid_answer(criterion, cells[key])}
        family_values = defaultdict(list)
        for run in runs:
            if run['name'] in observed:
                family_values[run['family']].append(observed[run['name']])
        mass = defaultdict(float)
        for values in family_values.values():
            for value in values:
                mass[value] += 1 / (len(family_values) * len(values))
        gini = 1 - sum(p * p for p in mass.values())
        mismatch = sum(1 - mass.get(v, 0) for v in refs.values()) / len(refs) if refs else 0
        seen[key[1]] += 1
        if len(set(observed.values()) | set(refs.values())) <= 1:
            continue
        disputed[key[1]] += 1
        critical = bool(criterion.get('critical'))
        effective_weight = max(criterion['weight'], max_weight) if critical else criterion['weight']
        reasons = []
        if len(set(observed.values())) > 1:
            reasons.append('модельные ответы расходятся')
        if any(v != r for v in observed.values() for r in refs.values()):
            reasons.append('модель и машинный эталон расходятся')
        if len(set(refs.values())) > 1:
            reasons.append('машинные эталоны расходятся')
        if any(len(set(v)) > 1 for v in family_values.values()):
            reasons.append('повторы/варианты одной семьи расходятся')
        if critical:
            reasons.append('критический флаг: вес 0 не обнуляет приоритет')
        candidates.append({'conv_id': key[0], 'criterion_id': key[1], 'criterion_hash': criterion['hash'],
                           'models': observed, 'references': refs, 'family_count': len(family_values),
                           'run_count': len(observed), 'gini': gini, 'reference_mismatch': mismatch,
                           'spread': max(gini, mismatch), 'official_weight': criterion['weight'],
                           'effective_weight': effective_weight, 'critical': critical, 'why': reasons})
    for cell in candidates:
        cid = cell['criterion_id']
        frequency = disputed[cid] / seen[cid]
        cell.update(criterion_disputed=disputed[cid], criterion_observed=seen[cid], frequency=frequency,
                    priority=cell['spread'] * cell['effective_weight'] * frequency)
    # Safety-first triage: a rare critical flag must not disappear below frequent style disagreements.
    return sorted(candidates, key=lambda c: (not c['critical'], -c['priority'], c['conv_id'], c['criterion_id']))


def make_queue(ranked, checklist, limit):
    return {'schema_version': 1, 'checklist_id': checklist['checklist_id'],
            'checklist_version': checklist['version'],
            'cells': [{k: c[k] for k in ('conv_id', 'criterion_id', 'criterion_hash')} for c in ranked[:limit]]}


def render_report(ranked, checklist, sources, limit):
    top = ranked[:limit]
    lines = ['# Приоритет ручной разметки DEV — только для координатора', '',
             '**Не показывайте этот файл разметчикам:** варианты ниже раскрывают ответы моделей и машинных эталонов.',
             'Для независимой разметки используйте `?label=1&queue=priority`: там только исходный диалог и один критерий.', '',
             f'Найдено спорных ячеек: **{len(ranked)}**; очередь: **{len(top)}** ячеек / **{len({c["conv_id"] for c in top})}** диалогов.',
             'Это выбранные трудные случаи DEV, а не случайная выборка и не независимый контроль точности.', '',
             '## Формула и ограничения', '',
             '`priority = max(Gini моделей, доля несовпадений с машинными эталонами) × effective_weight × frequency`.',
             'Каждая семья имеет массу 1; её доступные повторы/конфигурации делят эту массу поровну. '
             'Поэтому три Haiku и два Sonnet не становятся пятью независимыми голосами. Каскад — отдельная конфигурация, '
             'зависимая от Haiku/Gemini; независимость семейств также не предполагается.',
             '`frequency` — доля спорных диалогов среди наблюдаемых диалогов по этому критерию; '
             'это наблюдаемый proxy повторяемости неоднозначной трактовки, не доказанная причина ошибок.',
             '`effective_weight` равен весу критерия; для критического C8 — максимальному весу чек-листа. '
             'Кроме того, спорные критические флаги идут отдельной первой группой; затем остальные ячейки по priority. '
             'Иначе редкие грубые нарушения исчезли бы под частыми спорами о стиле общения. '
             'Это явно заданное правило безопасности при отборе, не изменение формулы итога.',
             'Пустые, неизвестные и недопустимые ответы исключены, а не считаются отдельным вариантом. '
             'В спор включается только ячейка хотя бы с одним ответом модели. Равные приоритеты сортируются детерминированно.',
             'Польза разметки и время работы человека **не измерены**. Число в колонке — эвристика, не ожидаемый прирост точности. '
             'Silver/gold_v2/gold_v2_full — машинные ориентиры, не независимые человеческие голоса.', '',
             '## Очередь', '', '| № | Диалог | Критерий | Приоритет | Ответы прогонов | Машинные ориентиры | Почему спорно |',
             '|---:|---|---|---:|---|---|---|']
    fmt = lambda values: '; '.join(f'{k}={json.dumps(v, ensure_ascii=False)}' for k, v in sorted(values.items()))
    for i, cell in enumerate(top, 1):
        lines.append(f'| {i} | {cell["conv_id"]} | {cell["criterion_id"]} | {cell["priority"]:.4f} | '
                     f'{fmt(cell["models"])} | {fmt(cell["references"])} | {"; ".join(cell["why"])} |')
    lines += ['', '## Экспорт и сверка', '',
              'Обычный `?label=1` экспортирует только полностью заполненный диалог. '
              'Приоритетная очередь экспортирует только явно отвеченные ячейки списка одним JSONL; '
              'пропуски не превращаются в ответы. Черновики разделены по разметчику, API, версии и хэшам исходных данных.',
              '`scripts/score_labels.py` принимает такой частичный JSONL без смены схемы: '
              '`conv_id, criterion_id, answer, annotator, checklist_version, criterion_hash, checklist_hash, '
              'transcript_hash, annotation_source, annotated_at`. MAE итога появится только при всех критериях диалога.',
              'Перед экспортом UI заново проверяет исходные хэши. Текущий scorer проверяет версию и хэш критерия, '
              'но не сверяет `checklist_hash` / `transcript_hash`; их наличие в JSONL само по себе не заменяет эту проверку. '
              'Scorer не изменялся (владелец — Claude).',
              'Одного разметчика недостаточно для независимого согласия. Отдайте одну и ту же слепую очередь двум людям, '
              'сохраните их коды, затем разберите расхождения отдельно. Объективную точность проверяйте на другой, заранее фиксированной выборке.', '',
              '## Воспроизведение', '',
              '```sh', 'python scripts/disputed_cells.py --source-root <корень репозитория>', '```', '',
              f'Чек-лист: `{checklist["checklist_id"]}`, версия **{checklist["version"]}**. '
              'Источник DEV — объединение tune/holdout из `eval/split.json`; историческая настройка на DEV известна.',
              'Хэши ниже фиксируют входы; исходные JSONL не копировались в репозиторий.', '', '| Источник | SHA-256 |', '|---|---|']
    lines += [f'| {name} | `{sha}` |' for name, sha in sources]
    return '\n'.join(lines) + '\n'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root', type=Path, default=SOURCE_ROOT)
    parser.add_argument('--checklist', type=Path, default=ROOT / 'data/checklists/v1.json')
    parser.add_argument('--split', type=Path, default=ROOT / 'eval/split.json')
    parser.add_argument('--out', type=Path, default=ROOT / 'docs/HUMAN_LABEL_PRIORITY.md')
    parser.add_argument('--queue', type=Path, default=ROOT / 'web/data/label-priority.json')
    parser.add_argument('--limit', type=int, default=40)
    args = parser.parse_args(argv)
    if args.limit < 1:
        parser.error('--limit must be positive')
    checklist = json.loads(args.checklist.read_text())
    split = json.loads(args.split.read_text())
    sources = []
    def load(relative):
        path = args.source_root / relative
        cells = read_cells(path)
        sources.append((relative, hashlib.sha256(path.read_bytes()).hexdigest()))
        return cells
    runs = [{'name': name, 'family': family, 'cells': load(path)} for name, family, path in RUNS]
    refs = {name: load(path) for name, path in REFERENCES}
    sources += [(args.checklist.name, hashlib.sha256(args.checklist.read_bytes()).hexdigest()),
                (args.split.name, hashlib.sha256(args.split.read_bytes()).hexdigest())]
    ranked = rank_cells(runs, refs, checklist['criteria'], set(split['tune']) | set(split['holdout']))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.queue.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render_report(ranked, checklist, sources, args.limit), encoding='utf-8')
    args.queue.write_text(json.dumps(make_queue(ranked, checklist, args.limit), ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'disputed_cells': len(ranked), 'queue_cells': min(len(ranked), args.limit),
                      'queue_dialogues': len({c['conv_id'] for c in ranked[:args.limit]})}))


if __name__ == '__main__':
    main()
