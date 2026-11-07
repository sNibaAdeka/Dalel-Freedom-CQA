# Quality metrics contract — v1

Owner: `web/js/quality.js`, `web/css/quality.css`, `tests/frontend_quality*.test.mjs`, this document.
Parent owns registration, tab routing, API and backend. This module never runs an evaluation.

## Implementation plan

1. Define provenance and denominators before displaying percentages.
2. Test validation with invented aggregate fixtures, including missing reference labels.
3. Implement a pure validator and formatting helpers.
4. Render accessible tables, empty/loading/error states and sample caveats.
5. Test the mount lifecycle with synthetic responses, including stale requests.
6. Verify scope, then commit only owned paths for the integrator.

## Parent hooks

```js
import { mount } from './quality.js';
const dispose = mount(CQA); // host: <div id="cqa-quality"></div>
// dispose() removes listeners and invalidates pending requests.
```

`mount` attaches `../css/quality.css` once. It reads **only** `await CQA.api.quality()` with no arguments. No direct fetch, provider calls, polling, local storage, or fallback data. The parent adapter may GET `metrics/quality`, then on HTTP 404 only try same-origin `./data/quality.json`. Both 404s and mock mode return `{reports: []}`; other errors must throw. The module does not fetch either path itself.

Initial mount loads once. Button “Обновить замеры”, bus `quality:refresh`, `api:mode`, and `tab:changed` with `quality` reload. No `run:completed` hook: completing an evaluation does not imply a reference comparison exists. Repeated mount on the same host is idempotent. Missing host returns a no-op disposer. Missing adapter is a connection error, not “no measurements”. Pending responses cannot repopulate a disposed view or overwrite a newer response. Prior figures are removed while reloading, so a failure cannot look like a current measurement.

Parent must register the fifth tab in its keyboard/routing lists and module whitelist. The module does not change `core.js`, `index.html`, `api.js` or backend files.

## Envelope

```json
{"schema_version": 1, "reports": []}
```

`schema_version` is optional (absent means v1); another version is invalid. `reports` is required and must be an array. **Only an empty array means no measurements.** Null, missing root, missing `reports`, malformed JSON, and HTTP errors are not successful empty responses. Reports are not implicitly merged or ranked: different samples/models/guidance cannot be compared as a controlled experiment merely because both appear here.

## Report (all numbers below are synthetic)

```json
{
  "id": "synthetic-report",
  "run_id": "synthetic-run",
  "measured_at": "2026-01-02T10:00:00Z",
  "source": {"kind": "cli", "label": "Synthetic CLI fixture", "artifact": "synthetic-summary.json"},
  "model": {"requested": "synthetic-model", "resolved": null},
  "mode": "fast",
  "guidance_version": "synthetic-g1",
  "checklist_version": "synthetic-v1",
  "cache": "unknown",
  "reference": {"id": "synthetic-reference", "type": "machine", "cells": 12},
  "sample": {
    "dataset": "synthetic DEV",
    "split": "holdout",
    "dialogues": 3,
    "dev_reused": true,
    "used_for_tuning": false,
    "untouched": false
  },
  "denominator": "all_cells",
  "counts": {"matched": 8, "total": 12, "missing": 1, "errors": 1, "unlabelled": 1},
  "score_mae": {"value": 2.5, "count": 1},
  "latency": {"p50_seconds": 4, "p95_seconds": 9, "count": 3, "scope": "cli_wall"},
  "cost": {"amount": null, "currency": null, "count": 0, "basis": "unknown"}
}
```

Required provenance: nonempty `id`, `run_id`, timezone-qualified ISO `measured_at`, `source.kind` (`cli|api|offline|synthetic`), `source.label`, `model.requested`, `mode`, `guidance_version`, `checklist_version`, `cache` (`fresh|cached|mixed|unknown`), reference and sample. `model.resolved` is a nonempty string or null; no substitution of the requested alias for an unknown resolved model. `source.artifact` is optional plain text, never a clickable arbitrary URL. Additional properties are ignored, never interpreted as percentages.

`reference`: nonempty `id`; `type` = `machine|human|mixed`; `cells` = number of labels in the full reference, at least `counts.total - counts.unlabelled`. Optional `description` and `annotator_agreement: {matched, total}`. Annotator agreement is **agreement between reference passes**, not the evaluated model's quality. Pass independence must be established by the producer; the UI does not assert it.

`sample`: nonempty `dataset`; `split` = `tune|holdout|dev|control|unspecified`; nonnegative integer `dialogues`; explicit boolean `dev_reused`; `used_for_tuning` and `untouched` are booleans or null (unknown). A sample marked `untouched: true` cannot be reused DEV, used for tuning, or have split `tune`. Unknown tuning/reuse provenance must not be marketed as independent validation. A named holdout reused during iterations remains a reused holdout. The UI always states that numbers on reused DEV do not establish quality on new data.

## Denominator and missingness

`denominator` must equal `all_cells`. Every counts object has nonnegative safe-integer `matched`, `total`, `missing`, `errors`; `unlabelled` is optional and defaults to zero.

- `total`: **all planned conversation × criterion cells** in this sample, fixed before inference. Include failed calls, missing predictions and missing reference labels. Do not send only successfully scored/comparable rows.
- `unlabelled`: no reference label available. These cells cannot be judged correct/incorrect. Classify these first; they must not also appear in `missing` or `errors`.
- `missing`: reference label exists, but no prediction was produced (and no explicit error classified).
- `errors`: reference label exists, but prediction failed (provider/parser/invalid-answer error). Disjoint from `missing`.
- `matched`: valid prediction exactly matches an available reference label. Remaining cells are valid disagreements.
- Enforce `matched + missing + errors + unlabelled <= total`. The module rejects inconsistent reports rather than clamping or guessing.

Primary metric = `matched / total`, labelled **confirmed matches / all planned cells**, not human accuracy. It is a conservative all-cells measure; cells without reference labels are **unverified**, not proven wrong. When `unlabelled > 0`, also show reference coverage `(total - unlabelled) / total` and agreement among labelled cells `matched / (total - unlabelled)`, explicitly named with its denominator. Missing predictions and errors stay in both applicable denominators. Never silently switch to answered-only accuracy. Zero total gives “не измерено”, never 0% or 100%.

Machine reference: “Совпадение с машинным эталоном”. Human: “Совпадение с ручной разметкой”. Mixed: “Совпадение со смешанным эталоном”. None is automatically unbiased ground truth. Unknown costs are not zero; zero is displayed only when explicitly measured/reported.

## Optional metrics

Omitted/null optional objects mean “не измерено” / “неизвестна”, not zero.

| Field | Shape and rules |
| --- | --- |
| `score_mae` | `{value: number|null, count: integer}`. Points on 0–100 scale. `count` is paired, complete conversation totals, at most `sample.dialogues`. With count 0, value must be null; otherwise finite 0–100. UI always shows count / all dialogues and warns that incomplete totals are excluded. |
| `latency` | `{p50_seconds, p95_seconds, count, scope}`. Each percentile may be null (unknown); at least one must be finite nonnegative for count > 0, both null for count 0. When both exist, p95 ≥ p50. Count ≤ sample dialogues. Scope `cli_wall|api_e2e|model_only`; CLI wall requires CLI source, API e2e requires API source. Values are **seconds per dialogue**, never batch duration divided by size unless separately measured. Never substitute mean for an unknown p95. Count means timed dialogues, not provider calls. Percentile method belongs in producer documentation. Cache state always displayed nearby; no “fresh” inference from transport. |
| `cost` | `{amount, currency, count, basis}`. Amount is **total** for count dialogues, not cost per call; per-dialogue value derived only for count > 0. Basis `reported|estimated|unknown`. Unknown requires null amount and count 0. Known requires finite amount ≥ 0, count > 0, uppercase three-letter currency, count ≤ sample dialogues. No currency conversion inferred. |
| `criteria` | Array of `{id, label?, counts}`. Unique ids; when supplied this is a complete partition, so summed matched/total/missing/errors/unlabelled must equal overall counts. Criteria are not hardcoded to C1–C8. |
| `languages` / `channels` | Same shape and complete-partition requirement. Groups without a known language/channel must appear as an explicit unknown group, not vanish. No averaging of group percentages. |
| `auto_approved` | `{counts}` with same count format, representing a subset of overall cells. No missing/error predictions permitted in auto-approved subset. Coverage is subset total / overall total; agreement is subset matched / subset total. Matched, unlabelled and valid disagreements cannot exceed respective overall counts. This is not independent evidence of safety. |
| `confusion` | Array of `{criterion_id, rows, columns, values}`. Unique criterion ids, must reference `criteria`. Rows = reference answer labels, columns = predicted labels; values = rectangular nonnegative integer counts. Labels are unique strings. Reserved column `__missing__` = missing, `__error__` = error, reserved row `__unlabelled__` = missing reference; the row's cells are counted as unlabelled first. Matrix total, diagonal matches and reserved buckets must exactly reconcile with that criterion's counts. N/A is a normal answer label, not a missing prediction. UI renders nonzero gold × predicted cells as a narrow table to fit mobile. |

No reference text, conversation text, evidence quotes, audio URLs or credentials belong in this response. Summary artifacts with real metrics must be audited by the parent; no real measurement is bundled with this module.

## Validation and tests

Exports: `validateQuality(payload)`, `agreement(counts)`, `formatPercent(numerator, denominator)`, `reportCaveats(report)`, `mount(CQA)`.
`validateQuality` returns `{state: 'empty'|'ready'|'invalid', reports, issues}`. On invalid input, reports is empty and issues identify the invalid path. It never mutates input. Unknown extra fields are ignored. It validates arithmetic/provenance consistency, not authenticity: the producer must audit raw artifacts, expected cells, dataset identity and leakage.

Tests use only invented aggregates and an isolated DOM fixture, never providers or real conversations:

```sh
node --test tests/frontend_quality*.test.mjs
```

Browser fixture checks, when available, use synthetic `CQA.api.quality()` only. No public measurements, network providers or core/index/API edits are needed to test this module.

Historical pre-W8 fixture evidence: 63 validation/lifecycle checks passed. The earlier separate Chromium fixture passed at 375/768/1024/1440 px, normal and projector modes: no horizontal overflow, targets at least 44 px, contrast at least 4.5:1, keyboard refresh/details, empty/error/invalid responses and reduced-motion loading. This covered the old isolated module, not the W8 analyst view, parent API wiring or real model quality. **Current W8 1440/projector browser verification and screenshots are blocked by browser access permissions and remain unverified.** Pure DOM tests are not a substitute for those visual checks.

To repeat the optional browser test, set `CQA_QUALITY_PLAYWRIGHT_MODULE` to an installed Playwright `index.mjs` and run `node --test tests/frontend_quality_browser.test.mjs`. It serves only in-memory synthetic fixtures and local CSS/JS on loopback; no provider calls. Optional `CQA_QUALITY_SCREENSHOT_DIR` writes screenshots outside the repository.

## W8 offline statistics extension (backward-compatible v1)

`scripts/quality_report.py` produces this envelope from existing raw artifacts only, without importing the evaluator or invoking models. `schema_version` remains **1** and the original report, counts, denominator and partition constraints above are unchanged. Legacy consumers can ignore these additional fields. The producer fixes the 21-dialogue `eval/split.json` holdout for every run, including files containing all 65 DEV dialogues; `sample.source_dialogues` records the source row count. This selection was reused for model/prompt choices and is not untouched validation.

There is one report for each **run × reference** (`silver`, `gold_v2`, `gold_v2_full`). Each reference retains its own missing labels and SHA256. IDs for the silver Gemini/cascade reports are retained so `profile_catalog.cloud.report_id` and `.cascade.report_id` remain compatible with the model selector. Reference hashes live in `reference.sha256`; run hashes in `source.sha256`. `model.resolved` stays null when logs record only a requested alias.

### Confidence intervals

Reports and each criterion/language/channel group can add `agreement_ci`, `all_cells_ci`; groups also add `dialogues` (unique conversations represented, not cell count). The following numbers are synthetic:

```json
{
  "agreement_ci": {
    "lower": 0.91, "upper": 0.98, "level": 0.95,
    "method": "dialogue_cluster_percentile", "unit": "dialogue",
    "repetitions": 10000, "valid_repetitions": 10000, "seed": 20261108,
    "effective_dialogues": 21
  },
  "dialogues": 21
}
```

- `agreement_ci` uses `matched / (total - unlabelled)`. Missing predictions and errors remain in this denominator.
- `all_cells_ci` uses `matched / total`, including unlabelled planned cells as unverified.
- Resample entire dialogues, with replacement, **10,000 times**, NumPy `default_rng(seed=20261108)`; sum numerator and denominator inside each sample. Bounds are linear 2.5th/97.5th percentiles, fractions in `[0,1]`, serialized to eight decimal places. Regenerating the same input bytes produces the same JSON and Markdown.
- A zero-denominator replicate has no defined ratio; `valid_repetitions` discloses how many are defined. `effective_dialogues` counts dialogue clusters with a positive denominator. No labelled cells: null bounds and `reason: "no_labelled_cells"`. Fewer than two **informative** dialogue clusters: null bounds and `reason: "insufficient_clusters"`, even if additional planned dialogues have no labels.
- A one-dialogue language subgroup must never show a point interval as precision. With zero observed errors, the empirical bootstrap may still be degenerate at 1; this describes the observed sample, not a guarantee of perfect future performance. Two voice calls are also insufficient for a strong generalization claim.
- Dialogue clusters may still share translated/repeated scenarios. Intervals do not account for that higher-level dependence or for adaptive model selection; both limitations appear in `methodology`.
- Original all-cell metrics remain available. Agreement on labelled cells cannot be presented as the all-cells percentage or as independent human accuracy.

### Paired comparisons

Optional top-level `comparisons` is an array of:

```json
{
  "report_id": "synthetic-model", "baseline_report_id": "synthetic-gemini",
  "reference_id": "silver", "win": 2, "loss": 4,
  "compared": 100, "dialogues": 20, "delta": -0.02,
  "delta_ci": {"lower": -0.06, "upper": 0.02, "level": 0.95,
    "method": "dialogue_cluster_percentile", "unit": "dialogue",
    "repetitions": 10000, "valid_repetitions": 10000, "seed": 20261108},
  "p_value": 0.6875, "significant": false, "conclusion": "within_noise",
  "exploratory": true, "missing_or_error_cells": {"model": 0, "baseline": 0}
}
```

Eligible cells are the **same planned cells with a reference label**, regardless of whether either prediction is missing/invalid. A missing/invalid prediction is not a match; it is not silently excluded. `win` means only the candidate matches; `loss` means only Gemini matches. `delta = (win-loss)/compared`. Paired bootstrap uses one set of sampled dialogue indices for the candidate-minus-baseline numerator and common denominator; delta bounds lie in `[-1,1]`.

`p_value` is the exact two-sided McNemar/binomial p-value `min(1, 2 * P[Binomial(win+loss,.5) <= min(win,loss)])`; with zero discordances among observed pairs it equals 1. With no labelled pairs (`compared:0`), p-value and delta are null, not evidence of agreement. The cell-level independence assumption is questionable within dialogues, so its p-value is supplementary. The conservative `significant` flag requires **both** p < .05 and a dialogue-bootstrap interval excluding zero. Otherwise `conclusion` is `within_noise`; this is not proof of equivalence. Multiple comparisons and historical model/guidance selection are **not** adjusted. All these checks are exploratory. Even an interval excluding zero alone is not the joint significance flag.

### Stability

Optional top-level `stability` contains `{id,left_run_id,right_run_id,matched,total,planned_cells,missing_or_error,dialogues,agreement_ci,same_configuration,configuration_note,left_configuration_sha256,right_configuration_sha256}`. `total` is the cells with two valid predictions; missing/error cells remain explicitly counted against `planned_cells`. There is no reference label in this calculation. Both runs must be present in the report set.

Configuration identity compares preset, mode, guidance, template, readings and criterion prompt-version sets. Haiku early/late g9 has matching recorded configuration, but model resolution and cache state are unknown, so this does not establish independent stochastic repetition. Sonnet CLI/API has different transport/requested model IDs: call it cross-configuration agreement, not repeatability of an identical system.

### Latency, cost and provenance

Latency p50/p95 uses **nearest rank**, sorted values at `ceil(p*n)-1`, per dialogue; optional `latency.mean_seconds` is the arithmetic mean only when all planned rows have timings. These files were produced by a CLI harness around the evaluator, so source is `cli` and scope is `cli_wall`, even when the provider is OpenRouter. It includes local evaluation overhead and is not a measured browser/API endpoint latency. Naive log timestamps are interpreted as UTC+5 and the assumption is explicit.

Cache provenance is inferred from the explicit cost semantics in `llm.py`: zero charged `cost_kzt` with positive `cost_kzt_full` means replay; a smaller positive charged cost means mixed cached/fresh calls; equal positive costs indicate fresh charged usage. Zero/zero, missing values or inconsistent costs remain unknown. This is **not an explicit cache flag in the slim file** and `cache_provenance` states the inference. The routing artifact is cached: its millisecond wall time must not be marketed as fresh model speed. Mixed/unknown wall time also cannot establish a fresh SLA.

**Correction to the initial W8 cost audit:** the loss of per-stage token attribution does not erase stored monetary attribution. `llm.py` prices each call before `evaluator._cascade` adds its stages' `cost_kzt_full`. The current report sums each evaluation's **stored full cost once**, checks the dialogue-level sum, and sets `cost.basis:"reported"`. It does not apply the final expensive model's token price to the entire cascade. The visible label is **«стоимость — по ценам моделей в каждом вызове; токены по моделям не разделены»**. This is a reported usage estimate, not an invoice or proof that the historical pricing table covered every call.

`cost_audit` retains `{known_dialogues,unknown_dialogues,total_dialogues,reasons,reason_counts,lower_bound_usd,upper_bound_usd,basis,allocation_note}`. `known + unknown = total` counts **availability of recorded full costs**, not certainty of historical pricing. Monetary bounds are null in this revision. Added fields:

- `method:"stored_full_cost"`, `recorded_total_kzt` (sum of per-evaluation full costs), `recorded_charged_total_kzt` (sum of the separate charged fields) and `zero_charged_positive_full_dialogues` make cache/cost accounting auditable. These two totals are never added together.
- `zero_cost_paid_usage_dialogues` counts dialogues with a zero stored full-cost cell despite nonzero usage for a known paid model. Such records remain visible as recorded zero but trigger `lower_bound:true`, `pricing_complete:false`, `comparable:false`, and a **visible warning of incomplete accounting**. Missing full-cost fields or a mismatch against the dialogue sum make the whole report cost unknown, never an average over only known rows.
- `combined_stage_dialogues` counts model labels containing the recorded stage arrow. Their merged tokens prevent validating the historical coverage of each stage from slim artifacts: `pricing_complete:null`, `comparable:false`. This is **undetermined coverage**, not proof of undercharging or proof that full cost is absent.
- `currency_conversion:{usd_kzt,source:"config/models.json:usd_kzt",assumption:true}` and `currency_conversion_assumed:true` make conversion explicit: historic run FX was not saved, so the current configured rate converts recorded KZT to USD. No market exchange-rate claim is made.
- `coverage_note` is mandatory visible prose when interpreting recorded costs. Historical prices and CLI subscription invoices are separate.
- `token_price_estimate` is an optional **separate current-price scenario** using allocated tokens once and exact config price keys. It remains unknown for merged stages/unpriced aliases. It must never be compared to another model's historical `reported` cost as a confirmed savings ratio. The original “2.2× cheaper” ratio mixed these methods; it is not certified here.

### Routing and bank-economics sources

The actual saved `or-hold-routing-g9` run is included against all three references and paired against Gemini. It is not a hypothetical recombination and the offline report does not rerun inference. `profile_catalog` now has cloud/cascade/routing/local. Routing includes `{label,first_label,escalate_label,report_id}`; local has no measured report and adds `api_cost:{amount:0,currency:"USD",basis:"no_external_api",scope:"local_only",source}` when its configured provider is Ollama. Local hardware, electricity, speed, accuracy and review fraction are unknown, not zero.

Each report adds `economics`:

```text
dialogues, criteria_per_dialogue,
cost_per_dialogue_kzt/usd, cost_per_pair_kzt/usd,
mean_seconds_per_dialogue, mean_seconds_per_pair,
recorded_mean_seconds_per_dialogue, p50_seconds, p95_seconds, timed_dialogues,
review_count, review_known_dialogues, review_fraction, review_basis,
currency_fx: {usd_kzt,source,assumption},
cost_provenance: {method,comparable,pricing_complete,zero_cost_paid_usage_dialogues,
                  coverage_note,currency_conversion_assumed,lower_bound},
latency_provenance: {cache_state,projection_allowed,basis,note},
channels: {chat: <same scalar fields>, call: <same scalar fields>, unknown?: ...}
```

Costs derive from the recorded full-cost totals. Per-pair values are **amortized planned-criterion costs**, not marginal costs of changing one criterion: fast mode can call the whole checklist. Per-channel means permit weighting the requested chat/call mix instead of applying the DEV mix universally. If a channel is absent, it remains absent/unknown.

`review_count` uses **final `needs_review` flags**, never escalation count: any final criterion true marks a dialogue for review. A complete set of explicit false flags means no review; missing flags without an explicit true remain unknown. `review_fraction` is null unless all planned dialogues have known flag status. It is an operational flag rate, not measured reviewer workload, safety, or quality after human review.

`recorded_mean_seconds_per_dialogue` always describes the observed run when all timings exist. `mean_seconds_per_dialogue/per_pair` are available for projection only when cache provenance is fresh and timing coverage complete; otherwise null. Even fresh serial extrapolation is not measured parallel throughput. Do not turn cached milliseconds into a ten-thousand-dialogue inference estimate.

`model_usage` has `{gemini_dialogues,escalated_dialogues,direct_routed_calls,known_dialogues,total_dialogues,basis}`, derived from saved evaluation model labels. This identifies observed routes, not complete telemetry of every provider call, and is independent of the review fraction. Do not substitute config documentation's expected route count for this observed count.

`methodology.inputs` lists filename + SHA256 for the checklist, split, model-price config and references. A change to an input during generation is rejected. Outputs contain no dialogue transcript, evidence quote, message ID or absolute machine-specific path. Only aggregate model/run/reference identifiers are published. The generator reads raw JSONL in place and never copies them into the repository.

```sh
python scripts/quality_report.py --source-root /path/to/w-int --legacy-root /path/to/w-m1 --references-root /path/to/agents/reports
python -m pytest -q tests/test_quality_report.py
```
