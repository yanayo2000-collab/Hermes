# GLE E04-S04-01A2 Canonical-v2 Frozen Replay Input

## Outcome

This additive adapter freezes one canonical v2 nested input bundle, one
explicit synthetic clock, and one requested `DEV` or `VALIDATION` context into
a deterministic, externally anchorable input artifact. It closes only the
canonical-v2 transport and revalidation boundary required by S04-01.

It does not execute Replay. It does not issue or approve an Objective or Spec,
observe source content, emit a Snapshot, assign a partition, build a Golden
set, unlock Holdout, or change Gate 0 or Gate 1.

The v1 adapter and canonical-v2 validator remain byte-for-byte unchanged. A v1
object or artifact is not upgraded or accepted by this entry point.

## Honest reachable Spec shape

Canonical v2 deliberately requires a standalone `DRAFT` Spec to carry exact
`UNFROZEN` method and policy sentinels. Snapshot v2 rejects those sentinels
because a frozen input must bind explicit evaluator and policy identifiers, and
bundle validation requires the Snapshot identifiers to equal the Spec. A
`DRAFT` therefore cannot form a valid canonical-v2 input bundle.

This adapter accepts only the existing, reachable `AUTHORITY_CANDIDATE` shape
with frozen identifier strings and synthetic target allocation. In canonical
v2 that status means caller-supplied shape only:

- `approved_at` is null;
- the authority reference is syntactically present but its content is not
  opened or verified;
- `authority_validation_status=UNVERIFIED_REFERENCE_ONLY`;
- `authority_effect=NONE`;
- actual allocation, allocation readback, and all Meta object identifiers are
  absent;
- the mutation-event list is empty; and
- Snapshot source status remains `SYNTHETIC_FIXTURE_ONLY` with effect `NONE`.

The opaque authority-reference value participates in object and input hashes,
but its presence never grants authority. The envelope mechanically labels the
shape `AUTHORITY_CANDIDATE_UNVERIFIED`, records that authority-reference,
evaluator, and policy content was not opened, and remains an unsigned local
synthetic fixture. `DRAFT`, `APPROVED_SHAPE_CANDIDATE`, actual readback, and
physical Meta identities are rejected by this adapter even if canonical v2 can
validate some of those shapes elsewhere.

The `META_SPLIT_TEST` assignment mechanism and its capability-assessment ID are
also caller-supplied identifier shape only. This adapter does not open the
capability assessment, verify an active Meta split, or grant allocation effect.
Likewise, it binds the metric-contract hash and evaluator/policy version strings
without opening or validating their referenced content or implementations.

No new Spec status is introduced in this change. A future schema revision may
define a dedicated synthetic-only Spec status, but it must be reviewed and
versioned independently rather than silently reinterpreting canonical v2.

## Input and deterministic binding

The caller supplies:

1. one mode-`0600`, single-link, canonical `gle-canonical-input-bundle-v2`
   file;
2. the externally recorded raw SHA-256 of that file;
3. a bounded `replay_input_id`;
4. requested context `DEV` or `VALIDATION`; and
5. an explicit canonical UTC synthetic clock no earlier than Snapshot
   `created_at`.

The adapter re-runs `validate_canonical_input_bundle_v2`, including all nested
self-hashes and Objective/Invariant/Spec/Snapshot cross-bindings. The input root
binds the raw bundle SHA, semantic bundle hash, four nested object hashes,
metric definition and contract, attribution window/version, deduplication and
qualification versions, evaluator and policy identifiers, experiment and
lineage IDs, Snapshot ID/cutoff, requested context, synthetic clock, and the
permanent adapter ceiling.

The implementation never reads wall-clock time. Identical input bytes and
explicit parameters therefore produce identical envelope and manifest bytes;
changing the clock or requested context changes the input root.

`requested_split` is only requested test context. It is not partition
membership. `HOLDOUT` and every other value fail closed.

## Exact-three artifact and external anchor

The new-only mode-`0700` output directory contains exactly three mode-`0600`
canonical JSON files:

1. `canonical-input-bundle.json`
2. `replay-input-envelope.json`
3. `manifest.json`

The v2 bundle is the single canonical nested cross-binding unit. Keeping its
original bytes avoids four derived standalone copies and the resulting
bundle-versus-copy replacement surface.

The manifest binds the two payload raw SHA-256 values and sizes, bundle and
envelope semantic hashes, input root, status, trust label, requested-context
effect, and full validation ceiling. A later loader must be given the externally
published raw SHA-256 of `manifest.json`. It reopens the exact three files,
revalidates the nested bundle, rederives the envelope and manifest, and requires
exact equality. Self-consistent rehashing cannot raise the ceiling.

Input and artifact readers reject duplicate JSON keys, NaN/Infinity,
noncanonical bytes, non-UTF-8, oversized content, extra or missing files,
symlinks, hard links, FIFO/device/socket entries, wrong modes, and
file/directory/name-to-fd identity drift. The writer is no-replace, uses fixed
directory descriptors with `O_EXCL|O_NOFOLLOW`, writes the manifest last, and
fsyncs each file plus the artifact and parent directories.

## Permanent machine ceiling

Every valid artifact has exactly:

- `status=SYNTHETIC_AUTHORITY_CANDIDATE_CONTRACT_FIXTURE_ONLY`;
- `trust_status=UNSIGNED_LOCAL_SYNTHETIC_FIXTURE`;
- the complete canonical-v2 ceiling, including
  `contract_effect=V2_SCHEMA_AND_SYNTHETIC_VALIDATION_ONLY`, caller-asserted
  Objective/Spec shape semantics, and
  `metric_contract_content_status=NOT_OPENED_NOT_VERIFIED`;
- `input_effect=INPUT_ADAPTER_ONLY`;
- evaluator/policy implementations not opened, assignment capability not
  opened or verified, and `allocation_effect=NONE`;
- Objective, Spec, authority-reference, source, Snapshot, and partition effects
  `NONE`;
- `snapshot_emitted=false`;
- `replay_executed=false` and `replay_eligible=false`;
- `golden_eligible=false`;
- Holdout `LOCKED_NOT_ASSIGNED`;
- Gate 0 effect `NONE` and result effect `UNCHANGED`;
- Gate 1 effect `NONE`; and
- not-a-Dataset, not-a-Snapshot, not-a-Replay, and not-a-Gate-receipt flags.

A successful CLI build returns exit `2`, not `0`, so validation cannot be
mistaken for an operational PASS. Invalid schema, hash, time, transport, or I/O
returns `64` without a traceback.

## Explicit exclusions and next dependency

This module imports no SQLite, HTTP/network, Meta, environment-derived version,
runtime clock, evaluator implementation, or policy implementation. It does not
change DB/schema/API/worker/scheduler/service or production state.

Still excluded and blocked on independent evidence are program-root and
registry governance, real Objective/Spec authority, B2A/B2B lineage and signed
DEV/VALIDATION partition, remaining metric definitions/content authority,
complete mutation provenance, a real Snapshot assembler, Golden/Threshold,
Replay execution/diff/receipt, Holdout, and Gate promotion. A later real input
adapter requires a new reviewed contract; this synthetic artifact cannot be
promoted in place.
