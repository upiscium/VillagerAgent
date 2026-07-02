# CRAFT V5 Action-Selection Diagnostics

This note records the first post-instrumentation diagnostic run for the V5 repeated-zero action-selection policy. The goal was to determine why V5 previously showed no observed suppression events, not to establish a new performance result.

## Setup

- Model: `gemma4:12b`
- Condition: VA + Dual-DAG + VOI Clarify + repeated-zero action-selection policy
- Config: `configs/craft/eval_gemma4_12b_ollama_dual_dag_value_of_information_repeated_zero_fix.yaml`
- Horizon: `turns=30`
- Oracle candidates: `oracle_n=5`
- Seeds: `[1, 3, 5]`
- Structures: `[0, 1, 2, 3, 4]`
- Temporary manifest: `/tmp/opencode/craft_action_selection_diagnostics_v5_oracle5_turn30.yaml`
- Generated artifacts:
  - `result/craft/comparison_gemma4_12b_action_selection_diagnostics_v5_oracle5_turn30.csv`
  - `result/craft/summary_gemma4_12b_action_selection_diagnostics_v5_oracle5_turn30.csv`
  - `result/craft/variance_gemma4_12b_action_selection_diagnostics_v5_oracle5_turn30.csv`

All three V5 diagnostic runs completed and passed leakage checks.

## Aggregate Results

| Metric | Seed 1 | Seed 3 | Seed 5 | Mean |
| --- | ---: | ---: | ---: | ---: |
| `mean_final_progress` | 0.6231281761716544 | 0.6739901081205428 | 0.639691323169584 | 0.6456032024872603 |
| `progress_auc` | 0.2892679500317181 | 0.35707404340157967 | 0.3020017462408767 | 0.3161145798913915 |
| `physical_action_count` | 149 | 150 | 150 | 149.66666666666666 |
| `clarify_count` | 1 | 0 | 0 | 0.3333333333333333 |
| `fallback_count` | 19 | 25 | 20 | 21.333333333333332 |

This rerun should not replace the previous V1/V4/V5 performance comparison. The run was performed after adding diagnostics and is used here only to classify trigger behavior.

## Suppression Diagnostics

| Metric | Seed 1 | Seed 3 | Seed 5 | Total |
| --- | ---: | ---: | ---: | ---: |
| `action_selection_suppression_enabled_count` | 150 | 150 | 150 | 450 |
| `action_selection_suppression_disabled_count` | 0 | 0 | 0 | 0 |
| `action_selection_suppression_attempt_count` | 92 | 113 | 98 | 303 |
| `action_selection_no_candidate_count` | 58 | 37 | 52 | 147 |
| `action_selection_repeated_zero_signature_count` | 1 | 0 | 1 | 2 |
| `action_selection_suppression_no_match_count` | 1 | 0 | 1 | 2 |
| `action_selection_all_candidates_suppressed_count` | 0 | 0 | 0 | 0 |
| `action_selection_suppression_applied_count` | 0 | 0 | 0 | 0 |
| `action_selection_suppressed_candidate_count` | 0 | 0 | 0 | 0 |

Key interpretation: V5 was enabled for every turn and evaluated candidates on 303 turns, but only detected two repeated zero/missing-progress exact action signatures. Both detected signatures failed to match any current candidate, so no reorder was applied.

## Event-Level Details

Detected repeated signatures occurred only twice:

| Seed | Structure | Turn | Current Action | Detected Repeated Signature | Outcome |
| --- | ---: | ---: | --- | --- | --- |
| 1 | 0 | 23 | `remove bs (2,1) layer 0` | `place|bs|(2,2)|0|None` | `no_match=true` |
| 5 | 4 | 23 | `remove bs (1,1) layer 0` | `place|bs|(2,0)|0|None` | `no_match=true` |

Neither event suppressed a candidate. The current action in both cases was a `remove`, while the repeated signature was a previous `place` at a different coordinate.

## Conclusion

The V5 exact-action repeated-zero trigger is too narrow for the observed failure modes. The policy did not fail because all candidates were suppressed, nor because it was disabled. It failed because repeated exact signatures were almost never detected, and the two detected signatures were not present in the current candidate set.

This supports the prior caveat: V5 aggregate changes should not be attributed to the repeated-zero suppression mechanism. The mechanism was observably active as diagnostics but behaviorally inactive as a reorder policy.

## Implications For V6

V6 should broaden the trigger while remaining opt-in/config-gated. Candidate directions:

- Region-level suppression: track repeated no-progress actions by coordinate/layer or small neighborhood, not exact action/block/span signature only.
- Inverse-loop suppression: detect place/remove alternation over the same location, including large-block spans.
- Terminal no-candidate handling: separate turns with `oracle_moves` exhausted from turns with actionable candidates; V5 observed 147 no-candidate diagnostic turns.
- Candidate-relative loop detection: compare current candidates against recent public actions using relaxed equivalence classes, then down-rank only when an alternative candidate remains.

Recommended next step: design V6 as an opt-in policy that first logs relaxed-match candidates without changing ordering, then enables reordering once the relaxed diagnostics are verified.
