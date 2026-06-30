# Security Review: msp

## Scope

The scan reviewed the canonical include paths and exclusions listed below.

- Scan mode: working_tree
- Target kind: git_worktree
- Target ID: target_sha256_c4c3bce58ce2bb7c06ffd02ad8640ffe
- Revision: c288dcd97ce7b372ed77ebca69a8581d94c35633
- Snapshot digest: codex-security-snapshot/v1:sha256:cd57ea605404bc123ec15147c2549abbf686641fb73d1893720ef0ac2a780318
- Inventory strategy: custom
- Included paths: apps/worker/app/adapters/broker/toss_auth.py, apps/worker/app/adapters/broker/toss_client.py, apps/worker/app/container.py
- Excluded paths: none
- Runtime or test status: not recorded
- Artifacts reviewed: apps/worker/app/adapters/broker/toss_auth.py, apps/worker/app/adapters/broker/toss_client.py, apps/worker/app/container.py, artifacts/02_discovery/deep_review_input.jsonl, artifacts/02_discovery/work_ledger.jsonl, artifacts/02_discovery/finding_discovery_report.md, app/tests/unit/test_toss_readonly.py, app/tests/unit/test_container_startup.py
- Scan context: Safety-first Korean stock trading worker; missing provider credentials must fail closed without permitting live orders.

Limitations and exclusions:
- No Render API/dashboard logs were available; behavior was reproduced with isolated local RUN_ONCE smoke.
- No live orders were placed.

### Scan Summary

| Field | Value |
| --- | --- |
| Reportable findings | 0 |
| Severity mix | none |
| Confidence mix | none |
| Coverage | complete |
| Validation mode | not recorded |

Canonical artifacts: `scan-manifest.json`, `findings.json`, and `coverage.json`. This report is a deterministic projection of those files.

## Threat Model

Assets include broker credentials, Supabase service-role access, live-order controls, order state, and evidence artifacts.

### Trust Boundaries

- Render env vars to worker-only provider adapters
- Worker adapters to external provider APIs
- Worker persistence to Supabase service-role PostgREST
- Desktop cockpit to RLS-backed Supabase tables

## Findings

### No findings

No reportable findings survived the canonical discovery, validation, and reportability gates.

## Reviewed Surfaces

| Surface | Risk Area | Outcome | Notes |
| --- | --- | --- | --- |
| apps/worker/app/adapters/broker/toss_auth.py | Toss credential boundary and live broker fail-closed behavior | Rejected | Missing Toss credentials defer to access_token and raise ProviderAuthError before HTTP. Evidence: artifacts/02_discovery/work_ledger.jsonl, artifacts/02_discovery/finding_discovery_report.md |
| apps/worker/app/adapters/broker/toss_client.py | Toss credential boundary and live broker fail-closed behavior | Rejected | Provider health catches ProviderError; order/cancel paths still require authenticated headers. Evidence: artifacts/02_discovery/work_ledger.jsonl, artifacts/02_discovery/finding_discovery_report.md |
| apps/worker/app/container.py | Toss credential boundary and live broker fail-closed behavior | Rejected | Render bootstrap can construct TossClient and reach fail-closed health behavior. Evidence: artifacts/02_discovery/work_ledger.jsonl, artifacts/02_discovery/finding_discovery_report.md |

## Open Questions And Follow Up

- Has the updated worker been redeployed on Render and observed via hosted Supabase heartbeat/provider health?
  - Follow-up prompt: Redeploy Render worker from this commit, then run paper health and hosted Supabase checks against retained output.
