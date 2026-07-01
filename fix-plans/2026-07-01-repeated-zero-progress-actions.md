# Repeated Zero-Progress Physical Action Suppression

## Problem

The V1/V4 `oracle_n=5`, 30-turn trace comparison shows that V4 value-of-information suppresses Clarify and preserves physical throughput, but still trails V1 on mean final progress. The remaining gap is associated with physical action quality rather than communication-turn loss.

Observed trace pattern:

- V4 mean max repeated zero-or-missing-progress streak: `5.066666666666666`.
- V1 mean max repeated zero-or-missing-progress streak: `3.0`.
- V4 has more zero-or-missing-progress physical turns (`168`) than V1 (`143`).
- Retrieval remains inactive, and failed Clarify is already eliminated.

## Goal

Add an opt-in, config-gated action-selection policy that down-ranks recently repeated physical actions when those actions have not produced progress.

## Non-Goals

- Do not change default CRAFT behavior.
- Do not alter Clarify gating semantics.
- Do not use hidden target structures, oracle plans beyond public candidate ordering, private Director views, or leakage-sensitive fields.
- Do not remove all repeated actions; repeated actions may be valid when they continue to make progress.

## Proposed Config

```yaml
dual_dag:
  action_selection:
    suppress_repeated_zero_progress:
      enabled: true
      window_turns: 6
      max_repeats: 2
      treat_missing_progress_as_zero: true
```

## Policy

For each candidate physical action, compute a public action signature from action type, block, position, layer, and span. If the same signature appears at least `max_repeats` times in the previous `window_turns` physical actions and those prior executions produced zero or missing progress, move that candidate to the end of the candidate list.

If every candidate is suppressed, preserve the original order to avoid producing an empty or invalid action set.

## Expected Evaluation

Run V1, V4, and V5 (`V4 + suppress_repeated_zero_progress`) on the same diagnostic setting:

- `oracle_n=5`
- `turns=30`
- structures `[0, 1, 2, 3, 4]`
- seeds `[1, 3, 5]`

Primary metrics:

- `mean_final_progress`
- `progress_auc`
- `physical_action_count`
- repeated zero-or-missing-progress streak from `benchmarks.craft.trace_compare`
- fallback rate
- leakage pass
