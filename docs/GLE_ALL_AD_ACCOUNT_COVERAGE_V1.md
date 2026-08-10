# GLE all-ad account coverage v1

## Scope

This read-only projection covers every Meta Ad returned for these five operator-selected accounts:

- `1012060198097836` / `自投-MX-TM`
- `1053439070674646` / `TUGAO自投-MX-TM`
- `1457588552349197` / `自投-BR-TM`
- `2282907019174017` / `测试户`
- `1250000910496826` / `自投-ID-TM`

`GET /api/ops/ad-data-dashboard/gle-ad-coverage` obtains the bounded live Ad roster with Meta GET calls and joins it to the latest 31 complete dashboard-fact days and exact `ad_experiment` object bindings. The token stays in the Authorization header and is never returned.

## Coverage modes

- `SINGLE_AD_OBSERVATION`: the Ad is covered by read-only operating observation. It is not represented as a causal experiment.
- `MULTI_CELL_EXPERIMENT`: the Ad belongs to an exact 2–4 member Meta object group with one baseline and unique challenger objects. This is a physical registry binding only, not current natural-window authority.

Every returned Ad is `COVERED_READ_ONLY`, including paused Ads and Ads still waiting for dashboard facts. Missing facts remain missing; they are not replaced with zero.

## Permanent safety boundary

- Gate0 remains `QUASI_ONLY / UNCHANGED`.
- Gate1 remains `NOT_READY`.
- Current natural-window exact Cell lineage remains `MISSING_EXACT_CELL_LINEAGE` until the separately scheduled evidence windows close.
- `causal_claim=false` and `meta_write_allowed_by_gate=false` for every Ad.
- The endpoint performs no Meta write, database write, Snapshot, partition, Replay, Golden, Holdout, or Gate receipt action.

The dashboard shows roster coverage, effective-active coverage, fact availability, and the difference between single-Ad observation and multi-Cell grouping. It must not label the result as a winner, causal effect, full source authority, or permission to mutate an Ad.
