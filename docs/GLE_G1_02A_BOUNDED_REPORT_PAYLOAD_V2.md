# GLE G1-02A bounded report payload contract v2

## Outcome

G1-02A v2 preserves the seven-table, single-transaction, read-only audit contract while preventing
large `ad_daily_report.payload_json` values from being retained in the Python rowset. The v1
production capture failed closed because 91 report payloads totalled about 247 MB, despite every
other source table being well below the 64 MiB projected-evidence bound.

## Frozen extraction rule

- `payload_json` remains a source field, but it is not retained in the materialized rowset or exported.
- Before hashing, an ordered lazy SQLite cursor returns only storage class and byte length. Python
  stops at the first violation while accumulating count, maximum, and total; it never consumes more
  than 50,001 rows or more than one row beyond the 512 MiB boundary. The frozen limits are 50,000
  rows, 8 MiB per row, and 512 MiB total source payload bytes.
- The ordered bound requires `report_id` to be the table's exact single-column primary key; all seven
  source descriptors now fail closed when their declared primary key does not match SQLite schema.
- Only after that preflight passes, a deterministic Python SQLite UDF receives one TEXT value at a
  time to compute SHA-256. This is bounded transient in-process access, not a claim that Python never
  sees the plaintext. The materialized row contains only storage class, `payload_sha256`, and
  `payload_size_bytes`.
- A report above 8 MiB, a NULL/BLOB/non-TEXT payload, a missing commitment, or an invalid commitment
  fails closed.
- The 64 MiB aggregate bound continues to cover the canonical materialized rowset. It is not raised
  to accommodate private report payloads.
- The table manifest distinguishes exact `source_columns` from `materialized_columns`, records the
  observed source byte totals and frozen limits, and names the row-commitment algorithm.
- `source_row_hash` commits to the safe report metadata, payload SHA-256, byte length, and storage class.
  Raw report payload text is neither exported nor retained in the in-memory audit bundle.

The request, bundle, manifest, generator, and query-contract versions are v2. G1-02B candidate and
manifest versions are also v2 because their accepted input root changed; G1-02B2 consumes that exact
v2 candidate manifest. Authority semantics, split, Replay, Golden, Holdout, and Gate effects remain
unchanged.

## Safety and acceptance

The database still opens with `mode=ro`, `query_only=ON`, the write-denying authorizer, and one
explicit read transaction. There is no schema, DML, API, worker, scheduler, Meta, or service change.
Acceptance requires the existing G1-02A/B1/B2A suites plus regressions proving aggregate raw payload
bytes may exceed the projected bound without disclosure, while per-row or total source payload bounds
fail before the Python hash callback runs.
