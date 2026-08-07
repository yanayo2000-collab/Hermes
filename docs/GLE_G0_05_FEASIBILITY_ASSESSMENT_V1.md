# GLE G0-05 Offline Feasibility Assessment v1

## Purpose

G0-05 compiles immutable Gate 0 evidence into an unsigned feasibility
candidate. It does not call Meta, mutate SQLite, update the E00 governance
configuration, create or activate an experiment, or issue a Gate receipt.

The candidate always carries `gate0_result_ceiling=QUASI_ONLY`,
`attestation_status=PENDING`, and `not_gate_receipt=true`. A later, separately
authorized trust boundary may bind the sole-owner attestation to the exact
`candidate_body_hash`. Signatures cannot override technical failures or a
polluted Study.

## Frozen scope

- Account: `自投-MX-TM` (`1012060198097836`), market `MX`.
- Qualified join: TugaoFunnel `guild_join_success_users`, persisted as
  `tugao_join_success_users` under contract
  `tugao_funnel_daily_metrics_api_v1`.
- Actual allocation: impressions primary, spend cross-check, two exact Cell Ad
  tuples, target 50/50, maximum absolute deviation 10 percentage points.
- Minimum evidence: 1,000 impressions, USD 5 spend, three complete days,
  48-hour settlement, and six-hour source freshness.
- Power: 14-day baseline, two-sided alpha 0.05, power 0.80, relative MDE 0.30,
  maximum 14 days / USD 20, maximum daily USD 2, expected daily USD 20/14.
- Governance: `SOLE_OWNER=Chauncey`. The implementation must not manufacture
  four independent signers.

The current real Study is explicitly ineligible because it changed multiple
creative fields and was activated outside Plan -> Approval -> Verify ->
Receipt. It must remain sticky `POLLUTED/NOT_FEASIBLE` evidence.

G0-02B must first be deployed and pass its governed migration/readback. The
G0-05 CLI intentionally fails with `G005_SOURCE_SCHEMA_MISSING` before that
prerequisite; in that state no candidate exists and the program-level Gate 0
remains `QUASI_ONLY`. The run request also freezes a
`gle-g0-02b-qualified-transport-deployment-v1` evidence object. It must bind
the accepted source commit, a passed governed release receipt, deployment
time, and `natural_evidence_not_before_date` into one canonical hash.
The CLI separately reads both the governed release manifest and controlled
restart receipt. It verifies both integrity blocks, exact file SHAs, release
ID, backend Invocation transition, receipt completion time, accepted source
commit, and a deterministic hash of the six deployed G0-02B runtime files.
A caller-provided self-hash or unrelated passed receipt is insufficient.
Allocation evidence predating that date is rejected so historical recovery
cannot masquerade as natural canary traffic.

## Inputs and joins

The CLI consumes:

1. the committed G0-04 manifest, receipt, and evidence bundle;
2. the G0-01 input contract and immutable report;
3. an exact-hash SQLite checkpoint opened with `mode=ro&immutable=1` and
   `PRAGMA query_only=ON`;
4. the frozen policy, exact subject, and E00 governance contract.

The same checkpoint is also the authority for the two
`experiment_id -> Study Cell -> Campaign/AdSet/Ad` bindings. G0-01 IDs are not
trusted merely because the request and report agree with each other.

Study integrity is derived from the verified G0-04 receipt, not accepted as a
caller assertion. G0-04 v1 does not provide a subject-bound audience overlap
or internal-auction receipt, so this version always emits
`AUDIENCE_OVERLAP_UNKNOWN` and `INTERNAL_AUCTION_CONTAMINATION_UNKNOWN`.
Those dimensions require a new immutable evidence producer before they can
ever pass.

Meta allocation is read only from exact account, market, campaign, AdSet, and
Ad facts. Tugao rows intentionally have no authoritative account ID, so they
are joined through the full `(date, campaign_id, adset_id, ad_id)` tuple to a
Meta allowlist for the frozen account. Names, ad-only joins, proportional
splits, `guild_joins`, Bind, and CRM metrics are never substitutes.

Only non-Internal Tugao rows with all of the following may contribute:

- `qualified_join_metric_observed is true`;
- `qualified_join_exact_attribution is true`;
- `qualified_join_attribution_status == "exact"`;
- source field `guild_join_success_users`;
- source contract `tugao_funnel_daily_metrics_api_v1`;
- a finite, non-negative integer count.

Every settled Cell/day must be present. Missing rows and zero denominators are
`UNKNOWN`, not observed zero and not a 50/50 allocation.

Attribution coverage is event-weighted: the denominator is the sum of all
eligible observed qualified joins for the frozen campaign/window, and the
numerator is the sum mapped to the exact two Cell tuples. Row counts are
diagnostic only and must never stand in for qualified-event coverage.

## Fail-closed decisions

- Hash, subject, schema, sidecar, source-drift, or artifact mismatch aborts the
  run without publishing a committed candidate.
- External activation or multi-variable contamination produces
  `NOT_FEASIBLE` and cannot be signed away.
- Missing allowlists, ownership/capability evidence, attribution coverage,
  audience overlap evidence, complete allocation, natural qualified joins,
  approved Power golden vectors, or candidate-bound attestation keeps Gate 0
  at `QUASI_ONLY`.
- The v1 policy freezes `golden_vectors_approved=false`. The named O'Brien-
  Fleming estimator is therefore not executed; target information remains
  unknown until a separately versioned, Data-approved golden-vector contract
  exists. A caller cannot flip this flag.
- The caller cannot provide `result`, `feasible`, shares, Power output, or an
  attestation inside the assessment request.

## Publication and rollback

The script exclusively creates a canonical candidate and then a committed
manifest containing its file SHA and body hash. Existing outputs are never
overwritten. Rollback for this PR is deletion of the new code, script, tests,
and document; it has no database, Meta, governance, or production rollback.

Production snapshot collection, G0-02B deployment/migration, historical
backfill, natural-event acceptance, attestation, Gate receipt finalization,
E00 Gate advancement, and any Meta write are separate governed milestones.
