# CRAFT V5 Repeated Zero-Progress Findings

This records the first Gemma4 12B evaluation of V5, the opt-in `value_of_information + suppress_repeated_zero_progress` policy.

## Inputs

V5 config:

- `configs/craft/eval_gemma4_12b_ollama_dual_dag_value_of_information_repeated_zero_fix.yaml`

Temporary manifests used:

- `/tmp/opencode/craft_v5_repeated_zero_smoke.yaml`
- `/tmp/opencode/craft_v5_repeated_zero_oracle5_turn30.yaml`

Generated reports:

- `result/craft/comparison_gemma4_12b_v4_v5_repeated_zero_smoke.csv`
- `result/craft/summary_gemma4_12b_v4_v5_repeated_zero_smoke.csv`
- `result/craft/comparison_gemma4_12b_v1_v4_v5_oracle5_turn30.csv`
- `result/craft/summary_gemma4_12b_v1_v4_v5_oracle5_turn30.csv`
- `result/craft/trace_compare_v4_v5_oracle5_turn30_seed{1,3,5}.json`
- `result/craft/trace_compare_v1_v5_oracle5_turn30_seed{1,3,5}.json`

## Smoke Result

The 5-turn smoke compared existing V4 smoke artifacts against newly generated V5 smoke artifacts for structures `[0, 1, 2, 3, 4]` and seeds `[1, 3, 5]`.

| Policy | n | mean_final_progress | progress_auc | physical_action_count | clarify_count | failed_clarification_count | builder_fallback_rate | retrieved_node_count | leakage_passed |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| V4 VOI | 3 | 0.251387163561077 | 0.153322352580324 | 23.666667 | 1.333333 | 0.0 | 0.0 | 0.0 | true |
| V5 VOI + repeated-zero suppression | 3 | 0.259242744021507 | 0.157131385629936 | 24.333333 | 0.666667 | 0.0 | 0.0 | 0.0 | true |

Trace comparison for V4 to V5 smoke showed:

- Mean final-progress delta: `+0.007758914193696803`.
- Mean max repeated zero-or-missing-progress streak: V4 `1.0666666666666667`, V5 `1.0`.
- Total zero-or-missing-progress turns: V4 `19`, V5 `17`.

However, V5 suppression did not activate in the 5-turn smoke (`0` action-selection suppression events). The smoke improvement should therefore be treated as diagnostic variance, not causal evidence for the suppression policy.

## Oracle5 Turn30 Result

The 30-turn comparison used structures `[0, 1, 2, 3, 4]`, seeds `[1, 3, 5]`, and `oracle_n=5`.

| Policy | n | mean_final_progress | delta vs V1 | progress_auc | physical_action_count | clarify_count | failed_clarification_count | builder_fallback_rate | retrieved_node_count | leakage_passed |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| V1 Clarify disabled | 3 | 0.660711741291451 | 0.0 | 0.315934824434824 | 150.0 | 0.0 | 0.0 | 0.137778 | 0.0 | true |
| V4 VOI | 3 | 0.647042265303135 | -0.013669475988316 | 0.260894673210132 | 150.0 | 0.0 | 0.0 | 0.111111 | 0.0 | true |
| V5 VOI + repeated-zero suppression | 3 | 0.663174463754174 | 0.002462722462722 | 0.272977793811127 | 149.333333 | 0.666667 | 0.0 | 0.117778 | 0.0 | true |

Trace comparison for V4 to V5 turn30 showed:

- Mean final-progress delta: `+0.016132198451039036`.
- Negative structure-level deltas: `3 / 15` structure-seed pairs.
- Positive structure-level deltas: `5 / 15` structure-seed pairs.
- Mean max repeated zero-or-missing-progress streak: V4 `5.066666666666666`, V5 `4.333333333333333`.
- Total zero-or-missing-progress turns: V4 `168`, V5 `160`.
- Total negative-progress turns: V4 `62`, V5 `64`.

Trace comparison for V1 to V5 turn30 showed:

- Mean final-progress delta: `+0.0024627224627224555`.
- Mean max repeated zero-or-missing-progress streak: V1 `3.0`, V5 `4.333333333333333`.
- Total zero-or-missing-progress turns: V1 `143`, V5 `160`.
- Total negative-progress turns: V1 `74`, V5 `64`.

## Caveat

V5 action-selection suppression did not activate in the 30-turn evaluation either (`0` suppression events across seeds `[1, 3, 5]`). The V5 aggregate improvement over V4 and slight improvement over V1 cannot yet be attributed to the repeated-zero suppression mechanism.

The current suppression trigger is likely too narrow: it only down-ranks a candidate when the same action signature is both recently repeated with zero or missing progress and appears again in the current oracle candidate set. The trace problem also includes broader loops and terminal-turn action ordering effects that may not reuse the exact same candidate signature at the moment suppression is checked.

## Implication

V5 should remain experimental and opt-in. The next implementation should focus on making the action-selection intervention observable and better aligned with the failure mode:

- Post-instrumentation diagnostics are recorded in `benchmarks/craft/V5_ACTION_SELECTION_DIAGNOSTICS.md`.
- Consider suppressing or penalizing broader repeated no-progress regions, not only exact current-candidate repeats.
- Consider terminal-turn loop handling for large block place/remove cycles.
- Prototype V6 first as relaxed-match diagnostics, then enable ordering changes after the trigger is verified.
