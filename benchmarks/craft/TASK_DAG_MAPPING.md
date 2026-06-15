# CRAFT Action Candidate To Task DAG Mapping

This note defines a narrow interoperability layer between CRAFT action candidates and the broader VillagerAgent Task DAG concepts. It does not replace the CRAFT-specific Dual-DAG runtime.

## Mapping

- CRAFT `ActionCandidateNode` maps to a read-only `TaskDAGNodeProjection`.
- `supported_by` claim IDs map to `evidence:* -> task:*` edges with `edge_type=supports_task`.
- `conflicts_with` claim IDs map to `evidence:* -> task:*` edges with `edge_type=blocks_task`.
- `required_evidence` claim IDs map to `task:* -> evidence:*` edges with `edge_type=requires_evidence`.
- CRAFT action payloads are copied only after dropping private or internal keys that start with `_`.

## Task Node Semantics

- `place_block` and `remove_block` become `task_type=craft_action`.
- `clarify` becomes `task_type=craft_clarification`.
- unresolved or unknown actions become `task_type=craft_evidence_wait`.
- candidates with conflicts are projected as `status=blocked`.
- candidates requiring more evidence are projected as `status=waiting_for_evidence`.
- chosen/running candidates may be projected as `status=running`; otherwise they remain `status=candidate`.

## Architectural Boundaries

- CRAFT action candidates remain CRAFT-owned runtime objects.
- Task DAG projections are interoperability artifacts for analysis and future integration.
- The projection must not feed hidden CRAFT state, raw private views, oracle moves, target structures, or other private data into VillagerAgent task state.
- CRAFT scoring, action verification, and clarification gating remain in CRAFT-specific modules.
- The broader VillagerAgent Task DAG should consume projection fields, not CRAFT internals.

## Implementation

The helper lives in `benchmarks/craft/dual_dag/task_mapping.py` and intentionally returns plain dictionaries/dataclasses instead of mutating `type_define.graph.Graph`. This keeps CRAFT and Minecraft/task runtime coupling low while preserving a clear bridge for future integration.
