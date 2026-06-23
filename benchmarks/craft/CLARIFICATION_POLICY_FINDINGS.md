# CRAFT Clarification Policy Findings

This records the Gemma4 12B Clarify policy smoke results for #148.

The run is diagnostic smoke, not final performance evaluation. It uses `gemma4:12b`, 5 turns, structures `[0, 1, 2, 3, 4]`, and seeds `[1, 3, 5]`.

## Inputs

Manifest:

```bash
python -m benchmarks.craft.experiment --config configs/craft/experiments/gemma4_12b_clarify_policy_smoke.yaml
```

Artifacts:

- `result/craft/comparison_gemma4_12b_clarify_policy_smoke.json`
- `result/craft/summary_gemma4_12b_clarify_policy_smoke.csv`
- `result/craft/variance_gemma4_12b_clarify_policy_smoke.csv`

## Aggregate Results

| Policy | n | mean_final_progress | delta vs V0 | progress_auc | physical_action_count | clarify_count | beneficial_clarification_count | neutral_clarification_count | failed_clarification_count | builder_fallback_rate | retrieved_node_count |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| V0 VA baseline | 3 | 0.265049919484702 | 0.0 | 0.161311433172303 | 25.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 |
| V1 Dual-DAG, Clarify disabled | 3 | 0.265049919484702 | 0.0 | 0.161311433172303 | 25.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 |
| V2 Dual-DAG, current Clarify | 3 | 0.248251361091941 | -0.016798558392761 | 0.157141967640518 | 23.666667 | 1.333333 | 0.0 | 0.666667 | 0.666667 | 0.0 | 0.0 |
| V3 Dual-DAG, throughput fix | 3 | 0.252405950463922 | -0.012643969020781 | 0.156969894946707 | 24.0 | 1.0 | 0.0 | 1.0 | 0.0 | 0.0 | 0.0 |
| V4 Dual-DAG, value of information | 3 | 0.251387163561077 | -0.013662755923626 | 0.153322352580324 | 23.666667 | 1.333333 | 0.0 | 1.333333 | 0.0 | 0.0 | 0.0 |

## Seed-Level Progress

| Policy | seed 1 | seed 3 | seed 5 |
| --- | ---: | ---: | ---: |
| V0 VA baseline | 0.26504991948470213 | 0.26504991948470213 | 0.26504991948470213 |
| V1 Dual-DAG, Clarify disabled | 0.26504991948470213 | 0.26504991948470213 | 0.26504991948470213 |
| V2 Dual-DAG, current Clarify | 0.26504991948470213 | 0.23926432022084193 | 0.24043984357027837 |
| V3 Dual-DAG, throughput fix | 0.2529036116862204 | 0.26504991948470213 | 0.23926432022084193 |
| V4 Dual-DAG, value of information | 0.24835426731078908 | 0.2529036116862204 | 0.2529036116862204 |

## Diagnosis

V1 exactly matches V0 on mean final progress, progress AUC, and physical-action throughput. In this smoke run, Dual-DAG bookkeeping and retrieval with Clarify disabled did not reduce progress.

V2 reproduces the Clarify throughput regression: mean final progress falls by `0.016798558392761`, physical actions fall from `25.0` to `23.666667`, and Clarify averages `1.333333` turns per run.

V3 improves over V2 but does not restore V0 throughput or progress. It removes failed Clarify outcomes in this smoke and raises mean physical actions to `24.0`, but still trails V0 by `0.012643969020781` mean final progress.

V4 also removes failed Clarify outcomes in this smoke and slightly improves over V2, but it does not suppress enough Clarify turns to recover progress. It keeps the same mean physical-action throughput as V2 (`23.666667`) and trails V0 by `0.013662755923626` mean final progress.

No Builder fallback occurred, and retrieval did not activate: `builder_fallback_rate`, `retrieved_node_count`, and `retrieval_used_in_top_action_count` are `0.0` for all policies. The observed differences are therefore Clarify-policy effects, not fallback or retrieval effects.

## Implications

The safest near-term default remains Clarify disabled for 5-turn progress comparisons unless a longer-horizon evaluation demonstrates net benefit. The policy experiments support keeping Clarify behavior opt-in and config-gated.

The value-of-information policy is directionally useful as an instrumentation and outcome-quality filter because it eliminates failed Clarify outcomes in this smoke. It is not sufficient as currently weighted to recover action throughput or progress.

For the next policy iteration, prioritize an explicit turn-budget opportunity-cost rule or minimum-remaining-turn rule for short horizons. VOI should either learn/tune a higher `min_value` from smoke traces or compose with the existing budget controls so low-margin Clarify actions do not consume turns late in the episode.

## Caveats

This is a 5-turn diagnostic smoke only. It should not be reported as final performance. Official `oracle_n=5`, 20-turn policy results are still required before making claims about full CRAFT performance.
