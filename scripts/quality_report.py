"""Reproduce aggregate quality metrics from existing JSONL artifacts, offline only.

No evaluator/provider imports, raw-text output, or inference. Example:
  python scripts/quality_report.py --source-root /path/to/w-int \
    --legacy-root /path/to/w-m1 --references-root /path/to/agents/reports
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from fractions import Fraction
import hashlib
import json
import math
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
SEED = 20261108
REPETITIONS = 10000
RUNS = [
    ("or-hold-google-gemini-4-argon-g9", "Gemini 4 Argon", "gemini-4-argon-g9-holdout", False),
    ("or-hold-cascade-g9", "Каскад Haiku → Gemini", "cascade-haiku-gemini-g9-holdout", False),
    ("or-hold-routing-g9", "Маршрутизация: чаты Haiku, звонки Gemini", "routing-haiku-gemini-g9-holdout", False),
    ("or-hold-anthropic-claude-sonnet-5-5-g9", "Sonnet 5.5 API", "sonnet-api-g9-holdout", False),
    ("or-hold-qwen-qwen3-8-27b-g9", "Qwen 3.8 27B API", "qwen-api-g9-holdout", False),
    ("or-hold-deepseek-deepseek-v4-pro-g9", "DeepSeek V4 Pro", "deepseek-api-g9-holdout", False),
    ("or-hold-openai-gpt-6-1-terra-g9", "GPT 6.1 Terra", "terra-api-g9-holdout", False),
    ("hk-g9-all", "Haiku g9 (поздний)", "haiku-g9-holdout", False),
    ("hk-g10-all", "Haiku g10", "haiku-g10-holdout", False),
    ("m1-sonnet-g9-all", "Sonnet 5.5 CLI", "sonnet-cli-g9-holdout", True),
    ("m1-hold-haiku-m1-c4", "Haiku g9 (ранний)", "haiku-g9-earlier-holdout", True),
    ("local-m4-qwen25-7b-hold-g9", "Локально: qwen2.5 7B (ноутбук M4, офлайн)", "local-qwen25-7b-m4-holdout", False),
]


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def json_text(value) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"


def normalized(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) else None
    value = str(value).strip().lower()
    aliases = {"да": "yes", "true": "yes", "нет": "no", "false": "no", "n/a": "na", "н/п": "na"}
    value = aliases.get(value, value)
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except ValueError:
        return value


def valid_answer(answer, criterion):
    if answer == "na":
        return bool(criterion.get("na_allowed"))
    if criterion["answer_type"] == "binary":
        return answer in ("yes", "no")
    return isinstance(answer, (int, float)) and answer in criterion["scale"]


def load_jsonl(path: Path):
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        if line.strip():
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError(f"{path.name}:{number}: expected object")
            yield row


def load_reference(path, criteria):
    out = {}
    for row in load_jsonl(path):
        key = (row["conv_id"], row["criterion_id"])
        if key in out:
            raise ValueError(f"{path.name}: duplicate reference cell")
        answer = normalized(row["answer"])
        if key[1] not in criteria or not valid_answer(answer, criteria[key[1]]):
            raise ValueError(f"{path.name}: invalid reference answer")
        out[key] = answer
    return out


FINAL_REJECTION_PREFIXES = ("ответ модели не прошёл проверку:", "оценка не получена:", "схема criterioneval:")


def final_validation_failed(entry):
    """True when the stored final answer was rejected by validation (strict review M1).

    Slim eval artifacts (scripts/eval_run.py) drop top-level ``errors`` but keep the final
    ``review_reasons``/``evidence``. Only explicit final-rejection markers count: ordinary
    review flags (low confidence, keyword-rule disagreement, critical hit, call scales) do not.
    Reduced to a boolean before the texts are discarded, so no quote/explanation is retained.
    """
    if entry.get("errors") or entry.get("final_validation_failed") is True:
        return True
    status = entry.get("validation_status")
    if isinstance(status, str) and status.strip().casefold() in ("rejected", "failed", "invalid"):
        return True
    reasons = entry.get("review_reasons") or []
    if isinstance(reasons, str):
        reasons = [reasons]
    if any(isinstance(reason, str) and reason.strip().casefold().startswith(FINAL_REJECTION_PREFIXES) for reason in reasons):
        return True
    return any(isinstance(item, dict) and item.get("valid") is False for item in (entry.get("evidence") or []))


def load_run(path):
    meta, rows = None, {}
    for row in load_jsonl(path):
        if "_meta" in row:
            if meta is not None:
                raise ValueError(f"{path.name}: duplicate metadata")
            meta = row["_meta"]
            continue
        cid = row["conv_id"]
        if cid in rows:
            raise ValueError(f"{path.name}: duplicate dialogue")
        evals = {}
        for evaluation in row.get("evals", []):
            criterion_id = evaluation["criterion_id"]
            if criterion_id in evals:
                raise ValueError(f"{path.name}: duplicate prediction cell")
            # Retain only measurement fields; discard quotes, explanations and message IDs.
            evals[criterion_id] = {key: evaluation.get(key) for key in (
                "answer", "model", "tokens_in", "tokens_out", "calls", "prompt_version", "errors", "cost_kzt_full", "needs_review"
            )}
            evals[criterion_id]["final_validation_failed"] = final_validation_failed(evaluation)
        rows[cid] = {key: row.get(key) for key in ("language", "channel", "wall_s", "cost_kzt", "cost_kzt_full")}
        rows[cid]["evals"] = evals
    if meta is None:
        raise ValueError(f"{path.name}: missing metadata")
    return {"meta": meta, "rows": rows, "artifact": path.name, "sha256": digest(path)}


STRONG_RUN = "or-hold-google-gemini-4-argon-g9"
STAGED_RUNS = ("or-hold-cascade-g9", "or-hold-routing-g9")


def reprice_replayed_strong_stage(runs, config):
    """Cascade / routing took the Gemini stage of some dialogues from the LLM cache of the Gemini bake-off, and the
    first Gemini rows of that bake-off were stored with price 0 (the OpenRouter price table was added mid-run).
    A correctly priced escalation can never cost less than the same Gemini call alone, so a stored dialogue total below
    the Gemini run's own token cost for that dialogue means the Gemini stage was replayed at price 0: add that cost
    (same cached call, same tokens, one model, exact price). Haiku stages keep their stored price."""
    strong = runs.get(STRONG_RUN)
    fx = config.get("usd_kzt")
    price = price_for("openai:google/gemini-4-argon", config["pricing_usd_per_mtok"])
    if not strong or not price or not fx:
        return
    for name in STAGED_RUNS:
        run = runs.get(name)
        if not run:
            continue
        fixed = []
        for cid, row in run["rows"].items():
            entries = row.get("evals", {})
            models = [e.get("model") or "" for e in entries.values()]
            if not any("→" in m or "маршрут: звонок" in m for m in models) or cid not in strong["rows"]:
                continue
            g = strong["rows"][cid]["evals"].values()
            if not all(is_nonnegative(e.get("tokens_in")) and is_nonnegative(e.get("tokens_out")) for e in g):
                continue
            gem_kzt = sum(e["tokens_in"] * price["in"] + e["tokens_out"] * price["out"] for e in g) * fx / 1e6
            stored = sum(e.get("cost_kzt_full") or 0 for e in entries.values())
            if stored + 1e-6 < gem_kzt:
                share = gem_kzt / len(entries)
                for e in entries.values():
                    e["cost_kzt_full"] = (e.get("cost_kzt_full") or 0) + share
                row["cost_kzt_full"] = (row.get("cost_kzt_full") or 0) + gem_kzt
                fixed.append(cid)
        run["stage_repriced_dialogues"] = fixed


def nearest_rank(values, probability):
    if not 0 < probability <= 1:
        raise ValueError("probability must be in (0, 1]")
    values = sorted(values)
    if not values:
        return None
    if any(not isinstance(x, (int, float)) or not math.isfinite(x) or x < 0 for x in values):
        raise ValueError("invalid latency")
    return values[math.ceil(probability * len(values)) - 1]


def cluster_ci(numerators, denominators, repetitions=REPETITIONS, seed=SEED):
    """Percentile interval for a ratio after resampling whole dialogues.

    Labels missing inside a sampled dialogue remain missing. Zero-denominator
    replicates cannot define a ratio; their count is explicitly reported.
    """
    if len(numerators) != len(denominators) or repetitions <= 0:
        raise ValueError("invalid bootstrap dimensions")
    n = len(numerators)
    result = {"lower": None, "upper": None, "level": 0.95,
              "method": "dialogue_cluster_percentile", "repetitions": repetitions,
              "seed": seed, "unit": "dialogue", "valid_repetitions": 0,
              "effective_dialogues": sum(denominator > 0 for denominator in denominators)}
    if not n or sum(denominators) == 0:
        result["reason"] = "no_labelled_cells"
        return result
    if result["effective_dialogues"] < 2:
        result["reason"] = "insufficient_clusters"
        return result
    draws = np.random.default_rng(seed).integers(0, n, size=(repetitions, n))
    num = np.asarray(numerators, dtype=float)[draws].sum(axis=1)
    den = np.asarray(denominators, dtype=float)[draws].sum(axis=1)
    values = num[den > 0] / den[den > 0]
    lower, upper = np.quantile(values, [0.025, 0.975], method="linear")
    result.update(lower=round(float(lower), 8), upper=round(float(upper), 8), valid_repetitions=len(values))
    return result


def mcnemar_exact(win, loss):
    if not isinstance(win, int) or not isinstance(loss, int) or min(win, loss) < 0:
        raise ValueError("discordances must be nonnegative integers")
    n = win + loss
    if n == 0:
        return 1.0
    return min(1.0, 2 * sum(math.comb(n, k) for k in range(min(win, loss) + 1)) / 2 ** n)


def prediction(run, conv_id, criterion):
    entry = run["rows"].get(conv_id, {}).get("evals", {}).get(criterion["id"])
    if entry is None:
        return None, "missing"
    answer = normalized(entry.get("answer"))
    if final_validation_failed(entry) or not valid_answer(answer, criterion):
        return None, "errors"
    return answer, "valid"


def measured_cells(run, reference, ids, criteria):
    cells = []
    for conv_id in ids:
        for criterion_id, criterion in criteria.items():
            gold = reference.get((conv_id, criterion_id))
            answer, state = prediction(run, conv_id, criterion)
            bucket = "unlabelled" if gold is None else state if state != "valid" else "matched" if answer == gold else "disagreed"
            cells.append((conv_id, criterion_id, bucket))
    return cells


def aggregate(cells):
    buckets = Counter(cell[2] for cell in cells)
    counts = {"matched": buckets["matched"], "total": len(cells), "missing": buckets["missing"],
              "errors": buckets["errors"], "unlabelled": buckets["unlabelled"]}
    ids = sorted({cell[0] for cell in cells})
    grouped = {cid: Counter(cell[2] for cell in cells if cell[0] == cid) for cid in ids}
    matches = [grouped[cid]["matched"] for cid in ids]
    totals = [sum(grouped[cid].values()) for cid in ids]
    labelled = [total - grouped[cid]["unlabelled"] for cid, total in zip(ids, totals)]
    return {"counts": counts, "dialogues": len(ids), "agreement_ci": cluster_ci(matches, labelled),
            "all_cells_ci": cluster_ci(matches, totals)}


def total_score(answers, criteria):
    numerator = denominator = Fraction(0)
    for cid, criterion in criteria.items():
        weight = Fraction(str(criterion.get("weight", 0)))
        if not weight:
            continue
        answer = answers.get(cid)
        if not valid_answer(answer, criterion):
            return None
        if answer == "na":
            continue
        denominator += weight
        if criterion["answer_type"] == "binary":
            numerator += weight if answer == "yes" else 0
        else:
            numerator += weight * Fraction(str(answer)) / max(Fraction(str(x)) for x in criterion["scale"])
    if not denominator:
        return None
    exact = 100 * numerator / denominator
    return float((Decimal(exact.numerator) / Decimal(exact.denominator)).quantize(Decimal(".01"), rounding=ROUND_HALF_UP))


def score_mae(run, reference, ids, criteria):
    errors = []
    for cid in ids:
        # Require all checklist cells, including the separate critical flag, to be paired.
        predicted = {key: prediction(run, cid, criterion)[0] for key, criterion in criteria.items()}
        gold = {key: reference.get((cid, key)) for key in criteria}
        if any(predicted[key] is None or gold[key] is None for key in criteria):
            continue
        actual, target = total_score(predicted, criteria), total_score(gold, criteria)
        if actual is not None and target is not None:
            errors.append(abs(actual - target))
    return {"value": round(sum(errors) / len(errors), 6) if errors else None, "count": len(errors)}


def price_for(model, prices):
    if not isinstance(model, str):
        return None
    model = model.removesuffix(" (каскад: без эскалации)").removesuffix(" (маршрут: звонок)")
    if "→" in model or "фолбэк" in model:
        return None
    return prices.get(model.split(":", 1)[-1])


def token_price_metrics(run, ids, criteria, config):
    """Allocated token fields are summed once; dialogue call totals are not added.

    Escalated slim artifacts merge cheap+strong tokens without stage attribution.
    They cannot support an exact total, even when an old reported cost is present.
    """
    prices = config["pricing_usd_per_mtok"]
    amounts, reasons, bounds = [], Counter(), []
    for cid in ids:
        entries = run["rows"].get(cid, {}).get("evals", {})
        amount, low, high, known, bounded = 0.0, 0.0, 0.0, True, True
        if set(entries) != set(criteria):
            reasons["incomplete_token_coverage"] += 1
            continue
        for entry in entries.values():
            ti, to = entry.get("tokens_in"), entry.get("tokens_out")
            if any(not isinstance(x, (int, float)) or not math.isfinite(x) or x < 0 for x in (ti, to)):
                known = bounded = False
                reasons["missing_token_counts"] += 1
                break
            model = entry.get("model") or ""
            price = price_for(model, prices)
            if price is not None:
                value = (ti * price["in"] + to * price["out"]) / 1_000_000
                amount += value
                low += value
                high += value
            elif "→" in model:
                known = False
                stage_prices = [price_for(part.strip(), prices) for part in model.split("→")]
                if all(stage_prices):
                    low += (ti * min(p["in"] for p in stage_prices) + to * min(p["out"] for p in stage_prices)) / 1_000_000
                    high += (ti * max(p["in"] for p in stage_prices) + to * max(p["out"] for p in stage_prices)) / 1_000_000
                else:
                    bounded = False
            else:
                known = bounded = False
        if known:
            amounts.append(amount)
        else:
            reasons["combined_stage_tokens" if any("→" in (e.get("model") or "") for e in entries.values()) else "unpriced_or_missing_usage"] += 1
        if bounded:
            bounds.append((low, high))
    complete = len(amounts) == len(ids) and bool(ids)
    cost = {"amount": round(sum(amounts), 8) if complete else None, "currency": "USD" if complete else None,
            "count": len(ids) if complete else 0, "basis": "estimated" if complete else "unknown"}
    audit = {"known_dialogues": len(amounts), "unknown_dialogues": len(ids) - len(amounts), "total_dialogues": len(ids),
             "reasons": sorted(reasons), "reason_counts": dict(sorted(reasons.items())),
             "lower_bound_usd": round(sum(x[0] for x in bounds), 8) if len(bounds) == len(ids) else None,
             "upper_bound_usd": round(sum(x[1] for x in bounds), 8) if len(bounds) == len(ids) else None,
             "basis": "allocated input/output tokens × exact config model prices; uncached input list price",
             "allocation_note": "Per-criterion allocations summed once; integer truncation can omit a few tokens. CLI estimates are not subscription invoices. Cache state is unknown."}
    return cost, audit


def is_nonnegative(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def cost_metrics(run, ids, criteria, config):
    """Read per-call full cost already allocated/summed by the evaluator.

    Unlike merged tokens, this field retains the price applied to each stage.
    It is reported usage, not an invoice or proof of historical price coverage.
    """
    fx = config.get("usd_kzt")
    if not is_nonnegative(fx) or fx <= 0:
        raise ValueError("config.usd_kzt must be a finite positive conversion rate")
    amounts, reasons = [], Counter()
    zero_paid_dialogues = compound_dialogues = unpriced_dialogues = repriced_entries = 0
    prices = config["pricing_usd_per_mtok"]
    for cid in ids:
        row = run["rows"].get(cid, {})
        entries = row.get("evals", {})
        if set(entries) != set(criteria) or any(not is_nonnegative(e.get("cost_kzt_full")) for e in entries.values()):
            reasons["missing_reported_full_cost"] += 1
            continue
        amount = sum(entry["cost_kzt_full"] for entry in entries.values())
        summary = row.get("cost_kzt_full")
        if summary is not None and (not is_nonnegative(summary) or abs(summary - amount) > .00011):
            reasons["reported_full_cost_mismatch"] += 1
            continue
        compound = any("→" in (entry.get("model") or "") for entry in entries.values())
        compound_dialogues += compound
        anomaly = unpriced = False
        for entry in entries.values():
            price = price_for(entry.get("model"), prices)
            if price is None:
                unpriced = True
                continue
            ti, to = entry.get("tokens_in"), entry.get("tokens_out")
            if is_nonnegative(ti) and is_nonnegative(to):
                expected = (ti * price["in"] + to * price["out"]) * fx / 1e6
                if entry["cost_kzt_full"] == 0 and expected > .0001:
                    if compound:
                        anomaly = True
                    else:  # one model answered this call: its own tokens x its own price is exact, not a guess
                        amount += expected
                        repriced_entries += 1
        amounts.append(amount)
        zero_paid_dialogues += anomaly
        unpriced_dialogues += unpriced and not compound
    if zero_paid_dialogues:
        reasons["zero_full_cost_with_paid_tokens"] = zero_paid_dialogues
    if compound_dialogues:
        reasons["stage_token_attribution_unavailable"] = compound_dialogues
    if unpriced_dialogues:
        reasons["historical_price_key_unverified"] = unpriced_dialogues
    complete = len(amounts) == len(ids) and bool(ids)
    total_kzt = round(sum(amounts), 8) if complete else None
    cost = {"amount": round(total_kzt / fx, 8) if complete else None, "currency": "USD" if complete else None,
            "count": len(ids) if complete else 0, "basis": "reported" if complete else "unknown"}
    pricing_complete = False if zero_paid_dialogues or not complete else None if compound_dialogues or unpriced_dialogues else True
    notes = ["Сохранённая оценка usage, не счёт; исторический прайс и курс в JSONL не закреплены."]
    if run.get("stage_repriced_dialogues"):
        notes.append(f"{len(run['stage_repriced_dialogues'])} диалогов: стадия Gemini взята из кэша с нулевой записанной ценой; добавлена цена того же вызова Gemini (токены × цена).")
    if repriced_entries:
        notes.append(f"{repriced_entries} оценок записаны с нулевой ценой (таблица цен OpenRouter добавлена во время прогона); пересчитаны по токенам этой же единственной модели × её цене.")
    if zero_paid_dialogues:
        notes.append(f"У {zero_paid_dialogues} диалогов сохранена нулевая цена при ненулевых токенах платной модели: нижняя оценка, неполный учёт.")
    if compound_dialogues:
        notes.append("cost_kzt_full суммирует стадии; разбивка токенов и историческое покрытие цен по стадиям не проверяются из slim-лога.")
    if unpriced_dialogues:
        notes.append("Точный ценовой ключ CLI отсутствует в текущей таблице; сохранённый usage не перепрайсован догадкой.")
    token_estimate, _ = token_price_metrics(run, ids, criteria, config)
    charged = [run["rows"].get(cid, {}).get("cost_kzt") for cid in ids]
    replay_count = sum(is_nonnegative(run["rows"].get(cid, {}).get("cost_kzt_full")) and
                       run["rows"].get(cid, {}).get("cost_kzt_full") > 0 and
                       run["rows"].get(cid, {}).get("cost_kzt") == 0 for cid in ids)
    audit = {"known_dialogues": len(amounts), "unknown_dialogues": len(ids) - len(amounts), "total_dialogues": len(ids),
             "reasons": sorted(reasons), "reason_counts": dict(sorted(reasons.items())), "lower_bound_usd": None,
             "upper_bound_usd": None, "method": "stored_full_cost", "recorded_total_kzt": total_kzt,
             "recorded_charged_total_kzt": round(sum(charged), 8) if all(is_nonnegative(value) for value in charged) else None,
             "zero_charged_positive_full_dialogues": replay_count,
             "pricing_complete": pricing_complete, "comparable": pricing_complete is True,
             "zero_cost_paid_usage_dialogues": zero_paid_dialogues, "combined_stage_dialogues": compound_dialogues,
             "repriced_single_model_entries": repriced_entries,
             "repriced_replayed_strong_stage_dialogues": list(run.get("stage_repriced_dialogues") or []),
             "lower_bound": zero_paid_dialogues > 0,
             "basis": "стоимость — по ценам моделей в каждом вызове; токены по моделям не разделены",
             "allocation_note": "Sum per-evaluation cost_kzt_full once; row total is checked, not added. _cascade already sums stage costs.",
             "coverage_note": " ".join(notes), "currency_conversion_assumed": True,
             "currency_conversion": {"usd_kzt": fx, "source": "config/models.json:usd_kzt", "assumption": True},
             "token_price_estimate": token_estimate,
             "token_price_note": "Separate current-price scenario; only unambiguous single-model tokens. Never compare it to another profile's historical reported cost as a verified saving."}
    return cost, audit


def review_metrics(run, ids, criteria):
    flagged = known = 0
    for cid in ids:
        entries = run["rows"].get(cid, {}).get("evals", {})
        values = [entry.get("needs_review") for entry in entries.values()]
        if any(value is True for value in values):
            flagged += 1
            known += 1
        elif set(entries) == set(criteria) and all(value is False for value in values):
            known += 1
    return {"review_count": flagged, "review_known_dialogues": known,
            "review_fraction": round(flagged / len(ids), 8) if known == len(ids) and ids else None,
            "review_basis": "Any final criterion needs_review=true; escalation is not a review flag."}


FRESH_OFFLINE_RUNS = {"local-m4-qwen25-7b-hold-g9"}
FRESH_OFFLINE_NOTE = "Время свежего офлайн-прогона на MacBook Air M4 (живое время, без кэша)."


def cache_state(run, ids):
    """Use stored charged/full cost semantics only; zero/zero is ambiguous.
    Runs measured fresh offline on a local model are fresh by protocol (cost is 0, so it cannot be inferred)."""
    if run.get("meta", {}).get("name") in FRESH_OFFLINE_RUNS:
        return "fresh"
    states = []
    for cid in ids:
        row = run["rows"].get(cid, {})
        charged, full = row.get("cost_kzt"), row.get("cost_kzt_full")
        if not is_nonnegative(charged) or not is_nonnegative(full) or full <= 0:
            states.append("unknown")
        elif charged == 0:
            states.append("cached")
        elif abs(charged - full) <= .00011:
            states.append("fresh")
        elif charged < full:
            states.append("mixed")
        else:
            states.append("unknown")
    if not states or "unknown" in states:
        return "unknown"
    return states[0] if len(set(states)) == 1 else "mixed"


def economics_metrics(run, ids, criteria, config):
    cost, audit = cost_metrics(run, ids, criteria, config)
    walls = [run["rows"].get(cid, {}).get("wall_s") for cid in ids]
    valid_walls = [wall for wall in walls if is_nonnegative(wall)]
    mean = sum(valid_walls) / len(ids) if ids and len(valid_walls) == len(ids) else None
    cache = cache_state(run, ids)
    projection_allowed = cache == "fresh" and mean is not None
    kzt = audit["recorded_total_kzt"] / len(ids) if ids and audit["recorded_total_kzt"] is not None else None
    usd = cost["amount"] / cost["count"] if cost["count"] else None
    pairs = len(criteria)
    provenance = {key: audit[key] for key in ("method", "comparable", "pricing_complete", "zero_cost_paid_usage_dialogues",
                  "coverage_note", "currency_conversion_assumed", "lower_bound")}
    return {"dialogues": len(ids), "criteria_per_dialogue": pairs,
            "cost_per_dialogue_kzt": round(kzt, 8) if kzt is not None else None,
            "cost_per_dialogue_usd": round(usd, 8) if usd is not None else None,
            "cost_per_pair_kzt": round(kzt / pairs, 8) if kzt is not None else None,
            "cost_per_pair_usd": round(usd / pairs, 10) if usd is not None else None,
            "mean_seconds_per_dialogue": round(mean, 8) if projection_allowed else None,
            "mean_seconds_per_pair": round(mean / pairs, 8) if projection_allowed else None,
            "recorded_mean_seconds_per_dialogue": round(mean, 8) if mean is not None else None,
            "latency_provenance": {"cache_state": cache, "projection_allowed": projection_allowed,
                                   "basis": "Inferred from charged cost_kzt versus cost_kzt_full and llm.py cache branch; no explicit cache flag in slim artifact.",
                                   "note": FRESH_OFFLINE_NOTE if run.get("meta", {}).get("name") in FRESH_OFFLINE_RUNS else "Время записанного CLI-прогона; повтор кэша, смешанный или неизвестный кэш не измеряют живую скорость. Даже fresh-сумма даёт серийную оценку, не throughput."},
            "p50_seconds": nearest_rank(valid_walls, .5), "p95_seconds": nearest_rank(valid_walls, .95),
            "timed_dialogues": len(valid_walls), **review_metrics(run, ids, criteria),
            "currency_fx": audit["currency_conversion"], "cost_provenance": provenance,
            "pair_cost_note": "Amortized per planned criterion, not marginal cost: fast-mode evaluation can call the whole checklist."}


def model_usage(run, ids):
    known = gemini = escalated = routed_calls = 0
    for cid in ids:
        entries = run["rows"].get(cid, {}).get("evals", {})
        models = [entry.get("model") for entry in entries.values()]
        if not models or not all(isinstance(model, str) and model for model in models):
            continue
        known += 1
        gemini += any("google/gemini-4-argon" in model for model in models)
        escalated += any("→" in model for model in models)
        routed_calls += any("маршрут: звонок" in model for model in models)
    return {"gemini_dialogues": gemini, "escalated_dialogues": escalated, "direct_routed_calls": routed_calls,
            "known_dialogues": known, "total_dialogues": len(ids),
            "basis": "Saved final evaluation model labels; not needs_review and not complete provider-call telemetry."}


def source_kind(meta):
    """OpenRouter / OpenAI-compatible runs are API calls; claude_cli runs are CLI. (scripts/eval_run.py drives both.)"""
    model = str((meta.get("preset") or {}).get("model") or "")
    return "api" if model.startswith(("openai:", "anthropic:")) else "cli"


def make_report(run, label, report_id, reference, reference_id, reference_hash, ids, criteria, checklist, metadata, config):
    cells = measured_cells(run, reference, ids, criteria)
    aggregate_values = aggregate(cells)
    meta = run["meta"]
    started = datetime.fromisoformat(meta["started"])
    measured_at = started.isoformat() + ("+05:00" if started.tzinfo is None else "")
    wall = [run["rows"][cid]["wall_s"] for cid in ids if cid in run["rows"] and isinstance(run["rows"][cid].get("wall_s"), (int, float))]
    cost, audit = cost_metrics(run, ids, criteria, config)
    report = {"id": report_id if reference_id == "silver" else f"{report_id}-{reference_id}",
              "run_id": meta["name"], "measured_at": measured_at,
              "source": {"kind": source_kind(meta), "label": label, "artifact": run["artifact"], "sha256": run["sha256"]},
              "model": {"requested": meta["preset"]["model"], "resolved": None}, "mode": meta["mode"],
              "guidance_version": meta.get("guidance_version") or "unknown", "checklist_version": str(checklist["version"]),
              "cache": cache_state(run, ids),
              "cache_provenance": "Inferred from cost_kzt=0<cost_kzt_full (cached), 0<cost_kzt<cost_kzt_full (mixed), equal positive costs (fresh); zero/zero is unknown. Semantics from llm.py cache branch; slim log has no explicit cache flag.",
              "reference": {"id": reference_id, "type": "machine", "cells": len(reference),
              "sha256": reference_hash, "description": "Машинный эталон DEV; независимость от настройки модели не установлена."},
              "sample": {"dataset": "DEV", "split": "holdout", "dialogues": len(ids), "dev_reused": True,
              "used_for_tuning": True, "untouched": False, "source_dialogues": len(run["rows"])},
              "denominator": "all_cells", "counts": aggregate_values["counts"],
              "agreement_ci": aggregate_values["agreement_ci"], "all_cells_ci": aggregate_values["all_cells_ci"],
              "score_mae": score_mae(run, reference, ids, criteria),
              "latency": {"p50_seconds": nearest_rank(wall, .5), "p95_seconds": nearest_rank(wall, .95),
                          "mean_seconds": round(sum(wall) / len(ids), 8) if len(wall) == len(ids) and ids else None,
                          "note": (FRESH_OFFLINE_NOTE if meta["name"] in FRESH_OFFLINE_RUNS else
                                   "Сквозное время вызова API на диалог; при cached это время повтора кэша, живое время не измерено."
                                   if source_kind(meta) == "api" else
                                   "Время записанного CLI-прогона; при cached это время повтора кэша, живое время не измерено."),
                          "count": len(wall), "scope": "api_e2e" if source_kind(meta) == "api" else "cli_wall", "method": "nearest_rank"},
              "cost": cost, "cost_audit": audit, "economics": economics_metrics(run, ids, criteria, config),
              "model_usage": model_usage(run, ids),
              "criteria": [{"id": cid, "label": criterion.get("title", cid), **aggregate([c for c in cells if c[1] == cid])}
                           for cid, criterion in criteria.items()]}
    for output, source in (("languages", "language"), ("channels", "channel")):
        groups = sorted({metadata.get(cid, {}).get(source) or "unknown" for cid in ids})
        report[output] = [{"id": group, **aggregate([cell for cell in cells if (metadata.get(cell[0], {}).get(source) or "unknown") == group])}
                          for group in groups]
    channel_groups = {}
    for group in report["channels"]:
        key = "call" if "call" in group["id"].lower() else "chat" if "chat" in group["id"].lower() else "unknown"
        channel_groups.setdefault(key, []).extend(cid for cid in ids if (metadata.get(cid, {}).get("channel") or "unknown") == group["id"])
    report["economics"]["channels"] = {key: economics_metrics(run, sorted(cids), criteria, config) for key, cids in sorted(channel_groups.items())}
    return report


def compare_runs(run, baseline, reference, ids, criteria, report_id, baseline_id, reference_id):
    win = loss = compared = 0
    nums, dens = [], []
    incomplete = {"model": 0, "baseline": 0}
    for conv_id in ids:
        num = den = 0
        for cid, criterion in criteria.items():
            gold = reference.get((conv_id, cid))
            if gold is None:
                continue
            answer, state = prediction(run, conv_id, criterion)
            base, base_state = prediction(baseline, conv_id, criterion)
            incomplete["model"] += state != "valid"
            incomplete["baseline"] += base_state != "valid"
            diff = int(answer == gold) - int(base == gold)
            win += diff == 1
            loss += diff == -1
            num += diff
            den += 1
        nums.append(num)
        dens.append(den)
        compared += den
    ci = cluster_ci(nums, dens)
    p_value = mcnemar_exact(win, loss) if compared else None
    significant = p_value is not None and p_value < .05 and ci["lower"] is not None and (ci["lower"] > 0 or ci["upper"] < 0)
    return {"report_id": report_id, "baseline_report_id": baseline_id, "reference_id": reference_id,
            "win": win, "loss": loss, "compared": compared, "dialogues": len(ids),
            "delta": round((win - loss) / compared, 8) if compared else None, "delta_ci": ci,
            "p_value": round(p_value, 10) if p_value is not None else None, "significant": significant,
            "conclusion": "difference_detected" if significant else "within_noise", "exploratory": True,
            "missing_or_error_cells": incomplete,
            "caveat": "Unadjusted exact cell-level McNemar assumes independent discordances; dialogue cells are dependent. Cluster CI is primary. Non-significance is not equivalence; repeated DEV comparisons are exploratory."}


def configuration(run):
    meta = run["meta"]
    config = {key: meta.get(key) for key in ("preset", "mode", "readings", "template", "guidance_version")}
    prompt_versions = {}
    for row in run["rows"].values():
        for cid, entry in row["evals"].items():
            if entry.get("prompt_version"):
                prompt_versions.setdefault(cid, set()).add(entry["prompt_version"])
    config["criterion_prompt_versions"] = {cid: sorted(values) for cid, values in sorted(prompt_versions.items())}
    return config


def configuration_complete(run):
    meta = run["meta"]
    fields = ("preset", "mode", "readings", "template", "guidance_version")
    return (all(key in meta and meta[key] is not None for key in fields)
            and isinstance(meta["preset"], dict) and bool(meta["preset"].get("model"))
            and bool(run["rows"]) and all(bool(row["evals"]) and all(entry.get("prompt_version")
                                        for entry in row["evals"].values()) for row in run["rows"].values()))


def stability(left, right, ids, criteria, label):
    matched = total = missing = 0
    nums, dens = [], []
    for conv_id in ids:
        num = den = 0
        for criterion in criteria.values():
            a, sa = prediction(left, conv_id, criterion)
            b, sb = prediction(right, conv_id, criterion)
            if sa != "valid" or sb != "valid":
                missing += 1
                continue
            num += a == b
            den += 1
        nums.append(num)
        dens.append(den)
        matched += num
        total += den
    left_config, right_config = configuration(left), configuration(right)
    complete = configuration_complete(left) and configuration_complete(right)
    same = complete and left_config == right_config
    return {"id": label, "left_run_id": left["meta"]["name"], "right_run_id": right["meta"]["name"],
            "matched": matched, "total": total, "planned_cells": len(ids) * len(criteria), "missing_or_error": missing,
            "dialogues": len(ids), "agreement_ci": cluster_ci(nums, dens), "same_configuration": same,
            "left_configuration_sha256": hashlib.sha256(canonical(left_config).encode()).hexdigest(),
            "right_configuration_sha256": hashlib.sha256(canonical(right_config).encode()).hexdigest(),
            "configuration_note": ("Recorded preset, guidance, template, readings and criterion prompt versions match; resolved model and cache state unknown. Not proof of independent stochastic repeats." if same else
                                   "Configuration metadata incomplete; same-configuration reproducibility is not established." if not complete else
                                   "Different recorded configurations. This measures cross-configuration agreement, not same-configuration reproducibility.")}


SUBSAMPLE_RUN = "or-kzcalls-gemini-g9"


def subsample_reports(source_root, holdout_run, refs, criteria, split, metadata):
    """All KZ chats and all calls of DEV from two Gemini g9 runs (the KZ/calls run plus the holdout run).

    On overlap (dialogues present in both runs) the holdout run row is used. Optional: absent run file -> [].
    """
    path = source_root / "results/eval_runs" / (SUBSAMPLE_RUN + ".jsonl")
    if not path.exists():
        return [], None
    extra = load_run(path)
    tune = set(split.get("tune", []))
    groups = (("kz-all", "Все KZ-чаты DEV", lambda m: m["language"] == "KZ" and "call" not in m["channel"].lower(),
               "tune",
               "Правила настраивали на {tune} из {n} KZ-чатов (tune); Gemini на них не настраивался. Это не независимая выборка."),
              ("calls-all", "Все звонки DEV", lambda m: "call" in m["channel"].lower(), "calls",
               "Звонков мало ({n}); модель завышает шкалы C4/C6 → такие оценки отправляются на проверку. Вывода о точности на звонках не делаем."))
    out = []
    for gid, label, pick, kind, note in groups:
        rows = {}
        for run in (extra, holdout_run):  # later wins: holdout run row preferred on overlap
            for cid, row in run["rows"].items():
                m = metadata.get(cid) or {key: row.get(key) or "unknown" for key in ("language", "channel")}
                if pick(m):
                    rows[cid] = row
        ids = sorted(rows)
        merged = {"rows": rows}
        tuned = sum(cid in tune for cid in ids)
        item = {"id": gid, "label": label, "dialogues": len(ids), "tune_dialogues": tuned,
                "model": "Gemini 4 Argon", "rules": "g9", "sample_kind": kind,
                "note": note.format(tune=tuned, n=len(ids)),
                "source": {"artifacts": [SUBSAMPLE_RUN + ".jsonl", RUNS[0][0] + ".jsonl"], "sha256": [digest(path)]},
                "references": {}}
        for ref_id, ref in refs.items():
            agg = aggregate(measured_cells(merged, ref, ids, criteria))
            c = agg["counts"]
            item["references"][ref_id] = {"matched": c["matched"], "labelled": c["total"] - c["unlabelled"],
                                          "counts": c, "agreement_ci": agg["agreement_ci"]}
        out.append(item)
    return out, path


def generate(source_root, legacy_root, references_root):
    input_paths = [source_root / p for p in ("data/checklists/v1.json", "eval/split.json", "config/models.json")]
    input_paths += [legacy_root / "eval/gold/silver.jsonl", references_root / "gold_v2.jsonl", references_root / "gold_v2_full.jsonl"]
    input_paths += [(legacy_root if old else source_root) / "results/eval_runs" / (name + ".jsonl") for name, _, _, old in RUNS]
    initial_hashes = {path: digest(path) for path in input_paths}
    checklist_path = source_root / "data/checklists/v1.json"
    checklist = json.loads(checklist_path.read_text())
    criteria = {criterion["id"]: criterion for criterion in checklist["criteria"]}
    if len(criteria) != len(checklist["criteria"]):
        raise ValueError("duplicate checklist criterion")
    split_path, config_path = source_root / "eval/split.json", source_root / "config/models.json"
    split = json.loads(split_path.read_text())
    ids = sorted(split["holdout"])
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate planned dialogue")
    config = json.loads(config_path.read_text())
    references = {"silver": legacy_root / "eval/gold/silver.jsonl",
                  "gold_v2": references_root / "gold_v2.jsonl", "gold_v2_full": references_root / "gold_v2_full.jsonl"}
    refs = {name: load_reference(path, criteria) for name, path in references.items()}
    runs = {name: load_run((legacy_root if legacy else source_root) / "results/eval_runs" / (name + ".jsonl"))
            for name, _, _, legacy in RUNS}
    reprice_replayed_strong_stage(runs, config)
    metadata = {}
    for run in runs.values():
        for cid, row in run["rows"].items():
            item = {key: row.get(key) or "unknown" for key in ("language", "channel")}
            if cid in metadata and metadata[cid] != item:
                raise ValueError("conflicting dialogue language/channel metadata")
            metadata[cid] = item
    subsamples, subsample_path = subsample_reports(source_root, runs[RUNS[0][0]], refs, criteria, split, metadata)
    if subsample_path is not None:
        initial_hashes[subsample_path] = digest(subsample_path)
    reports, comparisons = [], []
    for ref_id, reference in refs.items():
        for name, label, report_id, _ in RUNS:
            report = make_report(runs[name], label, report_id, reference, ref_id, digest(references[ref_id]),
                                 ids, criteria, checklist, metadata, config)
            reports.append(report)
            if name != RUNS[0][0]:
                baseline_id = RUNS[0][2] + ("" if ref_id == "silver" else "-" + ref_id)
                comparisons.append(compare_runs(runs[name], runs[RUNS[0][0]], reference, ids, criteria,
                                                report["id"], baseline_id, ref_id))
    stabilities = [stability(runs["m1-hold-haiku-m1-c4"], runs["hk-g9-all"], ids, criteria, "haiku-g9-repeated"),
                  stability(runs["m1-sonnet-g9-all"], runs["or-hold-anthropic-claude-sonnet-5-5-g9"], ids, criteria, "sonnet-cli-api")]
    if any(digest(path) != checksum for path, checksum in initial_hashes.items()):
        raise ValueError("An input changed while the report was generated; retry from a frozen snapshot")
    return {"schema_version": 1, "reports": reports, "subsamples": subsamples,
            "profile_catalog": {"cloud": {"label": RUNS[0][1], "report_id": RUNS[0][2]},
                                "cascade": {"first_label": "Haiku 5.5", "escalate_label": RUNS[0][1], "report_id": RUNS[1][2]},
                                "routing": {"label": "Маршрутизация", "first_label": "Haiku 5.5", "escalate_label": RUNS[0][1], "report_id": "routing-haiku-gemini-g9-holdout"},
                                "local": {"label": "qwen2.5:7b (ноутбук, офлайн)", "report_id": "local-qwen25-7b-m4-holdout", "model": config["profiles"]["local"]["fast"]["model"], "cloud_fallback": False,
                                          "api_cost": {"amount": 0 if config["profiles"]["local"]["fast"]["model"].startswith("ollama:") else None,
                                                       "currency": "USD", "basis": "no_external_api", "scope": "local_only",
                                                       "source": "config/models.json local Ollama profile; MacBook Air M4 16 GB run local-m4-qwen25-7b-hold-g9; electricity not measured"}},
                                "source": "config/models.json and frozen run metadata; no inferred measurements"},
            "comparisons": comparisons, "stability": stabilities,
            "methodology": {"generator": "scripts/quality_report.py", "seed": SEED, "repetitions": REPETITIONS,
                            "bootstrap_unit": "dialogue", "bootstrap_quantile": "linear percentile 2.5/97.5",
                            "latency_quantile": "nearest-rank ceil(p*n), CLI wall seconds per dialogue",
                            "split": "fixed holdout subset of reused DEV, including for 65-dialogue source runs",
                            "independence": "Translations/scenarios may cross dialogue clusters; intervals can understate uncertainty. No unseen-control or human-accuracy claim.",
                            "selection": "Models and guidance selected using reused DEV. P-values are unadjusted and exploratory, not confirmatory.",
                            "small_groups": "Intervals are withheld with fewer than two dialogue clusters; no language-generalization claim.",
                            "timestamp_timezone_assumption": "+05:00 for stored naive run start times",
                            "inputs": [{"artifact": path.name, "sha256": digest(path)} for path in [split_path, config_path, checklist_path, *references.values()]]}}


def pct(value):
    return "не измерено" if value is None else f"{100 * value:.1f}%"


def interval(ci):
    return "не измерено" if ci["lower"] is None else f"[{100 * ci['lower']:.1f}; {100 * ci['upper']:.1f}]"


def local_note(payload, lookup):
    """Local Ollama line of the snapshot, computed from the report (no hardcoded quality numbers)."""
    base = "Локальный Ollama: внешняя API-плата отсутствует по конфигурации; оборудование, электроэнергия и доля ручной проверки не измерены."
    local = lookup.get(payload.get("profile_catalog", {}).get("local", {}).get("report_id"))
    if not local:
        return base
    c, latency = local["counts"], local.get("latency") or {}
    labelled = c["total"] - c["unlabelled"]
    share = pct(c["matched"] / labelled if labelled else None).replace(".", ",")
    seconds = latency.get("mean_seconds")
    speed = f", {seconds:.0f} с на диалог в свежем офлайн-прогоне" if isinstance(seconds, (int, float)) else ""
    return (base + f" Качество и время измерены на holdout: {c['matched']}/{labelled} = {share} принятых валидатором совпадений "
            f"(ниже константы 83,0%; финально отклонённых ответов {c['errors']}, они остаются в знаменателе){speed} — только офлайн-резерв.")


def snapshot(payload):
    lines = ["# Воспроизводимый срез качества W8", "",
             "Сгенерировано `scripts/quality_report.py` из локальных артефактов; платные модели не вызывались.",
             f"Все строки: фиксированные {payload['reports'][0]['sample']['dialogues']} диалогов из повторно использованного DEV holdout. Это совпадение с машинной разметкой, не независимая человеческая точность.", "",
             "## Совпадение и полнота", "",
             "ДИ 95%: 10 000 бутстрепов целых диалогов, seed 20261108. Основной счётчик сохраняет все запланированные ячейки; отдельное совпадение с эталоном делится на ячейки с известной меткой. Ошибки и отсутствующие ответы не удаляются.", "",
             "| Модель | Эталон | Совпало / размечено | Совпадение [95% ДИ] | Все ячейки | Нет метки | MAE итога (n) |", "|---|---|---:|---|---:|---:|---:|"]
    for report in payload["reports"]:
        c = report["counts"]
        denominator = c["total"] - c["unlabelled"]
        mae = report["score_mae"]
        mae_text = "не измерено" if mae["value"] is None else f"{mae['value']:.2f}"
        lines.append(f"| {report['source']['label']} | {report['reference']['id']} | {c['matched']}/{denominator} | {pct(c['matched']/denominator if denominator else None)} {interval(report['agreement_ci'])} | {c['matched']}/{c['total']} | {c['unlabelled']} | {mae_text} ({mae['count']}) |")
    lines += ["", "## Время и записанная стоимость", "", "| Модель | p50, с | p95, с | n | Записано USD / диалог | Покрытие записей | Нулевая цена при платных токенах |", "|---|---:|---:|---:|---:|---:|---:|"]
    for report in payload["reports"]:
        if report["reference"]["id"] != "silver":
            continue
        cost, audit, latency = report["cost"], report["cost_audit"], report["latency"]
        price = f"{cost['amount']/cost['count']:.6f}" if cost["count"] else "не восстановлено"
        lines.append(f"| {report['source']['label']} | {latency['p50_seconds']} | {latency['p95_seconds']} | {latency['count']} | {price} | {audit['known_dialogues']}/{audit['total_dialogues']} | {audit['zero_cost_paid_usage_dialogues']} |")
    lines += ["", "Аудит сохранённого учёта (не банковский счёт):", "", "| Профиль | Диалогов | Сумма cost_kzt, ₸ | Сумма cost_kzt_full, ₸ | Нулевая цена + платные токены | cost=0 < full |", "|---|---:|---:|---:|---:|---:|"]
    by_id = {report["id"]: report for report in payload["reports"]}
    for profile in ("cloud", "cascade", "routing"):
        report = by_id[payload["profile_catalog"][profile]["report_id"]]
        audit = report["cost_audit"]
        lines.append(f"| {profile} | {audit['total_dialogues']} | {audit['recorded_charged_total_kzt']} | {audit['recorded_total_kzt']} | {audit['zero_cost_paid_usage_dialogues']} | {audit['zero_charged_positive_full_dialogues']} |")
    lines += ["", "**стоимость — по ценам моделей в каждом вызове; токены по моделям не разделены**. Используем сохранённый `cost_kzt_full`: llm считает цену вызова, evaluator распределяет её между критериями, каскад суммирует стадии. Общий итог строки проверяется, а не добавляется повторно. Это reported usage estimate, не счёт и не гарантия полного исторического учёта.",
              "", "**Исправление предыдущего W8-аудита:** отсутствие разбивки токенов не делает сохранённую сумму стоимости неизвестной. Вывод о полностью неизвестной стоимости каскада был чрезмерным. Нулевые сохранённые цены при ненулевых платных токенах показаны отдельно и означают нижнюю оценку/неполный учёт. Из объединённых токенов нельзя подтвердить или опровергнуть покрытие цены каждой стадии; у каскада/маршрутизации это остаётся неизвестным. Подтверждённый коэффициент экономии по несопоставимым источникам не выводится.",
              "", f"Конверсия сохранённых тенге в USD использует `config/models.json:usd_kzt` = {payload['reports'][0]['economics']['currency_fx']['usd_kzt']}; курс на момент запуска в slim JSONL не записан, поэтому это явное допущение. `cost_audit.token_price_estimate` хранит отдельный сценарий текущих цен только для однозначных моделей; его нельзя смешивать с исторической ценой другого профиля. Wall time включает локальный evaluator; состояние кэша отдельно выведено из charged/full cost, а не из явного флага JSONL.", "",
              "## Парные сравнения с Gemini", "", "| Модель / эталон | Побед / поражений | Общих ячеек | Разница п.п. [95% ДИ] | McNemar p | Вывод |", "|---|---:|---:|---|---:|---|"]
    lookup = {r["id"]: r for r in payload["reports"]}
    for comparison in payload["comparisons"]:
        r = lookup[comparison["report_id"]]
        delta = f"{100*comparison['delta']:.2f}" if comparison["delta"] is not None else "не измерено"
        p_value = f"{comparison['p_value']:.5g}" if comparison["p_value"] is not None else "не измерено"
        conclusion = "различие обнаружено" if comparison["significant"] else "в пределах шума" if comparison["compared"] else "нет размеченных пар"
        lines.append(f"| {r['source']['label']} / {comparison['reference_id']} | {comparison['win']}/{comparison['loss']} | {comparison['compared']} | {delta} {interval(comparison['delta_ci'])} | {p_value} | {conclusion} |")
    lines += ["", "Сравнение на одних и тех же размеченных ячейках. Missing/error считаются неуспехом. Точный двусторонний McNemar не учитывает зависимость критериев внутри диалога; основной ДИ разницы ресемплирует диалоги. Флаг различия требует одновременно p < 0,05 и ДИ без нуля. Поправка на множественные сравнения не применялась; все выводы разведочные после выбора моделей на DEV. Незначимость не доказывает эквивалентность.", "", "## Стабильность", "", "| Пара | Совпало | 95% ДИ | Конфигурация |", "|---|---:|---|---|"]
    for item in payload["stability"]:
        configuration_label = "одинаковые записанные настройки" if item["same_configuration"] else "неполные метаданные" if "metadata incomplete" in item["configuration_note"] else "разные записанные настройки"
        lines.append(f"| {item['id']} | {item['matched']}/{item['total']} | {interval(item['agreement_ci'])} | {configuration_label} |")
    lines += ["", "Совпадение записанных настроек проверяется по preset, guidance, template, readings и хешам промптов критериев. При неполных метаданных оно не утверждается. Resolved model и кэш неизвестны, независимость повторов не подтверждена. Разные конфигурации CLI/API нельзя представлять как чистую повторяемость.", "", "## Разрезы основной модели против silver", "", "| Группа | Диалогов | Совпало / размечено | 95% ДИ |", "|---|---:|---:|---|"]
    first = payload["reports"][0]
    for group in first["languages"] + first["channels"] + first["criteria"]:
        c = group["counts"]
        lines.append(f"| {group['id']} | {group['dialogues']} | {c['matched']}/{c['total']-c['unlabelled']} | {interval(group['agreement_ci'])} |")
    if payload.get("subsamples"):
        lines += ["", "## Подвыборки: все KZ-чаты и все звонки DEV", "",
                  "Gemini 4 Argon, правила g9, прогоны `or-kzcalls-gemini-g9` и holdout-прогон (на пересечении берётся holdout). Эталоны: silver (основной), gold_v2.", "",
                  "| Подвыборка | Диалогов | Из них tune | Эталон | Совпало / размечено | Совпадение [95% ДИ] |", "|---|---:|---:|---|---:|---|"]
        for sub in payload["subsamples"]:
            for ref_id, ref in sub["references"].items():
                if ref_id == "gold_v2_full":
                    continue
                lines.append(f"| {sub['label']} | {sub['dialogues']} | {sub['tune_dialogues']} | {ref_id} | {ref['matched']}/{ref['labelled']} | {pct(ref['matched']/ref['labelled'] if ref['labelled'] else None)} {interval(ref['agreement_ci'])} |")
        lines += ["", *["- " + sub["label"] + ": " + sub["note"] for sub in payload["subsamples"]]]
    lines += ["", "## Источники для экономики банка", "", "Доля ручной проверки вычисляется только по финальному `needs_review`: хотя бы один критерий помечен. Эскалация в Gemini не равна направлению человеку. При пропущенных флагах доля неизвестна. Стоимость пары — амортизированная цена запланированного критерия, не предельная цена изменения одного критерия.", "", "| Профиль / канал | n | Записано ₸ / диалог | Записанное среднее, с | p50 / p95, с | На проверку / известные | Доля |", "|---|---:|---:|---:|---|---:|---:|"]
    usage_notes = []
    for profile in ("cloud", "cascade", "routing"):
        rid = payload["profile_catalog"][profile]["report_id"]
        report = lookup[rid]
        for channel, econ in report["economics"]["channels"].items():
            price = f"{econ['cost_per_dialogue_kzt']:.4f}" if econ["cost_per_dialogue_kzt"] is not None else "неизвестно"
            mean = f"{econ['recorded_mean_seconds_per_dialogue']:.2f}" if econ["recorded_mean_seconds_per_dialogue"] is not None else "неизвестно"
            lines.append(f"| {profile} / {channel} | {econ['dialogues']} | {price} | {mean} | {econ['p50_seconds']} / {econ['p95_seconds']} | {econ['review_count']}/{econ['review_known_dialogues']} | {pct(econ['review_fraction'])} |")
        usage = report["model_usage"]
        usage_notes.append(f"{profile}: сохранённые model labels показывают Gemini в {usage['gemini_dialogues']}/{usage['known_dialogues']} диалогов, эскалацию в {usage['escalated_dialogues']}, прямой маршрут звонка в {usage['direct_routed_calls']}. Кэш: {report['cache']}. Это не полная телеметрия всех provider calls.")
    lines += ["", *["- " + note for note in usage_notes], "", "В таблице показано записанное wall time. Для cached/mixed/unknown скорость живого расчёта и её проекция на большой объём остаются неизвестными; калькулятор получает null вместо ложной скорости из кэша.", "", local_note(payload, lookup)]
    lines += ["", "ДИ групп с менее чем двумя информативными диалогами не выводится. Малые языковые и канальные подвыборки не подтверждают обобщение на новые диалоги. Сценарии/переводы могут зависеть друг от друга: кластеризация по диалогу не устраняет эту зависимость. MAE использует только полные пары всех критериев, N/A перераспределяет веса; критический флаг не добавляет баллы.", "", "## Воспроизведение", "", "```sh", "python scripts/quality_report.py --source-root /path/to/w-int --legacy-root /path/to/w-m1 --references-root /path/to/agents/reports", "python -m pytest -q tests/test_quality_report.py", "```", "", "SHA256 входов записаны в `quality.json`: `methodology.inputs`, `report.source.sha256`, `report.reference.sha256`. Полные JSONL, цитаты и ID реплик в отчёт не попадают. Два запуска при неизменных входах дают одинаковые JSON и Markdown."]
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=ROOT)
    parser.add_argument("--legacy-root", type=Path, required=True)
    parser.add_argument("--references-root", type=Path, required=True)
    parser.add_argument("--out-json", type=Path, default=ROOT / "web/data/quality.json")
    parser.add_argument("--out-md", type=Path, default=ROOT / "docs/QUALITY_SNAPSHOT.md")
    args = parser.parse_args()
    payload = generate(args.source_root, args.legacy_root, args.references_root)
    args.out_json.write_text(json_text(payload), encoding="utf-8")
    args.out_md.write_text(snapshot(payload), encoding="utf-8")
    print(f"Wrote {len(payload['reports'])} reports, {len(payload['comparisons'])} paired comparisons, {len(payload['stability'])} stability checks")


if __name__ == "__main__":
    main()
