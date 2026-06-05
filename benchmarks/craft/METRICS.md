# CRAFT Evaluation Metrics

This document explains the CRAFT metrics emitted by the VillagerAgent CRAFT integration. Metrics are written under `result/craft/{run_name}/normalized/` and are also aggregated by `benchmarks.craft.report`, `benchmarks.craft.dual_dag.analysis`, and `benchmarks.craft.experiment_summary`.

## Task Outcome

- `mean_final_progress`: Mean final progress across evaluated structures. This is the main task-performance score and estimates how closely the constructed structure matches the target.
- `final_progress`: Per-structure final progress. This is the per-game value used to compute `mean_final_progress`.
- `completion_rate`: Fraction of games that reached the complete-state condition. With short turn budgets this is usually low or zero.

## Runtime And Configuration

- `condition`: Evaluation condition, such as `villageragent_directors`, `single_director_ablation`, or `official_baseline`.
- `active_directors`: Director IDs that actually produce messages. Three-director runs use `D1,D2,D3`; single-director ablations use `D1`.
- `active_director_count`: Number of active Directors.
- `use_task_decomposer`: Whether the VillagerAgent task decomposer is enabled.
- `use_agent_controller`: Whether the VillagerAgent controller is enabled.
- `use_state_manager`: Whether the VillagerAgent state manager is enabled.
- `baseline_type`: Baseline implementation type. The current official baseline row is `comparable_artifact`, not a full official CRAFT API runner.

## Builder Behavior

- `builder_fallback_count`: Number of turns where the Builder output was incompatible with the verified candidates and the adapter fell back to an oracle-assisted candidate.
- `builder_fallback_rate`: `builder_fallback_count / turn_count`. This measures how often the Builder failed to return an acceptable candidate response.
- `candidate_count`: Number of action candidates represented in metadata. With `oracle_n=1`, this is usually close to the number of turns.
- `mean_action_confidence`: Mean confidence of the chosen action candidate. Confidence increases with supporting claims, decreases with hard conflicts, and receives a small boost for physically verified candidates.

## Epistemic Metadata

- `observed_fact_count`: Number of facts extracted from Directors' private CRAFT views.
- `reported_claim_count`: Number of claims extracted from public Director messages. Other Directors' messages are treated as claims, not ground-truth facts.
- `hypothesis_count`: Number of hypothesis nodes. This is reserved for future use and is usually zero in the current implementation.

## Claim And Evidence Relations

- `claim_support_count`: Number of reported claims supporting the chosen action. A claim supports an action when color/block and location evidence align.
- `claim_conflict_count`: Number of hard conflicts between reported claims and the chosen action. A hard conflict requires an explicit non-uncertain color/location mismatch.
- `claim_required_evidence_count`: Number of claims that indicate additional evidence is needed for the chosen action. Uncertainty or confirmation requests, such as “uncertain” or “please confirm”, are tracked here instead of as hard conflicts.

## Gated Clarification

- `clarification_count`: Number of turns where the final Builder action is `clarify`. This includes direct Builder clarifications and gate-induced clarifications.
- `gated_clarification_count`: Number of turns where the Dual-DAG gate replaced a candidate action with `clarify`.
- `gated_clarification_rate`: `gated_clarification_count / turn_count`.
- `mean_risk_score`: Mean risk score over gated clarification decisions. The current score is based primarily on `1.0 - chosen_confidence`.
- `low_confidence_gate_count`: Number of gated clarifications caused by low action confidence.
- `conflict_gate_count`: Number of gated clarifications caused by hard claim conflicts whose risk exceeds clarification cost.

## Dual-DAG Size

- `dual_dag_node_count`: Number of serialized Dual-DAG nodes. Nodes include observed facts, public facts, reported claims, action candidates, and public Builder actions.
- `dual_dag_edge_count`: Number of serialized Dual-DAG edges. Current edges are mainly `supports` and `conflicts_with`.

## Dual-DAG Analysis Metrics

- `supported_action_count`: Number of action candidates with at least one support edge.
- `conflicted_action_count`: Number of action candidates with at least one conflict edge.
- `required_evidence_action_count`: Number of action candidates with at least one required-evidence claim.
- `director_claim_counts`: Per-Director reported claim counts.
- `director_support_counts`: Per-Director support edge counts.
- `director_conflict_counts`: Per-Director conflict edge counts.
- `director_required_evidence_counts`: Per-Director required-evidence counts.

## Safety Metrics

- `leakage_passed`: Whether partial-information leakage checks passed for the run.
- Hidden-key scans verify that normalized Dual-DAG artifacts and analysis artifacts do not contain hidden fields such as `target_structure`, `oracle_moves`, `raw_private_view`, `all_private_views`, `hidden_spans`, or `hidden_labels`.

## Recommended Summary Columns

For paper-facing or quick inspection tables, focus on:

- Task performance: `mean_final_progress`, `completion_rate`
- Runtime stability: `builder_fallback_rate`, `gated_clarification_rate`
- Coordination quality: `claim_support_count`, `claim_conflict_count`, `claim_required_evidence_count`
- Graph richness: `dual_dag_node_count`, `dual_dag_edge_count`, `supported_action_count`, `conflicted_action_count`, `required_evidence_action_count`
- Safety: `leakage_passed`
