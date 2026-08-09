# E04-S04-01B4 GET-only Meta Activity / Current-State Evidence

Version: `gle-e04-s04-01b4-meta-activity-request-v1`

This package creates a caller-anchored, bounded Meta `/activities` capture claim
and double-read claim for the frozen five-object status denominator.  It is a
source-readiness fragment for a future canonical `EvaluationInputSnapshot`, not
proof that the network exchange happened.  It does not emit a Snapshot, execute
Replay, allocate a split, or change any Gate.

## Outcome boundary

The implementation reuses the reviewed G0-04 `GetOnlyGraphClient`.  Its public
surface has no mutation method, redirects are forbidden, every endpoint is
derived from the frozen subject, pagination is bounded to five pages and 100
activity rows, and the token exists only in process memory.

The exact denominator is:

- one Campaign `status`;
- two Cell Ad Set `status` values;
- two Cell Ad `status` values.

The capture performs first object reads; opens the frozen `SPLIT_TEST` Study
(including its exact start/end interval), its exact two Cells,
each Cell's Campaign/Ad Set edge, and every Ad→Ad Set binding; runs a bounded
account `/activities` query; then performs last object reads.  Study/Cell/object
topology, object IDs, account, Campaign membership, timestamps, pagination, and
the exact ordered token-free GET query/response-journal claims are checked before
publication.  The query claim binds `since`, `until`, `limit`, and each cursor.
Meta source timestamps are normalized to canonical UTC.  Request timestamps are
already canonical UTC and a window longer than exactly 31 days is rejected.

## Evidence semantics

Only an allowed `STATUS_UPDATE` activity whose source `object_type` matches the
denominator and whose changed-data explicitly names `field=status` can become a
transition claim with
`before`, `after`, and the Meta event time.  Actor and application IDs are
compared with the externally raw-SHA-pinned G0-04 actor registry:

- `ACTOR_REGISTRY_MATCHED_OBSERVATION` means only that the pair appears in the
  supplied frozen registry with the `ACTIVATE` role;
- `EXTERNAL_OR_UNGOVERNED_OBSERVATION` means a non-matching identity was seen;
- `UNKNOWN_ACTOR_OBSERVATION` means no classifiable identity was present.

None of these labels proves a real-world natural person, governance authority,
or a complete mutation history.  A later source-aware consumer must correlate a
registry-matched activity with the exact PR31 Plan/Approval/Task/Receipt chain
before it can classify the source as GLE.  Empty activities mean only “no rows
observed in this bounded GET capture,” never “no mutation.”

First/last equality proves only that the captured reads agreed.  It does not
back-project the current value to the cutoff or prove the absence of a change
between reads.  Retention outside the API capture surface remains unknown.
An exact activity whose `after` conflicts with the last readback, or whose
per-object transition sequence is discontinuous, is retained as evidence but
forces the incomplete claim state.

The raw request and actor registry hashes are caller supplied.  The normalized
capture does not carry a signature or an independently governed transport
ledger.  Consequently every output permanently includes
`LIVE_GRAPH_TRANSPORT_NOT_EXTERNALLY_ATTESTED` and
`ACTOR_REGISTRY_SELECTION_AUTHORITY_NOT_VERIFIED`; a wholesale caller re-sign or
rehash can never yield an observed/verified transport state.

## Artifact

The writer creates a new mode-0700 directory containing exactly six mode-0600,
single-link canonical files:

1. `source-request.json`
2. `graph-capture.json`
3. `activity-observations.json`
4. `current-state-readbacks.json`
5. `coverage.json`
6. `manifest.json`

`graph-capture.json` is the normalized, bounded source layer.  The loader
re-derives observations, readbacks, coverage, roots, and the manifest from it.
The caller must also supply the request raw SHA, actor-registry raw SHA, and the
externally pinned artifact manifest raw SHA.  Rehashing derived files cannot
promote the source capture or its ceilings.

All file/directory operations are fd-relative, no-follow, exact-set, mode and
single-link checked, bounded, identity-stable, new-only, and fsynced.  A final
parent-fsync failure is reported as durability uncertain and never as success.

## Status lattice

- `CALLER_ANCHORED_GET_CAPTURE_CLAIM_REDERIVED`: the normalized caller-anchored
  claim is internally consistent, with no classified external identity or drift.
- `INCOMPLETE_CALLER_ANCHORED_ACTIVITY_OR_STATE_CLAIM`: an unknown/unclassified,
  discontinuous, conflicting, or drifting claim is present.
- `POLLUTED_EXTERNAL_OR_UNGOVERNED_ACTIVITY_CLAIM`: a claimed activity for the
  exact denominator carried a non-registry actor/application pair.

Every valid state exits `2`.  It is a diagnostic evidence fragment, not a Gate
pass.  Invalid contract/integrity input exits `64`, Graph capture failure exits
`66`, existing output exits `73`, and durability uncertainty exits `74`.

## Permanent ceiling

Every request, coverage object, and manifest fixes:

- source authority `NOT_VERIFIED`;
- activity effect `CALLER_ANCHORED_GET_CAPTURE_CLAIM_ONLY`;
- live Graph transport attestation and actor-registry selection authority absent;
- complete event journal `false` and external coverage unknown;
- Objective, Spec, Snapshot, partition, Gate0 and Gate1 effects `NONE`;
- Snapshot not emitted, Replay not executed/ineligible, Golden ineligible;
- HOLDOUT `LOCKED_NOT_ASSIGNED`;
- not a Dataset, Snapshot, Replay, or Gate receipt.

Gate0 therefore remains `QUASI_ONLY`/unchanged.  This package cannot make B2A
VERIFIED, sign B2B, freeze thresholds, run Holdout, or authorize Meta writes.

## CLI

```bash
META_ACCESS_TOKEN='provided-out-of-band' \
python scripts/build_gle_evaluation_meta_activity_evidence.py \
  --execute-read-only \
  --request /path/request.json \
  --expected-request-sha256 <raw-sha256> \
  --actor-registry /path/actor-registry.json \
  --expected-actor-registry-sha256 <raw-sha256> \
  --output-dir /new/evidence-directory
```

The CLI never accepts a token value on the command line.  Without
`--execute-read-only` it is inert.

## Acceptance and exclusions

Focused acceptance covers exact subject/denominator binding, canonical clock and
31-day boundary, actor classes, current-state drift, explicit transition parsing,
pagination/cursor bounds, unknown objects, duplicate events, zero-write proof,
external raw anchors, full-rehash promotion, exact-set/mode/new-only I/O, and CLI
exit semantics.  Stacked G0/G1 contract tests must remain green.

This PR contains only the module, CLI, tests, and this document.  It introduces
no schema, migration, DB read/write, API/worker/scheduler/service change,
production deployment, live Meta execution during tests, Replay, Golden,
Threshold, Holdout, or Gate receipt.
