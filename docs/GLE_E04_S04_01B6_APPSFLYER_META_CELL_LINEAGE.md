# E04-S04-01B6 AppsFlyer + Meta exact Cell lineage

## Outcome

This contract converts one externally SHA-pinned AppsFlyer aggregate CSV with
stable `Ad ID` into an exact two-Cell lineage fragment by re-reading Meta Graph
v25.0:

`Study -> Cell /adsets -> Ad Set -> Ad -> Campaign -> Account`.

It solves the physical join problem for AppsFlyer installs. It does not verify
source-content authority and does not produce a canonical
`EvaluationInputSnapshot`, partition, Replay, Golden, Holdout, or Gate receipt.

## Input contract

The request is canonical JSON and freezes:

- app, Account, market, Study, Campaign;
- exactly C1 and C2, with distinct Cell, Ad Set, and Ad IDs;
- raw AppsFlyer CSV SHA-256;
- report start/end date, reporting IANA timezone, and cutoff;
- `HISTORICAL_TEST` or `NATURAL_AUDIT_CANDIDATE` mode.

The CSV is bounded to 32 MiB and 100,000 rows. It must contain the frozen
AppsFlyer fields and exactly one `Facebook Ads` aggregate row for each target
Ad. A missing or duplicate target row fails closed. Missing metrics remain
missing; no fallback or zero imputation is allowed.

`NATURAL_AUDIT_CANDIDATE` requires exact `Asia/Shanghai`. Historical fixtures
may retain `Asia/Hong_Kong`, but receive a permanent historical-window gap and
cannot be admitted by a later natural-window consumer.

## Meta GET-only contract

The CLI only calls these Graph v25.0 GET surfaces:

- `/{study_id}`;
- `/{study_id}/cells`;
- `/{cell_id}/adsets` for C1 and C2;
- `/{ad_id}` for C1 and C2.

The access token remains process-memory only and is sent in the HTTPS
`Authorization` header, never in the query string. Redirects and environment
proxy inheritance are disabled by default. Environments that require an
outbound proxy must opt in with `--allow-env-proxy`; that choice is reported by
the CLI and never upgrades the fixed `NOT_PROVIDED` transport-attestation
ceiling. No POST, PUT, PATCH, DELETE, SQLite, Meta object mutation, or
AppsFlyer mutation is present.

Validation requires:

- Study type `SPLIT_TEST` and exact Study ID;
- exactly two expected Cells, each 50% treatment and exactly one ad entity;
- Cell `/adsets` exact match to the frozen Ad Set and Campaign;
- each Ad exact match to frozen Account, Campaign, and Ad Set;
- both stable Ad IDs present exactly once in the SHA-pinned CSV.

## Output and trust ceiling

The output is a new-only mode-0600 canonical JSON file. Its self-hash binds the
request hash, AppsFlyer raw SHA/size, normalized Meta capture hash, exact rows,
gaps, and ceiling.

Permanent ceiling:

- `lineage_effect=REDERIVED_EXACT_CELL_LINEAGE_ONLY`;
- `source_content_authority=NOT_VERIFIED`;
- AppsFlyer and live Graph transport attestations are not provided;
- Objective, Spec, Snapshot, partition, Gate0, and Gate1 effects are `NONE`;
- Gate0 result is `UNCHANGED`;
- Snapshot is not emitted;
- Replay and Golden are false/ineligible;
- Holdout is `LOCKED_NOT_ASSIGNED`;
- the artifact is not a Dataset, Snapshot, Replay, or Gate receipt.

Successful CLI completion exits `2`; invalid input or uncertain I/O exits
`64`. The token is never printed.

## Historical test evidence

The 2026-08-10 test used the AppsFlyer export with SHA-256
`f25f09cb6dbd75de5b40eddf2fa38289873800a49bf7448441cf50e1125ad812`
and re-derived:

- C1 Cell `1657983691931915` -> Ad Set `120250588945530544` ->
  Ad `120250588945870544` -> 2 AppsFlyer installs;
- C2 Cell `1587562426321061` -> Ad Set `120250588946480544` ->
  Ad `120250588946840544` -> 9 AppsFlyer installs.

Both paths bind to Campaign `120250588944820544`, Account
`1012060198097836`, and Study `1755195762483275`. Because the export window is
historical and labelled `Asia/Hong_Kong`, this verifies implementation and
physical lineage only.

The natural-window jobs must re-run the full capture after the frozen
2026-08-11 and 2026-08-13 settlement checkpoints. They must not borrow this
historical artifact.
