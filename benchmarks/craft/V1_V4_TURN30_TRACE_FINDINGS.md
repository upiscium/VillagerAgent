# CRAFT V1 vs V4 Turn30 Trace Findings

This records a first trace-level diagnosis for the `oracle_n=5`, 30-turn sensitivity comparison after adding the V1 Clarify-disabled baseline.

## Inputs

Compared runs:

- V1 Clarify disabled: `craft_eval_gemma4_12b_ollama_dual_dag_retrieval_post_official_clarify_disabled_oracle5_turn30_seed{1,3,5}`
- V4 value of information: `craft_eval_gemma4_12b_ollama_dual_dag_value_of_information_post_official_value_of_information_oracle5_turn30_seed{1,3,5}`

Artifacts inspected:

- `normalized/metrics.csv`
- `normalized/turns.jsonl`
- `result/craft/comparison_gemma4_12b_clarify_policy_sensitivity_post_official.csv`

Generated trace comparisons:

- `result/craft/trace_compare_v1_v4_oracle5_turn30_seed1.json`
- `result/craft/trace_compare_v1_v4_oracle5_turn30_seed3.json`
- `result/craft/trace_compare_v1_v4_oracle5_turn30_seed5.json`

## Aggregate Result

| Policy | n | mean_final_progress | delta vs V1 | progress_auc | physical_action_count | clarify_count | failed_clarification_count | builder_fallback_rate | retrieved_node_count |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| V1 Dual-DAG, Clarify disabled | 3 | 0.660711741291451 | 0.0 | 0.315934824434824 | 150.0 | 0.0 | 0.0 | 0.137778 | 0.0 |
| V4 Dual-DAG, value of information | 3 | 0.647042265303135 | -0.013669475988316 | 0.260894673210132 | 150.0 | 0.0 | 0.0 | 0.111111 | 0.0 |

V4 preserves physical throughput and avoids Clarify, so the remaining progress gap is not a communication-turn loss.

## Automated Trace Comparison

`benchmarks.craft.trace_compare` compares normalized `metrics.csv` and `turns.jsonl` artifacts for a baseline/variant run pair. For the three V1/V4 seeds, it reports:

- Mean final-progress delta: `-0.013669475988316582`.
- Negative structure-level deltas: `3 / 15` structure-seed pairs.
- Positive structure-level deltas: `3 / 15` structure-seed pairs.
- Mean max repeated zero-or-missing-progress streak: V1 `3.0`, V4 `5.066666666666666`.
- Total zero-or-missing-progress physical turns: V1 `143`, V4 `168`.
- Total negative-progress turns: V1 `74`, V4 `62`.
- V4 has a larger repeated zero-or-missing-progress streak than V1 in `10 / 15` structure-seed pairs.

## Structure-Level Pattern

The V4 deficit is concentrated in a small number of structure/seed pairs:

- seed 1, structure 0: V4 trails V1 by `-0.09776857602944566`.
- seed 3, structure 1: V4 trails V1 by `-0.0488267770876466`.
- seed 5, structure 1: V4 trails V1 by `-0.09776857602944566`.

Several structure/seed pairs are identical on final progress, and structure 2 slightly favors V4 for seeds 3 and 5. This suggests a trace-level action-selection difference rather than a uniform policy-wide throughput penalty.

## Trace-Level Diagnosis

V1 and V4 often choose different valid physical action orders early in the episode. In the negative cases, the gap emerges from later action dynamics rather than Clarify:

- V4 sometimes reaches long stretches of repeated zero-progress placements after the useful construction phase.
- V1 sometimes enters place/remove loops on high-progress large blocks, but those loops can end on a positive placement at turn 30, improving final progress.
- V4 can preserve throughput while ending on a lower-progress board state because its repeated or offset physical actions do not recover the same high-value final block placement.
- Fallback is not the primary explanation for the V4 deficit in aggregate: V4 has lower mean fallback rate than V1 (`0.111111` vs `0.137778`).
- Retrieval remains inactive (`retrieved_node_count = 0.0`), so the observed difference is not retrieval behavior.

## Implication

The next fix should not target Clarify frequency. V4 already suppresses Clarify in this setting. The likely next target is physical action selection after VOI gating is enabled, especially repeated zero-progress placements and terminal-turn sensitivity for large block place/remove loops.

Recommended follow-up:

- Add a trace report that detects repeated zero-progress physical actions per structure.
- Compare final-turn board deltas for V1/V4 negative cases.
- Investigate whether VOI/gating metadata changes Builder prompt context enough to alter physical action ordering even when Clarify is not selected.
