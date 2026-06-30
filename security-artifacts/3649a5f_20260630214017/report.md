# Security Review: msp

## Scope

The scan reviewed the canonical include paths and exclusions listed below.

- Scan mode: working_tree
- Target kind: git_worktree
- Target ID: target_sha256_b01405b8a3a51d26a98832168dc4dafd
- Revision: 3649a5f415ea8f0bbf508f7aac099ad6a0467d8e
- Snapshot digest: codex-security-snapshot/v1:sha256:b01405b8a3a51d26a98832168dc4dafda878e5dc7f4701452069aae6353bc91a
- Inventory strategy: diff
- Included paths: apps/worker/app/adapters/broker/toss_client.py, apps/worker/app/adapters/market_data/toss_market_data.py, apps/worker/app/application/services/health_service.py, apps/worker/app/application/services/paper_health_report_service.py
- Excluded paths: none
- Runtime or test status: Focused health and Toss unit tests passed locally; ruff and mypy passed locally; no live orders placed.
- Artifacts reviewed: apps/worker/app/adapters/broker/toss_client.py, apps/worker/app/adapters/market_data/toss_market_data.py, apps/worker/app/application/services/health_service.py, apps/worker/app/application/services/paper_health_report_service.py, artifacts/02_discovery/deep_review_input.jsonl, artifacts/02_discovery/work_ledger.jsonl, artifacts/02_discovery/finding_discovery_report.md, artifacts/03_coverage/reviewed_surfaces.md
- Scan context: Safety-first Korean stock auto-trading worker. Provider diagnostics may be persisted to Supabase, so failure details must be safe, bounded, and non-secret while all provider uncertainty remains fail-closed.

Limitations and exclusions:
- No Render redeploy was performed by this local scan.
- No live broker orders were placed.
- Hosted Supabase currently shows old deployed `api_health.details` rows as empty until Render runs this patched worker.

### Scan Summary

| Field | Value |
| --- | --- |
| Reportable findings | 0 |
| Severity mix | none |
| Confidence mix | none |
| Coverage | complete |
| Validation mode | Parent-agent diff review with deterministic worklist and completion receipts; no candidates reached validation. |

Canonical artifacts: `scan-manifest.json`, `findings.json`, and `coverage.json`. This report is a deterministic projection of those files.

## Threat Model

Assets include broker credentials, accountSeq selection, Supabase service-role access, provider-health diagnostics, live-order controls, order state, and release evidence artifacts.

### Assets

- Toss credentials and accountSeq
- Supabase service-role persistence
- api_health.details diagnostic rows
- paper health operational evidence
- live order execution gate

### Trust Boundaries

- Render env vars to worker-only provider adapters
- Worker adapters to external Toss APIs
- Worker persistence to Supabase service-role PostgREST
- Desktop cockpit to RLS-backed Supabase tables

### Attacker Capabilities

- Influence provider failure modes or HTTP responses through external provider behavior
- Read RLS-authorized operational diagnostics from the cockpit
- Trigger scheduled worker cycles through configured runtime state

### Security Objectives

- No provider diagnostic detail may persist credentials, tokens, account identifiers, or raw provider payloads
- Any provider uncertainty fails closed and cannot enable live broker calls
- Only ExecutionService may call BrokerPort.place_order after RiskService approval
- Paper health diagnostics must not mask operational critical worker failures

### Assumptions

- Live order execution remains disabled unless explicit live gates and external evidence are satisfied
- ProviderError.safe_message values are intended to be safe codes, but HealthService still redacts secret-like values defensively

## Findings

### No findings

No reportable findings survived the canonical discovery, validation, and reportability gates.

## Reviewed Surfaces

| Surface | Risk Area | Outcome | Notes |
| --- | --- | --- | --- |
| apps/worker/app/adapters/broker/toss_client.py | Toss broker provider-health diagnostic boundary | No issue found | Full file reviewed; provider_health stores only ProviderError class and safe_message for HealthService sanitization, accountSeq readiness remains fail-closed, and no broker order path was widened. Evidence: artifacts/02_discovery/work_ledger.jsonl, artifacts/02_discovery/finding_discovery_report.md |
| apps/worker/app/adapters/market_data/toss_market_data.py | Toss market-data provider-health diagnostic boundary | No issue found | Full file reviewed; provider_health stores only ProviderError class and safe_message for HealthService sanitization, market calendar probing remains read-only, and quote/market-open behavior is unchanged. Evidence: artifacts/02_discovery/work_ledger.jsonl, artifacts/02_discovery/finding_discovery_report.md |
| apps/worker/app/application/services/health_service.py | api_health detail persistence and secret redaction | No issue found | Full file reviewed; false provider details and raised KnownFailClosedError details now pass through _safe_details. Secret-like keys are skipped, secret-like string values are redacted, generic unexpected exceptions persist only error_type, and tests cover both return-false and raised-error redaction paths. Evidence: artifacts/02_discovery/work_ledger.jsonl, artifacts/02_discovery/finding_discovery_report.md |
| apps/worker/app/application/services/paper_health_report_service.py | paper health repeated critical aggregation | No issue found | Full file reviewed; paper_health_report events are excluded only from repeated operational critical counting, while worker critical events continue to fail the report. The report remains read-only apart from its own summary engine_event. Evidence: artifacts/02_discovery/work_ledger.jsonl, artifacts/02_discovery/finding_discovery_report.md |

## Open Questions And Follow Up

- Has the updated worker been redeployed on Render and observed with non-empty safe provider-health details for degraded Toss providers?
  - Follow-up prompt: Redeploy the pushed worker commit manually on Render, then retain hosted Supabase `api_health.details` rows proving Toss provider degradation is explained by safe error_type/reason fields without secrets.
- Have the hosted worker critical HTTPStatusError events been resolved or aged out with fresh healthy cycles?
  - Follow-up prompt: After Render redeploy, run `py -m app.tools.paper_health_report` against hosted Supabase and retain output showing operational critical events no longer cause `FINAL=FAIL`.
