# Security Review: msp

## Scope

The scan reviewed the canonical include paths and exclusions listed below.

- Scan mode: working_tree
- Target kind: git_worktree
- Target ID: target_sha256_75fd53860dc09b3875e6867aa7e83d8d
- Revision: 93e239b32f9c71261325c8866f6a76a1344c2d91
- Snapshot digest: codex-security-snapshot/v1:sha256:e394fd88e74454a11b9d49f4eb03f55c0c2f09164ebfed03de064d99b8d2d795
- Inventory strategy: diff
- Included paths: apps/worker/app/adapters/broker/toss_client.py
- Excluded paths: none
- Runtime or test status: Focused unit tests, ruff, and mypy passed locally; no live orders placed.
- Artifacts reviewed: apps/worker/app/adapters/broker/toss_client.py, apps/worker/app/tests/unit/test_toss_readonly.py, docs/API_CONNECTIONS.md, docs/RUNBOOK.md, artifacts/02_discovery/deep_review_input.jsonl, artifacts/02_discovery/work_ledger.jsonl, artifacts/02_discovery/finding_discovery_report.md
- Scan context: Safety-first Korean stock auto-trading worker. Account ambiguity and provider uncertainty must fail closed before live orders.

Limitations and exclusions:
- No Render dashboard/API logs were available in this local scan.
- No live broker orders were placed.
- Toss account API responses were exercised with unit-test mock transport, not retained real provider output.

### Scan Summary

| Field | Value |
| --- | --- |
| Reportable findings | 0 |
| Severity mix | none |
| Confidence mix | none |
| Coverage | complete |
| Validation mode | Parent-agent diff review with deterministic worklist and completion receipt; no candidates reached validation. |

Canonical artifacts: `scan-manifest.json`, `findings.json`, and `coverage.json`. This report is a deterministic projection of those files.

## Threat Model

Assets include broker credentials, accountSeq selection, Supabase service-role access, live-order controls, order state, and evidence artifacts.

### Assets

- Toss credentials and accountSeq
- live order execution gate
- Supabase control-plane data
- desktop cockpit read views

### Trust Boundaries

- Render env vars to worker-only provider adapters
- Worker adapters to external Toss APIs
- Worker persistence to Supabase service-role PostgREST
- Desktop cockpit to RLS-backed Supabase tables

### Attacker Capabilities

- Influence provider responses or configuration completeness in non-production tests
- Operate desktop UI without direct broker credentials
- Trigger scheduled worker cycles through configured runtime state

### Security Objectives

- Any account ambiguity blocks live/account-scoped broker calls
- Only ExecutionService may call BrokerPort.place_order after RiskService approval
- No secrets are stored in desktop, docs, logs, seed data, or render.yaml
- Any provider uncertainty fails closed

### Assumptions

- Live order execution remains disabled unless explicit live gates and external evidence are satisfied
- TOSS_ACCOUNT_ID, when set, contains accountSeq rather than a raw account number

## Findings

### No findings

No reportable findings survived the canonical discovery, validation, and reportability gates.

## Reviewed Surfaces

| Surface | Risk Area | Outcome | Notes |
| --- | --- | --- | --- |
| apps/worker/app/adapters/broker/toss_client.py | Toss account-scoped API header selection | Rejected | Full file reviewed; no/multiple accounts raise ProviderAuthError before account-scoped calls, and provider_health verifies account readiness. Evidence: artifacts/02_discovery/work_ledger.jsonl, artifacts/02_discovery/finding_discovery_report.md |

## Open Questions And Follow Up

- Has the updated worker been redeployed on Render and observed with retained provider-health output?
  - Follow-up prompt: Redeploy Render worker from the committed hash, then retain provider health and paper-cycle output proving accountSeq readiness is healthy or fail-closed.
