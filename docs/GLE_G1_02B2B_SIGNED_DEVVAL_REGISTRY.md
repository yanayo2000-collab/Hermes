# GLE G1-02B-2B signed DEV/VALIDATION registry v1

## Purpose

This package is the offline bridge from a verified G1-02B-2A lineage authority
attestation to a signed deterministic DEV/VALIDATION partition. It never assigns
HOLDOUT, runs Replay or Golden evaluation, changes Gate state, reads a database,
calls Meta, or connects to a runtime service.

The current production authority artifact is `MISSING`. That input can only create
a `BLOCKED` diagnostic with zero assignments and `split_effect=NONE`. A nonzero
registry requires a separately anchored B2A `VERIFIED` artifact and a new three-role
DEV/VALIDATION signature set.

## Source and trust chain

Every public build or load operation reopens the externally anchored G1-02A,
G1-02B-1, and G1-02B-2A directories through
`load_validated_authority_directory`. The registry request binds:

- raw A, B1, and B2A manifest SHA-256 values;
- B2A request, fragment, response, authority, and external key-registry hashes;
- the frozen data cutoff;
- all eligible signed lineage nodes and their canonical Study IDs;
- an externally pinned seed-selection record;
- an externally pinned prior registry head for generations after genesis.

Named exclusions from B2A never become eligible lineages. Each eligible unit is the
complete `lineage_id`, not an evaluation row or legacy Cell. All canonical Studies
inside that lineage receive one assignment.

The DEV/VALIDATION key registry is independent from the B2A authority registry.
Keys must be active RSA public keys of at least 2048 bits with distinct key IDs,
signer IDs, roles, and SPKI fingerprints. The only accepted purpose is
`DEV_VALIDATION_REGISTRY_ATTESTATION`. Reusing a key that is authorized only for
`LINEAGE_AUTHORITY_ATTESTATION` fails closed.

## Frozen schemas

The exact five-file artifact is:

- `registry-request.json`
- `registry-response.json` (`null` while blocked or pending)
- `trusted-key-registry.json` (`null` while blocked or pending)
- `devval-registry.json`
- `manifest.json`

Files are canonical JSON plus one LF, at most 2 MiB each, mode `0600`, in a new
mode-`0700` directory. Symlinks, special files, extra paths, noncanonical JSON,
unknown keys, nonfinite numbers, overwrites, or hash mismatches are rejected.

The request freezes `registry_id`, generation, request/evaluation clocks, complete
authority binding, prior binding, policy, eligible lineages, retained assignments,
and all hard ceilings. Genesis uses generation 1 and no prior. Later generations
must point at an independently pinned immediately preceding manifest and keep the
same registry ID and policy. That head SHA is an external governance trust root;
this artifact does not recursively prove how the caller selected or published it.

The policy uses:

- unit `LINEAGE_ID`;
- exact allowed splits `[DEV, VALIDATION]`;
- integer `validation_threshold_bps` in `1..9999`;
- algorithm `SHA256_U64_THRESHOLD_V1`;
- an external seed-selection record dated no later than `requested_at`;
- `holdout_status=LOCKED_NOT_ASSIGNED`.

The response carries a 32-byte hex seed, binds the request and complete assignment
payload hash, and carries exactly three ordered signatures:
`BUSINESS_OWNER`, `DATA_OWNER`, and `TECH_OWNER`. The signed message is domain
separated and binds response payload hash, external key-registry hash, key ID,
signer ID, role, and purpose. `authorized_at` must fall between the request and
external evaluation clocks and within each key's validity interval.

## Deterministic assignment and retained-state behavior

For a new lineage, the engine hashes:

```text
GLE_DEVVAL_ASSIGNMENT_V1
<policy_hash>
<seed_reveal>
<lineage_id>
```

The first eight digest bytes form an unsigned 64-bit score. Scores below the
signed validation threshold are `VALIDATION`; the remainder are `DEV`. There is no
row-level randomization, outcome-aware balancing, retry, or threshold adjustment.
The mechanism proves only that the signed partition is reproducible from the
selected seed. It is not commit-reveal, does not provide an anti-grinding proof,
and does not prove that the seed selection was unbiased. Its hard ceilings prohibit
Replay, Golden, Holdout, and Gate use.

Every prior assignment is recomputed from the frozen policy and the same seed,
then required to match the independently pinned prior head byte-for-byte. A later authority must still
contain the same lineage membership and canonical Study IDs. Removal, mutation,
policy drift, generation gaps, registry-ID drift, changed split, or changed score
fails closed. New lineages may be appended under the same frozen policy and seed.
The registry state root binds the full current assignment set, policy, generation,
and prior manifest anchor. It is a current-state content root, not a recursive
proof of all earlier publications.

## State lattice and hard ceilings

- `BLOCKED`: B2A is not `VERIFIED`; zero assignments and no split effect.
- `PENDING_SIGNATURES`: authority and policy are source-bound, but the response is
  absent; zero assignments and no split effect.
- `SIGNED_DETERMINISTIC_PARTITION`: the complete request, selected seed, assignments, external
  key registry, and three signatures validate.

Invalid schema, timestamps, hashes, signatures, source anchors, or artifact bytes
raise an invalid-input error. Append or membership conflicts fail closed and never
produce a consumable registry.

Every state fixes:

- `holdout_status=LOCKED_NOT_ASSIGNED`;
- `replay_eligible=false`;
- `golden_eligible=false`;
- `gate1_effect=NONE`;
- `not_dataset_receipt=true`;
- `not_gate_receipt=true`.

Even `SIGNED_DETERMINISTIC_PARTITION` proves only signed deterministic
DEV/VALIDATION lineage assignment.
It is not a Golden dataset, Replay result, Holdout release, Gate 1 receipt, Meta
approval, or production execution authorization.

## CLI and exit codes

`scripts/build_gle_lineage_devval_registry.py` accepts only explicit directories,
raw manifest SHA anchors, clocks, and canonical JSON policy/response/key files. It
creates a new directory and refuses overwrite.

For later generations, `--expected-prior-devval-key-registry-hash` validates the
independently pinned prior artifact. It is distinct from
`--expected-devval-key-registry-hash`, which is supplied only with the current
signed response. This separation permits a source-bound generation to remain
`PENDING_SIGNATURES` without pretending that a current response exists.

- exit `0`: `SIGNED_DETERMINISTIC_PARTITION`;
- exit `2`: successfully materialized `BLOCKED` or `PENDING_SIGNATURES`;
- exit `64`: invalid or conflicting input.

The caller must externally record the raw `manifest.json` SHA. A manifest-contained
hash is never its own transport or governance anchor.

## Acceptance and rollback

Focused tests cover a real B2A source chain, missing-authority blocking, pending
signatures, distinct three-role verification, seed and purpose tampering, full
rehash promotion attempts, genesis output reload, prior-head anchoring, and
byte-stable second-generation retention. Regression acceptance also runs the
G1-02A/B1/B2A suites serially.

Rollback is abandonment or deletion of the new offline artifact directory and
revert of these four additive repository files. There is no database, Meta, service,
or backend rollback because this package has no such path.
