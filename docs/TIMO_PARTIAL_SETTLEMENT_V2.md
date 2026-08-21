# Timo scoped partial-settlement contract v2

## Business semantics

- `businessDate` is the Beijing business date (`beijing_business_date_v1`).
- A source failure in one guild must not make another guild's verified close unusable.
- A missing guild is represented by `consumable=false` and null fact fields. Missing is never encoded as zero rows or zero income.
- The day remains `PARTIAL`, `ready=false`, and `consumable=false` until every expected guild is complete.
- Downstream systems consume MCN scope facts only. They must not recreate Timo facts from another source.

## Event

Endpoint: `POST /api/internal/timo/materialization-complete` using the existing Timo HMAC headers.

Schema version 2 adds:

- day: `businessDate`, `dateContract`, `dayStatus`, `ready`, `consumable`, `expectedScopeCount`, `scopeTotal`, `scopeSucceeded`, `scopeFailed`, `failedScopes`, `sourceGeneration`, `materializedAt`;
- scope: `guildId`, `guildName`, `guildStorageName`, `country`, `qualityStatus`, `consumable`, `rowCount`, `totalIncome`, `checksum`, `revision`, `sourceGeneration`, `materializedAt`, and optional `failureReason`.

`qualityStatus` is `COMPLETE`, `SOURCE_MISSING`, or `ANOMALY`. Only `COMPLETE + consumable=true` carries facts.

The global `checksum` is SHA-256 of UTF-8 canonical JSON for the final `scopes` array, using sorted object keys and separators `,` and `:`. Scope order is the MCN frozen Timo guild identity order. `eventId` is `timo:{businessDate}:{runId}:{checksum-prefix-16}`.

## Consumer rules

- Atomic key: `(businessDate, guildId, revision)`; exact duplicate checksum is idempotent.
- Lower revision is rejected. Equal revision with a different checksum is a conflict and must not overwrite facts.
- A non-consumable scope updates only scope/day quality metadata. It never deletes or replaces facts and never writes zero facts.
- A consumable scope is independently validated and exact-replaced inside its own transaction.
- Day aggregates, reconciliation and dependent projections are published only after all expected scopes are complete.
- A later complete scope revises only that scope, then upgrades the day from `PARTIAL` to `COMPLETE` and rebuilds dependent projections.

## Recovery

The regular revision run publishes partial scope state. Source-readiness failures keep a persistent retry waterline. The incremental retry worker writes an atomic status artifact, and its systemd post-step invokes the same notifier after natural recovery. No manual event send or downstream recomputation is part of the contract.
