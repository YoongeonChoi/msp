# Security Review: msp

## Scope

The scan reviewed the canonical include paths and exclusions listed below.

- Scan mode: commit
- Target kind: git_diff
- Target ID: target_sha256_6a17fb5fefcf6e2a087b5e4b0965898ead17781578a3633b0b46681e1d9dd60a
- Revision range: 02e268a591feeb412c31df13ed9d35df36daa137...9d17a1b945adb557138b8fa14c2e3e014975a3ab
- Snapshot digest: codex-security-snapshot/v1:sha256:1d74df0bc5da366ec7aad16a4841552de3d91d1cb5319d4e849096130ccb54eb
- Inventory strategy: diff
- Included paths: apps/worker/app/application/services/trading_loop.py, apps/worker/app/main.py, apps/worker/app/application/use_cases/run_trading_cycle.py, apps/worker/app/application/services/execution_service.py, apps/worker/app/application/services/risk_service.py
- Excluded paths: none
- Runtime or test status: not recorded
- Artifacts reviewed: apps/worker/app/application/services/trading_loop.py, apps/worker/app/main.py, apps/worker/app/application/use_cases/run_trading_cycle.py, apps/worker/app/application/services/execution_service.py, apps/worker/app/application/services/risk_service.py, artifacts/01_context/threat_model.md, artifacts/02_discovery/deep_review_input.jsonl, artifacts/02_discovery/work_ledger.jsonl, artifacts/02_discovery/finding_discovery_report.md, artifacts/03_coverage/reviewed_surfaces.md
- Scan context: Diff scan of worker continuous KnownFailClosedError loop handling and directly supporting live-order safety boundaries. No runtime deployment proof is claimed.

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

Primary assets are Supabase service-role control plane, broker credentials, live order execution authority, operator approval/audit history, and hosted worker integrity. Trust boundaries include desktop publishable-key UI, worker-only secrets, Supabase RLS/RPC grants, external provider APIs, Render deployment, and retained release evidence. The scan focuses on preserving fail-closed live trading invariants and preventing broker/API/secret exposure.

## Findings

### No findings

No reportable findings survived the canonical discovery, validation, and reportability gates.

## Reviewed Surfaces

| Surface | Risk Area | Outcome | Notes |
| --- | --- | --- | --- |
| apps/worker/app/application/services/trading_loop.py | continuous worker fail-closed loop handling | No issue found | Full file reviewed; continuous mode catches only KnownFailClosedError, records warning details fail_closed=true and loop_continues=true, and leaves RUN_ONCE fail-fast semantics plus unexpected exception handling outside this loop catch. Evidence: artifacts/02_discovery/work_ledger.jsonl, artifacts/02_discovery/finding_discovery_report.md, artifacts/03_coverage/reviewed_surfaces.md |
| apps/worker/app/main.py | top-level fail-closed and unexpected-error handling | No issue found | Full file reviewed; main still records known fail-closed warnings for RUN_ONCE/operator smoke and records critical unexpected errors before re-raising. No secret or broker path is introduced. Evidence: artifacts/02_discovery/work_ledger.jsonl, artifacts/02_discovery/finding_discovery_report.md, artifacts/03_coverage/reviewed_surfaces.md |
| apps/worker/app/application/use_cases/run_trading_cycle.py | live order proposal gate sequence | No issue found | Full file reviewed; live mode still evaluates live risk, persists snapshots, skips disabled settings and pending reconciliation, and calls propose_live_order only after live feature readiness checks. Loop continuation does not alter this per-cycle gate order. Evidence: artifacts/02_discovery/work_ledger.jsonl, artifacts/02_discovery/finding_discovery_report.md, artifacts/03_coverage/reviewed_surfaces.md |
| apps/worker/app/application/services/execution_service.py | broker placement boundary and durable pending row | No issue found | Full file reviewed; broker.place_order remains reachable only after evaluate_live_order allows, duplicate/evidence checks pass, valid quote/quantity exist, and an unknown_requires_manual_check pending row is persisted. Known fail-closed broker errors remain critical and fail closed. Evidence: artifacts/02_discovery/work_ledger.jsonl, artifacts/02_discovery/finding_discovery_report.md, artifacts/03_coverage/reviewed_surfaces.md |
| apps/worker/app/application/services/risk_service.py | live RiskService policy aggregation | No issue found | Full file reviewed; live policy list still includes bot enabled, live permission, market open, account sync, provider health, daily count, max order, duplicate, and other fail-closed checks. The loop change does not bypass policy aggregation. Evidence: artifacts/02_discovery/work_ledger.jsonl, artifacts/02_discovery/finding_discovery_report.md, artifacts/03_coverage/reviewed_surfaces.md |

## Open Questions And Follow Up

- Has the pushed worker change been deployed and observed on Render with a matching release_sha heartbeat?
  - Follow-up prompt: Set RENDER_DEPLOY_HOOK_URL or deploy manually in the Render dashboard, then run verify_worker_release_freshness until it returns FINAL=PASS for 9d17a1b945ad.
