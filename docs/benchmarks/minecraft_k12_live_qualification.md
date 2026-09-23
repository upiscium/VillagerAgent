# K12 live qualification and final admission

## Authority import and revision trust boundary

Prospective K12 authority code uses a one-way import DAG. Both provenance and
the presentation aggregate consume the dependency-neutral
`k12_qualification_semantics` module; the semantic module imports neither of
them and imports no runner or backend. The parent issues one-shot capabilities
for the exact fifteen cells and P1--P4, stores immutable normalized terminal
snapshots, and independently recomputes the frozen predicate. The resulting
semantic projection is written into the durable terminal event before the
parent mints a `QualificationSemanticAttestation`. Only after that causal chain
is complete may callers build an optional `LiveQualificationAggregate` report;
the aggregate is not needed to append the terminal event or mint the
attestation.

For Issue #585, each coordinate is also closed by a parent-owned execution
receipt. A cell receipt contains the ordered reset, provider, permit, effect,
oracle, and containment stage receipts; each stage binds one typed boundary
artifact, its predecessor, and an immutable observation digest. A probe receipt
contains the corresponding containment boundary. The parent derives the
normalized cell/probe records from those artifacts, never from caller-authored
semantic fields. The deterministic receipt mint is available only through the
injected-test controller. The runtime adapter accepts only a complete bundle
of boundary artifacts that the runtime parent has already authenticated; it
performs no execution or I/O and has no synthetic fallback. Concrete runtime
integrations must supply a complete, independently authenticated bundle of
typed parent-owned boundary artifacts. There is deliberately no generic
raw-observation-to-runtime-artifact promotion path: until a concrete runtime
adapter provides those artifacts, the runtime adapter fails closed rather than
inventing `runtime_verified` evidence.

Terminal receipts bind the coordinate execution-receipt identity and its full
stage-receipt identity chain. The durable passed event stores the normalized
registry snapshot and those terminal identities, so an interrupted in-memory
registry commit can be recovered only after the ledger chain, semantic
projection, and receipt identities are revalidated. A durable failed event is
similarly recoverable into a terminal failed session and cannot be retried.
This recovery is a same-parent registry repair: it does not rehydrate an
authority or controller after a process restart, and it does not recreate the
already-consumed live boundary objects.

Parent registry ownership, not constructor secrecy, is the authorization
boundary. An exact-class object made with `object.__new__`, a copied capability,
or a copied attestation has no authority unless the parent recognizes that
exact object and can reverify it against its own terminal registry and ledger.

The current Git revision is not authority embedded in tracked source. A runtime
parent accepts a commit/tree tuple only when an external verifier receipt
authenticates the complete checkout and fresh pull-request observations. The
same tuple is then required across checkout HEAD/tree, upstream and remote,
pull-request head/base, source closure, qualification authorization, and both
first-consume boundaries. Injected controllers exercise the same agreement
checks but remain non-runtime-admissible.

The live qualification is an authority-owned, non-scientific preflight.  A
parent first mints and first-consumes an `ActiveQualificationAuthority`; the
runner then supplies exactly the ordered fifteen-cell `K12Q` observations and
the separate ordered probe set `P1`, `P2`, `P3`, `P4`. The parent registry
rejects duplicate, reordered, replaced, retried, stale, cross-authority, or
cross-reservation publication. Only the complete registry can produce a passed
semantic attestation, and the qualification ledger terminal binds its semantic
projection and verifier identities. `LiveQualificationAggregate` remains a
descriptive reporting artifact; mutating or fabricating its shallow fields
cannot change the parent decision or authorize final execution.

`qualify_live_cell()`, `qualify_live_probes()`, `qualify_mock_campaign()`, and
`LiveArtifact` are diagnostic or presentation helpers only. Their caller-facing
pass fields never establish authority, publish a terminal, or satisfy final
prerequisites. Their `mock_only`/`test_only` origin is retained in the
immutable identity. Controller-owned injected observations use
the distinct `injected_fake` evidence origin while retaining the operational
`live_qualification`/`qualification_probe` bindings; they remain
`runtime_admissible == False`.  Runtime authorities accept only
`runtime_verified` evidence.  The A/R/S oracle distinction is unchanged: A
and R require a known true objective; S may terminate with a typed,
binding-matched expected stale rejection (`not_applicable`) but that rejection
is not task success.  Stop policy outcomes remain terminal;
retry, resume, replacement, and same-cell relaunch are forbidden.

Final execution is a separate phase. `FinalExecutionPrerequisites`, the
separate `K12FinalRunAuthorization`, final authority mint, first-consume check,
and final admission all require the exact parent-owned semantic attestation and
its common source/profile/contracts/capsule/environment closure. The final run
authorization, authority, first-consume check, and campaign admission explicitly
bind the semantic projection, probe projection, terminal-receipt census,
terminal event, and terminal ledger digests; no empty legacy aggregate or
object-receipt field substitutes for those bindings. A
parent-owned `ActiveFinalAuthority`
must mint one `FinalCampaignAdmission` using a phase-specific final manifest
and the exact ninety-cell schedule from `build_k12_cells()`.  Runtime final
manifests must be the sealed fixture/randomization manifest pair; qualification
manifests are rejected as final manifests.  An injected-test final authority
may create an injected `FinalCampaignAdmission` and one-shot cell authorities,
but the chain remains `runtime_admissible == False`.  Every final cell shares the
authority/profile/manifest/common-closure binding, and
`FinalCampaignAdmission.issue_cell()` returns a one-shot typed
`FinalCellAuthority`.  Duplicate, partial, spliced, mixed-phase, mixed-origin,
or mixed-closure censuses are rejected. A runtime final gate consumes the exact
parent-owned semantic attestation rather than the optional aggregate/probe
presentation objects, and additionally requires the active final binding,
fresh time-bounded authority state, and a clean manifest closure;
injected chains are structurally valid but never runtime-admissible.

The diagnostic helpers and aggregate inputs consume typed observations and
parent-minted authority objects but construct report-facing data only. The
authoritative mutating path is the explicit
`publish_live_qualification_terminals()`/parent-ledger path, which appends the
durable terminal event and mints the semantic attestation from parent-owned
receipts. No subprocess, process, network, RCON, systemd, cgroup, provider, or
native-tool fallback is permitted.
