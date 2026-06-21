# Minecraft Normalized Metrics Schema

Minecraft experiment outputs are decoupled from CRAFT analysis code. They reuse only the shared Dual-DAG `schema_version` value so downstream tooling can detect artifact versions consistently.

Each run directory contains:

- `summary.json`: normalized run metadata, task-selection recommendation, selected task, final score/progress, and artifact counts.
- `metrics.json`: normalized scalar metrics for comparison across Minecraft runs.
- `action_log.json`: sanitized public action log.
- `task_graph_snapshot.json`: sanitized public task graph projection.
- `dual_dag_artifact.json`: read-only Minecraft Dual-DAG projection.
- `decision_support.json`: read-only task-selection recommendation context.

`metrics.json` fields:

- `schema_version`: shared Dual-DAG artifact schema version.
- `run_name`: harness run identifier.
- `mode`: `dry_run` or `execute`.
- `task_completion_rate`: completed tasks divided by task count, or `null` when no tasks exist.
- `task_count`: number of tasks in the normalized task graph snapshot.
- `completed_task_count`: tasks with `status == "success"`.
- `failed_task_count`: tasks with `status == "failure"`.
- `action_count`: flattened action log entry count.
- `failed_action_count`: action entries whose result status is `false`.
- `retry_replan_count`: action entries whose public action/result text mentions retry or replan.
- `time_to_completion`: sum of numeric action durations, or `null` when durations are unavailable.
- `recommendation_adopted_count`: `1` when selected task equals recommended task, otherwise `0`.
- `recommendation_helped_count`: reserved for real-environment outcome attribution; currently `0`.
- `recommendation_hurt_count`: reserved for real-environment outcome attribution; currently `0`.
- `recommended_task_id`: decision-support recommendation.
- `selected_task_id`: task selected after optional ranking.
- `progress`: normalized final progress/score when available.
- `error`: environment/runtime error string when available.
- `mutates_runtime`: always `false` for normalized metric extraction.

All normalized outputs are passed through the Minecraft public sanitizer, which drops underscore-prefixed fields and credential-like keys such as API keys, passwords, secrets, and tokens.
