# GLE G1-01B — ObjectiveContract Authority Attestation v1

## Purpose and trust boundary

This offline contract creates a canonical Objective candidate and proves that three
role-specific signatures are valid under an **externally supplied and externally
pinned** key registry. It does not prove that the caller chose a registry already
authorized by real-world governance. Consequently its strongest result is
`OBJECTIVE_AUTHORITY_ATTESTATION_VERIFIED`, while `authority_effect` remains `NONE`.

The signed `approved_by` and `approved_at` fields are part of the attested candidate;
they are not, by themselves, a governance approval receipt. A later, independently
rooted governance consumer must authorize the registry and promote the candidate.
Until that exists, S04-01B's Objective content gaps remain open.

The real lineage chain remains B2A `MISSING`, B2B `BLOCKED`, and Gate 0
`QUASI_ONLY`. This artifact cannot change any of those states.

## Two-stage freeze

The workflow is deliberately split:

1. `prepare` validates the proposal and externally pinned registry, derives the
   request, and writes an exact two-file request directory. It exits `2` because no
   attestation exists yet.
2. Governance records the request directory's raw `manifest.json` SHA-256 outside
   the package. The response and every detached signature bind that raw SHA.
3. `finalize` reopens the request directory using the external raw anchor, reopens
   the proposal and registry using their external anchors, validates the response,
   and writes the final exact four-file artifact.

There is no one-call API that constructs a request and accepts a response in the
same operation. Missing signatures remain represented by the frozen request package,
not by a misleading final authority artifact.

## Frozen metric contract

The proposal contains a canonical `primary_metric_contract` whose hash is bound by
the request and signatures. It freezes:

- formula: `SPEND_USD / QUALIFIED_JOIN_SUCCESS_USERS`;
- numerator source, USD currency, and Cell/cutoff cumulative grain;
- qualified-source contract, source metric, and qualification version;
- attribution version/window and exact-identity dedup version/unit;
- settlement requirement and late-data rebuild rule;
- zero-event handling (`DATA_INCOMPLETE`, never zero or infinity).

The Objective keeps the true metric-definition version
`gle-qualified-join-cpa-v1`; attribution remains a separate version inside the
metric contract. Canonical bundle v1 currently compares these semantically different
fields, so this candidate is intentionally not bundle/replay compatible. That
cross-binding must be corrected in a separately reviewed canonical v2; this module
does not corrupt either version string to pass the old comparison.

Business choices remain explicit in the proposal: minimum improvement, secondary
metrics, guardrails, budgets, write limit, deadline, approval TTL, and creator.
Mutable policy, legacy rows, deployment receipts, service health, or unsigned Gate 0
observations cannot silently provide them.

## Registry and signatures

The registry and proposal are canonical mode-`0600` regular files with external raw
SHA anchors. The registry also has an externally supplied semantic hash. It contains
exactly one active RSA key (at least 2048 bits) for each purpose:

- `BUSINESS_OWNER / OBJECTIVE_BUSINESS_APPROVAL`;
- `DATA_OWNER / OBJECTIVE_METRIC_CONTRACT_APPROVAL`;
- `TECH_OWNER / OBJECTIVE_TECHNICAL_BINDING_APPROVAL`.

Key IDs, signer IDs, asserted stable principal IDs, roles, and SPKI fingerprints are
all distinct. These checks prevent trivial alias/key reuse inside the registry but do
not prove the real-world identity assertion. Each signature binds the response
payload, frozen request raw manifest SHA, registry raw/semantic hashes, key, signer,
principal, role, and purpose. Time order is proposal creation <= request <=
authorization/signature <= externally supplied evaluation time, and the signing key
must be valid at authorization.

## Artifacts and loader

The request directory contains exactly:

- `manifest.json`
- `authority-request.json`

The final directory contains exactly:

- `manifest.json`
- `authority-request.json`
- `authority-response.json`
- `objective-contract.json`

Directories are mode `0700`; files are mode `0600`, regular, single-link, bounded,
canonical JSON. Writers are new-only and fd-relative. Readers bind opened file
identity to the directory entry before accepting bytes and recheck directory identity
and exact file sets. Loaders require external raw manifest anchors and fully rederive
request, response, Objective, descriptors, and ceilings from the same upstream roots.

## Status and permanent ceilings

The local terminal status is:

- `OBJECTIVE_AUTHORITY_ATTESTATION_VERIFIED`
- `trust_status=SIGNATURES_VALID_UNDER_EXTERNALLY_PINNED_REGISTRY`
- `attestation_effect=OBJECTIVE_AUTHORITY_ATTESTATION_VERIFIED`
- `objective_effect=SIGNED_CANDIDATE_NOT_GOVERNANCE_PROMOTED`
- `authority_effect=NONE`

Both `prepare` and `finalize` return CLI exit `2`, including a cryptographically
valid final attestation. Exit `0` is reserved for a future independently rooted
governance consumer with a real authority effect. Invalid input returns `64`.

All request and final artifacts also fix:

- `snapshot_effect=NONE`, `snapshot_emitted=false`;
- `partition_effect=NONE`, HOLDOUT `LOCKED_NOT_ASSIGNED`;
- Replay and Golden eligibility false;
- `gate1_effect=NONE`;
- not a Dataset, Replay, or Gate receipt.

## Non-goals and next dependency

This module has no SQLite, network, Meta, scheduler, service, production, or wall-clock
path. It does not modify canonical v1 or schema, close the S04-01B readiness ledger,
produce a real Spec/Snapshot, or authorize Replay/Holdout/Gate.

Next dependencies are: an independently rooted registry-governance policy/consumer;
then a separately versioned real Spec authority (canonical Spec v1 remains
`DRAFT`, method/policy `UNFROZEN`, and actual allocation absent); then real B2A/B2B,
same-cutoff Cell metrics, data quality, and complete mutation provenance.
