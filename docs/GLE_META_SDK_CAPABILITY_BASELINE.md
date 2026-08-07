# GLE Meta SDK capability baseline

Version: `gle-meta-sdk-v1`  
SDK: `facebook-business==25.0.1`  
Graph API: `v25.0`

## Outcome

GLE no longer treats ad-object fields as an undocumented collection of raw HTTP strings. The exact SDK/API version, allowed fields, enums and required edges are generated into `config/meta_sdk_contract_v25_0_1.json`. CI must run:

```bash
.venv/bin/python scripts/generate_meta_sdk_contract.py --check
```

An SDK upgrade is a reviewed change: update the pin, regenerate the manifest, inspect the JSON diff, update compiler/read-back tests, and only then promote the new contract. Silent drift is a release failure.

## Change packages

### 1. SDK contract baseline

- Pinned SDK and Graph API version.
- Allow-contract for Account, Campaign, Ad Set, Creative, Ad, Study, Cell and asynchronous Report Run.
- Generated fields, relevant enums, methods/edges, and a stable contract hash.

### 2. Execution safety and complete read-back

- New copy-only Plans carry `sdk_contract_version=gle-meta-sdk-v1`.
- Final verification independently reads Campaign, each Ad Set, Creative and Ad, plus Study and Cells.
- Planned objective, status, budget, Cost Cap, promoted object, audience, attribution, Page, copy, image, CTA, lineage and split allocation are compared when present in the immutable Plan.
- Any mismatch returns `UNKNOWN`; execution history is retained and the write is never replayed automatically.
- Meta errors retain code, subcode, transient flag, user message and trace ID while credentials are recursively redacted.

### 3. Read-only operating data

- Historical Insights use an asynchronous report job; the service has no synchronous 90-day pull path.
- Activity, account capability, review/learning fields and Study/Cell/objective surfaces are GET/read-job only.
- API usage headers are captured so scheduling can back off before Meta rate limiting.

### 4. Controlled capability expansion

Only `COPY_ONLY_SPLIT_TEST` is admitted to rehearsal and still requires Plan, explicit approval, PAUSED creation, separate activation approval and independent read-back. Ad preview is read-only ready. Copying Ad Sets, scaling, video, carousel, budget schedules and CBO remain fail-closed until their compiler, read-back and policy contracts qualify. Lead Ads, Catalog, Messaging and Custom Audiences remain outside the GLE primary path.

## Release boundary

This baseline adds contracts, read-only services and fail-closed verification. It does not enable an account/action allowlist, create or activate Meta objects, run a canary, change budgets, or write production data. Production activation remains a separate governed release after the v1.1 Gate prerequisites pass.
