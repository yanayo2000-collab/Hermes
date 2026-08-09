# GLE G1-02B-2A Immutable Lineage Authority V1

## Outcome

This package defines an offline, fail-closed contract for accepting externally
attested Study-level lineage roots and parent edges. It converts the complete,
externally anchored G1-02A audit denominator and its G1-02B-1 lineage-candidate
derivation into one of four diagnostic states:

- `MISSING`: no signed authority response was supplied.
- `VERIFIED`: the full denominator, Study graph, evidence references, trust root,
  and all three detached signatures are valid.
- `CONFLICT`: the proposed graph, component membership, or denominator coverage
  contradicts the anchored input.
- `INVALID`: schema, hash, time, trust-root, or signature validation failed.

Even `VERIFIED` means only `LINEAGE_AUTHORITY_ATTESTATION_VERIFIED`. It is not a dataset,
Replay, Golden, Holdout, Gate, or execution receipt.

## Inputs and trust roots

The request builder requires both prior artifact directories and their externally
pinned raw manifest SHA-256 values:

1. G1-02A immutable as-of audit bundle;
2. G1-02B-1 lineage candidate bundle.

The loader reopens, bounds, hashes, parses, and fully validates both sources. The
request binds their manifest hashes, bundle hashes, source/as-of roots, cutoff,
complete legacy-entry denominator, component denominator, and exact subject IDs.
A caller cannot replace the denominator and repair only the outer hashes.
The request also binds an externally supplied deterministic `evaluated_at` clock;
the enforced order is `cutoff <= requested_at <= authorized_at <= evaluated_at`.
This contract never reads wall-clock time. The caller must pin the evaluation
clock in the surrounding governed evidence rather than treating it as self-authenticating.

An authority response is trusted only against a separately supplied trusted-key
registry whose canonical hash is pinned outside the response. Inline keys do not
establish trust. The registry must contain distinct active keys and signers for:

- `BUSINESS_OWNER`
- `DATA_OWNER`
- `TECH_OWNER`

Every signature is `RSA_PKCS1_V1_5_SHA256` with a minimum 2048-bit RSA key. The
three selected SPKI fingerprints must be distinct. Its domain-separated message
binds the registry hash, key ID, signer ID, role, purpose, and authority payload
hash, so one signature cannot be relabeled into another role. Each signature is purpose-bound to
`LINEAGE_AUTHORITY_ATTESTATION`, covers the canonical authority payload hash, and
must fall within the pinned key's validity interval. The implementation invokes
the local `openssl` verifier without reading a network, database, or environment
secret. Private keys are never accepted by this package.

## Study-level lineage contract

Each signed lineage assertion binds:

- a canonical Study experiment ID and exact Spec hash;
- exactly one G1-02B-1 component and all of its captured legacy Cell IDs;
- all candidate entries belonging to that component;
- a content-derived root `lineage_id`;
- either an explicit root declaration or an explicit parent edge;
- authority-asserted evidence references by artifact manifest SHA, record ID,
  and record hash.

Those evidence references are signed assertions, not independently loaded or
content-verified artifacts in this version. The canonical response plus its three
valid detached signatures is the authority artifact. A later evidence-ingestion
version must add externally pinned artifact loaders before it may describe those
supplemental references as verified evidence.

Roots have `parent=null` and `iteration_no=1`. A child must reference an existing
parent in the same lineage, bind the parent's component and Spec hash, and use
exactly the parent's iteration plus one. Each captured legacy experiment may
belong to only one canonical Study node. Every candidate entry and every component
must occur exactly once across nodes or named exclusions.

Named exclusions are explicit authority decisions, not inferred omissions. They
must include nonempty machine reason codes and `NAMED_EXCLUSION` evidence. A
G1-02B-1 conflict may be named and excluded, but it cannot be silently converted
to a valid lineage node. Entries that have no component remain in the denominator
and may be covered only by a named exclusion with an empty component list.

The following observations are never accepted as parent authority:

- matching account;
- nearby creation time;
- shared launch token;
- similar name;
- reused Meta object ID;
- winner relationship.

Generation proposals and legacy Cell identifiers are likewise not canonical
Study parent edges unless an external immutable authority artifact explicitly
binds them under this contract.

## Hard ceilings

All output fragments mechanically keep:

- `split_assignments=[]` and `split_effect=NONE`;
- `holdout_status=LOCKED_NOT_ASSIGNED`;
- `replay_eligible=false` and `golden_eligible=false`;
- `gate1_effect=NONE`;
- `not_dataset_receipt=true` and `not_gate_receipt=true`.

This package never assigns DEV or VALIDATION and has no schema, migration, DB,
API, worker, scheduler, Meta, or production path. The later G1-02B-2B milestone
must independently define and approve an append-only DEV/VALIDATION registry,
policy signatures, prior-head pinning, and randomness commitment. HOLDOUT remains
outside that milestone as well.

## CLI and artifacts

`scripts/validate_gle_lineage_authority.py` accepts canonical JSON authority and
key-registry inputs only. Duplicate keys, non-finite values, noncanonical bytes,
symlinks, special files, and oversized inputs fail closed through the bounded
loader. It creates a new output directory and refuses overwrite.

The output is exactly:

- `authority-request.json`
- `authority-response.json` (`null` when absent)
- `trusted-key-registry.json` (`null` when absent)
- `authority-fragment.json`
- `manifest.json`

The writer revalidates both anchored source directories, re-evaluates the response
and signatures, and requires the supplied fragment to equal that derivation. The
response and public-key registry are retained so a later consumer is not forced
to trust an unverifiable `VERIFIED` label.
The manifest projects every hard ceiling. Its loader requires an external raw
manifest SHA and an independently supplied key-registry hash, reopens all five
files, revalidates both source bundles, verifies the signatures, and rederives the
fragment. A manifest-contained registry hash is never accepted as its own anchor.

Exit codes are `0` for `VERIFIED`, `2` for a successfully materialized `MISSING`
or `CONFLICT` diagnostic, and `64` for invalid input/authority. Exit `0` still
does not mean Gate 1 PASS or permission to run Replay, Golden, Holdout, or Meta.

## Acceptance and rollback

Focused acceptance covers source-anchor replacement, fully rehashed denominator
tampering, component drift, missing coverage, false parent edges, forbidden
inference fields, wrong-purpose or invalid signatures, an unpinned key registry,
all no-split ceilings, canonical CLI input, and new-directory-only output.

Rollback is deletion or abandonment of the newly generated local artifact
directory and removal/revert of these four additive files. No production or
persistent-data rollback exists because this package has no such write path.
