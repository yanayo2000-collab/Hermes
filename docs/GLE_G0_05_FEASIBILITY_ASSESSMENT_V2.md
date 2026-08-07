# GLE G0-05 feasibility assessment v2

## Version boundary

V2 is a strict, incompatible successor to the immutable G0-05 v1 contract:

- input: `gle-g0-05-assessment-input-v2`
- engine: `gle-g0-05-feasibility-engine-v2`
- policy: `gle-g0-05-mx-policy-v2`
- candidate: `gle-g0-05-gate0-candidate-v2`
- estimator: `gle-two-sample-poisson-log-rate-ratio-fixed-endpoint-v1`

V1 inputs are rejected rather than silently reinterpreted. All v1 artifact,
subject, transport, snapshot, qualification, allocation, capability, audience,
governance, publication, and zero-write rules otherwise remain fail-closed.
The qualified source dimensions are frozen as `country=Mexico`,
`media_source=Meta`, and `external_app=TUGAO`; baseline completeness requires
every expected Meta tuple/day to have that exact observed Tugao source grain.

## Power semantics

V2 adds the content-addressed fixed-endpoint calculation specified by
`GLE_G0_FIXED_ENDPOINT_POWER_V1.md`. The frozen estimand is qualified joins per
US dollar and the offset is spend. When the 14-day baseline, source freshness,
coverage, events, and spend are complete, the candidate reports target
information plus projected days and spend. Incomplete historical evidence,
zero events, or zero spend remains `UNKNOWN`.

This diagnostic is not a group-sequential decision rule. The policy continues
to freeze `golden_vectors_approved=false`; O'Brien-Fleming information looks,
alpha spending, efficacy boundaries, and futility are explicitly `UNFROZEN`.
Consequently a fixed-endpoint `PASS` leaves the G0-05 Power check `UNKNOWN` and
the candidate `QUASI_ONLY`. A complete fixed-endpoint projection that exceeds
14 days or 20 USD is a necessary-condition `FAIL` and yields `NOT_FEASIBLE`;
missing, zero, or incomplete evidence stays `UNKNOWN`. Every candidate remains
unsigned and `gate0_result_ceiling` remains `QUASI_ONLY`.

The caller cannot provide Power outputs, approval state, feasibility, or a Gate
result. Code-owned vectors and hashes establish deterministic computation only;
they do not substitute for candidate-bound SOLE_OWNER attestation or Gate 1's
sequential-design contract.

## Operational boundary

The v2 engine is pure and offline. It adds no schema, migration, table, network
call, Meta operation, scheduler, service, or production action. Publication is
still exclusive-create canonical JSON plus a committed manifest. Rollback is a
code-package revert; there is no database or Meta rollback.
