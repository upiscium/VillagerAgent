# Issue 114 Builder Fallback Monitoring

## Intended Batch

- Manifest: `configs/craft/experiments/qwen_dual_dag_issue114_fallback.yaml`
- Structures: `0,1,2`
- Turns: `2`
- Seed: `3`
- Runs:
  - `craft_eval_qwen_ollama_issue114_fallback_baseline_seed3`
  - `craft_eval_qwen_ollama_dual_dag_issue114_fallback_dual_dag_seed3`

## Commands

```bash
python -m benchmarks.craft.experiment --config configs/craft/experiments/qwen_dual_dag_issue114_fallback.yaml
python -m benchmarks.craft.fallback_monitor --runs craft_eval_qwen_ollama_issue114_fallback_baseline_seed3 craft_eval_qwen_ollama_dual_dag_issue114_fallback_dual_dag_seed3 --result-root result/craft --output result/craft/fallback_monitor_issue114.json --csv-output result/craft/fallback_monitor_issue114.csv
```

## Current Environment Result

After DNS was fixed to resolve `ollama.arc.upiscium.dev` through `192.168.100.31` and the Ollama configs were switched to `http://ollama.arc.upiscium.dev`, the batch completed for both runs.

Generated reports:

- `result/craft/comparison_qwen_dual_dag_issue114_fallback_issue114_fallback.csv`
- `result/craft/comparison_qwen_dual_dag_issue114_fallback_issue114_fallback.json`
- `result/craft/summary_qwen_dual_dag_issue114_fallback_issue114_fallback.csv`
- `result/craft/summary_qwen_dual_dag_issue114_fallback_issue114_fallback.json`
- `result/craft/variance_qwen_dual_dag_issue114_fallback_issue114_fallback.csv`
- `result/craft/variance_qwen_dual_dag_issue114_fallback_issue114_fallback.json`
- `result/craft/fallback_monitor_issue114.csv`
- `result/craft/fallback_monitor_issue114.json`

Overall fallback comparison:

| Condition | Progress | Completion | Builder fallback count | Builder fallback rate | Leakage |
| --- | ---: | ---: | ---: | ---: | --- |
| Baseline | `0.11790660225442834` | `0.0` | `2` | `0.3333333333333333` | pass |
| Dual-DAG | `0.09628556092324207` | `0.0` | `2` | `0.3333333333333333` | pass |

Structure-level fallback comparison:

| Condition | Structure | Turns | Builder fallback count | Builder fallback rate |
| --- | ---: | ---: | ---: | ---: |
| Baseline | `0` | `2` | `0` | `0.0` |
| Baseline | `1` | `2` | `1` | `0.5` |
| Baseline | `2` | `2` | `1` | `0.5` |
| Dual-DAG | `0` | `2` | `2` | `1.0` |
| Dual-DAG | `1` | `2` | `0` | `0.0` |
| Dual-DAG | `2` | `2` | `0` | `0.0` |

Interpretation: the larger issue114 batch did not reproduce an overall Dual-DAG fallback increase. Dual-DAG had the same aggregate fallback count/rate as baseline, though fallback concentrated on different structures.

## Monitoring Status

- `builder_fallback_count` and `builder_fallback_rate` are already present in normalized CRAFT summaries and comparison reports.
- `benchmarks.craft.fallback_monitor` now reports fallback counts/rates by condition and by condition plus structure.
- Failed runs are recorded separately with `error_type` and `error_message`, rather than being interpreted as Dual-DAG fallback behavior.

## Interpretation Rule

If a future successful larger run shows Dual-DAG consistently increasing fallback rate by condition and structure, inspect Builder prompt/action-candidate metadata and open an implementation fix issue.
