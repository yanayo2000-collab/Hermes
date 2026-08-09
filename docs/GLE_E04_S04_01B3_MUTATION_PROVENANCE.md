# GLE E04 S04-01B3 — Bounded Mutation-Provenance Evidence v1

## 1. Purpose

This milestone publishes a deterministic, externally anchorable mutation-evidence fragment from a caller-pinned immutable SQLite snapshot. It closes one physical evidence lane required by a future `EvaluationInputSnapshot`; it does not emit that Snapshot.

The artifact can prove only:

- a row was retained in `ad_experiment_events` in the pinned database bytes; or
- a status intent and observed after-state have an exact, internally consistent `REACTIVATE_AD` Plan → Approval → execution Task → per-step receipt → final Verify → final Receipt chain.

It cannot prove that the database is an authoritative production capture, that retained rows cover all history, or that no external Meta mutation occurred.

## 2. Scope and exclusions

The implementation is offline and read-only. It does not use wall-clock time, network, Meta, environment fallbacks, live databases, schema migration, workers, schedulers, APIs, or service operations.

Explicit exclusions:

- no Objective or Spec authority;
- no real `EvaluationInputSnapshot`;
- no DEV/VALIDATION/HOLDOUT assignment;
- no Replay execution or metric;
- no Golden Case or Threshold;
- no Gate receipt or Gate result change;
- no production deployment, DB write, Meta write, or backend restart.

## 3. Input contract

`source-request.json` is canonical JSON with LF termination, externally pinned by its raw SHA-256, and mode `0600`. It binds:

- exact UTC `window_start <= data_cutoff_at <= requested_at`, maximum 31 days;
- exact account, Study, launch, campaign, two Cells, two experiments, two Study cells, two AdSets, and two Ads;
- exact five-field status denominator: one campaign, two AdSets, and two Ads;
- source logical ID and immutable SQLite raw SHA-256.

The SQLite file must be regular, single-link, nonempty, mode `0600`, at most 30 GiB, and have no nonempty WAL, journal, or SHM sidecar. It is held by file descriptor, hashed on that descriptor, opened with `mode=ro&immutable=1`, and forced to `PRAGMA query_only=ON`. The name-to-inode and parent identities are checked after derivation.

The fixed source tables are:

- `growth_operation_action`;
- `growth_operation_approval`;
- `meta_execution_task`;
- `meta_execution_task_receipt`;
- `ad_experiment_events`.

Primary keys and required columns are checked. Before row bodies or JSON are materialized, each query runs a bounded `typeof`/byte-length preflight with row, field, row-byte, per-table byte, and shared 256 MiB cross-table materialization ceilings.

## 4. Evidence semantics

### `GLE_RECEIPT_CHAIN_OBSERVATION`

Only a `REACTIVATE_AD` plan with exact `PAUSED → ACTIVE` intent for all five admitted objects can produce receipt-chain observations. The implementation requires:

- verified experiment-scoped action and exact target binding;
- exactly one approved approval whose plan bytes/hash match;
- approval time order and `operator:` approver;
- exactly one successful live execution task with matching request hash, account, action type, plan, complete approval snapshot, TTL, terminal state, and object denominator;
- exact ordered execution steps, final `VERIFY`, and final `RECEIPT`;
- per-step successful result and exact object-ID/status readback plus exact five-object final readback;
- exact object key/ID binding and timestamps no later than the cutoff.

The Plan values are labeled `APPROVED_INTENT_ONLY`; only the after value is labeled `GET_READBACK_OBSERVED`. Receipt time is stored separately as `receipt_observed_at`; `changed_at` stays null because the actual Meta mutation instant is not observed. Every such output retains `ACTUAL_BEFORE_NOT_OBSERVED`, `MUTATION_TIME_NOT_OBSERVED`, and `EXTERNAL_ACTIVITY_NOT_CORRELATED` gaps. Thus a receipt chain is not described as an actual mutation event. Creation, idempotent already-active steps, and other action types remain gaps. Deployment or controlled-restart receipts are not read and cannot serve as mutation evidence.

### Retained local context

An `ad_experiment_events` row is outside the frozen five-object Meta status denominator. It contributes only a hashed retained-context root and count in coverage; it never enters `mutation-events.json` or the admitted event root. The row does not establish retention completeness, an external mutation denominator, or source authority. An empty table yields `NO_MUTATIONS_OBSERVED_WITH_INCOMPLETE_COVERAGE`, never “no mutations.”

Values are stored only as canonical SHA-256 commitments. Actor text, raw evidence JSON, tokens, PII, and creative payloads are not copied to the artifact.

## 5. Exact artifact

Every generation is a new directory with mode `0700` and exactly five mode-`0600` files:

1. `source-request.json`
2. `mutation-events.json`
3. `coverage.json`
4. `provenance-assessment.json`
5. `manifest.json`

All files are canonical JSON plus LF. The manifest binds each raw file SHA-256 and size plus the request, event, coverage, and assessment semantic hashes. The loader requires an external raw manifest SHA-256, reopens the original SQLite bytes, re-derives every object, and compares exact values. Rehashing a locally modified artifact cannot promote it.

The writer reserves the final directory name with `mkdir`, writes only through fixed directory descriptors using `O_EXCL|O_NOFOLLOW`, fsyncs files/directory/parent, and refuses replacement. A crash may leave an unanchored partial reservation; the exact-set loader rejects it.

## 6. State lattice and permanent ceilings

Valid outputs always return CLI exit `2`:

- `RECONCILED_GLE_RECEIPT_OBSERVATION_SUBSET` — at least one fully closed GLE receipt-chain after-state observation;
- `INCOMPLETE_MUTATION_PROVENANCE` — retained context exists, but no admitted GLE receipt-chain observation;
- `NO_MUTATIONS_OBSERVED_WITH_INCOMPLETE_COVERAGE` — no admitted rows were observed.

Invalid schema, hash, transport, source, time, resource, or artifact input returns `64` and produces no successful artifact.

All valid states mechanically fix:

- `mutation_effect=RECEIPT_OBSERVATION_SUBSET_ONLY`;
- `source_content_authority=NOT_VERIFIED`;
- `complete_event_journal=false`;
- `external_mutation_coverage=UNKNOWN`;
- Objective/Spec/Snapshot/partition effects `NONE`;
- `snapshot_emitted=false`;
- HOLDOUT `LOCKED_NOT_ASSIGNED`;
- Replay not executed and ineligible;
- Golden ineligible;
- Gate 0 effect `NONE`, result `UNCHANGED`;
- Gate 1 effect `NONE`;
- not a Dataset, Snapshot, Replay, or Gate receipt.

## 7. Acceptance boundary

Acceptance requires focused threat tests, the stacked Gate0/Gate1 contract suites, Python compilation, CLI help, and diff checking. A later source-authority/capture contract must independently bind the SQLite raw SHA to a governed production capture. A later consumer must also close external Meta activity coverage, current-state readback, Objective/Spec authority, real lineage/partition, remaining metrics, and Gate0 `CONTROLLED_FEASIBLE` before any real Snapshot or Replay work can proceed.
