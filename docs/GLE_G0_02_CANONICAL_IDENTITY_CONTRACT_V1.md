# GLE G0-02A Consumer Canonical Identity Contract v1

## 1. Scope and evidence level

This change defines the consumer-side wire and persistence contract for canonical
GLE identity on Tugao Bind success events. The contract version is:

```text
gle-canonical-identity-v1
```

It does not change `/api/leads/upsert`, create a new truth table, modify an
external Bind producer, backfill historical events, or authorize a production or
Meta write. Consumer support alone does not prove producer pass-through. Gate 0
therefore remains `QUASI_ONLY`; this change cannot produce Gate PASS or
`CONTROLLED_FEASIBLE` evidence.

## 2. Wire contract

Canonical identity is read only from these top-level fields:

```json
{
  "identity_contract_version": "gle-canonical-identity-v1",
  "lead_id": "<canonical lead id>",
  "customer_id": "<canonical customer id>"
}
```

Both IDs must be nonempty strings and must satisfy `raw == raw.strip()`. The
consumer never trims, case-folds, truncates, prefixes, derives, or otherwise
repairs either ID. Nested identity objects are not canonical input.

For a versioned payload, `customer_id` means only canonical customer identity.
`customer_user_id` is populated only from an explicit top-level
`customer_user_id` field. A payload without `identity_contract_version` retains
the legacy `customer_user_id -> customer_id -> user_id` lookup so existing
consumer behavior is not broken, but it is always `LEGACY_UNVERIFIED` and never
promoted to canonical evidence. A present but malformed or unsupported version
is not treated as legacy.

Phone, name, `customer_user_id`, `bind_id`, `event_id`, timestamps, and nearby
records are forbidden as canonical identity fallbacks.

## 3. Existing-table extension

The existing `tugao_bind_success_raw_events` table gains five columns:

| Column | Contract |
|---|---|
| `identity_contract_version` | Nullable; set only when the first valid pair is accepted |
| `canonical_lead_id` | Nullable; exact accepted wire value |
| `canonical_customer_id` | Nullable; exact accepted wire value |
| `canonical_identity_status` | `LEGACY_UNVERIFIED`, `PENDING_VERIFICATION`, `VERIFIED`, or `BLOCKED` |
| `canonical_identity_reason` | Stable reason code; empty only for `VERIFIED` |

Schema initialization checks `PRAGMA table_info` and adds only missing columns.
The two canonical columns and version are nullable. Status and reason default to
`LEGACY_UNVERIFIED`, so old rows receive the legacy read-time state without a
historical `UPDATE`. No historical row is parsed or guessed, and existing raw
fields, hashes, timestamps, and row count are unchanged by migration.

## 4. Sticky event identity

`event_id` identifies an immutable canonical identity slot:

1. The first syntactically valid v1 pair fixes the contract version and both
   canonical IDs.
2. A first versioned attempt that is malformed, unsupported, missing either ID,
   or contains an invalid ID is permanently blocked even though no pair was
   accepted. A later valid replay cannot wash out the anomaly.
3. A replay with the same pair is idempotent.
4. If the first pair is waiting only because a CRM counterpart does not yet
   exist, the same pair may be verified after CRM later closes the relationship.
5. After a valid pair, any payload with no accepted v1 pair—including a missing
   version, missing ID, malformed ID, or unsupported version—sets permanent
   `EVENT_IDENTITY_MISSING_AFTER_VALID`.
6. A different valid pair sets permanent `EVENT_IDENTITY_DRIFT`.
7. CRM invalid, conflicting, ambiguous, or unavailable evidence is also
   fail-closed. Only the pure `CANONICAL_IDENTITY_NOT_IN_CRM` waiting state may
   automatically advance.
8. A partially populated or malformed persisted contract/version/pair is treated as
   `CANONICAL_IDENTITY_STORED_INCOMPLETE`; replay cannot overwrite it.
9. Stored state combinations are validated on every replay: `VERIFIED` requires
   an empty reason, and `PENDING_VERIFICATION` requires exactly
   `CANONICAL_IDENTITY_NOT_IN_CRM`. Illegal combinations preserve the canonical
   fields and become permanent `CANONICAL_IDENTITY_STORED_INCOMPLETE`.
10. Permanent states cannot be cleared by replay. A future recovery mechanism
   requires a separate Plan/Approval/Receipt change; this milestone provides no
   manual override.

The existing raw payload and its hash continue to represent the latest accepted
transport observation. They may change on replay, including a blocked replay.
The contract version and canonical columns are separate immutable evidence and
are never derived again from the latest raw payload after first acceptance.
Downstream canonical consumers must read the canonical columns and status, not
re-parse latest raw JSON. The current G0-01 raw-snapshot auditor remains blocked
from Gate proof until its dependent baseline is rebased to this persisted
contract; this three-file milestone does not modify that separate audit module.

Concurrent first-pair writers use compare-and-swap persistence. A write is
conditional on the exact version, pair, status, and reason read by that attempt.
If another connection wins, `rowcount=0` forces a bounded reread and state
recalculation; a different losing pair becomes `EVENT_IDENTITY_DRIFT` without
overwriting the winning pair. Exhausted contention fails with a fixed redacted
error code.

## 5. Exact CRM verifier

`VERIFIED` requires one exact closed pair across existing CRM truth:

```text
leads.lead_id                    == canonical_lead_id
leads.matched_customer_id        == canonical_customer_id
customer_projection.customer_id == canonical_customer_id
customer_projection.lead_id      == canonical_lead_id
```

The verifier performs parameterized equality queries only. Every direct and
reverse query has `LIMIT 2`; both reverse lookups run even when a direct
counterpart is absent, so an existing conflicting or multiple reverse mapping
cannot be misclassified as recoverable pending. Reverse lookups prove that
exactly one lead points to the customer and exactly one customer points to the
lead. Persisted IDs must also be nonempty strings with `raw == raw.strip()`.

| Condition | Status and reason |
|---|---|
| Both direct counterparts absent or one genuinely not yet present | `PENDING_VERIFICATION / CANONICAL_IDENTITY_NOT_IN_CRM` |
| Persisted ID is malformed | `BLOCKED / CANONICAL_IDENTITY_INVALID` |
| Direct or reverse pair disagrees | `BLOCKED / LEAD_CUSTOMER_LINK_CONFLICT` |
| Either reverse direction has zero or more than one result after direct closure | `BLOCKED / AMBIGUOUS_CANONICAL_IDENTITY` |
| CRM query cannot be completed | `BLOCKED / CANONICAL_IDENTITY_SOURCE_UNAVAILABLE` |
| Unique bidirectional closure | `VERIFIED` with an empty reason |

The verifier returns only status and stable reason codes. It does not include
complete identifiers, SQL, raw rows, or PII in ordinary errors or logs.

## 6. Acceptance and deferred work

The local acceptance suite covers legacy migration immutability, exact wire
validation, explicit `customer_user_id` semantics, nested/fuzzy fallback
rejection, sticky missing/drift behavior, allowed counterpart-late verification,
permanent CRM anomaly states, exact bidirectional ambiguity checks, bounded SQL,
and identifier redaction.

Still required outside G0-02A:

- the external Bind producer must pass the three top-level fields unchanged;
- natural new traffic must demonstrate the producer-consumer contract;
- Product/Data must freeze the qualification rule;
- G0-04/G0-05 must close read-back provenance, permissions, power, allocation,
  and Gate receipt evidence;
- downstream G0-01 evidence must consume the persisted canonical columns after
  the branches are rebased in the coordinated sequence.
