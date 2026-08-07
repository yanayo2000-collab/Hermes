# GLE G1-02B Lineage Candidates v1

## Outcome

G1-02B-1 consumes one externally anchored G1-02A artifact directory, reconstructs and validates the
entire audit bundle, and derives an audit-only ledger of exact evaluation-to-experiment relations,
same-launch component candidates, conflicts, and unresolved parent lineage.

This package does **not** assign DEV, VALIDATION, or HOLDOUT. Current G1-02A evidence has no
immutable `parent_experiment_id`, `parent_launch_id`, or approved `lineage_id`; launch tokens,
co-membership in a legacy evaluation, winner fields, object tokens, names, and timestamps are not
parent-lineage proof. Every entry therefore remains `split=UNASSIGNED`. HOLDOUT is mechanically
absent and locked.

## Input trust and integrity

The caller must provide the SHA-256 of the exact `manifest.json` bytes. The consumer requires the
four fixed regular files and rejects extra files, symlinks, non-canonical JSON/NDJSON, duplicate
keys, non-finite numbers, size/hash mismatches, file drift, and cross-bundle substitution. It then
reconstructs the G1-02A bundle and calls the complete G1-02A validator.

The input must remain:

- `trust_status=UNSIGNED_LOCAL_CAPTURE`
- `status=INCOMPLETE`
- `replay_eligibility=AUDIT_ONLY`
- `not_replay_receipt=true`

The SHA anchor proves that the selected bytes did not change. It is not a signer identity or a
production authenticity proof. The derived output is consequently fixed to
`UNSIGNED_LOCAL_DERIVATION / AUDIT_ONLY`, `replay_eligible=false`, `gate1_effect=NONE`, and
`not_dataset_receipt=true`.

Derivation, validation, and output writing all require the same audit directory plus its external
manifest-byte SHA. Candidate evidence references are checked back against the exact source
`(table, source_id, record_hash)` records, and the legacy-evaluation denominator must match the
source bundle exactly. A candidate cannot be validated from its own self-hashes alone.

## Resolver semantics

The resolver may establish only these relations:

1. A legacy evaluation subject ID may bind to the exact captured `ad_experiment` record.
2. A Creative Group or Audience Pair evaluation may form a same-launch component candidate only
   when all subjects exist, their launch tokens agree with the evaluation launch token, and their
   account token, market, and platform agree. Its subject set must also equal the complete captured
   `ad_experiment` set for that launch token; extra or missing current members are a conflict, not a
   guessed component.
3. Single-experiment rows remain insufficient to establish a multi-member component.

The resolver emits `CONFLICT` for dangling subjects, multiple launch tokens, subject metadata
disagreement, or changing member sets for the same launch. It never chooses one conflicting edge.
An exact component remains `COMPONENT_RESOLVED_PARENT_UNRESOLVED`; it is not a lineage.

Every evidence reference binds the G1-02A record hash and table-manifest hash. Hash-only hypothesis,
control, archive snapshot, report payload, and metric payloads are not interpreted as lineage facts.

## Split boundary

The emitted split registry is deliberately blocked:

```text
policy_status = UNFROZEN
allowed_splits = [DEV, VALIDATION]
assignments = []
holdout_status = LOCKED_NOT_ASSIGNED
```

G1-02B-2 must first obtain explicit immutable parent/lineage evidence plus a versioned, signed
DEV/VALIDATION policy. That later contract must assign complete lineages atomically, preserve an
append-only prior registry, bind seed commitments to the frozen membership root, and continue to
reject HOLDOUT. It must not rewrite this audit candidate into a trusted dataset.

## Files and exclusions

The CLI writes one new directory and refuses overwrite:

- `manifest.json`
- `lineage_candidates.ndjson`
- `components.ndjson`
- `coverage.json`

This change adds no schema, migration, database reader, API, worker, scheduler, random generator,
current-time read, Meta access, Golden labels, Replay execution, Holdout operation, or Gate receipt.
Removing these additive source/test/doc files and any local output directory is the complete
rollback. No production or service state is changed.
