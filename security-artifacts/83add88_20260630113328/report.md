# Security Review: msp

## Scope

The scan reviewed the canonical include paths and exclusions listed below.

- Scan mode: working_tree
- Target kind: git_worktree
- Target ID: target_sha256_c4c3bce58ce2bb7c06ffd02ad8640ffe
- Revision: 83add888ebe7b06d9d37e789b2f12cc7fd7b9636
- Snapshot digest: codex-security-snapshot/v1:sha256:9c9524bc53a86d8c1e1b72b3c759b720670bb813fb9ac5a61dd9b4033638277b
- Inventory strategy: custom
- Included paths: apps/desktop/src/pages/DashboardPage.tsx, apps/desktop/src/pages/SignalsPage.tsx, apps/worker/app/adapters/broker/toss_client.py, apps/worker/app/adapters/broker/toss_mock.py, apps/worker/app/adapters/market_data/toss_market_data.py, apps/worker/app/adapters/persistence/models.py, apps/worker/app/adapters/persistence/sql_repository.py, apps/worker/app/adapters/persistence/supabase_repository.py, apps/worker/app/application/ports/repository_port.py, apps/worker/app/application/services/feature_service.py, apps/worker/app/application/services/portfolio_service.py, apps/worker/app/application/services/risk_service.py, apps/worker/app/application/use_cases/run_trading_cycle.py, apps/worker/app/config.py, apps/worker/app/container.py, apps/worker/app/tools/run_live_recovery_drill_once.py
- Excluded paths: none
- Runtime or test status: not recorded
- Artifacts reviewed: apps/desktop/src/pages/DashboardPage.tsx, apps/desktop/src/pages/SignalsPage.tsx, apps/worker/app/adapters/broker/toss_client.py, apps/worker/app/adapters/broker/toss_mock.py, apps/worker/app/adapters/market_data/toss_market_data.py, apps/worker/app/adapters/persistence/models.py, apps/worker/app/adapters/persistence/sql_repository.py, apps/worker/app/adapters/persistence/supabase_repository.py, apps/worker/app/application/ports/repository_port.py, apps/worker/app/application/services/feature_service.py, apps/worker/app/application/services/portfolio_service.py, apps/worker/app/application/services/risk_service.py, apps/worker/app/application/use_cases/run_trading_cycle.py, apps/worker/app/config.py, apps/worker/app/container.py, apps/worker/app/tools/run_live_recovery_drill_once.py, artifacts/02_discovery/deep_review_input.jsonl, artifacts/02_discovery/work_ledger.jsonl, artifacts/02_discovery/finding_discovery_report.md
- Scan context: Safety-first Korean domestic stock auto-trading system; UI must not call broker APIs, secrets remain worker-only, and uncertain live order evidence fails closed.

Limitations and exclusions:
- Real hosted Supabase, provider lifecycle, incident ACK, and system-order-scope remote evidence remain external release gates.

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

No explicit canonical threat-model summary was recorded.

## Findings

### No findings

No reportable findings survived the canonical discovery, validation, and reportability gates.

## Reviewed Surfaces

| Surface | Risk Area | Outcome | Notes |
| --- | --- | --- | --- |
| apps/desktop/src/pages/DashboardPage.tsx | live trading safety and control-plane integrity | Rejected | Diff-scoped file reviewed by parent/delegated security reviewers; no plausible candidate survived. Evidence: artifacts/02_discovery/work_ledger.jsonl, artifacts/02_discovery/finding_discovery_report.md |
| apps/desktop/src/pages/SignalsPage.tsx | live trading safety and control-plane integrity | Rejected | Diff-scoped file reviewed by parent/delegated security reviewers; no plausible candidate survived. Evidence: artifacts/02_discovery/work_ledger.jsonl, artifacts/02_discovery/finding_discovery_report.md |
| apps/worker/app/adapters/broker/toss_client.py | live trading safety and control-plane integrity | Rejected | Diff-scoped file reviewed by parent/delegated security reviewers; no plausible candidate survived. Evidence: artifacts/02_discovery/work_ledger.jsonl, artifacts/02_discovery/finding_discovery_report.md |
| apps/worker/app/adapters/broker/toss_mock.py | live trading safety and control-plane integrity | Rejected | Diff-scoped file reviewed by parent/delegated security reviewers; no plausible candidate survived. Evidence: artifacts/02_discovery/work_ledger.jsonl, artifacts/02_discovery/finding_discovery_report.md |
| apps/worker/app/adapters/market_data/toss_market_data.py | live trading safety and control-plane integrity | Rejected | Diff-scoped file reviewed by parent/delegated security reviewers; no plausible candidate survived. Evidence: artifacts/02_discovery/work_ledger.jsonl, artifacts/02_discovery/finding_discovery_report.md |
| apps/worker/app/adapters/persistence/models.py | live trading safety and control-plane integrity | Rejected | Diff-scoped file reviewed by parent/delegated security reviewers; no plausible candidate survived. Evidence: artifacts/02_discovery/work_ledger.jsonl, artifacts/02_discovery/finding_discovery_report.md |
| apps/worker/app/adapters/persistence/sql_repository.py | live trading safety and control-plane integrity | Rejected | Diff-scoped file reviewed by parent/delegated security reviewers; no plausible candidate survived. Evidence: artifacts/02_discovery/work_ledger.jsonl, artifacts/02_discovery/finding_discovery_report.md |
| apps/worker/app/adapters/persistence/supabase_repository.py | live trading safety and control-plane integrity | Rejected | Diff-scoped file reviewed by parent/delegated security reviewers; no plausible candidate survived. Evidence: artifacts/02_discovery/work_ledger.jsonl, artifacts/02_discovery/finding_discovery_report.md |
| apps/worker/app/application/ports/repository_port.py | live trading safety and control-plane integrity | Rejected | Diff-scoped file reviewed by parent/delegated security reviewers; no plausible candidate survived. Evidence: artifacts/02_discovery/work_ledger.jsonl, artifacts/02_discovery/finding_discovery_report.md |
| apps/worker/app/application/services/feature_service.py | live trading safety and control-plane integrity | Rejected | Diff-scoped file reviewed by parent/delegated security reviewers; no plausible candidate survived. Evidence: artifacts/02_discovery/work_ledger.jsonl, artifacts/02_discovery/finding_discovery_report.md |
| apps/worker/app/application/services/portfolio_service.py | live trading safety and control-plane integrity | Rejected | Diff-scoped file reviewed by parent/delegated security reviewers; no plausible candidate survived. Evidence: artifacts/02_discovery/work_ledger.jsonl, artifacts/02_discovery/finding_discovery_report.md |
| apps/worker/app/application/services/risk_service.py | live trading safety and control-plane integrity | Rejected | Diff-scoped file reviewed by parent/delegated security reviewers; no plausible candidate survived. Evidence: artifacts/02_discovery/work_ledger.jsonl, artifacts/02_discovery/finding_discovery_report.md |
| apps/worker/app/application/use_cases/run_trading_cycle.py | live trading safety and control-plane integrity | Rejected | Diff-scoped file reviewed by parent/delegated security reviewers; no plausible candidate survived. Evidence: artifacts/02_discovery/work_ledger.jsonl, artifacts/02_discovery/finding_discovery_report.md |
| apps/worker/app/config.py | live trading safety and control-plane integrity | Rejected | Diff-scoped file reviewed by parent/delegated security reviewers; no plausible candidate survived. Evidence: artifacts/02_discovery/work_ledger.jsonl, artifacts/02_discovery/finding_discovery_report.md |
| apps/worker/app/container.py | live trading safety and control-plane integrity | Rejected | Diff-scoped file reviewed by parent/delegated security reviewers; no plausible candidate survived. Evidence: artifacts/02_discovery/work_ledger.jsonl, artifacts/02_discovery/finding_discovery_report.md |
| apps/worker/app/tools/run_live_recovery_drill_once.py | live trading safety and control-plane integrity | Rejected | Diff-scoped file reviewed by parent/delegated security reviewers; no plausible candidate survived. Evidence: artifacts/02_discovery/work_ledger.jsonl, artifacts/02_discovery/finding_discovery_report.md |

## Open Questions And Follow Up

- Have hosted Supabase verifier commands been run against real staging/production-readiness credentials?
  - Follow-up prompt: Run hosted Supabase live readiness and live enable flow verifiers with real hosted credentials.
- Have real provider, incident, and system-order-scope artifacts been published and byte-verified?
  - Follow-up prompt: Run provider lifecycle, incident ACK, and system-order-scope remote artifact verifiers.
