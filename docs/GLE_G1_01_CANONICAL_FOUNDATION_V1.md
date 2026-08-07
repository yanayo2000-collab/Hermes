# GLE G1-01 Canonical Evaluation Foundation v1

## Outcome

This package defines the complete logical v1.1 ObjectiveContract, ExperimentSpec,
EvaluationInputSnapshot, and EvaluationRecord shapes as canonical, content-addressed domain objects.
It also projects the three existing legacy
evaluation tables into a common observational envelope without changing their rows or meanings.

G1-01 is offline preparation. It is not Gate 1 PASS, a replay run, a Golden/Holdout result, a decision,
or authorization for Meta execution. Gate 0 remains a hard upstream prerequisite.

## Logical to physical mapping

| Logical entity | Physical v1 representation |
|---|---|
| ObjectiveContract | Full §3.1 goal, metric, guardrail, risk, approval, version and self-hash contract |
| Copy-only invariant projection | Separate content-addressed hashes for the shared non-primary-text fields; it is not a Cell's full config hash |
| ExperimentSpec | Full §3.2/3.3 lineage, Copy-only invariant projection reference, assignment, Cell, power/evaluation plan and self-hash contract |
| EvaluationInputSnapshot | Full §3.7 cutoff, version, per-Cell metrics, quality, mutation and self-hash contract |
| EvaluationRecord | Full §3.8 effect, safety, quality, contamination, maturity and result shape |
| Legacy evaluation | Read-only projection of `ad_experiment_evaluation`, `ad_creative_group_evaluation`, or `ad_audience_pair_evaluation` |

Existing evaluation tables remain the historical single truth. G1-01 adds no table, migration,
backfill, alternate history, API, scheduler, database loader, or production reader. A later as-of
adapter must establish an immutable source snapshot/cutoff before calling the pure projection.

## Frozen safety semantics

- Canonical objects reject unknown keys, non-finite numbers, ambiguous timestamps, hash/version drift,
  duplicate Cell identities, and non-50/50 allocation.
- Each Cell keeps a distinct full `config_hash`, because primary text is the sole intended difference.
  The shared `invariant_config_hash` instead binds a separate field-level invariant projection whose
  `IMAGE_SHA` is cross-checked against both Cells. Later compiler evidence owns the remaining field hashes.
- Snapshot `created_at` remains the §3.7 immutable creation timestamp. A later Replay invocation will
  carry its own synthetic clock; this foundation does not reinterpret the Snapshot field.
- D1/HARD_STOP cannot emit effect winners, D3 cannot emit binding closure, and incomplete, polluted,
  immature, observational evidence cannot satisfy a binding result state.
- Objective budget/deadline remain hard ceilings for the ExperimentSpec power plan.
- The v1 foundation admission ceiling is deliberately `DRAFT`: method and policy remain `UNFROZEN`,
  `approved_at` remains null, and INFORMATION_LOOK/FINAL cannot carry a binding effect result. A later
  version may lift this only with the separately frozen method, policy, and trust contracts.
- Cross-object bundle validation is restricted to `SYNTHETIC_CONTRACT_FIXTURE`. It proves schema,
  hashes, state-lattice, time causality, and binding behavior; it is not a real D1/D3 evaluation.
  Foundation bundles admit only `OBSERVATIONAL` evidence. Gate0-derived `CONTROLLED` or `REPLICATED`
  evidence requires a later bundle version with exact receipt provenance.
- Because no quality-threshold contract is frozen here, `data_status=COMPLETE` is conservative: fresh,
  no missing source, attribution coverage exactly 1, and duplicate rate exactly 0.
- Legacy D1/D3/D7 are labeled `LEGACY_CALENDAR_CHECKPOINT`; they are not information looks.
- Missing legacy JSON/text is preserved as an explicit `MISSING` field and reason, never rewritten as an empty value.
- Objective/Spec/Snapshot self-hashes and cross-object Bundle hashes are recomputed, not trusted as labels.
- Legacy winner/effective labels remain observational and `binding_eligible=false`.
- A future lineage may be assigned to exactly one of DEV, VALIDATION, or HOLDOUT. This package defines
  the vocabulary but does not split or execute data.

## Deferred work

Historical as-of export and gap ledger, lineage resolution, blind DEV/VALIDATION replay, reviewer A/B/C
Golden labels, threshold signatures, the OBF method and mathematical vectors, one-shot Holdout locking,
Decision Policy, Compiler, database persistence, and Gate 1 receipt are separate dependent milestones.
No current historical record may be silently relabeled as the frozen qualified-join objective.

## Verification and rollback

Acceptance requires focused contract/projection tests, Gate 0 regression tests, Python compilation, and
`git diff --check`. Rollback is removal of these five additive files; there is no database or runtime
state to restore. This package performs no network, Meta, production database, or service action.
