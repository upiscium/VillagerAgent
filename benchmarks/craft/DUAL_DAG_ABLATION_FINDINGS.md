# CRAFT Dual-DAG Ablation Findings

This records the Gemma4 12B C0-C6 ablation smoke results for #136.

The run is diagnostic smoke, not final performance evaluation. It uses `gemma4:12b`, 5 turns, structures `[0, 1, 2, 3, 4]`, and seeds `[1, 3, 5]`.

## Inputs

Primary manifest:

```bash
python -m benchmarks.craft.experiment --config configs/craft/experiments/gemma4_12b_dual_dag_ablation_smoke.yaml
```

The primary command timed out after producing C0-C5 and C6 seed 1 artifacts. C6 seeds 3 and 5 were completed with a temporary local manifest containing only the existing C6 config and the same `_ablation_smoke_c6_full_dual_dag` suffix.

## Aggregate Results

| Condition | n | mean_final_progress | delta vs C0 | progress_auc | physical_action_count | clarify_count | gated_clarification_count | retrieved_node_count | fallback_count |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| C0 VA baseline | 3 | 0.26504991948470213 | 0.0 | 0.16131143317230273 | 25.0 | 0.0 | 0.0 | 0.0 | 0.0 |
| C1 metadata only | 3 | 0.26504991948470213 | 0.0 | 0.16131143317230273 | 25.0 | 0.0 | 0.0 | 0.0 | 0.0 |
| C2 current evidence | 3 | 0.26504991948470213 | 0.0 | 0.16131143317230273 | 25.0 | 0.0 | 0.0 | 0.0 | 0.0 |
| C3 retrieval | 3 | 0.26504991948470213 | 0.0 | 0.16131143317230273 | 25.0 | 0.0 | 0.0 | 0.0 | 0.0 |
| C4 gating, no coordination action | 3 | 0.26504991948470213 | 0.0 | 0.16131143317230273 | 25.0 | 0.0 | 0.3333333333333333 | 0.0 | 0.0 |
| C5 clarify only | 3 | 0.2438107507093014 | -0.021239168775400746 | 0.1544411011425504 | 23.333333333333332 | 1.6666666666666667 | 1.6666666666666667 | 0.0 | 0.0 |
| C6 full Dual-DAG | 3 | 0.24224108580630319 | -0.02280883367839895 | 0.15310500728471743 | 23.0 | 2.0 | 2.0 | 0.0 | 0.0 |

## Diagnosis

C1-C4 are indistinguishable from C0 on progress, progress AUC, and physical-action throughput in this smoke run. That argues against graph bookkeeping, current evidence summaries, retrieval, and gate scoring metadata as the immediate cause of the observed regression.

The first drop appears at C5, where Clarify coordination actions are emitted. Mean final progress falls by `0.021239168775400746` while physical actions fall from `25.0` to `23.333333333333332`. C6 is similar, with mean final progress down by `0.02280883367839895` and physical actions down to `23.0`.

Retrieval did not activate in this smoke run: `retrieved_node_count` and `retrieval_used_in_top_action_count` are `0.0` for C3-C6. This run cannot evaluate retrieval quality or pollution; it only shows retrieval was not the active regression path here.

Builder fallback did not activate: `fallback_count` is `0.0` for all conditions.

Partial-information checks in normalized summaries stayed false for target blueprint exposure, oracle plan exposure, and director view payload sharing.

## Recommended Fix Direction

For #131, prioritize config-gated Clarify behavior changes rather than evidence summary, graph bookkeeping, or retrieval changes.

Concrete candidates:

1. Raise or disable Clarify gating for 5-turn progress smoke when the candidate action is already executable and has no public conflict.
2. Make Clarify non-turn-consuming or delayed unless the expected mistake cost clearly exceeds the lost physical-action opportunity cost.
3. Keep C4-style gate metadata available for diagnostics so the gate can be measured without reducing action throughput.

Avoid changing retrieval behavior based on this smoke alone, because retrieval did not activate.

## Follow-Up Fix

#131 adds an opt-in gate option, `dual_dag.gated_clarification.suppress_executable_low_confidence`, and the Gemma4 config `configs/craft/eval_gemma4_12b_ollama_dual_dag_clarify_throughput_fix.yaml`.

When enabled, the gate suppresses Clarify actions caused only by low confidence if the selected candidate is already executable. Conflict, required-evidence, and large-block span uncertainty Clarify behavior remains unchanged.
