# K11 Natural Trace Prevalence / Structural Exposure Study — Draft Protocol

Status: **DRAFT — NOT FROZEN**  
Protocol identity: `minecraft-eac-k11-natural-prevalence`  
Draft version: `0.2`<br>
Issue lineage: `#511`, `#533`, `#535`<br>
Subject repository: `upiscium/VillagerAgent`  
Audited base before this draft: `66a904de8af2b0bbaf79071628f06bed91a40078`

This document is a design-stage protocol. It is not the final K11 protocol, does not authorize a final prevalence run, and must not be represented as prospectively frozen evidence. P0 instrumentation validation and P1 reconnaissance remain development/reconnaissance cohorts and are never pooled with the final K11 cohort. K11-E will produce the frozen machine-checkable protocol, task pool, selection manifest, configuration bindings, and analysis bindings.

## 1. Study identity and purpose

K11 is an observational ecological/structural-exposure study. It is not an additional K8/K10 synthetic census and is not a mechanism-novelty study.

The controlled K8/K10 result concerns the separation:

`semantic invalidation detection != execution prevention`

K11 asks whether the prepare-to-effect state required by that controlled failure is naturally exposed in ordinary supported VillagerAgent execution, and if so how often actor-visible action-relevant semantic support changes while an exact prepared action remains in flight.

The study must preserve negative ecological results. In particular, zero retained stale requests is a valid outcome if the production execution structure rarely or never exposes the corresponding interval.

## 2. Reporting separation from K8 and K10

K8, K10, and K11 are separate study identities and separate cohorts.

Forbidden reporting includes:

- pooling K8/K10/K11 cells or actions into one estimator;
- reporting a combined sample size as though it were one cohort;
- using K8/K10 controlled cells as K11 prevalence observations;
- treating repeated actions within a run as IID population samples;
- inferring population prevalence outside the frozen K11 task/run population;
- claiming external replication from K11.

K11 may cite K8/K10 only as prior controlled evidence motivating the ecological question.

## 3. Supported runtime boundary

K11 observes only the existing supported EAC path:

`EAC-configured VillagerBench -> guard_tool_actions() -> MinecraftEACRuntime -> EffectGateway -> native Minecraft effect`

Out of scope:

- direct `Agent` tool calls that bypass VillagerBench guarding;
- direct bridge HTTP calls;
- bridge internals;
- judgers and evaluator truth as epistemic evidence;
- setup/admin actions;
- unclassified-tool bypasses;
- hostile code modification;
- OS-level sandbox claims.

The current EAC launcher additionally requires explicit non-judged, non-production admission and `task_type=none`. K11 does not weaken that admission boundary. Therefore the primary K11 ecological population is the natural agent/controller loop available under the existing EAC-admitted non-judged configuration, not all judged VillagerBench benchmark tasks. This limitation must be stated in the final report unless the supported runtime boundary changes through a separately reviewed change before freezing K11.

## 4. Primary condition

The primary natural cohort uses:

`dual_dag_advisory`

Rationale: K11 estimates latent structural/semantic exposure without allowing Authority rejection to alter the subsequent natural task trajectory. The primary observational cohort therefore does not use Authority as an intervention.

An Authority replay may later be performed on captured eligible critical traces if technically valid, but such replay is secondary, non-prevalence evidence and must not modify or replace the primary natural observations.

## 5. Naturalness invariants

Primary K11 runs prohibit K11-induced intervention in the behavior being measured.

Forbidden:

- artificial semantic revision injection;
- forced sleeps intended to widen prepare-to-execution intervals;
- synchronization barriers inserted to force an interleaving;
- planner/LLM/controller suppression;
- forced retention of a prepared request;
- deliberate concurrent evidence injection;
- manual operator intervention during the measured run;
- outcome-triggered task substitution within the frozen final cohort.

Allowed:

- observability-only instrumentation satisfying the interference rules below;
- existing runtime behavior already present before K11 instrumentation, including existing audit persistence and existing runtime event journaling;
- ordinary model calls, observations, communication, tool execution, task management, retries, and scheduler interleavings produced by the unmodified behavioral control flow.

## 6. Research questions

### RQ1 — Mechanical prepared-action exposure

For primary-eligible prepared exact actions, what is the observed prepare-to-disposition / prepare-to-execution-decision interval under ordinary execution?

Logical ordering and monotonic wall-clock duration are reported separately.

### RQ2 — Actor-visible semantic exposure

During that interval, does the acting actor naturally ingest any actor-visible semantic mutation, and does such a mutation intersect the original action's semantic dependency set?

World changes that never enter the acting actor's runtime-admitted evidence do not count.

### RQ3 — Natural reconsideration

After an action-relevant semantic change, is there positive trace evidence that ordinary planner/model/controller behavior abandons, replaces, or regenerates the old prepared request before it reaches the execution decision?

Absence of an execution attempt is not by itself evidence of reconsideration.

### RQ4 — Retained stale execution decision

Does a preparation-admissible exact request become epistemically inadmissible before disposition and nevertheless reach the execution-time decision as the same `ExactRequest`?

### RQ5 — Structural suppression / avoidance

If RQ4 events are rare or absent, which observable structural fact explains the result: near-synchronous prepare/execute adjacency, absence of same-actor evidence ingress in the gap, natural reconsideration, or trace-observable architectural constraints?

## 7. Primary analysis unit

The primary action-level unit is one exact prepared primary-eligible action.

The primary effect-bearing action set is frozen at the construct already evaluated in K8/K10:

- `MineBlock`
- `placeBlock`
- `navigateTo`
- `attackTarget`
- `handoverBlock`

Instrumentation may record all classified tools for context, including observation/communication tools, but such tools are not added to the primary D1 denominator after seeing K11 outcomes.

Actions are nested within actors, tasks, and runs. K11 reports raw counts and descriptive distributions and does not treat nested action observations as independent population draws.

## 8. Semantic definitions

`J_i^r(phi)` is a fallible runtime justification witness and is not factive logical knowledge `K_i phi`.

`EAdm_i(a,r)` denotes runtime-policy-defined epistemic admissibility under the frozen SupportPolicy and SourceProfile. It is not objective world truth, `EnvPre`, or `SecPre`.

Hidden evaluator/world truth does not constitute K11 semantic invalidation unless it becomes actor-visible/runtime-admitted evidence through the supported evidence-ingestion contract.

Runtime freshness is not objective world correctness.

## 9. K11 trace architecture

### 9.1 Prospective measurement cut (#535)

The final prospective trace uses `minecraft-k11-trace/3` and carries one
top-level `measurement_cut` with schema `minecraft-k11-measurement-cut/1`.
The exact cut fields are `schema_version`, `boundary` (`[open,close)`),
`window_open_monotonic_ns`, `window_close_monotonic_ns`, `close_reason`,
`identity`, `close_sequence`, `event_prefix_high_water_sequence`,
`in_window_event_count`, `in_window_event_digest` (`sha256:` plus the digest of
canonical exported in-window events), `evidence_state_event_count`,
`evidence_state_digest`, `snapshot_state_digest`, `snapshot_valid`,
`snapshot_errors`, and the bounded `active_executions`, `open_lifecycles`,
`prepared_requests`, `evidence_high_water`, and `censoring_inventory`
collections (`items` and complete retention metadata). Analysis fails closed
on any missing, malformed, or mismatching binding. The prospective analysis
identity is `minecraft-k11-prospective-analysis/1`; the preserved `/2` trace
analysis remains `minecraft-k11-trace-analysis-draft` version 1.

`identity` has exactly `run_id`, `manifest_digest`, `execution_revision`,
`runtime_digest`, `premanifest_identity`, `validation_contract`, and
`trace_schema`; it is included in `snapshot_state_digest` and independently
correlated with the parent runner authority.

For a fixed observation horizon, the cut is authoritative: post-close decision,
native-effect, and evidence events are never allowed to complete or alter an
action's classification. A natural-terminal unresolved disposition remains
unresolved and makes the prospective result ineligible; it is not converted to
censoring. A trace with no primary actions is likewise ineligible, rather than
being interpreted as zero prevalence.

Post-window cleanup is a distinct append-only authority. The controller verdict
at its bounded grace deadline is immutable, but a failed budget-time verdict may
later receive `qualified_late` only from identity-bound, complete, non-truncated
evidence that execution capability, providers, tool/native effects, movement,
bridge children, worker descendants, and the process group are terminal. This
late projection does not alter the cut, runtime error, or scientific fields;
missing evidence remains `unknown`.

K11 introduces a dedicated append-only in-memory trace for all new K11 high-frequency instrumentation.

The K11 trace must not write to disk from the measured critical path and must not add a new synchronization lock to the EAC prepare/evidence/execute seam.

Each run has one prospectively configured monotonic observation window. The window opens at `GlobalController.run` entry and closes at the earlier of (a) natural runtime terminal return/failure or (b) the fixed configured horizon. The fixed horizon must be declared in the run manifest before execution and must not depend on observed outcomes, candidate counts, EAC events, N1/N2 classifications, or task progress. Once the fixed horizon marker is linearized, the K11-only wrapper requests ordinary controller shutdown so the in-memory trace can be flushed through the existing pilot finalization path. The outer process-supervision timeout remains an infrastructure backstop and is not the observational endpoint.

The development P0 manifest currently binds a 600-second horizon. This is a pilot binding, not the final K11 horizon and not a protocol freeze. Final horizon selection remains subject to the K11-E prospective freeze.

The existing runtime event journal remains unchanged as baseline behavior. Current real execution creates `JsonlRuntimeEventRecorder` with its default durable behavior, which may flush/fsync on existing runtime events. K11 must not route newly added high-frequency model/tool/EAC trace events through that durable sink.

The K11 trace is serialized after the measured runtime operation, or included in the terminal runtime-result collection path. A crash that prevents complete trace recovery is an incomplete observation, not permission to reconstruct missing events from guesses.

## 10. K11 trace ordering

### 10.1 EAC-local ordering

`MinecraftEACRuntime` already serializes preparation, actor-visible evidence ingestion, and execution-decision entry through its existing `RLock`.

K11 records an EAC-local monotonically increasing trace sequence while that existing lock is held. No second EAC trace lock is introduced.

Each EAC trace event also records `time.monotonic_ns()` and thread identity.

### 10.2 Higher-level ordering

Agent/model/tool context events use the same K11 in-memory trace infrastructure and record a K11 trace sequence, `monotonic_ns`, actor/task context, and thread identity.

Cross-thread ordering is interpreted using the recorded trace ordering and monotonic timestamps. If the data do not support an unambiguous ordering needed for a substantive classification, the case is marked unresolved rather than forced into N1–N4.

## 11. Required online event facts

The minimum K11 event vocabulary is:

- `k11.agent_step_started`
- `k11.agent_step_completed`
- `k11.model_call_started`
- `k11.model_call_completed`
- `k11.model_call_failed`
- `k11.tool_call_entered`
- `k11.tool_call_exited`
- `k11.eac_action_prepared`
- `k11.eac_evidence_ingested`
- `k11.eac_execution_decision_attempted`
- `k11.eac_native_effect_entered`
- `k11.eac_native_effect_completed`
- `k11.eac_action_terminal`
- `k11.observation_window_opened`
- `k11.observation_window_closed`

The instrumentation records observed facts. It does not emit synthetic semantic labels such as `N2`, `reconsidered`, or `invalidated` online.

Positive pre-decision disposition is derived offline from ordinary lifecycle facts rather than emitted as an online semantic label. The original scoped tool call must return normally without an execution decision. A later same-actor/task preparation before the agent step returns establishes replacement; otherwise a normal return of the scoped agent step establishes cancellation. Missing, raised, cross-scope, or ambiguously ordered lifecycle facts remain `disposition_unresolved`.

## 12. Correlation identities

K11 must support deterministic reconstruction of:

`run_id -> task_id -> actor_id -> agent_step_id -> tool_call_id -> candidate_id -> attempt_id -> ExactRequest`

Minimum identifiers:

- run / attempt identity;
- task identity;
- actor identity;
- agent-step identity;
- tool-call identity;
- candidate identity;
- attempt identity;
- action identity/version/digest;
- canonical exact-request digest;
- canonical arguments and target or an authenticated binding to them.

The same-exact-request test requires equality of the complete canonical `ExactRequest` identity, not merely tool name or target equality.

## 13. Evidence reconstruction requirements

Every successful actor-visible evidence ingestion needed for K11 semantics must be recoverable with at least:

- actor identity;
- root identity;
- record type;
- proposition and polarity;
- revision;
- supersession relation;
- source / provenance identity;
- actor visibility;
- source-stream revision where applicable.

The bounded `MinecraftEACRuntime._records` / exported audit projections are corroborating artifacts only and are not the K11 source of record. K11 completeness must not depend on the existing 256-record bounded projections.

## 14. Offline admissibility replay

K11 does not add `RuntimeAuthority.evaluate()` calls merely for measurement.

For each eligible prepared action, analysis reconstructs actor-visible evidence state from the captured initial evidence state plus ordered evidence-ingestion events and evaluates the original EPre under the frozen SupportPolicy, SourceProfile, classification, and ingestion semantics.

Two states are required:

- `EAdm_prepare`: admissibility immediately after the preparation marker;
- `EAdm_disposition`: admissibility immediately before the original request's observed disposition.

The preparation-time semantic dependency/watch set is reconstructed without mutating the live runtime. Authority-mode validation fixtures may compare the offline reconstruction with a live manifest, but this validation is not part of the primary Advisory prevalence cohort.

## 15. Disposition definition

The disposition of a prepared exact request is the earliest trace-supported event that resolves whether that original request remains in flight:

1. the same original `ExactRequest` reaches `k11.eac_execution_decision_attempted`; or
2. positive higher-level trace evidence shows that the old request was abandoned/replaced/cancelled before an execution decision.

If neither can be established, the action has unresolved disposition and is retained in the raw archive but is not silently classified as natural reconsideration.

The observation interval is half-open: `window_open_monotonic_ns <= event.monotonic_ns < window_close_monotonic_ns`. A primary preparation inside a fixed-horizon window with no complete disposition inside that window is right-censored. It remains in D1, is reported as `observation_window_censored`, and is excluded from D2-D6 and N0-N4. Events at or after the close boundary cannot retrospectively complete its measured disposition. A missing disposition at a natural runtime terminal is not horizon censoring; it remains an instrumentation/lifecycle QC failure or unresolved case. Reports include prepared-inside-window count, complete-disposition-inside-window count, censored count, and censoring fraction.

## 16. Denominators D1–D6

These definitions are draft-frozen conceptually at K11-C; field-level implementation bindings are finalized at K11-E after P0/P1 validation.

### D1 — Prepared primary-eligible actions

All successfully prepared exact actions in the five primary effect-bearing strata with a valid K11 preparation record.

### D2 — Baseline-admissible actions with observable disposition

D1 actions for which:

- offline evidence reconstruction is complete;
- `EAdm_prepare = true`;
- a trace-supported disposition exists; and
- the prepare marker precedes the disposition marker with a positive monotonic interval.

Prepared actions with `EAdm_prepare = false` are reported separately as `prepared_inadmissible_baseline` and are not treated as post-preparation invalidations.

### D3 — Same-actor semantic mutation exposure

D2 actions with at least one successfully ingested actor-visible semantic mutation for the acting actor strictly after preparation and before disposition.

### D4 — Action-relevant semantic mutation exposure

D3 actions with at least one interval mutation whose changed semantic dependency/watch identities intersect the reconstructed preparation-time semantic dependency set for the original request.

### D5 — Post-preparation epistemic invalidation

D4 actions for which `EAdm_disposition = false` under the frozen runtime policy semantics.

The primary interpretation is a true-to-false transition from `EAdm_prepare = true` to `EAdm_disposition = false`.

### D6 — Retained stale execution-decision exposure

D5 actions for which the same complete `ExactRequest` reaches `k11.eac_execution_decision_attempted` rather than being positively abandoned/replaced first.

D6 is the closest natural analogue of the retained stale request studied under controlled K8/K10 conditions.

## 17. Secondary effect-reaching metric

Among D6 cases, K11 separately reports whether the same request reaches `k11.eac_native_effect_entered` and its terminal native outcome.

This is not pooled with K8/K10 and is not the primary denominator definition.

## 18. Natural event taxonomy

The substantive N0–N4 taxonomy applies only where preparation-time admissibility and trace completeness are sufficient for classification.

### N0 — No actor-visible semantic mutation

`EAdm_prepare = true` and no same-actor actor-visible semantic mutation occurs before disposition.

### N4 — Actor-visible but action-unrelated mutation

At least one same-actor actor-visible semantic mutation occurs before disposition, but none intersects the original action's reconstructed semantic dependency set.

### N3 — Relevant mutation, still admissible

At least one action-relevant semantic mutation occurs before disposition and `EAdm_disposition = true`.

### N2 — Relevant invalidation with retained exact request

At least one action-relevant mutation occurs, `EAdm_prepare = true`, `EAdm_disposition = false`, and the same original `ExactRequest` reaches the execution decision.

### N1 — Relevant invalidation with positive natural reconsideration

At least one action-relevant mutation occurs, `EAdm_prepare = true`, `EAdm_disposition = false`, the old exact request does not reach the execution decision, and positive trace evidence establishes ordinary redecision/replacement/cancellation before disposition.

## 19. Non-substantive QC states

K11 records but does not force substantive classification for:

- `prepared_inadmissible_baseline`;
- `trace_incomplete`;
- `disposition_unresolved`;
- `ordering_ambiguous`;
- `offline_replay_failed`;
- `unsupported_path_observed`;
- `infrastructure_failure`.

A relevant mutation followed by disappearance of the request is not N1 unless positive reconsideration evidence exists.

## 20. Primary reporting quantities

At minimum, final K11 reporting contains raw numerators and denominators for:

- D1 prepared-action count;
- D2/D1 baseline-admissible observable-disposition fraction;
- D3/D2 same-actor semantic-mutation exposure;
- D4/D2 action-relevant semantic-mutation exposure;
- D5/D4 post-preparation invalidation fraction;
- D6/D5 retained-stale execution-decision fraction;
- native-effect entry among D6;
- N0–N4 counts;
- all QC-state counts.

Zero denominators are reported explicitly as undefined ratios, never converted to zero rates. Any exposure-dependent ratio whose denominator is undefined is non-interpretable: it is reported as undefined and must not be read as zero exposure, zero prevalence, or evidence of absence.

Mechanical prepare-to-disposition / prepare-to-execution-decision durations are reported descriptively (count, median, quantiles, minimum, maximum as appropriate) without IID population interpretation.

Task-, actor-, action-stratum-, and run-level breakdowns are descriptive secondary analyses.

## 21. Task/run metadata

Each natural run records at least:

- task identity/family and frozen task specification digest;
- agent count and actor names;
- model/runtime identity and relevant model settings;
- run duration and terminal state;
- task success/failure if objectively available from the supported non-judged runtime;
- number of primary eligible prepared actions;
- all classified-tool counts;
- model-call count;
- observation/tool-result/message counts where trace-supported;
- actor-visible evidence-ingestion count;
- K11 trace event count and completeness status.

## 22. Pilot P0

P0 is instrumentation validation only and is never used as K11 prevalence evidence. The P0 gate is prospective: development smoke runs and other development artifacts cannot later be promoted into the formal P0 cohort or treated as qualifying cohort evidence.

Target: **8 natural runs**. Formal P0 is a separate fixed eight-run cohort gate. The gate counts qualifying actor-visible evidence-ingestion events that occur within each run's prospectively configured observation window. All eight designated runs must be retained and assessed; an all-zero cohort, or a cohort whose evidence occurs only before the observation windows, fails the gate. Failed attempts remain disclosed and do not become unrecorded replacements.

Per-run structural validity is distinct from stochastic evidence exposure. A structurally complete run can validly contain zero qualifying evidence events; that run is retained as a zero-evidence run and is not retried or replaced because of that result. The cohort gate nevertheless fails when the fixed eight-run cohort has no qualifying in-window evidence (including when all observed evidence is pre-window). Structural validity must not be used to manufacture evidence exposure.

This separation is bound prospectively by `minecraft-k11-p0-validation-contract/1` and `configs/minecraft/k11-p0-natural-manifest-v1.json`. The preserved v0 manifest remains provenance for earlier development work and is not accepted as the identity of a fresh run under the revised validator contract. The v1 manifest changes only manifest/validation-contract identity metadata: its eight run descriptors, ordering, task goals, runtime/model configuration, and 600-second pilot horizon are byte-for-byte equivalent as JSON values to v0.

P0 validates:

- every preparation can be correlated to its exact request;
- every execution-decision attempt maps to exactly one preparation;
- every native-effect entry maps to an execution-decision attempt;
- evidence-ingestion events reconstruct the live actor-visible state in deterministic validation cases;
- offline `EAdm` replay agrees with independently checkable expected semantics;
- trace sequence/order invariants hold under concurrent agent execution;
- the 256-entry legacy audit limits do not truncate the K11 source trace;
- no K11 critical event causes filesystem I/O;
- K11 tracing adds no new synchronization primitive at the EAC critical seam;
- deterministic functional behavior is unchanged with tracing enabled.

P0 also calibrates added timing overhead. If K11-added critical instrumentation materially determines the measured prepare-to-disposition interval, K11 is NO-GO until instrumentation is reduced. The P0 report must disclose the chosen measurement method and observed overhead rather than hiding unfavorable calibration.

## 23. Reconnaissance P1

P1 is a disclosed event-rate/feasibility reconnaissance cohort and is never pooled with final K11.

Target: **24 natural runs**.

P1 asks whether D3/D4/D5 events are observable at all under the supported natural path and estimates only the order of magnitude needed to freeze a practical final run budget.

P1 may inform final sample size and whether the final study is framed primarily as semantic-prevalence or structural-exposure characterization. P1 must not be used to cherry-pick individual task instances because they produced N2/D5 events.

Any task-pool changes after P1 must be based on a disclosed static eligibility/coverage rule or must redefine the final estimand as conditional on the newly frozen population. Outcome-enrichment without changing the estimand is forbidden.

## 24. Final task pool and selection

K11-E freezes before the first final run:

- `TASK_POOL.json`;
- task eligibility rule;
- task-family/agent-count coverage rule;
- deterministic selection/order rule;
- final run count;
- model/runtime configuration;
- SupportPolicy/SourceProfile/classification bindings;
- K11 event schema digest;
- offline analysis implementation digest;
- D1–D6 field bindings;
- N0–N4 classifier implementation binding.

The final cohort does not expand or stop based on observed N2/D5 counts.

## 25. Run inclusion and exclusions

Final primary analysis requires a complete trace sufficient to determine D1–D6 status for each included prepared action.

Infrastructure failures, crashes, trace corruption, unsupported-path execution, and instrumentation failures are preserved and reported separately. They are not silently replaced with successful reruns under the same final run identity.

A replacement run, if allowed by the final frozen protocol, must follow a prospective deterministic replacement rule and retain the failed attempt in provenance.

Substantive negative outcomes such as zero relevant mutation, zero invalidation, zero N2, task failure, or Authority-style non-reaching behavior are never exclusion criteria.

## 26. Interference controls

K11-added instrumentation must satisfy all of the following:

- no sleep/wait/barrier added to measured behavior;
- no new live semantic evaluation merely for tracing;
- no new EAC critical-path filesystem operation;
- no new EAC critical-path lock;
- no network request added for tracing;
- no prompt/model behavior modification for tracing;
- only bounded construction of small event records in the critical seam;
- expensive canonical serialization/digesting is reused from already-existing identities when possible or deferred until after the critical interval.

Existing runtime audit persistence and existing durable runtime-event journaling are baseline runtime behavior and are not disabled merely to make K11 timing look cleaner.

## 27. Trace completeness invariants

At minimum:

- every `eac_action_prepared` has a unique candidate/attempt identity;
- every `eac_execution_decision_attempted` references exactly one prior preparation;
- every `eac_native_effect_entered` references exactly one prior execution decision;
- every completed native effect has a matching native-effect entry;
- every successful K11-observed evidence ingestion has actor/root/proposition/revision identity;
- trace sequence is strictly monotonic within its defined recorder ordering;
- run termination records total K11 event count and trace-close status.

Invariant violation marks the affected action/run as trace-incomplete. It does not permit inferred event repair.

## 28. Negative-result interpretation

All of the following are valid primary outcomes:

1. N2 observed: natural retained stale execution-decision exposure exists in the frozen evaluated population.
2. N2 absent while N1 occurs: ordinary reconsideration appears to absorb some invalidated prepared states before execution decision.
3. D4/D5 nearly absent: actor-visible action-relevant semantic changes rarely occur inside the prepared-action interval.
4. D3 nearly absent and prepare/decision adjacency is tight: the current runtime architecture structurally exposes little opportunity for the controlled critical state.

K11 must not be rewritten after the fact into an N2-hunting experiment.

## 29. Prohibited inference

K11 does not establish:

- mechanism novelty;
- objective-world correctness;
- completeness of EPre declarations;
- prevalence outside the frozen K11 task/run population;
- independence of repeated actions within a run;
- general safety against bypasses outside the supported Authority boundary;
- recovery utility after rejection (reserved for K12).

## 30. Planned archive

Final archive target:

`results/issue511/k11-natural-prevalence-001/`

Planned contents include:

- `README.md`
- `STUDY_PROTOCOL.md`
- `FROZEN_CONFIG.json`
- `TASK_POOL.json`
- `SELECTION_MANIFEST.json`
- event/schema and derivation bindings
- disclosed `pilot/` manifests for P0/P1
- raw run/trace artifacts
- derived critical intervals and natural-event inventory
- denominators and aggregate outputs
- final aggregate/final manifest
- `ARCHIVE_PROVENANCE.md`
- `SHA256SUMS`

The final archive is immutable after finalization. Pilot/reconnaissance exposure remains disclosed.

## 31. K11-C decisions that should not be outcome-adjusted later

The following study semantics are fixed by this draft and should change only for an explicit pre-final correctness reason documented in version history, never because observed N2/D5 rates are inconvenient:

- K11 is observational;
- primary cohort is Advisory;
- five primary effect-bearing action strata;
- actor-visible/runtime-admitted evidence only;
- true-to-false `EAdm` transition defines post-preparation invalidation;
- exact-request identity, not semantic similarity, defines retention;
- positive evidence is required for N1 reconsideration;
- baseline-inadmissible preparation is not post-preparation invalidation;
- P0/P1 are excluded from the final estimator;
- K8/K10/K11 pooling is forbidden;
- zero N2 is a valid result.

## 32. Items intentionally deferred to K11-E

The following are not frozen in this draft because P0/P1 are explicitly designated to validate feasibility before finalization:

- exact final run count;
- checked-in final task pool;
- deterministic final selection/order manifest;
- final instrumentation implementation SHA/digest;
- final event schema digest;
- final analysis implementation digest;
- exact trace-overhead acceptance measurement after P0 calibration;
- whether a secondary Authority replay cohort is technically feasible;
- final paper wording.

Deferral of these items is part of the prospective design and must not be concealed.

## 33. Gate to K11-D

K11-D may begin only after:

1. K11 instrumentation is implemented according to this protocol and K11-B design;
2. unit/integration tests establish correlation and trace invariants without a Minecraft final run;
3. the K11-added trace does not use the existing durable JSONL sink for high-frequency events;
4. no final K11 archive or final prevalence estimate has been produced.

K11-D then executes P0 only. P1 and final runs require later checkpoints.
