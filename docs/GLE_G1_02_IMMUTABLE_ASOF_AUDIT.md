# GLE G1-02A Immutable As-of Audit v1

## Outcome

G1-02A creates a deterministic, content-addressed audit of seven available Growth history
surfaces. It freezes one SQLite read-transaction view at one explicit cutoff, keeps every captured
legacy evaluation in the denominator, and emits named gaps instead of silently reconstructing
missing facts.

The first production-shaped result is expected to be `INCOMPLETE / AUDIT_ONLY`. Existing legacy
rows do not contain canonical EvaluationInputSnapshot, lineage, current qualified-join Objective,
or complete mutation provenance. This package cannot emit Replay eligibility, Golden labels,
DEV/VALIDATION assignments, Holdout results, or Gate 1 PASS.

## Fixed source contract

The audit scope is `AVAILABLE_CURRENT_AND_ARCHIVED_GLE_LEGACY` and cannot be narrowed by caller
input. It is not a claim that retention-deleted history is complete. The fixed tables are:

- `ad_experiment_evaluation`
- `ad_creative_group_evaluation`
- `ad_audience_pair_evaluation`
- `ad_experiment_events`
- `ad_daily_report`
- `ad_creative_group_evaluation_history`
- `ad_experiment`

All rows are read with `LIMIT max+1`, then classified by parsed UTC instants in Python; SQLite TEXT
ordering is never used as a time comparison. Invalid or missing timestamps remain in the captured
denominator with an explicit gap, and `captured + post_cutoff = physical` for every table.
Current `ad_experiment` rows are retained for audit context but always labeled
`CURRENT_ONLY_NOT_ASOF`; their mutable state is never presented as historical state at the cutoff.
Post-cutoff timed rows are counted in table manifests but never enter record artifacts.

## Read-only and privacy boundary

- SQLite opens with `mode=ro`, `query_only=ON`, and an authorizer that rejects DML, DDL,
  temporary schema writes, ATTACH, and DETACH.
- Schema and all seven rowsets are read in one explicit transaction with fixed columns, fixed tables,
  primary-key ordering, and bounded row/byte limits. No `ensure_*schema` helper is imported.
- The authority hash is the canonical schema/query/cutoff/table row-chain projection. A live DB file
  hash is not used as a transaction snapshot under WAL. Capture telemetry (file stat, data version,
  and post-cutoff counts) stays outside `authoritative_asof_hash`, so later facts do not redefine the
  already captured at-or-before-cutoff rowset.
- Account, launch, and Meta object IDs in experiment context are deterministic linkable pseudonyms.
  Raw legacy metrics/evidence, report payloads, hypothesis/control JSON, actors, and private account
  IDs are never exported; only hashes, field-presence metadata, or safe scalar projections leave the
  read transaction.
- NULL/missing legacy JSON remains `MISSING`, distinct from a present empty object and explicit zero.

## Artifact set

The CLI creates a new directory and refuses overwrite:

- `manifest.json`
- `records.ndjson`
- `gaps.ndjson`
- `coverage.json`

Records, gaps, table manifests, the source snapshot, the bundle, output files, and the manifest are
content-addressed and cross-bound: request/source identity, table descriptors, row/projection chains,
source table chain, exact gap closure, and coverage are recomputed. The output is explicitly
`UNSIGNED_LOCAL_CAPTURE` and `not_replay_receipt=true`: hashes prove deterministic integrity, not an
external trust root or production authenticity.

## Mandatory gaps and status

Legacy calendar checkpoints remain observational and nonbinding. Each legacy evaluation carries
the G1-01 projection plus gaps including missing canonical snapshot, unresolved lineage, incompatible
Objective, and absent mutation provenance. Single-experiment rows additionally record missing episode
or exact event references where applicable. Mutable current experiment state records always carry
`MUTABLE_CURRENT_STATE_NO_PREIMAGE`.

G1-02A intentionally fixes the output status at `INCOMPLETE` and replay eligibility at `AUDIT_ONLY`.
G1-02B may resolve exact lineage edges from this frozen source; Golden/threshold/Replay/Holdout are
separate later milestones.

Coverage reports each evaluation table separately. It also reports mutable experiment state among
cutoff-eligible experiments as `cutoff_eligible_experiment_current_context.not_asof=true`, including
MATURING rows with and without a captured single-experiment evaluation; this context is never
presented as a complete current denominator or as historical state.

Successful CLI capture returns exit `0`; contract, source, bound, or output failures return nonzero.
Business eligibility must be read from the manifest and never inferred from process exit alone.

## Verification and rollback

Acceptance requires focused adversarial tests, G1-01/Gate0 regression tests, Python compilation,
CLI help, and `git diff --check`. Rollback removes these four additive files and any local audit output
directory. There is no schema, database, API, worker, scheduler, Meta, production, or service state to
restore.
