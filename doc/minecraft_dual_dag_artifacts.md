# Minecraft Dual-DAG Artifact Projection

This note defines the minimal Minecraft-side Dual-DAG projection for runtime analysis. It is intentionally separate from the CRAFT `DualDAGRuntime`.

## Equivalents

- Minecraft observations are public tool responses, environment query results, and task feedback.
- Minecraft actions are agent tool calls recorded in `data/action_log.json`.
- Minecraft claims are agent final answers and explicit communication actions such as `talkTo` messages.
- Minecraft task nodes come from `type_define.graph.Task` instances and task dependency edges come from `type_define.graph.Graph`.

## Artifact Shape

- `minecraft_task` nodes contain task description, status, milestones, assigned agents, candidate agents, reflection, and public metadata.
- `minecraft_action` nodes contain the agent, tool name, public arguments, duration, and timing provenance.
- `minecraft_observation` nodes contain public tool result or feedback payloads.
- `minecraft_claim` nodes contain public messages reported by agents.
- `precedes_task`, `task_invokes_action`, `produces_observation`, and `reports_claim` edges preserve the minimal runtime relations.

## Boundaries

- The projection is read-only and does not mutate `type_define.graph.Graph` or `Task`.
- The CRAFT Dual-DAG runtime remains decoupled; Minecraft artifacts use a separate helper in `env/minecraft_dual_dag.py`.
- Private, internal, credential, and underscore-prefixed fields are dropped recursively before serialization.
