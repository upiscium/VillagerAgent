# Gemma4 12B Progress Smoke Batch

This report covers a 5-turn diagnostic smoke batch. It should not be reported as final CRAFT progress performance; use `configs/craft/experiments/gemma4_12b_progress_full.yaml` for the full 20-turn evaluation manifest.

## Batch

- Historical manifest: `configs/craft/experiments/gemma4_12b_progress_large.yaml`
- Preferred smoke manifest: `configs/craft/experiments/gemma4_12b_progress_smoke.yaml`
- Full evaluation manifest: `configs/craft/experiments/gemma4_12b_progress_full.yaml`
- Model: `gemma4:12b` through `http://ollama.arc.upiscium.dev`
- Structures: `0,1,2,3,4`
- Turns: `5`
- Seeds: `1,3,5`
- Conditions:
  - Official CRAFT baseline: `configs/craft/official_baseline_gemma4_12b_ollama.yaml`
  - VA baseline: `configs/craft/eval_gemma4_12b_ollama.yaml`
  - VA + Dual-DAG: `configs/craft/eval_gemma4_12b_ollama_dual_dag.yaml`

## Commands

```bash
python -m benchmarks.craft.experiment --config configs/craft/experiments/gemma4_12b_progress_large.yaml
python -m benchmarks.craft.validate_comparison_configs --baseline configs/craft/eval_gemma4_12b_ollama.yaml --treatment configs/craft/eval_gemma4_12b_ollama_dual_dag.yaml
python -m benchmarks.craft.experiment_summary --runs craft_official_baseline_gemma4_12b_ollama_progress_large_official_seed1 craft_official_baseline_gemma4_12b_ollama_progress_large_official_seed3 craft_official_baseline_gemma4_12b_ollama_progress_large_official_seed5 craft_eval_gemma4_12b_ollama_progress_large_baseline_seed1 craft_eval_gemma4_12b_ollama_progress_large_baseline_seed3 craft_eval_gemma4_12b_ollama_progress_large_baseline_seed5 craft_eval_gemma4_12b_ollama_dual_dag_progress_large_dual_dag_seed1 craft_eval_gemma4_12b_ollama_dual_dag_progress_large_dual_dag_seed3 craft_eval_gemma4_12b_ollama_dual_dag_progress_large_dual_dag_seed5 --result-root result/craft --output result/craft/summary_gemma4_12b_progress_large_progress_large.csv --json-output result/craft/summary_gemma4_12b_progress_large_progress_large.json --variance-output result/craft/variance_gemma4_12b_progress_large_progress_large.csv --variance-json-output result/craft/variance_gemma4_12b_progress_large_progress_large.json --variance-group-by run_group
```

Future smoke runs should use:

```bash
python -m benchmarks.craft.experiment --config configs/craft/experiments/gemma4_12b_progress_smoke.yaml
```

The full evaluation manifest can be checked without executing model calls with:

```bash
python -m benchmarks.craft.experiment --config configs/craft/experiments/gemma4_12b_progress_full.yaml --dry-run
```

## Generated Reports

- `result/craft/comparison_gemma4_12b_progress_large_progress_large.csv`
- `result/craft/comparison_gemma4_12b_progress_large_progress_large.json`
- `result/craft/summary_gemma4_12b_progress_large_progress_large.csv`
- `result/craft/summary_gemma4_12b_progress_large_progress_large.json`
- `result/craft/variance_gemma4_12b_progress_large_progress_large.csv`
- `result/craft/variance_gemma4_12b_progress_large_progress_large.json`

## Run-Group Aggregate

| Condition | Runs | Mean progress | Progress stddev | Min | Max | Completion | Builder fallback | Mean confidence | Leakage |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| Official CRAFT baseline | `3` | `0.0` | `0.0` | `0.0` | `0.0` | `0.0` | `0.0` | `0.0` | pass |
| VA baseline | `3` | `0.26504991948470213` | `0.0` | `0.26504991948470213` | `0.26504991948470213` | `0.0` | `0.0` | `0.8973333333333331` | pass |
| VA + Dual-DAG | `3` | `0.24503826393681463` | `0.010086016019350387` | `0.23080055210489991` | `0.2529036116862204` | `0.0` | `0.0` | `0.8700000000000001` | pass |

## Seed-Level Progress Deltas

| Seed | VA baseline - official | Dual-DAG - VA baseline | Dual-DAG - official |
| ---: | ---: | ---: | ---: |
| `1` | `0.26504991948470213` | `-0.013639291465378456` | `0.2514106280193237` |
| `3` | `0.26504991948470213` | `-0.012146307798481748` | `0.2529036116862204` |
| `5` | `0.26504991948470213` | `-0.03424936737980222` | `0.23080055210489991` |
| Mean | `0.26504991948470213` | `-0.020011655547887475` | `0.24503826393681463` |

## Post-Hoc Throughput Diagnostics

These diagnostics were derived from existing normalized `turns.jsonl` artifacts without rerunning the model.

| Condition | Progress AUC | Physical actions | Clarify | Wait | Positive turns | Zero turns | Negative turns | Delta / turn | Delta / physical action |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Official CRAFT baseline | `0.0` | `0.0` | `0.0` | `0.0` | `0.0` | `25.0` | `0.0` | `0.0` | `0.0` |
| VA baseline | `0.1613114331723028` | `25.0` | `0.0` | `0.0` | `21.0` | `0.0` | `4.0` | `0.014415458937198066` | `0.014415458937198066` |
| VA + Dual-DAG | `0.1541060041407868` | `23.0` | `2.0` | `0.0` | `19.0` | `2.0` | `4.0` | `0.014415458937198068` | `0.015731155586228052` |

## Interpretation

This batch does not support a Dual-DAG progress advantage over the VA baseline. Both VA conditions outperform the official CRAFT baseline on mean progress, but VA + Dual-DAG trails VA baseline by `0.020011655547887475` mean progress across the matched seeds.

Builder fallback does not explain the gap in this batch: all three conditions have `0.0` builder fallback rate. Leakage checks passed for all runs.

Post-hoc throughput diagnostics suggest that the smoke-run gap is more consistent with reduced physical action throughput than worse physical action quality: VA + Dual-DAG executes fewer physical actions and spends turns on Clarify, while mean progress delta per physical action is not lower than VA baseline.

The variance summary must be grouped by `run_group` for this manifest. Grouping by `condition` merges VA baseline and VA + Dual-DAG because both use `villageragent_directors` as the condition label.
