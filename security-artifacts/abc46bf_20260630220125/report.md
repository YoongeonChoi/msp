# Security Review: msp

## Scope

The scan reviewed the canonical include paths and exclusions listed below.

- Scan mode: working_tree
- Target kind: git_worktree
- Target ID: target_sha256_0660ec79b6e8f41ce2b2e7a1de9a3b84
- Revision: abc46bf3d43d5c85f805e6379543fabb85215b80
- Snapshot digest: codex-security-snapshot/v1:sha256:0660ec79b6e8f41ce2b2e7a1de9a3b8441edee93c9695837a64c1d6862b9b666
- Inventory strategy: diff
- Included paths: apps/worker/app/application/services/feature_service.py
- Excluded paths: none
- Runtime or test status: Focused feature/trading-cycle tests passed locally; full worker pytest suite, ruff, and mypy passed locally; no live orders placed.
- Artifacts reviewed: apps/worker/app/application/services/feature_service.py, apps/worker/app/application/use_cases/run_trading_cycle.py, apps/worker/app/tests/unit/test_feature_service.py, apps/worker/app/tests/integration/test_trading_cycle.py, artifacts/02_discovery/deep_review_input.jsonl, artifacts/02_discovery/work_ledger.jsonl, artifacts/02_discovery/finding_discovery_report.md, artifacts/03_coverage/reviewed_surfaces.md
- Scan context: Safety-first Korean stock auto-trading worker. Live feature snapshots must fail closed unless all provider evidence is real, complete, and auditable before any live broker proposal can be created.

Limitations and exclusions:
- No Render redeploy was performed by this local scan.
- No live broker orders were placed.
- Verified market/sector provider evidence is intentionally still absent, so provider_live_v1 remains not live-ready.

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

Assets include broker credentials, accountSeq selection, Supabase service-role access, provider evidence, live-order controls, order state, and release evidence artifacts.

### Assets

- Toss credentials and accountSeq
- Supabase service-role persistence
- provider feature evidence snapshots
- live order execution gate
- release evidence artifacts

### Trust Boundaries

- Render env vars to worker-only provider adapters
- Worker adapters to external Toss/OpenDART/Naver/provider APIs
- Worker persistence to Supabase service-role PostgREST
- Desktop cockpit to RLS-backed Supabase tables

### Attacker Capabilities

- Influence provider failure modes or incomplete provider payloads through external provider behavior
- Read RLS-authorized operational diagnostics from the cockpit
- Trigger scheduled worker cycles through configured runtime state

### Security Objectives

- Any provider uncertainty fails closed and cannot enable live broker calls
- Only ExecutionService may call BrokerPort.place_order after RiskService approval
- Feature evidence must not persist secrets or unbounded provider payloads
- UI code must not call broker/order APIs directly

### Assumptions

- Live order execution remains disabled unless explicit live gates and external evidence are satisfied
- Market/sector evidence is not yet production-proven and must remain a blocking readiness reason until implemented

## Findings

### No findings

No reportable findings survived the canonical discovery, validation, and reportability gates.

## Reviewed Surfaces

| Surface | Risk Area | Outcome | Notes |
| --- | --- | --- | --- |
| apps/worker/app/application/services/feature_service.py | live feature evidence gate before live order proposal | No issue found | Full file reviewed; the change makes provider_live_v1 fail closed until positive PER/PBR and verified market/sector evidence exist. The existing RunTradingCycle live path records live_feature_snapshot_not_ready and skips propose_live_order for not-ready provider snapshots, so no broker-call bypass or secret exposure was introduced. Evidence: artifacts/01_context/threat_model.md, artifacts/02_discovery/rank_input.jsonl, artifacts/02_discovery/deep_review_input.jsonl, artifacts/02_discovery/work_ledger.jsonl, artifacts/02_discovery/finding_discovery_report.md, artifacts/03_coverage/reviewed_surfaces.md, artifacts/04_reconciliation/dedupe_report.md, artifacts/05_findings/validation_summary.md, artifacts/05_findings/attack_path_analysis_report.md |

## Open Questions And Follow Up

- What production provider will supply verified Korean market/sector evidence so market_sector_evidence_missing can be removed safely?
  - Follow-up prompt: Implement or wire a verified KRX/sector provider, then retain local and hosted evidence showing provider_live_v1 snapshots include non-mock market/sector provenance before live_trading_ready=true.
- Have the pushed worker changes been redeployed and observed against hosted Supabase/Toss evidence?
  - Follow-up prompt: After pushing this commit, redeploy the Render worker manually and retain hosted Supabase, Toss provider, incident ACK, and system-order-scope proof for the final live readiness bundle.
