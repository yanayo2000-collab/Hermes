# GLE G0-04A Audience Risk Audit v1

G0-04A is a bounded, subject-bound, GET-only Gate 0 evidence fragment. It
answers one narrow question: are the two identical Copy-only audiences bound
to exactly two 50/50 Cells in one Meta `SPLIT_TEST`, with no extra Cell or
AdSet, and with identical delivery configuration?

## Proven configuration claim

A technically clean fragment proves:

- the G0-04 manifest, receipt, evidence, subject, and immutable SQLite snapshot
  hashes are exact and current;
- the Study is `SPLIT_TEST` with exactly two expected Cells at 50/50;
- each Cell owns exactly its expected AdSet and the two AdSets use the same
  targeting, promoted object, campaign, budget, bid, optimization, billing, and
  attribution configuration;
- two account-level delivery-estimate requests built from the exact AdSet
  targeting plus an independent same-targeting reference request are ready and
  non-zero;
- the AdSet projections are identical on a second read at the end of the
  audit, so drift during collection cannot pass; and
- this audit issued GET requests only and performed no Meta or database write.

The resulting classification is `TARGETING_CONFIG_EQUIVALENT`, together with
`SPLIT_TEST` topology evidence. The account-level delivery-estimate calls are
repeated availability checks for the same targeting; they are not Cell reads
and do not calculate an audience intersection. Therefore
`internal_auction_classification` remains `UNKNOWN`, the receipt remains
`INCOMPLETE`, and both `AUDIENCE_OVERLAP_UNKNOWN` and
`INTERNAL_AUCTION_CONTAMINATION_UNKNOWN` remain blockers. Graph does not expose
an account-wide auction-collision metric, and this fragment does not claim that
no unrelated campaign competes for the same users.

## Fail-closed boundaries

Cross-subject, expired, or non-PASS G0-04 plan/topology/freshness evidence,
extra/missing Cells or entities, wrong
Study type, non-50/50 allocation, targeting or delivery-policy drift, partial
pagination, unavailable estimates, redirects, and any non-GET transport keep
the fragment FAIL/INCOMPLETE. Even a technically clean collection remains
INCOMPLETE for the unobservable overlap/auction dimensions. G0-04A is always
`not_gate_receipt=true` with a `QUASI_ONLY` ceiling.

## Files and execution

- Engine: `app/growth/gate0_audience_risk_audit.py`
- CLI: `scripts/audit_gle_gate0_audience_risk.py`
- Tests: `tests/test_growth_gate0_audience_risk_audit.py`

The CLI requires explicit `--execute-read-only`, a token supplied only through
an environment variable, committed G0-04 artifacts, and three new immutable
outputs in one directory. It has no Meta mutation method and adds no table,
migration, service, scheduler, or production timer.

G0-05 consumes the committed G0-04A manifest/receipt/evidence tuple and binds it
again to its exact subject, source snapshot, G0-04 receipt hash, parent expiry,
GET journal, and assessment clock. The tuple is mandatory input; if absent or
malformed the assessment aborts without a candidate. A valid tuple records the
configuration evidence but deliberately preserves `AUDIENCE_OVERLAP_UNKNOWN`
and `INTERNAL_AUCTION_CONTAMINATION_UNKNOWN`.
