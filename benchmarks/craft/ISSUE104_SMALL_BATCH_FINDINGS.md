# CRAFT Dual-DAG Small Batch Findings

This note summarizes the first comparable CRAFT small batch for Issue #104 and the evaluation pass for Issue #105.

## Run Matrix

- Baseline: `craft_eval_qwen_ollama_issue104_small_baseline_seed3`
- Dual-DAG: `craft_eval_qwen_ollama_dual_dag_issue104_small_dual_dag_seed3`
- Structures: `0,1`
- Seed: `3`
- Turns: `2`
- Manifest: `configs/craft/experiments/qwen_dual_dag_issue104_small.yaml`

## Generated Reports

- `result/craft/comparison_qwen_dual_dag_issue104_small_issue104_small.csv`
- `result/craft/comparison_qwen_dual_dag_issue104_small_issue104_small.json`
- `result/craft/summary_qwen_dual_dag_issue104_small_issue104_small.csv`
- `result/craft/summary_qwen_dual_dag_issue104_small_issue104_small.json`
- `result/craft/variance_qwen_dual_dag_issue104_small_issue104_small.csv`
- `result/craft/variance_qwen_dual_dag_issue104_small_issue104_small.json`
- `result/craft/dual_dag_analysis_issue104_small.json`
- `result/craft/dual_dag_turns_issue104_small.csv`

## Main Metrics

| Condition | Mean progress | Completion rate | Mean action confidence | Builder fallback count | Gated clarification count | Hypothesis count | Executed candidates | Leakage |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| Baseline | `0.13051529790660224` | `0.0` | `0.9` | `1` | `0` | `10` | `4` | pass |
| Dual-DAG | `0.13051529790660224` | `0.0` | `0.9375` | `3` | `0` | `12` | `4` | pass |

## Failure Modes

- Progress and completion were identical in this tiny batch.
- Dual-DAG produced a higher mean action confidence but also more Builder fallback events (`3` vs `1`).
- No low-confidence, conflict, required-evidence, clarification, or gated-clarification failure modes were observed.
- All action candidates reached `executed`; no invalidated/executed mismatch was observed.
- Hypotheses remained `open` in both conditions (`10` baseline, `12` Dual-DAG), which is expected for a 2-turn smoke-sized batch without clarification resolution.

## Artifact And Safety Checks

- Dual-DAG analysis artifact health passed for both runs.
- `schema_version` was present and matched `1.0.0`.
- Leakage checks passed for both runs.
- The artifact validator passed for #106 smoke and #104 small-batch outputs.

## Follow-Up

- No blocking implementation defect was found in this batch.
- The increased fallback count in the Dual-DAG condition should be monitored in larger batches before drawing conclusions.
