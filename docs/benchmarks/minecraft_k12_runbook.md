# Minecraft K12 controlled recovery runbook

Protocol identity: `minecraft-eac-k12-controlled-recovery/1`. Detached hashes
are recomputed over constrained canonical JSON with only the detached hash
field removed; stale or missing hashes fail the gate.

This is an offline artifact gate, not a runtime procedure and not a scientific
results report. Do not start Minecraft, a model provider, a network service, or
an external evaluator for this gate.

1. Work from a clean checkout and inspect only the checked-in v1 artifacts.
2. Load JSON with duplicate-key rejection and validate the request, trace, cell
   result, and validation documents as strict schemas (`additionalProperties`
   is false where the record is closed).
3. Recompute every detached SHA-256 over the document with its digest field
   removed, using the repository's constrained canonical JSON encoding.
4. Confirm the identities match across protocol, manifests, and schemas.
5. Confirm five strata × two templates × three seeds = 30 triplets, three arms
   per triplet = 90 cells, and six distinct permutations per stratum.
6. Confirm the checked-in schedule has 90 unique ordered cells and agrees with
   the frozen triplets and arm permutations. No replacement, retry, or schedule
   regeneration is allowed.
 7. Confirm planner-visible fixture fields exclude evaluator-only alternatives
    and truth; confirm worker event vocabulary, reset, oracle, finite budgets,
    and containment clauses are present. Before each native callback, confirm
    the parent has admitted both deadline and effect budget from one authority;
    evidence is one combined budget of at most three completed operations.
    Confirm reset generations are unique and monotonic, and each descriptor
    binds the exact generated fixture and backend initial-state digest.
8. Run the narrow schema test. A failure is a gate failure; do not repair it by
   editing an output or by running a live system.

The gate can establish only that the design artifacts are internally coherent.
It cannot establish model performance, recovery prevalence, causal effects, or
any other scientific result.
