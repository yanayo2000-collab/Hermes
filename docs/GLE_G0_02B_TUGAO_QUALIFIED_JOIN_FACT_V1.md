# GLE G0-02B Tugao Qualified-Join Fact Contract v1

## Purpose

Preserve TugaoFunnel's explicit `guild_join_success_users` metric so later Gate 0 evidence can distinguish an observed successful join from the legacy total-join dashboard metric.

This change is a prerequisite evidence transport. It is not a Gate receipt, does not promote Gate 0, does not create or activate Meta objects, and does not infer historical values from other metrics.

## Frozen source mapping

- Source contract: `tugao_funnel_daily_metrics_api_v1`
- Source field: `guild_join_success_users`
- Stored metric: `tugao_join_success_users`
- Diagnostic companion: `guild_join_success_no_wa_users` → `tugao_join_success_no_wa_users`
- Required source dimensions, in exact order: `date,country,media_source,campaign_id,adset_id,ad_id,external_app`

`guild_join_total_users`, `guild_joins`, BindSuccess, CRM success, and `tugao_real_bind_count` are not substitutes for the qualified numerator.

## Observation and attribution states

The two numeric columns are additive and default to zero for SQLite compatibility. A zero is usable only when `payload_json` also proves:

- `qualified_join_metric_observed=true`
- `qualified_join_source_field=guild_join_success_users`
- `source_metric_contract=tugao_funnel_daily_metrics_api_v1`

Rows created before this contract have no observation marker. Their default zero is `UNKNOWN`, not an observed zero.

For exact paid-media attribution, the payload must additionally contain:

- `qualified_join_exact_attribution=true`
- `qualified_join_attribution_status=exact`
- non-empty `campaign_id`, `adset_id`, `ad_id`, and `external_app`

Observed Tugao qualified rows use the frozen exact source tuple as their row identity. Display-name changes therefore update the same fact, while different exact IDs remain separate even when their names match. Duplicate exact tuples, missing paid-media IDs, and mixed observed/unobserved inputs fail closed before a qualified replacement can be marked complete.

## Numeric contract

Qualified counts accept only finite, non-negative integers. Missing, null, blank, `N/A`, booleans, negative values, fractions, NaN, and infinity are rejected. An explicit numeric zero remains an observed zero and never falls back to the total-join field.

## Persistence and migration

The existing `ad_dashboard_fact_rows` table receives two additive columns. No new truth table is introduced. Legacy rows retain the old name-based row identity; newly observed Tugao qualified rows use the exact source tuple. A complete Tugao refresh deletes only Tugao rows in its bounded date window before inserting the authoritative exact set, so legacy and exact row IDs cannot coexist and double-count. The dedicated SQLite writer verifies both columns after schema migration, carries the Tugao completeness gate through fact replacement, and compares the committed row set, metrics, IDs, and provenance with the materialized input before commit.

Migration compatibility is additive. A bounded pre-migration preimage may omit only the two new metric columns; restore fills those columns with numeric zero while preserving the old payload without an observation marker, so the restored value remains `UNKNOWN`.

## Imported production baseline

The forward sync entrypoint and source client were missing from the prior GLE source closure, so this PR explicitly freezes them before modification:

- production `app/tugao_funnel_api.py` SHA-256 before this PR: `c02751e401996c667cf40486f4cd1c418c02d73c801200c1ae76c5a79c86e4af`
- production `scripts/backfill_ad_dashboard_fact_rows.py` SHA-256 before this PR: `fa94195005739e5fca57a647b22735bcebac9150caef134f6aadb352674447c3`
- production `app/batch_runtime.py` SHA-256 before this PR: `de9abc1133b1ba47675c07c55bc747810aad0bfb7cf1cca9b1ffe1f1a3a6f49e`
- required production phase-handoff dependency `scripts/mcn_phase_resource_handoff.py` SHA-256: `95e568e4d11df165a077f1acb24a7e63c3afa1601bff628a950794cf0f07b92f`

The active production timer routes the daily ad-dashboard batch through this backfill path. The phase-handoff module is an explicit production dependency outside this PR's evidence-transport boundary: a non-production checkout may skip that handoff, while `/opt/mcn-ai-automation` fails closed if the module is absent. A future deployment must recheck the dependency SHA before applying the minimal patch; this PR is not a fresh-install production bundle.

Importing the source files into Git is code provenance only; this PR does not execute a backfill, alter a systemd unit, or write production.

Historical restoration is a separate governed operation. It may only replay the bounded authoritative Tugao GET API and must preserve source/request hashes and per-day readback. It must never derive the new fields from the existing `guild_joins` column.

## Gate 0 usage

G0-05 may consume only settled rows matching the frozen contract, exact subject IDs, exact Cell mapping, and explicit observation/attribution markers. Missing rows, legacy rows, collisions, partial windows, or mixed contract versions remain `UNKNOWN/QUASI_ONLY`.

At least one post-deployment natural positive `guild_join_success_users` event is still required for controlled canary evidence. Historical replay can support baseline and power calculations but cannot replace natural-event acceptance.
