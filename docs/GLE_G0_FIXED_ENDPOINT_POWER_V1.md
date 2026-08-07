# GLE Gate 0 fixed-endpoint Power diagnostic v1

## Scope

This package adds a deterministic Wald normal-approximation calculation for
the planned final information of a two-arm Poisson log-rate-ratio test. It performs no
network, Meta, SQLite, scheduler, governance, or production operation.

The frozen Gate 0 estimand is qualified joins per US dollar. The offset is
spend, allocation is 50/50, the alternative is two-sided, alpha is 0.05,
desired power is 0.80, and the relative MDE is 30% (`rate_ratio=1.30`).

## Fixed-endpoint contract

For expected qualified-event counts `E0` and `E1`:

```
theta = log(1.30)
I = E0 * E1 / (E0 + E1)
I_target = ((z_0.975 + z_0.80) / theta)^2
```

The frozen target is `114.024535562432...`. Under the 1:1 exposure contract
and rate ratio 1.30, 463 total events are below the target and 464 total events
are above it. The 45-event vector has information `11.058601134216` and power
approximately `0.140720868010`.

A complete control-baseline projection of 45 events per 20 USD implies, under
the 50/50 RR=1.30 alternative, about 125.52 days and 179.32 USD to reach the
fixed endpoint. A complete historical baseline is valid for this diagnostic,
but it never counts as post-deployment natural canary evidence. If its
spend/window/coverage or tuple/day observation set is incomplete, the result is
`UNKNOWN`, not `NOT_FEASIBLE`.

## Explicit non-goals

This is not an O'Brien-Fleming or other group-sequential estimator. Look count,
information fractions, alpha spending, efficacy boundaries, and futility rules
remain `UNFROZEN`. A fixed-endpoint `PASS` therefore leaves the G0-05 Power
check `UNKNOWN` and the candidate at `QUASI_ONLY`. A complete, trusted
fixed-endpoint `FAIL` is different: the fixed endpoint is a necessary
feasibility condition, so exceeding the frozen time or budget is a Power
`FAIL` and makes the exact subject `NOT_FEASIBLE`. Incomplete evidence, zero
events, or zero spend remains `UNKNOWN`.

Neither outcome is a Gate receipt or Meta-write authorization.

Golden vectors are content-addressed in code and tested for tampering. Their
technical determinism does not substitute for a later candidate-bound
SOLE_OWNER attestation or the separate Gate 1 sequential-design contract.
