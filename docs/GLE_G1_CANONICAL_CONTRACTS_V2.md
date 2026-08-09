# GLE G1-01C Canonical Evaluation Contracts v2

## Outcome

This additive contract corrects the v1 metric-version cross-binding and makes a
future frozen/approved Copy-only experiment Spec structurally representable.
It validates canonical objects and a synthetic input bundle only. It does not
issue, approve, promote, execute, or persist a real Objective, Spec, Snapshot,
Replay, Golden set, Holdout assignment, or Gate receipt.

The v1 modules and artifacts remain unchanged. A v1 object is not implicitly
upgraded, migrated, re-signed, or accepted as v2.

## Versions and exact objects

- `gle-objective-contract-v2`
- `gle-copy-only-invariant-projection-v2`
- `gle-experiment-spec-v2`
- `gle-evaluation-input-snapshot-v2`
- `gle-canonical-input-bundle-v2`

Every object has an exact schema and canonical self-hash. The bundle binds the
full nested objects and has its own self-hash.

## Correct metric bindings

Objective v2 freezes separate fields for:

- the qualified-join CPA definition version;
- the canonical metric-contract hash;
- attribution version and attribution window;
- deduplication version; and
- qualification-rule version.

Snapshot v2 carries those fields separately. Bundle validation compares each
field to its corresponding Objective field. In particular,
`snapshot.attribution_version` is never compared to
`objective.primary_metric.definition_version`, which was the known v1 defect.
Frozen version/mechanism fields reject reserved uncertainty labels including
`UNKNOWN`, `UNFROZEN`, `TBD`, `PENDING`, and `UNSET`. A `DRAFT` Spec must use
the exact method/policy sentinel `UNFROZEN`; `AUTHORITY_CANDIDATE` and
`APPROVED_SHAPE_CANDIDATE` must use non-reserved frozen versions.
Reserved-label detection is case-insensitive, and the DRAFT sentinel must use
that exact uppercase canonical spelling.

The metric-contract hash is a content-addressed reference. This module validates
its shape and binding but does not open or independently verify the referenced
metric contract or any observed metric source.

## Spec state and allocation readback

Spec v2 represents `DRAFT`, `AUTHORITY_CANDIDATE`, and
`APPROVED_SHAPE_CANDIDATE` shapes:

- `DRAFT` has no approval timestamp, authority reference, or actual allocation.
- `AUTHORITY_CANDIDATE` has a syntactically valid authority reference and frozen
  method/policy versions, but no approval timestamp.
- `APPROVED_SHAPE_CANDIDATE` has an approval timestamp, a syntactically valid authority
  reference, and non-`UNFROZEN` method/policy versions.

These names describe caller-supplied object shape only. Objective and Spec each
carry exact machine-readable fields stating that the claim/reference is
unverified and has `authority_effect=NONE`. This module does not open the
authority artifact or grant authority effect. A later source-aware,
externally governed loader must validate the authority content before any
consumer treats a Spec as governed approval.

Configured target allocation and observed actual allocation are distinct.
Actual allocation, when present, is allowed only on an
`APPROVED_SHAPE_CANDIDATE` and requires two complete physical Cell identities,
one readback timestamp no earlier than the declared approval, one evidence hash,
total share exactly 1, and compliance with the frozen deviation limit. Snapshot
allocation then declares `ACTUAL_READBACK`, binds the same evidence hash/time,
and must equal the per-Cell Spec actual allocation. Without actual readback the
Snapshot must declare `SYNTHETIC_TARGET_FIXTURE` and cannot carry a readback
hash/time. Approval and readback must precede the Snapshot cutoff; the cutoff
cannot exceed the hard deadline.

## Validation ceiling

Every accepted bundle mechanically contains this immutable ceiling:

- `contract_effect=V2_SCHEMA_AND_SYNTHETIC_VALIDATION_ONLY`
- Objective/Spec status semantics: `CALLER_ASSERTED_SHAPE_ONLY`
- authority-reference content effect: `NONE`
- metric-contract content: `NOT_OPENED_NOT_VERIFIED`
- source-content effect: `NONE`
- Objective authority, Spec authority, Snapshot, and partition effects: `NONE`
- `snapshot_emitted=false`
- Holdout: `LOCKED_NOT_ASSIGNED`
- Replay executed/eligible: `false`
- Golden eligible: `false`
- Gate 0 effect: `NONE`; Gate 0 result effect: `UNCHANGED`
- Gate 1 effect: `NONE`
- not a Dataset, Snapshot, Replay, or Gate receipt

The CLI returns exit `2` for a valid bundle to prevent schema validation from
being mistaken for an operational PASS. Invalid input returns `64`.

## CLI and input trust boundary

`scripts/validate_gle_canonical_contracts_v2.py` reads exactly one caller-supplied
bundle and requires its externally recorded raw SHA-256. The file must be
canonical JSON with one trailing LF, regular, mode `0600`, single-link, bounded,
and stable under name-to-file and parent-directory identity checks. Symlinks,
hardlinks, duplicate JSON keys, noncanonical bytes, oversized input, and anchor
drift fail closed. The CLI is read-only and emits only status, bundle hash, and
the validation ceiling.

## Explicit exclusions and next dependency

This change does not read SQLite, Meta, network, environment-derived versions,
or wall-clock time. It does not modify schemas, production runtime, workers,
schedulers, services, or existing v1 contracts.

After v2 is frozen, future work still requires real external governance roots,
source-aware Objective/Spec authority validation, actual metric and mutation
evidence, signed lineage/DEV-VALIDATION partition, and a separately governed
real Snapshot assembler. Gate 0 remains independent and must be truly
`CONTROLLED_FEASIBLE`; this contract cannot promote it.
