# GLE G0-03 — Strict Primary-Text-Only Compiler v1

## Status and boundary

This change implements the local, default-off compiler and execution gates for
the GLE v1.1 Copy-only golden path. It does not promote Gate 0, enable a canary,
write Meta, add a table, backfill an old Plan, merge a PR, or deploy production.

The current production Gate 0 classification remains `QUASI_ONLY`. The existing
real Study is not admissible evidence because its creatives changed more than
primary text and its Campaign/Ad Sets were activated by an external human action
without a matching GLE Action/Approval/Receipt.

## Canonical contract

- compiler: `gle-primary-text-only-compiler-v1`
- plan schema: `gle-copy-only-plan-schema-v1`
- canonical JSON: `gle-canonical-json-v1`
- action: `CREATE_PAUSED_AD`
- experiment: `COPY_ONLY`
- unique variable: `PRIMARY_TEXT`
- cells: exactly two, one `BASELINE` and one `CHALLENGER`
- allocation: exactly `50 / 50`
- Study: `SPLIT_TEST`
- Campaign, Ad Sets, and Ads: `PAUSED`
- preflight: current `VERIFIED` GET-only evidence, zero Meta writes, exact
  account/market/Study references and `C1`/`C2` delivery estimates
- approval: authenticated `operator:<principal>`, unexpired TTL, exact immutable Plan

Canonical JSON is UTF-8, object-key sorted, compact, and rejects non-finite
numbers, invalid/control characters, implicit string trimming, and unsupported
types. Array order is significant.

The only Meta configuration path permitted to differ is:

`cells[*].steps.CREATIVE_CREATE.object_story_spec.link_data.message`

Cell/object identifiers and names are excluded from the invariant projection,
but their canonical identities are constrained: `C1=BASELINE`,
`C2=CHALLENGER`, and experiment, Study-cell, and copy-version identities are
non-empty and unique.
The frozen image, Page, destination, headline, description, CTA, targeting,
audience, placement, budget, bid, optimization, billing, attribution, promoted
object, statuses, Study type, and delivery guardrails must remain identical.
Unknown keys at the Plan, Cell, Meta step, targeting, promoted-object,
attribution, CTA, Campaign, Study, and execution-policy schema surfaces fail.

## Receipt

The compiler stores one deterministic receipt inside the immutable Plan:

- `status`
- compiler/schema/canonicalization versions
- `plan_core_hash`
- `invariant_projection_hash`
- `cell_primary_text_hashes`
- `changed_paths`
- `reason_codes`
- `receipt_hash`

`plan_core_hash` hashes the Plan without `compiler_receipt`.
`receipt_hash` hashes the receipt without `receipt_hash`. The existing
`payload_hash(final_plan)` remains the approval/task Plan hash; no second Plan
truth is introduced.

## Fail-closed execution chain

1. Copy-launch input rejects headline or description as a second variable.
2. Plan build compiles and embeds the receipt, then checks current E00 and
   preflight permissions before Action/Approval creation.
3. Generic Action creation and Approval proposal reject missing/tampered receipts
   or Action/Plan mismatch.
4. Approval proposal and transition recompile, recheck E00/preflight and TTL,
   and accept only an authenticated `operator:<principal>`.
5. Strict dry-run requires the approved human Plan and records compiler hashes.
6. Central `enqueue_task()` rechecks Action, Plan, Approval, dry-run, E00 Gate,
   canary account/market, execution preflight, and kill switches before consuming
   the Approval or creating a task.
7. The worker reloads the same evidence and E00 contract immediately before
   every adapter write. A changed kill switch moves the task to manual review
   with zero adapter calls.
8. Caller-supplied continuation/recovery state is forbidden on the strict
   initial enqueue. Old `copy_variant` Plans without the explicit v1 contract
   fail closed rather than being silently promoted or backfilled.
9. Every live choke point resolves the frozen image ID through the approved
   creative registry, requires the exact registry path/hash and an approved
   review, then verifies a regular file no larger than 25 MiB with chunked
   SHA-256 before the worker's first adapter call. The upload boundary repeats
   the size/hash check and performs no POST on mismatch.
10. The existing autopilot reaches the real strict Plan path but stops at
    `WAITING_HUMAN_APPROVAL`; it cannot self-approve or silently enter the live
    queue. Caller-supplied recovery/continuation remains rejected centrally.

Uncertain Meta POST behavior remains unchanged: the worker performs GET
reconciliation and never automatically repeats an uncertain write.

## Gate 0 follow-up

G0-03 completion alone cannot produce `CONTROLLED_FEASIBLE`. Remaining Gate 0
work includes upstream canonical-ID natural-flow evidence, GET-only permission
and ownership topology, activation provenance, allocation/Insights evidence,
PowerAssessment, frozen business thresholds, and named Gate/Business/Technical/
Data sign-off. A future real experiment must use a new experiment ID; creation
as PAUSED and activation require separate Plan/Approval/Verify/Receipt chains.
