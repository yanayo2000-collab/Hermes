# GLE G0-05 feasibility assessment v3

## Version boundary

V3 is a strict, incompatible successor to the immutable G0-05 v2 contract:

- input: `gle-g0-05-assessment-input-v3`
- engine: `gle-g0-05-feasibility-engine-v3`
- policy: `gle-g0-05-mx-policy-v3`
- candidate: `gle-g0-05-gate0-candidate-v3`
- estimator: `gle-two-sample-poisson-log-rate-ratio-fixed-endpoint-v1`

V1 and V2 inputs and policies are rejected rather than reinterpreted. All
existing subject, transport, topology, audience, allocation, power,
governance, publication, and zero-write rules remain fail-closed.

## Physical qualified-source dimensions

V2 incorrectly used the normalized platform label `Meta` and producer/service
label `TUGAO` as exact source dimensions. The TimeTrade
`/api/v1/analytics/funnel-daily-metrics` rows persisted for the frozen MX
subject use the physical values:

- `country=Mexico`
- `media_source=Facebook Ads`
- `external_app=Linky`

V3 freezes those exact values. The producer name remains
`data_source=TugaoFunnel`; it is not an `external_app`. The normalized platform
remains `platform=Meta`; it is not the source `media_source`. Aliases and case
folding are deliberately rejected. Baseline and natural-evidence completeness
still require every expected Meta tuple/day to have the exact observed Tugao
source grain. A missing Cell/day remains missing even when the other Cell has a
valid observed zero.

## Gate and operational boundary

This correction does not make the existing externally activated,
multi-variable Study eligible and does not relax settlement, minimum-day,
audience, O'Brien-Fleming, golden-vector, or attestation blockers. A fixed
endpoint PASS still cannot promote the Gate; a trusted fixed-endpoint failure
may only reject feasibility. Every candidate remains unsigned with
`gate0_result_ceiling=QUASI_ONLY` unless all independent Gate requirements are
later satisfied.

The engine and collector remain offline and read-only. This package adds no
schema, migration, table, network call, Meta operation, scheduler, service, or
production action.
