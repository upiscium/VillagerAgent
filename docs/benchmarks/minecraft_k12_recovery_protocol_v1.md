# Minecraft K12 controlled recovery protocol v1

Frozen protocol identity: `minecraft-eac-k12-controlled-recovery/1`.
The request canonicalization, fixture, randomization, reset, trace,
validation-contract, cell-result, aggregate, and analysis identities are the
exact frozen identities recorded in `k12_protocol_v1.json`.

This checkout contains the frozen, checked-in design only. It is not a report of
scientific results. The protocol is an offline-only gate: no network, Minecraft
runtime, provider, world reset, replacement, retry, or post-hoc schedule change
is permitted while checking the artifacts.

## Frozen census

There are five strata (`S1`–`S5`), two templates and three seeds per template:
30 unique triplets. Each triplet has the three arms A, R, and S, giving 90
ordered cells. Every stratum contains each of the six permutations exactly once.
The randomization manifest is the complete ordered schedule; it is not a seed
from which a runner may generate a different schedule. Cells are consumed once;
there is no replacement or reconsideration.

Each authenticated triplet row also commits the complete generated fixture
descriptor: fixture/action identity, template and seed, original and alternative
arguments, both initial-state digests, and the complete fixture digest. The
parent reconstructs and verifies that commitment before reset or launch.
Template and seed coordinates deterministically vary the bounded local target
positions or identities; they are executable fixture inputs, not labels only.

The protocol digest binds the request, trace, result, validation, fixture, and
randomization identities. Detached digests cover constrained canonical JSON
(sorted keys, no floating point or unsafe integers, and no duplicate keys).
An empty `results` array is intentional and is part of the results-free freeze.

## Execution contract

The parent owns admission, reset, trace ordering, budgets, and containment. A
worker may emit only the bounded semantic vocabulary from `cell_started`
through one `worker_terminal_candidate`; it cannot author provenance,
oracle, containment, validity, or recovery. Reset is single-use per launch and must
attest empty EAC, candidate, permit, and cache state, frozen RNG, fresh model
and oracle state, the exact backend initial-state digest, and the actual
preceding-cell containment status. Reset generations are parent-owned,
campaign-monotonic, and unique across all 90 cells. A containment or cleanup
failure is an infrastructure failure and blocks the next launch.
The blocked campaign nevertheless has a finite parent finalization: it reports
the terminalized launched prefix and the exact unlaunched remainder. Its
authenticated aggregate retains all 90 ordered slots, counts attempted and
`not_started` separately, and gives every unlaunched suffix slot one shared
campaign-stop reference; it never fabricates replacement cell outcomes.
All three arms in a triplet use the same committed post-invalidation backend
state; arm assignment changes enforcement/recovery behavior, not reset state.

For R, the worker emits only `recovery_proposed` with the frozen action and
semantic arguments. The parent canonicalizes and checks novelty, appends
`proposal_validated`, and only then may prepare a fresh request, issue a permit,
enter the effect, and append its known terminal. A repeated original semantic
request terminalizes before `new_request_prepared` or any permit/effect event.
Every authority rejection, backend effect, oracle verdict, reset, and
containment result is resolved through a frozen parent evidence snapshot bound
to protocol, campaign, cohort, cell, triplet, arm, fixture, template, seed,
reset token/generation, and reset attestation. Oracle evaluation additionally
requires the exact cell and reset generation. Worker operation events carry
unique operation IDs and obey a non-interleaving start/terminal state machine;
the raw trace validator independently replays that state machine.

The finite budgets are 180 seconds absolute parent time, 120 seconds after the
branch, a 30-second finalization reserve, four steps, two model calls, three
evidence operations (a combined maximum of three completed operations), and
two effects. Admission and deadline checks occur before any native callback;
one parent budget snapshot supplies authoritative final counters. The oracle is evaluator-owned; its truth
and alternatives never enter the planner-visible fixture payload.
The structured rejection communication and each admitted observation share the
same evidence counter. Branch work stops no later than parent time 150 seconds
to retain the final 30 seconds for containment and terminalization.

## Analysis boundary

The parent prepends `reset_attested`, assigns sequence/time/digests, interleaves
authenticated authority events after worker semantic observations/proposals,
and appends `objective_oracle_evaluated`, `process_finalized`, and
`cell_terminal` after the worker terminal candidate. Public aggregate and
analysis entry points reauthenticate protocol, manifests, validation contract,
schedule, campaign, cohort, and aggregate identity. Cell dispositions are
exactly `RECOVERED`,
`BUDGET_EXHAUSTED`, `REPEATED_ORIGINAL_REQUEST`, `RECOVERY_SAFETY_FAILURE`,
`UNRECOVERABLE`, `STOPPED_AFTER_REJECTION`, `A_TERMINAL`,
`INFRASTRUCTURE_FAILURE`, `TRACE_INVALID`, and `RESET_INVALID`.

Only finite descriptive validation is allowed: census, identity, event order,
budget, reset, oracle-binding, and containment checks. The artifacts contain no
scientific result and must not acquire p-values, confidence intervals, standard
errors, effect estimates, hypothesis tests, pooled estimates, or conclusions.
