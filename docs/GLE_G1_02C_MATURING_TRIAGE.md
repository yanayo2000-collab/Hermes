# G1-02C MATURING Audit Triage v1

This milestone classifies the `MATURING` denominator already captured by a fully validated G1-02A v2 artifact. It is an offline audit aid, not a historical as-of reconstruction: `ad_experiment.state` is mutable current context among cutoff-eligible experiments, and the output preserves `not_asof=true`.

## Evidence rule

The frozen reason order is `EXTERNAL_MUTATION`, `DATA_SOURCE_MISSING`, `STATE_MACHINE_STUCK`, `SCHEDULER_MISSED`, `ATTRIBUTION_PENDING`, `NO_DELIVERY`, `SPEND_TOO_LOW`, `EVENTS_TOO_LOW`, `TIME_NOT_REACHED`, then `UNKNOWN`.

This version never promotes a reason above `UNKNOWN`. A retained cutoff-eligible `ad_experiment_events.event_type` that exactly equals a reason is preserved only as `OBSERVED_UNVERIFIED_EVENT_LABEL`; the captured source has no frozen producer, actor, purpose, or typed evidence contract that could make the label authoritative. Multiple labels are preserved in full and add `CONFLICTING_REASON_EVENTS`; they are never silently collapsed by priority.

Hashes and payload commitments are never interpreted as values. The tool does not infer delivery, spend, events, attribution, elapsed-time, scheduler health, or mutation from metrics hashes, names, timestamps, or current state. Every item remains `UNKNOWN_INSUFFICIENT_EVIDENCE` and requires manual review because current `MATURING` is not an as-of state and retained events have incomplete retention provenance.

The threshold contract remains `UNFROZEN`; therefore this artifact does not complete the S02-02 business threshold acceptance on its own.

## Artifact and trust boundary

The output directory name is reserved atomically with `mkdir` and is never replaced. Successful output contains exactly:

- `manifest.json`
- `triage.ndjson`
- `manual-review.ndjson`
- `coverage.json`

Every public derive, validate, write, and load path reopens the source G1-02A directory using its externally supplied raw manifest SHA and re-derives the full result. A self-consistent rehash cannot change a reason, denominator, evidence reference, review decision, coverage value, status, or ceiling.

The writer holds fixed parent and output directory file descriptors, writes only exclusive leaf files, fsyncs every file and directory, and verifies the published directory inode. A process kill can leave a reserved partial directory, but that directory has no externally pinned manifest SHA and the exact-set loader rejects it. A durability error after all four files are complete preserves the complete, independently reloadable directory and returns an explicit error instead of deleting published evidence.

The immutable ceilings are:

- trust: `UNSIGNED_OFFLINE_DERIVATION`
- evidence use: `AUDIT_ONLY`
- split assignments: empty
- Holdout: `LOCKED_NOT_ASSIGNED`
- Replay and Golden eligibility: false
- Gate 1 effect: `NONE`
- not a dataset, Replay, or Gate receipt

CLI exit `2` means the artifact was successfully written and manual review remains. Empty denominators also remain incomplete. Exit `64` means invalid input or artifact construction failure. Version 1 has no successful-classification exit `0`; no exit code means Gate PASS.

## Exclusions

This PR does not read SQLite directly, alter schema or data, call Meta or network services, assign DEV/VALIDATION/Holdout, create Golden labels, freeze thresholds, execute Replay, implement Evaluation/Decision/Compiler, publish a Gate receipt, or connect to any API, worker, scheduler, or service.
