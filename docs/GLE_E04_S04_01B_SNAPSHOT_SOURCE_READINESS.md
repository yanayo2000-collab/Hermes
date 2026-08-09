# GLE E04-S04-01B — Real Snapshot Source Readiness v1

## Purpose

This package audits whether externally anchored evidence is sufficient to build a real canonical `EvaluationInputSnapshot`. It deliberately does **not** build a Snapshot, execute Replay, assemble Golden cases, assign Holdout, or create any Gate receipt.

The current real G1 chain remains blocked: G1-02B-2A is `MISSING`, G1-02B-2B is `BLOCKED`, and Gate 0 remains `QUASI_ONLY`. Therefore the current production evidence can only produce `BLOCKED_UPSTREAM_AUTHORITY`, zero subjects, and a gap ledger.

## Why this precedes a real Snapshot adapter

The repository can validate a synthetic canonical Snapshot, but current real evidence does not yet prove, at one immutable cutoff:

- an approved canonical Objective and its metric, guardrail, and risk versions;
- an approved canonical Spec with authoritative Study lineage, copy-only topology, actual allocation readback, and frozen evaluator/policy versions;
- all seven required per-Cell metrics (`spend`, `impressions`, `clicks`, `installs`, `qualified_joins`, `invalid_users`, and `allocation_share`);
- freshness, attribution coverage, missing-source, and duplicate-rate facts;
- a complete cutoff-bound mutation journal.

Missing values are never zero-filled. Configured allocation is never accepted as observed allocation. Mutable current state and retained event fragments are not treated as a complete historical mutation journal.

## Inputs and external trust roots

The builder reopens the externally raw-SHA-anchored A → B1 → B2A → B2B artifact chain through the accepted source-aware B2B loader. It also requires a canonical source-observation JSON file and its raw SHA-256 supplied outside that file. The observation separately binds the claimed Gate 0 raw manifest SHA, assessment hash, result, and evidence references; `CONTROLLED_FEASIBLE` requires one reference that simultaneously matches the manifest SHA, assessment record hash, and `CONTROLLED_GATE0_ASSESSMENT` class. This v1 package still does not open that Gate 0 artifact, so the claim always remains `GATE0_RESULT_CONTENT_NOT_VERIFIED` and cannot produce a ready state.

The source-observation file is an externally anchored **unsigned observation assertion** (`EXTERNALLY_ANCHORED_UNSIGNED_SOURCE_ASSERTIONS`). Its positive field statuses do not become independently verified facts merely because the file is hashed. Every `ASSERTED_AVAILABLE` field must contain a value commitment and at least one immutable or signed evidence reference. The v1 loader does not open those referenced artifacts, so every such field remains an explicit `SOURCE_FIELD_CONTENT_NOT_VERIFIED` gap. Every non-available field must contain no value commitment and must carry explicit reason codes.

The time chain is explicit and mechanical: upstream cutoff ≤ observation time ≤ readiness request time. The module never reads the wall clock.

The exact subject denominator is derived from a `SIGNED_DETERMINISTIC_PARTITION` registry. If B2A/B2B is not verified/signed, the observation must contain zero subjects. `HOLDOUT` and unknown splits are rejected everywhere.

## Required field denominator

Every eligible canonical experiment must carry exactly these ordered field paths:

- Objective: approval authority, primary metric versions, secondary metrics, guardrails, risk boundary.
- Spec: identity/lineage, copy-only topology, invariant projection, assignment target, actual allocation readback, power plan, evaluator version, policy version.
- Cell metrics: spend, impressions, clicks, installs, qualified joins, invalid users, allocation share.
- Data quality: freshness, attribution coverage, missing sources, duplicate rate.
- Mutation provenance: complete event journal and cutoff binding.

Each field is exactly one of `ASSERTED_AVAILABLE`, `MISSING`, `UNFROZEN`, `CONFLICT`, or `UNAUTHORIZED`.

## Artifact

The output directory is new-only, mode `0700`, and contains exactly four mode-`0600` files:

- `request.json`
- `source-observations.json`
- `gaps.ndjson`
- `manifest.json`

All JSON is canonical UTF-8 with one trailing LF. The manifest binds every payload SHA and byte size. The loader requires an external raw manifest SHA, reopens the upstream chain and source-observation anchor, rederives the request and complete gap ledger, and rejects extra, missing, non-regular, symlinked, oversized, noncanonical, or changing files.

## States

- `BLOCKED_UPSTREAM_AUTHORITY`: lineage authority or signed DEV/VALIDATION partition is absent; zero subjects.
- `BLOCKED_GATE0_NOT_CONTROLLED`: subject partition exists but Gate 0 is not `CONTROLLED_FEASIBLE`.
- `SOURCE_INCOMPLETE`: the upstream authorities are present and Gate 0 is controlled, but at least one required source field is missing, unfrozen, conflicting, or unauthorized.
- `SOURCE_ASSERTIONS_UNVERIFIED`: the exact field denominator is externally anchored and every field is asserted available, but neither Gate 0 nor the referenced field content has been independently loaded and rederived. It remains blocked from Snapshot emission.

## Permanent ceilings

Every state and the manifest mechanically fix:

- `snapshot_effect=NONE`
- `snapshot_emitted=false`
- `partition_effect=NONE`
- `holdout_status=LOCKED_NOT_ASSIGNED`
- `replay_executed=false`
- `replay_eligible=false`
- `golden_eligible=false`
- `gate1_effect=NONE`
- `not_snapshot_receipt=true`
- `not_dataset_receipt=true`
- `not_replay_receipt=true`
- `not_gate_receipt=true`

The CLI returns exit `2` for every valid artifact because no state in this version is Replay-ready or Gate-complete. Validation, anchor, schema, or I/O failures return `64` and do not create a valid artifact.

## Security and non-goals

The package has no SQLite, Meta, network, scheduler, service, production, or wall-clock dependency. Time is explicit input. It does not modify the existing canonical v1 schemas, infer missing Objective/Spec facts, read live production data, freeze evaluator/policy methods, generate a canonical `snapshot_id`/`snapshot_hash`, or authorize Replay/Holdout/Gate activity.

The next real milestone is a separately reviewed Snapshot assembler only after Objective/Spec authority, real B2A/B2B, Gate 0 `CONTROLLED_FEASIBLE`, complete same-cutoff Cell metrics, and complete mutation provenance are independently closed.
