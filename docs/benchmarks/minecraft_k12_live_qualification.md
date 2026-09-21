# K12 live qualification and final admission

## Authority import and revision trust boundary

Prospective K12 authority code uses the one-way import DAG
`k12_live_qualification -> k12_execution_provenance -> k12_authority_contracts`.
The dependency-neutral contracts module imports neither provenance nor the
qualification implementation. Provenance therefore verifies the exact
registered aggregate, projection, terminal evidence, authority, and parent
controller without importing the concrete qualification module.
Before a passed terminal event can be published, the parent atomically
preclaims the exact canonical aggregate object under its active qualification
authority. The same locked claim gates terminal publication and is consumed
once when the projection/receipt pair is fulfilled; subclasses, replacement
objects, duplicate claims, and post-terminal registration are rejected.

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
the separate ordered probe set `P1`, `P2`, `P3`, `P4`.  The resulting
`LiveQualificationAggregate` is accepted only when every cell is terminal,
fresh, bound to the same profile/campaign/authority, and the qualification
ledger has a verified `passed` terminal event containing both aggregate
digests.

`qualify_mock_campaign()` and `LiveArtifact` remain diagnostic-only.  Their
`mock_only`/`test_only` origin is retained in the immutable identity and can
never satisfy final prerequisites.  Controller-owned injected observations use
the distinct `injected_fake` evidence origin while retaining the operational
`live_qualification`/`qualification_probe` bindings; they remain
`runtime_admissible == False`.  Runtime authorities accept only
`runtime_verified` evidence.  The A/R/S oracle distinction is unchanged: A
and R require a known true objective; S may terminate with a typed,
binding-matched expected stale rejection (`not_applicable`) but that rejection
is not task success.  Stop policy outcomes remain terminal;
retry, resume, replacement, and same-cell relaunch are forbidden.

Final execution is a separate phase.  A parent-owned `ActiveFinalAuthority`
must mint one `FinalCampaignAdmission` using a phase-specific final manifest
and the exact ninety-cell schedule from `build_k12_cells()`.  Runtime final
manifests must be the sealed fixture/randomization manifest pair; qualification
manifests are rejected as final manifests.  An injected-test final authority
may create an injected `FinalCampaignAdmission` and one-shot cell authorities,
but the chain remains `runtime_admissible == False`.  Every final cell shares the
authority/profile/manifest/common-closure binding, and
`FinalCampaignAdmission.issue_cell()` returns a one-shot typed
`FinalCellAuthority`.  Duplicate, partial, spliced, mixed-phase, mixed-origin,
or mixed-closure censuses are rejected.  A runtime final gate additionally
requires `runtime_verified` qualification/probe evidence, the active final
binding, fresh time-bounded authority state, and a clean manifest closure;
injected chains are structurally valid but never runtime-admissible.

The public APIs only consume typed observations and parent-minted authority
objects.  They construct inert data; no subprocess, process, network, RCON,
systemd, cgroup, provider, or native-tool fallback is permitted.
