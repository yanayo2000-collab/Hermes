# E04-S04-01B2 Same-cutoff Cell Metric Evidence v1

## Purpose

This offline contract rederives the mechanically supportable Cell metric subset from one externally SHA-256-pinned SQLite file. It reuses the exact collector used by G0-05 so Gate0 feasibility and this diagnostic artifact cannot drift into separate query semantics.

The artifact proves deterministic derivation from pinned bytes only. The caller-provided database anchor and transport receipt do not prove that those database bytes came from production, identify a governed capture time, or bind the bytes to a backend Invocation. `SOURCE_CONTENT_AUTHORITY_NOT_VERIFIED` therefore remains permanent in v1. The artifact is not an `EvaluationInputSnapshot`, Dataset, Replay, Golden, Holdout, or Gate receipt.

## Source boundary

The builder requires all of the following external anchors:

- canonical G0-05 run request raw SHA-256;
- immutable SQLite snapshot raw SHA-256;
- governed G0-02B transport manifest raw SHA-256;
- matching controlled-restart receipt raw SHA-256.

The request must be canonical JSON with mode `0600`, a single link, and an externally pinned raw SHA-256. The source, transport manifest, and transport receipt are opened through stable no-follow file descriptors; hashing, parsing, and SQLite queries use those same file identities. Name replacement cannot redirect the query to different bytes. The collector opens SQLite through the pinned fd with `mode=ro&immutable=1`, enables `PRAGMA query_only=ON`, validates the exact database list, tables, columns, and `row_id` primary key, and rejects nonempty `-wal`, `-journal`, or `-shm` sidecars.

The evidence path additionally limits the combined window to 31 days and rows to 200,000. Before any fact value or JSON payload is returned to Python, a type/length-only preflight limits every variable field to 8 MiB, each materialized row to 16 MiB, all materialized fact bytes to 640 MiB, each JSON payload to 8 MiB, and aggregate payload bytes to 512 MiB. The strict path also limits each sync source to 31 unique daily rows with 1 KiB fields, and requires exactly two unique experiment rows while limiting each field to 1 MiB and their combined bytes to 2 MiB. All three table preflights finish before any row body or control JSON is materialized or parsed. The shared G0-05 call path keeps its prior collector behavior; these limits are enabled only by this evidence builder.

Exact source identity remains account + market + Study/Cell + Campaign/AdSet/Ad. Qualified joins require the TugaoFunnel `guild_join_success_users` field, `tugao_funnel_daily_metrics_api_v1`, exact Meta attribution, the frozen qualification version, and exact country/media-source/external-app dimensions. Legacy `guild_joins` and rows without the observation marker are excluded.

## Artifact

The new-only artifact directory contains exactly four canonical files:

- `source-run-request.json`
- `cell-metric-evidence.json`
- `coverage.json`
- `manifest.json`

The directory is mode `0700`; files are mode `0600`, regular, single-link, bounded, no-follow, fsynced, and protected by name-to-fd identity checks. The loader requires an externally recorded raw `manifest.json` SHA-256, reopens the pinned SQLite and transport evidence, reruns the collector, and requires exact derived equality.

## Admitted fields

When the reporting window is settled, both Cell/day grains are complete, exact qualified attribution equals the eligible denominator, every admitted row has a cutoff-bounded timestamp, the physical Study/Cell binding matches exactly, and the impression denominator is positive, the artifact may report `REDERIVED_METRIC_SUBSET_FROM_PINNED_BYTES` for:

- Cell spend in USD;
- impressions;
- qualified joins;
- actual impression allocation share;
- source freshness;
- attribution coverage when its denominator is nonzero.

The following remain explicit gaps in v1 and must never be inferred or filled with zero:

- clicks;
- installs;
- invalid users;
- canonical duplicate rate.

These fields appear under `rederived_fields`; `verified_fields` is always empty because source authority and capture provenance are not established. Missing Cell/day grains, unsettled windows, zero denominators, or incomplete qualified attribution produce `INCOMPLETE_METRIC_SUBSET` and explicit reason codes.

## Permanent ceilings

All states fix:

- Objective and Spec authority effects to `NONE`;
- source content authority to `NOT_VERIFIED` and source provenance effect to `NONE`;
- `snapshot_emitted=false` and Snapshot effect `NONE`;
- partition effect `NONE` and HOLDOUT `LOCKED_NOT_ASSIGNED`;
- Replay not executed/ineligible;
- Golden ineligible;
- Gate0 effect `NONE`, result unchanged;
- Gate1 effect `NONE`;
- Dataset/Snapshot/Replay/Gate receipt flags false.

CLI success returns exit `2`, not `0` or PASS. Invalid schema, anchor, transport, source, artifact, or I/O input returns exit `64`.

## Explicit exclusions

This milestone does not read live production, write SQLite, access Meta/network/current time, promote Objective or Spec authority, validate the mutation journal, emit a real Snapshot, assign DEV/VALIDATION/HOLDOUT, execute Replay, assemble Golden cases, freeze thresholds, or produce a Gate receipt.
