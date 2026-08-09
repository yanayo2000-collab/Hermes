# GLE E04-S04-01A Frozen Replay Input Adapter v1

## Outcome

This package freezes and validates the first offline Replay input boundary:

- one canonical `ObjectiveContract`;
- one Copy-only invariant projection;
- one canonical `ExperimentSpec`;
- one canonical `EvaluationInputSnapshot`;
- one explicit synthetic clock;
- one requested `DEV` or `VALIDATION` context.

It produces a content-addressed input envelope and exact manifest. It does not
execute Replay and does not produce an Evaluation, Decision, GoldenCase,
Threshold Contract, Holdout result, or Gate receipt.

## Why this is S04-01A, not Replay-ready

The G1-01 foundation deliberately admits only a synthetic contract fixture:
the Spec is `DRAFT`, evaluator and policy versions are `UNFROZEN`, and no real
source/partition authority is bound. The current production evidence chain is
also still blocked at B2A `MISSING`, so G1-03A has zero real tasks and no
canonical `snapshot_id` suitable for Golden assembly.

Accordingly, every valid v1 envelope has exactly:

- `status=SYNTHETIC_CONTRACT_FIXTURE_ONLY`;
- `trust_status=UNSIGNED_LOCAL_SYNTHETIC_FIXTURE`;
- `input_effect=INPUT_ADAPTER_ONLY`;
- `partition_effect=NONE`;
- `replay_executed=false` and `replay_eligible=false`;
- `golden_eligible=false`;
- `holdout_status=LOCKED_NOT_ASSIGNED`;
- `gate1_effect=NONE`;
- all dataset, Replay, and Gate receipt flags disabled.

There is no reachable `READY`, `PASS`, or real Replay state in this version.
The CLI returns exit `2` after successfully writing a valid synthetic-only
fixture and exit `64` for schema, hash, time, I/O, or integrity failures.

## Cross-object contract

The adapter runs the existing G1-01 validators over all four objects, then
mechanically requires:

- Spec `objective_contract_id` equals the Objective ID;
- invariant experiment and projection hashes bind the Spec;
- invariant `IMAGE_SHA` binds both Copy-only Cells;
- Snapshot Objective/Spec hashes and experiment ID bind the same objects;
- Snapshot Cell metrics contain exactly the two Spec Cells;
- evaluator/policy versions match the Spec evaluation plan;
- attribution/dedup versions match the Objective primary metric;
- Objective approval, Spec creation, Snapshot cutoff/creation, and synthetic
  clock are causally ordered.

The synthetic clock is caller-supplied canonical UTC and participates in both
the input root and envelope hash. The implementation never reads wall-clock
time. Identical inputs and clock therefore produce identical bytes; a changed
clock produces a different root.

`requested_split` accepts only `DEV` or `VALIDATION`. It is a requested test
context, not a signed partition assignment. `HOLDOUT` and all other values are
rejected on every public path.

## Artifact and external anchor

The output is a new-only mode-`0700` directory containing exactly six
mode-`0600` canonical JSON files:

1. `manifest.json`
2. `objective-contract.json`
3. `copy-only-invariant-projection.json`
4. `experiment-spec.json`
5. `evaluation-input-snapshot.json`
6. `replay-input-envelope.json`

The caller must publish and later supply the raw SHA-256 of `manifest.json` as
an external anchor. The manifest binds every payload raw SHA/size, all semantic
self-hashes, the synthetic clock-derived input root, the adapter status, and
all ceilings. A loader does not trust self-consistent output labels: it reopens
all six files, reruns every canonical and cross-object validator, rederives the
entire envelope and manifest, and requires exact equality.

Input and artifact reads are bounded and regular-file-only. Canonical JSON
rejects duplicate keys, NaN/Infinity, non-UTF-8, and noncanonical bytes. The
artifact reader rejects extra/missing files, symlinks, FIFO/device/socket
entries, hard links, wrong modes, oversized data, and file/directory identity
drift. The writer reserves the final name without replacement, writes through
fixed directory file descriptors with `O_EXCL|O_NOFOLLOW`, fsyncs files and
directories, and never reports a partial directory as valid.

## Explicit safety boundary

This module imports no SQLite, Meta, HTTP/network, scheduler, service, runtime
clock, evaluator, or decision implementation. It reads only the four explicit
canonical input files and writes only the requested new artifact directory.

This PR explicitly excludes:

- real production Snapshot collection or DB adapters;
- B2A/B2B authority or partition publication;
- G1-03B GoldenCase assembly or S02-04 Threshold values/signatures;
- Replay batch execution, per-case receipts, diffs, or metrics;
- Evaluation Engine 2.0 or Decision Policy;
- Holdout assignment, disclosure, read, or execution;
- API, worker, scheduler, schema/migration, Meta, deployment, or Gate effects.

## Next dependency

A later version may become Replay-ready only after independently frozen real
Objective/Spec/method/policy/attribution/dedup contracts, a source-aware real
Snapshot adapter, and signed DEV/VALIDATION authority exist. GoldenCase
assembly additionally requires resolved signed G1-03A labels that bind those
real Snapshot IDs. Threshold signing follows an actual Golden distribution;
one-shot Holdout remains a later, separately authorized gate.
