# CRAFT Evaluation Metrics

This document explains the CRAFT metrics emitted by the VillagerAgent CRAFT integration. Metrics are written under `result/craft/{run_name}/normalized/` and are also aggregated by `benchmarks.craft.report`, `benchmarks.craft.dual_dag.analysis`, and `benchmarks.craft.experiment_summary`.

## Task Outcome

- `mean_final_progress`: Mean final progress across evaluated structures. This is the main task-performance score and estimates how closely the constructed structure matches the target.
- `final_progress`: Per-structure final progress. This is the per-game value used to compute `mean_final_progress`.
- `max_progress`: Maximum per-turn progress reached in an episode, averaged across games for run-level summaries.
- `progress_auc`: Turn-normalized mean progress across observed turns. This is useful for comparing early progress within the same horizon without changing `mean_final_progress`.
- `completion_rate`: Fraction of games that reached the complete-state condition. With short turn budgets this is usually low or zero.
- `positive_progress_turn_count`: Number of turns with positive progress delta relative to the previous turn.
- `zero_progress_turn_count`: Number of turns with no progress delta relative to the previous turn.
- `negative_progress_turn_count`: Number of turns with negative progress delta relative to the previous turn.
- `mean_progress_delta_per_turn`: Mean progress delta across observed turns.
- `mean_progress_delta_per_physical_action`: Mean progress delta divided by physical `place`/`remove` action count. This helps separate action quality from action throughput.

## Runtime And Configuration

- `status`: Run status in aggregate reports. Completed runs default to `completed`; experiment-level failure artifacts use `failed`.
- `run_group`: Run name with a trailing `_seedN` suffix removed. Robustness variance summaries use this by default to keep conditions such as non-Dual-DAG and Dual-DAG qwen runs separate even when their `condition` value matches.
- `error_type`: Exception class recorded for a failed experiment run, empty for completed runs.
- `error_message`: Failure message recorded for a failed experiment run, empty for completed runs.
- `condition`: Evaluation condition, such as `villageragent_directors`, `single_director_ablation`, or `official_baseline`.
- `active_directors`: Director IDs that actually produce messages. Three-director runs use `D1,D2,D3`; single-director ablations use `D1`.
- `active_director_count`: Number of active Directors.
- `use_task_decomposer`: Whether the VillagerAgent task decomposer is enabled.
- `use_agent_controller`: Whether the VillagerAgent controller is enabled.
- `use_state_manager`: Whether the VillagerAgent state manager is enabled.
- `baseline_type`: Baseline implementation type. `comparable_artifact` is the lightweight artifact row generated without invoking upstream CRAFT; `full_official_runner` is produced by `external/CRAFT/run_craft.py` and normalized into the local CRAFT artifact format.
- `seed`: Random seed used for the run.
- `structures`: Comma-separated CRAFT structure IDs evaluated by the run.

## Builder Behavior

- `builder_fallback_count`: Number of turns where the Builder output was incompatible with the verified candidates and the adapter fell back to an oracle-assisted candidate.
- `builder_fallback_rate`: `builder_fallback_count / turn_count`. This measures how often the Builder failed to return an acceptable candidate response.
- `physical_action_count`: Number of Builder turns that execute a physical `place` or `remove` action.
- `place_action_count`: Number of physical `place` actions.
- `remove_action_count`: Number of physical `remove` actions.
- `clarify_count`: Number of Builder turns whose action is `clarify`.
- `wait_count`: Number of Builder turns whose action is `wait_for_evidence`.
- `fallback_count`: Number of Builder turns carrying fallback metadata. This is the action-throughput counterpart of `builder_fallback_count`.
- `no_op_count`: Number of empty/no-op Builder action turns.
- `invalid_action_count`: Number of Builder turns marked as invalid by runtime metadata.
- `candidate_count`: Number of action candidates represented in metadata. With `oracle_n=1`, this is usually close to the number of turns.
- `mean_action_confidence`: Mean confidence of the chosen action candidate. Confidence increases with supporting claims, decreases with hard conflicts, and receives a small boost for physically verified candidates.

## Epistemic Metadata

- `observed_fact_count`: Number of facts extracted from Directors' private CRAFT views.
- `reported_claim_count`: Number of claims extracted from public Director messages. Other Directors' messages are treated as claims, not ground-truth facts.
- `hypothesis_count`: Number of hypothesis nodes. Current hypotheses are generated from unresolved public claims, conflicting action evidence, and required-evidence action metadata.

## Claim And Evidence Relations

- `claim_support_count`: Number of reported claims supporting the chosen action. A claim supports an action when color/block and location evidence align.
- `claim_conflict_count`: Number of hard conflicts between reported claims and the chosen action. A hard conflict requires an explicit non-uncertain color/location mismatch.
- `claim_required_evidence_count`: Number of claims that indicate additional evidence is needed for the chosen action. Uncertainty or confirmation requests, such as “uncertain” or “please confirm”, are tracked here instead of as hard conflicts.

## Gated Clarification

- `clarification_count`: Number of turns where the final Builder action is `clarify`. This includes direct Builder clarifications and gate-induced clarifications.
- `unique_clarification_count`: Number of distinct canonical clarification keys. Keys are built from public target Director/coordinate/layer/block/span metadata when present, otherwise from candidate action metadata and clarification text.
- `repeated_clarification_count`: `clarification_count - unique_clarification_count`.
- `clarification_response_count`: Number of public Director response payloads recorded on turns.
- `clarification_to_unlock_count`: Number of clarification turns whose next non-clarify action satisfies the existing unlock/resolution heuristic.
- `clarification_to_unlock_rate`: `clarification_to_unlock_count / clarification_count`.
- `clarification_to_positive_action_count`: Number of clarification turns followed by a later non-clarify turn with higher progress.
- `clarification_to_positive_action_latency`: Mean turn distance from clarification to the later positive-progress non-clarify action.
- `clarification_without_state_change_count`: Number of clarification turns without downstream resolution under the existing unlock heuristic.
- `gate_invocation_count`: Number of persisted gate decision metadata records. Current artifacts persist intervention decisions; future allow decisions are counted when stored with `reason=none` or `decision=allow`.
- `gate_allow_count`: Number of persisted allow decisions.
- `gate_block_count`: Number of persisted gate interventions that blocked the candidate action.
- `gate_clarify_count`: Number of gate interventions that selected `clarify`.
- `gate_wait_count`: Number of gate interventions that selected `wait_for_evidence`.
- `gate_reason_counts`: JSON object aggregating persisted gate reasons.
- `gated_clarification_count`: Number of turns where the Dual-DAG gate replaced a candidate action with `clarify`.
- `gated_clarification_rate`: `gated_clarification_count / turn_count`.
- Adaptive gated clarification is experimental and opt-in. When `dual_dag.gated_clarification.adaptive_thresholds.enabled=true`, the gate adjusts `min_action_confidence` and `clarification_cost` from public action metadata: support lowers intervention pressure, while conflicts and required evidence raise it. Static thresholds remain the default.
- `clarification_resolution_count`: Number of clarification turns whose next non-clarify action shows downstream resolution. Resolution is heuristic-based: progress increases, chosen action confidence improves, or conflict/required-evidence counts decrease.
- `clarification_resolution_rate`: `clarification_resolution_count / clarification_count`.
- `mean_clarification_quality_score`: Mean heuristic specificity score for clarification turns. The score rewards explicit clarification wording, concrete gate reasons, conflict/required-evidence/span reasons, and public evidence context.
- `mean_post_clarification_progress_delta`: Mean progress change from a clarification turn to the next non-clarify action turn.
- `mean_risk_score`: Mean risk score over gated clarification decisions. The current score is based primarily on `1.0 - chosen_confidence`.
- `low_confidence_gate_count`: Number of gated clarifications caused by low action confidence.
- `conflict_gate_count`: Number of gated clarifications caused by hard claim conflicts whose risk exceeds clarification cost.
- `required_evidence_gate_count`: Number of gated clarifications caused by required-evidence metadata.
- `span_uncertainty_gate_count`: Number of gated clarifications caused by large-block span uncertainty.

## Dual-DAG Size

- `dual_dag_node_count`: Number of serialized Dual-DAG nodes. Nodes include observed facts, public facts, reported claims, action candidates, and public Builder actions.
- `dual_dag_edge_count`: Number of serialized Dual-DAG edges. Current edges include action support/conflict edges and epistemic `supports`, `conflicts_with`, `derived_from`, and `requires_confirmation_from` edges.

## Historical Retrieval Diagnostics

- `retrieved_node_count`: Total retrieved public graph nodes referenced by action-candidate `graph_context` metadata.
- `retrieved_claim_count`: Retrieved public claim count.
- `retrieved_action_count`: Retrieved public Builder action count.
- `mean_retrieved_node_age`: Mean `current_turn - source_turn` across retrieved nodes when both turns are known.
- `max_retrieved_node_age`: Maximum retrieved node age.
- `retrieved_executed_candidate_count`: Retrieved public action count treated as executed historical action evidence.
- `retrieved_invalidated_candidate_count`: Retrieved nodes marked with `state=invalidated`.
- `retrieved_superseded_node_count`: Retrieved nodes marked `state=superseded` or carrying `superseded_by` metadata.
- `retrieval_used_in_top_action_count`: Number of turns where the chosen candidate has non-empty retrieval context.
- `retrieval_changed_top_action_count`: Number of turns explicitly marked as retrieval changing the top action. Existing artifacts do not infer this from confidence alone.

## Candidate Lifecycle Diagnostics

- `candidate_created_count`: Number of non-coordination action candidate nodes created in the Dual-DAG artifact.
- `candidate_blocked_count`: Number of candidate nodes currently in `blocked` state.
- `candidate_executable_count`: Number of candidate nodes currently in `executable` state.
- `candidate_executed_count`: Number of candidate nodes currently in `executed` state.
- `candidate_invalidated_count`: Number of candidate nodes currently in `invalidated` state.
- `candidate_repeated_after_execution_count`: Number of non-coordination candidates whose public action repeats an already executed action candidate at a later turn.
- `candidate_state_transition_counts`: JSON object of action lifecycle edges by `edge_type:state`, such as `unlocks_action:executable`, `blocks_action:blocked`, or `executes_action:executed`.

## Dual-DAG Analysis Metrics

- `supported_action_count`: Number of action candidates with at least one support edge.
- `conflicted_action_count`: Number of action candidates with at least one conflict edge.
- `required_evidence_action_count`: Number of action candidates with at least one required-evidence claim.
- `director_claim_counts`: Per-Director reported claim counts.
- `director_support_counts`: Per-Director support edge counts.
- `director_conflict_counts`: Per-Director conflict edge counts.
- `director_required_evidence_counts`: Per-Director required-evidence counts.
- `epistemic_edge_type_counts`: Epistemic edge counts by type, such as `supports`, `conflicts_with`, `derived_from`, and `requires_confirmation_from`.
- `action_edge_type_counts`: Action-candidate edge counts by type.

## Safety Metrics

- `leakage_passed`: Whether partial-information leakage checks passed for the run.
- Hidden-key scans verify that normalized Dual-DAG artifacts and analysis artifacts do not contain hidden fields such as `target_structure`, `oracle_moves`, `raw_private_view`, `all_private_views`, `hidden_spans`, or `hidden_labels`.

## Recommended Summary Columns

For paper-facing or quick inspection tables, focus on:

- Task performance: `mean_final_progress`, `max_progress`, `progress_auc`, `completion_rate`
- Action throughput: `physical_action_count`, `clarify_count`, `wait_count`, `mean_progress_delta_per_turn`, `mean_progress_delta_per_physical_action`
- Runtime stability: `builder_fallback_rate`, `gated_clarification_rate`, `clarification_resolution_rate`
- Retrieval health: `retrieved_node_count`, `mean_retrieved_node_age`, `retrieved_invalidated_candidate_count`, `retrieved_superseded_node_count`, `retrieval_used_in_top_action_count`
- Candidate lifecycle: `candidate_created_count`, `candidate_executed_count`, `candidate_repeated_after_execution_count`, `candidate_state_transition_counts`
- Coordination quality: `claim_support_count`, `claim_conflict_count`, `claim_required_evidence_count`
- Graph richness: `dual_dag_node_count`, `dual_dag_edge_count`, `supported_action_count`, `conflicted_action_count`, `required_evidence_action_count`
- Safety: `leakage_passed`

## Variance Summary Columns

Robustness manifests can emit a variance summary grouped by `run_group` by default:

- `group`: Group key used for aggregation, usually `run_group`.
- `run_count`: Total runs in the group, including failed runs.
- `completed_run_count`: Runs with `status=completed` included in numeric variance calculations.
- `failed_run_count`: Runs with non-completed status.
- `seed_count`: Number of distinct seeds represented by the group.
- `structures`: Union of structure IDs represented by the group.
- `mean_final_progress_mean`, `mean_final_progress_stddev`, `mean_final_progress_min`, `mean_final_progress_max`: Mean, population standard deviation, minimum, and maximum over completed runs.
- `completion_rate_mean`, `completion_rate_stddev`, `completion_rate_min`, `completion_rate_max`: Mean, population standard deviation, minimum, and maximum over completed runs.
