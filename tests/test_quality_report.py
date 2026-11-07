"""Offline statistics/provenance regressions; no real conversations or providers."""
import importlib.util
import json
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location("quality_report", Path(__file__).resolve().parents[1] / "scripts/quality_report.py")
Q = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(Q)


@pytest.mark.parametrize("values,p,expected", [([], .95, None), ([7], .5, 7),
    (list(range(1, 22)), .5, 11), (list(range(1, 22)), .95, 20), ([4, 1, 3, 2], .5, 2)])
def test_nearest_rank_not_interpolated_or_mean(values, p, expected):
    assert Q.nearest_rank(values, p) == expected


@pytest.mark.parametrize("win,loss,expected", [(0, 0, 1), (1, 0, 1), (5, 0, .0625),
    (6, 0, .03125), (1, 6, .125), (3, 3, 1)])
def test_exact_two_sided_mcnemar(win, loss, expected):
    assert Q.mcnemar_exact(win, loss) == expected
    assert Q.mcnemar_exact(loss, win) == expected


def test_cluster_bootstrap_known_endpoints_and_determinism():
    all_correct = Q.cluster_ci([2, 8], [2, 8])
    assert (all_correct["lower"], all_correct["upper"]) == (1, 1)
    opposite = Q.cluster_ci([0, 8], [2, 8])
    assert (opposite["lower"], opposite["upper"]) == (0, 1)
    assert opposite == Q.cluster_ci([0, 8], [2, 8])
    assert opposite["valid_repetitions"] == 10000


def test_small_group_is_not_false_precise_interval():
    ci = Q.cluster_ci([5], [6])
    assert ci["lower"] is ci["upper"] is None
    assert ci["reason"] == "insufficient_clusters"
    empty = Q.cluster_ci([0, 0], [0, 0])
    assert empty["reason"] == "no_labelled_cells"


def test_missing_label_bootstrap_records_undefined_replicates():
    ci = Q.cluster_ci([0, 1, 1], [0, 2, 2])
    assert ci["lower"] == ci["upper"] == .5
    assert 9500 < ci["valid_repetitions"] < 9750
    assert ci["effective_dialogues"] == 2


def test_unlabelled_dialogues_do_not_inflate_informative_cluster_count():
    ci = Q.cluster_ci([1, 0], [2, 0])
    assert ci["lower"] is ci["upper"] is None
    assert ci["reason"] == "insufficient_clusters"
    assert ci["effective_dialogues"] == 1


def criteria():
    return {"B": {"id": "B", "answer_type": "binary", "weight": 50, "na_allowed": True},
            "S": {"id": "S", "answer_type": "scale", "weight": 50, "scale": [0, 5, 10], "na_allowed": True}}


def run_with(answers):
    return {"rows": {cid: {"evals": {key: {"answer": value} for key, value in row.items()}} for cid, row in answers.items()}}


def test_all_cells_keeps_missing_errors_unlabelled_and_na():
    crit = criteria()
    run = run_with({"a": {"B": "na", "S": "nonsense"}, "b": {}})
    ref = {("a", "B"): "na", ("a", "S"): 10, ("b", "B"): "yes"}
    result = Q.aggregate(Q.measured_cells(run, ref, ["a", "b"], crit))
    assert result["counts"] == {"matched": 1, "total": 4, "missing": 1, "errors": 1, "unlabelled": 1}


def test_paired_comparison_keeps_failed_predictions_and_uses_common_clusters():
    crit = criteria()
    baseline = run_with({"a": {"B": "yes", "S": 10}, "b": {"B": "no", "S": 5}})
    model = run_with({"a": {"S": 10}, "b": {"B": "yes", "S": 5}})
    ref = {("a", "B"): "yes", ("a", "S"): 10, ("b", "B"): "yes", ("b", "S"): 5}
    result = Q.compare_runs(model, baseline, ref, ["a", "b"], crit, "model", "base", "gold")
    assert (result["win"], result["loss"], result["compared"], result["delta"]) == (1, 1, 4, 0)
    assert result["missing_or_error_cells"] == {"model": 1, "baseline": 0}
    assert (result["delta_ci"]["lower"], result["delta_ci"]["upper"]) == (-.5, .5)
    assert result["p_value"] == 1
    assert not result["significant"]


def test_significant_requires_both_cluster_ci_and_mcnemar():
    crit = {"B": criteria()["B"]}
    ids = [str(i) for i in range(8)]
    model = run_with({cid: {"B": "yes"} for cid in ids})
    base = run_with({cid: {"B": "no"} for cid in ids})
    ref = {(cid, "B"): "yes" for cid in ids}
    result = Q.compare_runs(model, base, ref, ids, crit, "model", "base", "gold")
    assert result["significant"] is True
    assert result["p_value"] == .0078125


def test_empty_reference_is_unmeasured_not_all_concordant():
    run = run_with({"a": {"B": "yes"}})
    comparison = Q.compare_runs(run, run, {}, ["a"], {"B": criteria()["B"]}, "one", "two", "empty")
    assert comparison["compared"] == 0
    assert comparison["p_value"] is None and comparison["delta"] is None
    assert comparison["significant"] is False


def test_total_reweights_na_and_requires_complete_paired_scores():
    crit = criteria()
    assert Q.total_score({"B": "na", "S": 5}, crit) == 50
    assert Q.total_score({"B": "yes", "S": 5}, crit) == 75
    assert Q.total_score({"B": "yes"}, crit) is None
    run = run_with({"a": {"B": "yes", "S": 5}, "b": {"B": "no", "S": 5}})
    ref = {("a", "B"): "yes", ("a", "S"): 10, ("b", "B"): "no"}
    assert Q.score_mae(run, ref, ["a", "b"], crit) == {"value": 25, "count": 1}


def test_cost_sums_allocations_once_and_never_prices_merged_cascade_as_one_model():
    crit = criteria()
    config = {"pricing_usd_per_mtok": {"cheap": {"in": 1, "out": 2}, "strong": {"in": 10, "out": 20}}}
    row = {key: {"tokens_in": 50, "tokens_out": 10, "model": "openai:cheap", "calls": .5} for key in crit}
    run = {"rows": {"a": {"evals": row}}}
    cost, audit = Q.token_price_metrics(run, ["a"], crit, config)
    assert cost["amount"] == .00014
    assert cost["count"] == 1
    for entry in row.values():
        entry["model"] = "openai:cheap → openai:strong"
        entry["cost_kzt"] = 0  # A stale zero cost is not proof of a free cascade.
    cost, audit = Q.token_price_metrics(run, ["a"], crit, config)
    assert cost == {"amount": None, "currency": None, "count": 0, "basis": "unknown"}
    assert audit["unknown_dialogues"] == 1
    assert audit["lower_bound_usd"] == .00014
    assert audit["upper_bound_usd"] == .0014
    assert audit["reasons"] == ["combined_stage_tokens"]


def test_partial_known_cost_does_not_become_total_or_subset_average():
    config = {"usd_kzt": 500, "pricing_usd_per_mtok": {"cheap": {"in": 1, "out": 2}}}
    crit = {"B": criteria()["B"]}
    run = {"rows": {"a": {"evals": {"B": {"model": "openai:cheap", "tokens_in": 100, "tokens_out": 10, "cost_kzt_full": 2}}},
                    "b": {"evals": {"B": {"model": "cli:unpriced-alias", "tokens_in": 100, "tokens_out": 10}}}}}
    cost, audit = Q.cost_metrics(run, ["a", "b"], crit, config)
    assert cost["amount"] is None and cost["count"] == 0
    assert (audit["known_dialogues"], audit["unknown_dialogues"]) == (1, 1)
    assert audit["lower_bound_usd"] is None


def test_cascade_preserves_recorded_stage_cost_even_when_token_attribution_is_lost():
    crit = criteria()
    config = {"usd_kzt": 500, "pricing_usd_per_mtok": {"cheap": {"in": 1, "out": 2}, "strong": {"in": 10, "out": 20}}}
    entries = {key: {"cost_kzt_full": 5, "cost_kzt": 0, "model": "openai:cheap → openai:strong",
                     "tokens_in": 100, "tokens_out": 10} for key in crit}
    run = {"rows": {"a": {"evals": entries, "cost_kzt_full": 10}}}
    cost, audit = Q.cost_metrics(run, ["a"], crit, config)
    assert cost == {"amount": .02, "currency": "USD", "count": 1, "basis": "reported"}
    assert audit["recorded_total_kzt"] == 10  # Dialogue summary is checked, never added twice.
    assert audit["pricing_complete"] is None and audit["comparable"] is False
    assert audit["zero_cost_paid_usage_dialogues"] == 0
    assert audit["token_price_estimate"]["amount"] is None


def test_zero_historical_full_cost_with_paid_tokens_is_repriced_for_a_single_model():
    # one model answered the call, so its own tokens x its own price is the exact cost (zero = price table added mid-run)
    config = {"usd_kzt": 500, "pricing_usd_per_mtok": {"paid": {"in": 2, "out": 10}}}
    crit = {"B": criteria()["B"]}
    run = {"rows": {"a": {"evals": {"B": {"cost_kzt_full": 0, "model": "openai:paid", "tokens_in": 1000, "tokens_out": 100}}}}}
    cost, audit = Q.cost_metrics(run, ["a"], crit, config)
    assert cost["amount"] == .003 and cost["basis"] == "reported"
    assert audit["repriced_single_model_entries"] == 1 and audit["zero_cost_paid_usage_dialogues"] == 0
    assert audit["token_price_estimate"]["amount"] == .003


def test_zero_full_cost_in_a_cascade_stays_a_visible_lower_estimate():
    config = {"usd_kzt": 500, "pricing_usd_per_mtok": {"cheap": {"in": 1, "out": 1}, "paid": {"in": 2, "out": 10}}}
    crit = {"B": criteria()["B"]}
    run = {"rows": {"a": {"evals": {"B": {"cost_kzt_full": 0, "model": "openai:cheap → openai:paid", "tokens_in": 1000, "tokens_out": 100}}}}}
    cost, audit = Q.cost_metrics(run, ["a"], crit, config)
    assert audit["repriced_single_model_entries"] == 0 and audit["pricing_complete"] is not True


def test_reported_dialogue_cost_must_reconcile_with_allocated_evaluation_costs():
    config = {"usd_kzt": 500, "pricing_usd_per_mtok": {}}
    crit = {"B": criteria()["B"]}
    run = {"rows": {"a": {"cost_kzt_full": 8, "evals": {"B": {"cost_kzt_full": 4}}}}}
    cost, audit = Q.cost_metrics(run, ["a"], crit, config)
    assert cost["amount"] is None
    assert "reported_full_cost_mismatch" in audit["reasons"]


def test_review_fraction_uses_final_flags_and_never_escalation_or_missing_flags():
    crit = {"B": criteria()["B"]}
    run = {"rows": {"a": {"evals": {"B": {"model": "cheap → strong", "needs_review": False}}},
                    "b": {"evals": {"B": {"model": "cheap", "needs_review": True}}}}}
    result = Q.review_metrics(run, ["a", "b"], crit)
    assert (result["review_fraction"], result["review_count"], result["review_known_dialogues"]) == (.5, 1, 2)
    del run["rows"]["a"]["evals"]["B"]["needs_review"]
    result = Q.review_metrics(run, ["a", "b"], crit)
    assert result["review_fraction"] is None
    assert (result["review_count"], result["review_known_dialogues"]) == (1, 1)


def test_routing_usage_counts_saved_model_labels_not_policy_claims():
    run = {"rows": {
        "a": {"evals": {"B": {"model": "openai:google/gemini-4-argon (маршрут: звонок)"}}},
        "b": {"evals": {"B": {"model": "openai:cheap → openai:google/gemini-4-argon"}}},
        "c": {"evals": {"B": {"model": "openai:cheap", "needs_review": True}}}}}
    usage = Q.model_usage(run, ["a", "b", "c"])
    assert (usage["gemini_dialogues"], usage["direct_routed_calls"], usage["escalated_dialogues"]) == (2, 1, 1)


def test_economics_uses_recorded_cost_review_and_complete_mean_without_hiding_missing():
    config = {"usd_kzt": 500, "pricing_usd_per_mtok": {"cheap": {"in": 1, "out": 2}}}
    crit = {"B": criteria()["B"]}
    run = {"rows": {cid: {"wall_s": wall, "cost_kzt": cost, "cost_kzt_full": cost, "evals": {"B": {"cost_kzt_full": cost, "needs_review": review,
           "model": "openai:cheap", "tokens_in": 1, "tokens_out": 1}}} for cid, wall, cost, review in [("a", 2, 5, True), ("b", 8, 15, False)]}}
    result = Q.economics_metrics(run, ["a", "b"], crit, config)
    assert result["cost_per_dialogue_kzt"] == 10
    assert result["cost_per_dialogue_usd"] == .02
    assert (result["mean_seconds_per_dialogue"], result["p50_seconds"], result["p95_seconds"]) == (5, 2, 8)
    assert result["review_fraction"] == .5
    del run["rows"]["a"]["wall_s"]
    assert Q.economics_metrics(run, ["a", "b"], crit, config)["mean_seconds_per_dialogue"] is None


def test_cached_milliseconds_are_not_a_fresh_model_speed_projection():
    config = {"usd_kzt": 500, "pricing_usd_per_mtok": {"cheap": {"in": 1, "out": 2}}}
    crit = {"B": criteria()["B"]}
    run = {"rows": {"a": {"wall_s": .01, "cost_kzt": 0, "cost_kzt_full": 4, "evals": {"B": {
        "cost_kzt_full": 4, "needs_review": False, "model": "openai:cheap", "tokens_in": 100, "tokens_out": 100}}}}}
    result = Q.economics_metrics(run, ["a"], crit, config)
    assert Q.cache_state(run, ["a"]) == "cached"
    assert result["recorded_mean_seconds_per_dialogue"] == .01
    assert result["mean_seconds_per_dialogue"] is None and result["mean_seconds_per_pair"] is None
    assert result["latency_provenance"]["projection_allowed"] is False
    run["rows"]["a"]["cost_kzt"] = 1
    assert Q.cache_state(run, ["a"]) == "mixed"
    run["rows"]["a"]["cost_kzt_full"] = 0
    assert Q.cache_state(run, ["a"]) == "unknown"


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")


def test_duplicate_reference_and_prediction_fail_closed(tmp_path):
    path = tmp_path / "ref.jsonl"
    row = {"conv_id": "a", "criterion_id": "B", "answer": "yes"}
    write_jsonl(path, [row, row])
    with pytest.raises(ValueError, match="duplicate reference"):
        Q.load_reference(path, criteria())
    write_jsonl(path, [{"_meta": {}}, {"conv_id": "a", "evals": [{"criterion_id": "B", "answer": "yes"}] * 2}])
    with pytest.raises(ValueError, match="duplicate prediction"):
        Q.load_run(path)


def synthetic_inputs(tmp_path):
    source, legacy, refs = (tmp_path / name for name in ("source", "legacy", "refs"))
    for path in [source / "data/checklists", source / "eval", source / "config", refs]:
        path.mkdir(parents=True, exist_ok=True)
    checklist = {"version": 1, "criteria": list(criteria().values())}
    (source / "data/checklists/v1.json").write_text(json.dumps(checklist))
    (source / "eval/split.json").write_text(json.dumps({"holdout": ["a", "b"]}))
    (source / "config/models.json").write_text(json.dumps({"usd_kzt": 500, "pricing_usd_per_mtok": {"cheap": {"in": 1, "out": 2}},
        "profiles": {"local": {"fast": {"model": "ollama:synthetic"}}}}))
    labels = [{"conv_id": cid, "criterion_id": key, "answer": answer} for cid in ["a", "b"] for key, answer in [("B", "yes"), ("S", 10)]]
    for path in [legacy / "eval/gold/silver.jsonl", refs / "gold_v2.jsonl", refs / "gold_v2_full.jsonl"]:
        write_jsonl(path, labels)
    for name, _, _, old in Q.RUNS:
        meta = {"name": name, "mode": "fast", "preset": {"model": "openai:cheap"}, "started": "2026-01-01 00:00:00", "guidance_version": "synthetic"}
        rows = [{"_meta": meta}]
        for cid in ["a", "b"]:
            rows.append({"conv_id": cid, "language": "RU", "channel": "chat", "wall_s": 2 if cid == "a" else 8,
                         "evals": [{"criterion_id": key, "answer": answer, "tokens_in": 50, "tokens_out": 10,
                                    "model": "openai:cheap", "cost_kzt_full": 1, "needs_review": False, "explanation": "PRIVATE_SENTINEL", "evidence": [{"msg_id": "DO_NOT_EXPORT:7"}]} for key, answer in [("B", "yes"), ("S", 10)]]})
        write_jsonl((legacy if old else source) / "results/eval_runs" / f"{name}.jsonl", rows)
    return source, legacy, refs


def test_two_generations_are_identical_reconcile_and_exclude_raw_data(tmp_path):
    source, legacy, refs = synthetic_inputs(tmp_path)
    first, second = Q.generate(source, legacy, refs), Q.generate(source, legacy, refs)
    assert Q.json_text(first) == Q.json_text(second)
    assert Q.snapshot(first) == Q.snapshot(second)
    assert len(first["reports"]) == 33 and len(first["comparisons"]) == 30
    output = Q.json_text(first)
    assert "PRIVATE_SENTINEL" not in output and "DO_NOT_EXPORT" not in output and str(tmp_path) not in output
    for report in first["reports"]:
        for group_key in ["criteria", "languages", "channels"]:
            for count_key, value in report["counts"].items():
                assert sum(group["counts"][count_key] for group in report[group_key]) == value
        assert report["sample"]["untouched"] is False
        assert report["cache"] == "unknown"


def test_stability_exposes_configuration_difference_and_missing_cells():
    crit = criteria()
    left = run_with({"a": {"B": "yes", "S": 10}, "b": {"B": "yes", "S": 10}})
    right = run_with({"a": {"B": "yes", "S": 5}, "b": {"B": "yes"}})
    left["meta"] = {"name": "left", "preset": {"model": "cli:alias"}}
    right["meta"] = {"name": "right", "preset": {"model": "api:model"}}
    measured = Q.stability(left, right, ["a", "b"], crit, "pair")
    assert measured["same_configuration"] is False
    assert (measured["matched"], measured["total"], measured["planned_cells"], measured["missing_or_error"]) == (2, 3, 4, 1)


def test_matching_empty_metadata_is_not_evidence_of_same_configuration():
    left = run_with({"a": {"B": "yes"}})
    right = run_with({"a": {"B": "yes"}})
    left["meta"], right["meta"] = {"name": "a"}, {"name": "b"}
    result = Q.stability(left, right, ["a"], {"B": criteria()["B"]}, "pair")
    assert result["same_configuration"] is False
    assert "metadata incomplete" in result["configuration_note"]


def _staged_runs(stored_cascade_kzt):
    gem = {"rows": {"d": {"evals": {"B": {"model": "openai:google/gemini-4-argon", "tokens_in": 1000, "tokens_out": 100,
                                          "cost_kzt_full": 0}}}}}
    casc = {"rows": {"d": {"cost_kzt_full": stored_cascade_kzt,
                           "evals": {"B": {"model": "openai:anthropic/claude-haiku-5.5 → openai:google/gemini-4-argon",
                                           "tokens_in": 1500, "tokens_out": 150, "cost_kzt_full": stored_cascade_kzt}}}}}
    return {Q.STRONG_RUN: gem, "or-hold-cascade-g9": casc}


CFG = {"usd_kzt": 500, "pricing_usd_per_mtok": {"google/gemini-4-argon": {"in": 2, "out": 10}}}


def test_replayed_zero_cost_gemini_stage_is_priced_from_the_same_gemini_call():
    runs = _staged_runs(1.0)                      # only the Haiku stage was paid; Gemini call costs (1000*2+100*10)/1e6*500 = 1.5 KZT
    Q.reprice_replayed_strong_stage(runs, CFG)
    row = runs["or-hold-cascade-g9"]["rows"]["d"]
    assert abs(row["cost_kzt_full"] - 2.5) < 1e-9 and runs["or-hold-cascade-g9"]["stage_repriced_dialogues"] == ["d"]


def test_paid_gemini_stage_is_left_alone():
    runs = _staged_runs(3.0)                      # already >= the Gemini call alone
    Q.reprice_replayed_strong_stage(runs, CFG)
    assert runs["or-hold-cascade-g9"]["rows"]["d"]["cost_kzt_full"] == 3.0
    assert runs["or-hold-cascade-g9"]["stage_repriced_dialogues"] == []


def test_without_the_strong_run_nothing_changes():
    runs = _staged_runs(1.0)
    del runs[Q.STRONG_RUN]
    Q.reprice_replayed_strong_stage(runs, CFG)
    assert runs["or-hold-cascade-g9"]["rows"]["d"]["cost_kzt_full"] == 1.0
