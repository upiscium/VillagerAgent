# Dual-DAG Artifact Schema

Current schema version: `1.0.0`

Dual-DAG artifacts expose `schema_version` at the artifact root. CRAFT normalized artifacts also include the schema registry in `dual_dag_summary.json`; Minecraft read-only artifacts include the same registry at `schema`.

## Node Types

- `observed_fact`
- `public_fact`
- `reported_claim`
- `hypothesis`
- `resolved_fact`
- `suggested_constraint`
- `place_block`
- `remove_block`
- `clarify`
- `wait_for_evidence`
- `minecraft_task`
- `minecraft_action`
- `minecraft_observation`
- `minecraft_claim`

## Edge Types

- `supports`
- `conflicts_with`
- `derived_from`
- `requires_confirmation_from`
- `resolved_by`
- `requires_clarification`
- `waits_for_evidence`
- `coordinates_action`
- `clarification_response`
- `unlocks_action`
- `blocks_action`
- `executes_action`
- `precedes_task`
- `task_invokes_action`
- `produces_observation`
- `reports_claim`

## Lifecycle Fields

Hypothesis nodes use `content.status` with these values:

- `open`
- `supported`
- `conflicted`
- `resolved`
- `invalidated`

Action candidate nodes use `state` with these values:

- `candidate`
- `executable`
- `waiting_for_evidence`
- `blocked`
- `invalidated`
- `executed`

Coordination action candidates use `action_type`:

- `clarify`
- `wait_for_evidence`

## Compatibility Guidance

- Patch versions are compatible additions or documentation-only clarifications.
- Minor versions may add backward-compatible node types, edge types, or metadata fields.
- Major versions may change existing node, edge, or lifecycle semantics.
- Downstream tools should ignore unknown fields and preserve `schema_version` in derived artifacts.
- Hidden/private fields remain forbidden regardless of schema version.
