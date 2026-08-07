# GLE G0-04 GET-only Capability / Topology Audit

Version: `gle-g0-04-get-only-topology-audit-v1`

Baseline: FINAL EXECUTION PLAN v1.1
Gate contribution: Gate 0 evidence fragment only

## Outcome boundary

G0-04 proves, for one bounded and fresh read window:

- the token principal and required granted scopes;
- the exact Business → Ad Account / Page / App ownership and assignment edges;
- the Study → Cells → Campaign / Ad Sets / Ads / Creatives topology;
- the exact Page/App/promoted-object and Copy-only creative bindings;
- whether an ACTIVE object has a matching, separate activation Plan, human Approval,
  dry-run, execution Receipt, actor/application binding, and activity event;
- that this audit process issued only Graph `GET` requests and opened SQLite in
  immutable query-only mode.

It does **not** prove write capability, actual randomization, impressions/spend
allocation, overlap, qualified-join attribution, power/MDE/budget feasibility, or
Gate sign-off. Its ceiling is always `QUASI_ONLY`; it cannot emit
`CONTROLLED_FEASIBLE` or a Gate receipt. G0-05 must combine this fragment with
G0-01 attribution, actual allocation, PowerAssessment, frozen parameters, and
named Business/Data/Tech/Ops attestations.

The known legacy Study activated outside local Plan → Approval → Verify → Receipt,
and changing primary text, headline, and description, must be classified
`POLLUTED / INELIGIBLE`. It cannot be repaired by backdating or borrowing a receipt.

## Physical mapping

No table or migration is introduced.

- `growth_operation_action`, `growth_operation_approval`,
  `growth_idempotency_record`, `meta_execution_task`, and
  `meta_execution_task_receipt` remain the only local execution truth.
- `ad_audience_preflight` remains the only server-owned preflight truth. G0-04
  reads its exact row and hash; it never invokes the write-capable preflight runner.
- The strict G0-03 compiler receipt is recompiled and verified. Legacy or
  multi-variable Plans are inadmissible.
- Graph evidence is collected through a dedicated transport with no mutation
  method. `MetaGraphReadService.submit_async_insights()` is deliberately not used
  because that helper performs a POST.

## Immutable inputs

The request uses exact keys and canonical JSON:

- audit identity, timestamp, nonce, Graph/API/SDK/topology versions;
- exact create action ID and optional, separate activation action ID;
- signed actor-binding-registry hash;
- frozen required permission and account-task sets;
- bounded runtime, receipt TTL, activity-settlement, clock-skew, page, and event limits.

This version will not accept a caller-weakened contract. Its minimum scopes are
`ads_management`, `ads_read`, `business_management`, `pages_manage_metadata`,
`pages_read_engagement`, and `pages_show_list`; minimum account tasks are
`ADVERTISE` and `MANAGE`. The freshness policy is exactly 120 seconds runtime,
300 seconds receipt TTL, 300 seconds activity settlement, 60 seconds clock skew,
five pages, and 100 events. Changing those values requires a new reviewed
contract version rather than a looser request.

The caller supplies IDs only. Plan, compiler receipt, Approval, dry-run, task,
step Receipts, object IDs, and preflight evidence are independently loaded from
the SQLite snapshot. The source file must match its supplied SHA-256 and remain
unchanged; non-empty `-wal`, `-journal`, or `-shm` sidecars fail closed.

The token is read only from an environment variable. It is never accepted on the
command line, stored in a receipt, logged, or included in a response hash.

## GET-only evidence contract

The client accepts only the exact paths derived from the server-owned topology.
It disables redirects and bounds page count and total items. Missing pages,
cursor loops, Graph errors, redacted/absent required fields, or response drift
produce `INCOMPLETE` or `FAIL`, never PASS.

Every declared required response must be successful and fully paginated; an
unconsumed Graph error cannot be hidden behind another passing dimension. The
same `/me` principal must hold the required Account and Page tasks and an App
role of `ADMINISTRATOR` or `DEVELOPER`. The debug-token App must match the frozen
executor App. Account promotable objects must contain the target App, and the
Study must expose exactly one supported `COST_PER_ACTION` objective.

The run reads:

- `debug_token`, `/me`, `/me/permissions`;
- account identity, status, tasks, capabilities, promotable objects, and assignments;
- Business owned/client accounts, pages, apps, and system users;
- Page identity/publication/assignments and App identity/roles;
- Study, all Cells/objectives, and each Cell's accounts/campaigns/adsets;
- Campaign, Ad Sets, Ads, and Creatives twice;
- account activities from create-Plan time through the bounded audit cutoff.

Creation projection compares every frozen non-status Plan field. Current
ACTIVE/PAUSED state is evaluated only by the separate activation-provenance
check, so a legitimate later activation does not falsify the original PAUSED
creation receipt.

Any first/last object mismatch is `OBJECT_DRIFT_DURING_AUDIT`. An ACTIVE object
must have exactly one PAUSED→ACTIVE activity record, an exact object-set match to
the activation Plan, and actor ID plus application ID inside the frozen registry
and Approval TTL. The activity time must also be after Approval consumption and
task creation and no later than the exact object's verified status-step Receipt;
an earlier same-actor event cannot be borrowed by a later execution chain. Actor
names never authorize. External/manual activation is `POLLUTED`, even when the
present status later becomes PAUSED.

## Receipt

`gle-g0-04-audit-receipt-v1` contains only redacted hashes and aggregate checks:

- exact request/source/local/Graph evidence hashes;
- token/permission, ownership, topology, activation, freshness, and zero-write checks;
- per-request endpoint template, field set, page, HTTP status, response hash/size;
- GET and forbidden-method counters;
- `not_gate_receipt=true`, `gate0_result_ceiling=QUASI_ONLY`;
- `attestation_status=PENDING_ATTESTATION`.

A separate canonical evidence bundle preserves exact IDs, statuses, timestamps,
relationships, response hashes, and transport journal while hashing names, links,
ad copy, actor names, and other free text. The receipt binds its immutable bundle
hash; existing output paths are rejected rather than overwritten.
Receipt and bundle are published with exclusive same-filesystem links. A manifest
containing both file hashes is published last; consumers must ignore any pair
without `committed=true`, so an interrupted two-file write cannot masquerade as
a complete evidence package.

The receipt SHA proves integrity, not identity. G0-05 must attach detached named
attestations before using this fragment in a Gate decision.

## CLI

The CLI is inert unless `--execute-read-only` is present:

```bash
META_ACCESS_TOKEN='provided-out-of-band' \
python scripts/audit_gle_gate0_topology.py \
  --execute-read-only \
  --request /path/request.json \
  --actor-registry /path/actor-registry.json \
  --database /path/immutable-snapshot.db \
  --database-sha256 <64-lowercase-hex> \
  --output /path/g0-04-receipt.json \
  --evidence-output /path/g0-04-redacted-evidence.json \
  --manifest-output /path/g0-04-artifact-manifest.json
```

Exit codes: `0` means this fragment passed; `2` means FAIL/INCOMPLETE/POLLUTED;
`64` means invalid input; `66` means source or Graph read failure. Exit `0` never
means Gate 0 PASS.

## Stable failure families

Input/hash/version, immutable-source/query-only, endpoint/method/redirect,
pagination/cursor, token/scope/principal, Business/account/Page/App ownership and
tasks, legacy Study/compiler binding, Cell/object/account/Page/App topology,
object drift, activity settlement, activation event/actor/application/TTL,
create-vs-activation separation, Approval/preflight/receipt hash, and receipt
expiry failures are all fail closed and machine-readable.

## Acceptance and exclusions

Required tests cover canonical determinism, exact schema, endpoint allowlist,
GET-only behavior, pagination loop/truncation, token/ownership/topology mismatch,
promotable-object and Study-objective mismatch, exact App roles, double-read
drift, external activation pollution, governed ACTIVE provenance timing,
immutable DB/hash/sidecars, redaction, and explicit CLI execution. No production
DB, Meta credential, real Graph call, Meta write, schema change, deployment, or
backend restart belongs to this PR.
