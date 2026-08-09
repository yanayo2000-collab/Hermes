# GLE G1-03A Blind Label Assignment and Adjudication v1

## Purpose

This package implements the DEV/VALIDATION-only blind review process required by
GLE v1.1 S02-03. It derives review tasks from a fully revalidated G1-02B-2B
registry, validates two independent signed reviews, and requires a distinct
third reviewer for every disagreement.

The output is a **label candidate packet for later assembly**. It is not a
`GoldenCase`: the current foundation does not yet provide the canonical frozen
`snapshot_id` required by section 3.12. It is not a Dataset, Replay, Holdout, or
Gate receipt.

## Source boundary

Every public build, validation, write, and load path reopens the externally
anchored G1-02A v2 -> B1 v2 -> B2A -> B2B chain through
`load_validated_registry_directory`.

Tasks exist only when B2B is exactly `SIGNED_DETERMINISTIC_PARTITION`. A
`BLOCKED` or `PENDING_SIGNATURES` B2B artifact produces
`BLOCKED_SOURCE_PARTITION`, zero tasks, zero labels, and zero label effect. The
current real production chain remains in this blocked state because its B2A
authority response is `MISSING`.

Only `DEV` and `VALIDATION` assignments are accepted. `HOLDOUT` is rejected and
is always projected as `LOCKED_NOT_ASSIGNED`.

## Blind task payload

One coordinator task is derived for every B1 historical candidate entry that is
explicitly referenced by a signed B2A Study node in a signed B2B lineage
assignment. This closes `B2B assignment -> B2A node -> B1 entry -> A record
hashes` exactly once.

The coordinator record contains:

- content-addressed lineage, authority-node, assignment, and evidence references;
- the frozen DEV or VALIDATION split and opaque source identity;
- an opaque task identifier and blinded payload hash;
- the label schema/version and a mechanical blindness policy.

Before task derivation, the custodian supplies a separate canonical blinding
map whose raw SHA-256 is externally pinned. The supported issuance API uses
Python `secrets.token_hex(16)` to assign every exact B1 candidate-entry hash one
unique `blind_case_` token before external signing. It is signed by the frozen
`BLINDING_CUSTODIAN` for purpose `BLIND_TASK_ID_ISSUANCE`, is bound into the
assignment request, and is never copied into the review artifact or delivered
to reviewers. The implementation verifies the token shape, uniqueness,
signature, time, issuance-method assertion, raw anchor, and exact task
denominator; validation of a signed external map cannot independently prove
that the custodian actually invoked the supported issuance API. The secret mapping remains
coordinator-only. Public round data and stable source ordering therefore do not
derive the reviewer-visible token.

The CLI and supported public writer publish the coordinator-private and
reviewer-safe domains only as two distinct sibling directories under the same
already-existing physical parent. Equal, nested, or symlink-aliased output
targets fail before either domain is written. Success is returned only after
both exact file sets are reopened through their source-aware loaders. The
underscore-prefixed single-domain writers are internal implementation details,
not supported publication APIs.

The separately hashed reviewer-visible `blind_payload` contains only that
task-local opaque ID and a bounded, field-allowlisted evidence packet rederived from the
externally anchored A-v2 records. The packet exposes only audit-quality facts:
reconstruction/cutoff disposition, audit reason codes, subject and missing-field
counts, source kind, and boolean presence of evaluation time and
control/hypothesis commitments.
It does not expose upstream hashes, record IDs, lineage ID, canonical Study ID,
source identity, exact timestamps, market/account/object tokens, dataset split,
engine or Replay output, evaluator/policy result, peer response, legacy status
or winner, score, seed, raw account/customer identity, raw payload, or Holdout
identity. A task must include exactly one primary frozen evaluation fact; a
label must cite that fact. `snapshot_id` remains deliberately `null`, and
`not_golden_case=true`; this is an audit-fact packet, not the later canonical
EvaluationInputSnapshot.

Supplemental authority evidence references remain signed assertions. This tool
does not claim to independently content-verify those external artifacts.

## Reviewer trust and blindness attestation

The reviewer key registry, the signed blinding map, and both raw canonical-file
SHA-256 anchors are externally
pinned **before task derivation** and embedded into the request/task root. The
registry identifies an external identity issuer and its manifest SHA-256, and
contains exactly four active roles:

1. `BLINDING_CUSTODIAN`
2. `REVIEWER_A`
3. `REVIEWER_B`
4. `ADJUDICATOR_C`

Each role is frozen to an exact key ID, signer ID, stable issuer-asserted
`principal_id`, and RSA SPKI fingerprint. All four values are unique across the
round, so changing aliases or keys after assignment cannot select a different
review panel. The externally pinned reviewer-registry bytes also record the
identity issuer and its manifest SHA; v1 treats those fields as a governance
assertion and does not load or independently verify that identity-registry
artifact. It therefore cannot prove that the issuer mapped a natural person
honestly. Keys are RSA 2048 bits or stronger. Signatures bind the principal ID and use
`RSA_PKCS1_V1_5_SHA256` with purpose-bound, domain-separated messages.

The custodian key has two exact purposes: `BLIND_TASK_ID_ISSUANCE` and
`BLIND_LABEL_PACKET_DELIVERY`; neither purpose can substitute for the other.
Each A/B response embeds a custodian-signed delivery receipt attesting that the
declared visible artifact set was exactly the task's blinded payload hash before
either reviewer submitted. A/B separately attest that they did not see engine
output, the peer label, or the legacy conclusion. These signatures prove who
accepted which bytes; they do **not** cryptographically prove that no off-system
disclosure occurred.

## Label and adjudication contract

The committed `gle-g1-03a-audit-label-contract-v1` hash freezes the only valid
result/decision/action/reason combinations for this audit-only input:

- `WAITING_EVIDENCE -> CONTINUE_WAITING -> [NONE] -> MORE_EVIDENCE_REQUIRED`
- `DATA_INCOMPLETE -> CREATE_DATA_FIX_TASK -> [NONE] -> DATA_INCOMPLETE`

`NONE` is exclusive. Critical-risk labels come from the contract's closed
catalog. A caller cannot supply a different label version or expand the state
lattice. Each signed review contains:

- one canonical Evaluation result;
- one canonical Decision;
- a non-empty executable action proposal list;
- non-empty machine reason codes;
- non-empty task-local fact IDs including the primary frozen evaluation fact;
- zero or more critical-risk labels.

Reviewer A and Reviewer B must be different principals. Agreement is exact
across every label field. Any difference becomes
`CONFLICT_PENDING_ADJUDICATION`; priority rules or majority voting cannot
silently resolve it.

Reviewer C is a third distinct principal and may receive a custodian-signed
packet only after both A and B responses are signed. C binds both response
hashes and signs the resolved label.
An adjudication is forbidden when A and B agree.

## States and exits

| Aggregate state | Meaning | CLI exit |
| --- | --- | ---: |
| `BLOCKED_SOURCE_PARTITION` | B2A/B2B trust chain is not verified/signed | 4 |
| `BLIND_REVIEWS_PENDING` | At least one A/B response is missing | 2 |
| `ADJUDICATION_PENDING` | A/B disagree and C is missing | 3 |
| `ASSIGNED_EVALUATION_SUBSET_LABELS_RESOLVED` | Every assigned legacy-evaluation task is exact pair agreement or signed C adjudication | 0 |
| invalid schema/hash/signature/source/I/O | Fail closed; no valid artifact | 64 |

`ASSIGNED_EVALUATION_SUBSET_LABELS_RESOLVED` means only that this assigned
legacy-evaluation subset's signed review process is closed. The request, round,
and manifest also bind the complete B1 legacy-evaluation count, assigned task
count, named-exclusion count, unresolved/conflict counts, authority-node count,
and A-v2 current-context MATURING count (`not_asof=true`). Therefore exclusions
and MATURING gaps remain visible and cannot be mistaken for resolved labels.
`s02_03_effect=NONE`; this does not mean S02-03, Golden, Replay, Holdout, or Gate
success.

## Artifacts

The coordinator-private output directory is new-only, mode `0700`, and contains exactly eight mode
`0600` files:

- `manifest.json`
- `assignment-request.json`
- `trusted-reviewer-key-registry.json`
- `blind-tasks.ndjson`
- `blind-responses.ndjson`
- `adjudications.ndjson`
- `label-fragments.ndjson`
- `review-round.json`

`blind-tasks.ndjson` is deliberately coordinator-private: it retains the exact
identity-to-opaque-ID mapping needed for source-aware revalidation. It must
never be distributed to Reviewer A, Reviewer B, or Reviewer C.

For a signed source, the CLI also requires a separate reviewer output directory.
That directory has an independent manifest/raw-SHA trust domain and contains
exactly two mode-`0600` files:

- `manifest.json`
- `blind-payloads.ndjson`

The reviewer packet contains only the allowlisted `blind_payload` objects. It
contains no coordinator mapping, upstream identity, source hash, lineage,
canonical Study ID, or dataset split. Its source-aware loader rederives the
payloads from the same externally anchored chain and checks its independent
raw manifest SHA. Custodian delivery receipts bind individual
`blind_payload_hash` values from this reviewer-safe domain; response and
delivery schemas do not expose the coordinator-private `task_hash`. Giving reviewers the
coordinator artifact violates this contract and invalidates the blindness
claim.

All JSON/NDJSON in both domains is canonical and bounded. Each loader requires an externally
pinned raw `manifest.json` SHA-256, checks exact files, modes, sizes, hashes, and
read stability, then reopens the entire upstream chain and rederives all tasks,
responses, adjudications, fragments, and manifest projections.

The writer reserves the final directory atomically without replacement, writes
through fixed directory file descriptors with `O_EXCL|O_NOFOLLOW`, fsyncs every
file and directory, and never reports a partial directory as a valid artifact.

## Permanent ceilings

Every state, including a fully resolved label packet, fixes:

- `holdout_status=LOCKED_NOT_ASSIGNED`
- `not_golden_case=true`
- `golden_eligible=false`
- `replay_eligible=false`
- `gate1_effect=NONE`
- `s02_03_effect=NONE`
- `not_dataset_receipt=true`
- `not_replay_receipt=true`
- `not_gate_receipt=true`

## Explicit exclusions

This milestone does not implement or authorize:

- final `GoldenCase` assembly or snapshot adapters;
- Threshold Contract or metric thresholds;
- Holdout assignment, disclosure, read, or execution;
- Replay Harness, Evaluation Engine 2.0, Decision Policy, or Compiler changes;
- DB/schema/API/UI/worker/scheduler/network/Meta changes;
- production deployment or any Gate receipt.
