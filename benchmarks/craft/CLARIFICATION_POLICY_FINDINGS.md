# CRAFT Clarification Policy Findings

This records the Gemma4 12B Clarify policy smoke and official results for #148.

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

## Official 20-Turn Evaluation

After fixing the completed-public-structure leakage false positive in #158, the official `oracle_n=5`, 20-turn comparison completed for V0-V4 with seeds `[1, 3, 5]` and structures `[0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19]`.

Artifacts:

- `result/craft/comparison_gemma4_12b_clarify_policy_official_post_leakage_fix.json`
- `result/craft/summary_gemma4_12b_clarify_policy_official_post_leakage_fix.csv`
- `result/craft/variance_gemma4_12b_clarify_policy_official_post_leakage_fix.csv`

| Policy | n | mean_final_progress | delta vs V0 | progress_auc | physical_action_count | clarify_count | neutral_clarification_count | failed_clarification_count | builder_fallback_rate | retrieved_node_count | leakage_passed |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| V0 VA baseline | 3 | 0.763131497736379 | 0.0 | 0.338122418062063 | 400.0 | 0.0 | 0.0 | 0.0 | 0.044167 | 0.0 | true |
| V1 Dual-DAG, Clarify disabled | 3 | 0.755134296375715 | -0.007997201360664 | 0.344422476527625 | 400.0 | 0.0 | 0.0 | 0.0 | 0.045833 | 0.0 | true |
| V2 Dual-DAG, current Clarify | 3 | 0.748356992062942 | -0.014774505673438 | 0.477945195773800 | 269.0 | 131.0 | 1.0 | 130.0 | 0.0 | 0.0 | true |
| V3 Dual-DAG, throughput fix | 3 | 0.748356992062942 | -0.014774505673438 | 0.434534854258872 | 290.0 | 110.0 | 14.666667 | 95.333333 | 0.0 | 0.0 | true |
| V4 Dual-DAG, value of information | 3 | 0.766066632508662 | 0.002935134772282 | 0.338729654477652 | 399.0 | 1.0 | 1.0 | 0.0 | 0.043333 | 0.0 | true |

Official results change the smoke-only conclusion: V4 is the only Clarify-enabled policy that avoids the high-clarification throughput collapse and slightly exceeds V0 mean final progress. V2 and V3 still reproduce the regression path, with 131 and 110 Clarify actions on average and physical actions reduced to 269 and 290 respectively.

V1 Clarify-disabled Dual-DAG keeps physical throughput at 400 actions but trails V0 by `0.007997201360664` mean final progress. Retrieval did not activate in this official run (`retrieved_node_count` and `retrieval_used_in_top_action_count` are `0.0`), so the V1 difference should not be interpreted as retrieval behavior.

The strongest config-gated candidate from this evaluation is V4 value-of-information. It preserves near-baseline physical throughput (`399.0` vs `400.0`), limits Clarify to `1.0` per run on average, and has no failed Clarify outcomes in this run. It should remain opt-in until replicated, but it is the first policy variant here with a positive official delta against V0.

## Sensitivity Evaluation

The post-official sensitivity sweep completed for structures `[0, 1, 2, 3, 4]`, seeds `[1, 3, 5]`, oracle counts `[1, 3, 5]`, and 20 turns. It also includes 30-turn runs for V2-V4 at `oracle_n=5`. The 20-turn sensitivity comparison uses V1 Clarify-disabled Dual-DAG as the within-setting baseline because V0 VA baseline was not part of this manifest.

Artifacts:

- `result/craft/comparison_gemma4_12b_clarify_policy_sensitivity_post_official.json`
- `result/craft/summary_gemma4_12b_clarify_policy_sensitivity_post_official.csv`
- `result/craft/variance_gemma4_12b_clarify_policy_sensitivity_post_official.csv`

| Policy | oracle_n | turns | n | mean_final_progress | delta vs V1 | progress_auc | physical_action_count | clarify_count | neutral_clarification_count | failed_clarification_count | builder_fallback_rate | retrieved_node_count | leakage_passed |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| V1 Dual-DAG, Clarify disabled | 1 | 20 | 3 | 0.649131410725614 | 0.0 | 0.313576066043457 | 100.0 | 0.0 | 0.0 | 0.0 | 0.076667 | 0.0 | true |
| V2 Dual-DAG, current Clarify | 1 | 20 | 3 | 0.642337704163791 | -0.006793706561822 | 0.421490261483015 | 74.0 | 26.0 | 4.666667 | 21.333333 | 0.06 | 0.0 | true |
| V3 Dual-DAG, throughput fix | 1 | 20 | 3 | 0.656313012805766 | 0.007181602080153 | 0.412917142090331 | 74.0 | 26.0 | 7.333333 | 18.666667 | 0.05 | 0.0 | true |
| V4 Dual-DAG, value of information | 1 | 20 | 3 | 0.661866274895260 | 0.012734864169647 | 0.316752217133377 | 99.666667 | 0.333333 | 0.333333 | 0.0 | 0.073333 | 0.0 | true |
| V1 Dual-DAG, Clarify disabled | 3 | 20 | 3 | 0.643734989648033 | 0.0 | 0.275121164021164 | 100.0 | 0.0 | 0.0 | 0.0 | 0.066667 | 0.0 | true |
| V2 Dual-DAG, current Clarify | 3 | 20 | 3 | 0.636849781458477 | -0.006885208189556 | 0.427002231423971 | 60.0 | 40.0 | 0.666667 | 39.333333 | 0.0 | 0.0 | true |
| V3 Dual-DAG, throughput fix | 3 | 20 | 3 | 0.636849781458477 | -0.006885208189556 | 0.414224208266237 | 61.666667 | 38.333333 | 3.666667 | 34.666667 | 0.0 | 0.0 | true |
| V4 Dual-DAG, value of information | 3 | 20 | 3 | 0.639732661326864 | -0.004002328321169 | 0.304584574175154 | 100.0 | 0.0 | 0.0 | 0.0 | 0.073333 | 0.0 | true |
| V1 Dual-DAG, Clarify disabled | 5 | 20 | 3 | 0.655471373500359 | 0.0 | 0.289395219622031 | 100.0 | 0.0 | 0.0 | 0.0 | 0.056667 | 0.0 | true |
| V2 Dual-DAG, current Clarify | 5 | 20 | 3 | 0.636849781458477 | -0.018621592041882 | 0.426905329345909 | 60.0 | 40.0 | 0.0 | 40.0 | 0.0 | 0.0 | true |
| V3 Dual-DAG, throughput fix | 5 | 20 | 3 | 0.636849781458477 | -0.018621592041882 | 0.354985576259489 | 69.666667 | 30.333333 | 4.0 | 26.333333 | 0.0 | 0.0 | true |
| V4 Dual-DAG, value of information | 5 | 20 | 3 | 0.652458679270273 | -0.003012694230085 | 0.292610789398471 | 100.0 | 0.0 | 0.0 | 0.0 | 0.073333 | 0.0 | true |

The sensitivity sweep supports the official diagnosis that V2 and V3 still over-use Clarify. Across 20-turn oracle settings, V2 issues `26.0` to `40.0` Clarify actions per run and V3 issues `26.0` to `38.333333`, mostly failed. Their final-progress deltas are negative except V3 at `oracle_n=1`, where it improves final progress despite losing 26 physical actions.

V4 is more stable than V2/V3. It issues at most `0.333333` Clarify actions per run in the 20-turn sensitivity settings, has no failed Clarify outcomes, and preserves physical-action throughput at `99.666667` to `100.0`. Its final-progress delta is positive at `oracle_n=1` and near V1 at `oracle_n=3` and `oracle_n=5`, with small negative deltas of `-0.004002328321169` and `-0.003012694230085`.

The 30-turn `oracle_n=5` sensitivity runs did not include V1, so they are not causal comparisons against Clarify-disabled Dual-DAG. They still show the same behavior split: V2 and V3 spend `90.0` and `75.666667` turns on Clarify with many failed outcomes, while V4 spends `0.0` turns on Clarify and executes `150.0` physical actions.

Retrieval still did not activate in this sensitivity sweep (`retrieved_node_count` and `retrieval_used_in_top_action_count` are `0.0`), and all runs passed the leakage guard. The remaining observed policy differences are therefore Clarify-policy and fallback/throughput effects rather than retrieval effects.
