# Security Review: msp

## Scope

The scan reviewed the canonical include paths and exclusions listed below.

- Scan mode: working_tree
- Target kind: git_worktree
- Target ID: target_sha256_a91b758d8f35da1cd383d6add32bf3f831b18577
- Revision: 1eefb6e11d847e897d0c91ee767786be32e259cc
- Snapshot digest: codex-security-snapshot/v1:sha256:a91b758d8f35da1cd383d6add32bf3f831b18577fcdda31176612eaefca90de6
- Inventory strategy: diff
- Included paths: apps/worker/app/application/services/paper_health_models.py, apps/worker/app/application/services/paper_health_report_service.py, apps/worker/app/application/use_cases/run_trading_cycle.py, apps/worker/app/infrastructure/release_metadata.py, apps/worker/app/tools/paper_health_report.py, apps/worker/app/tools/write_release_metadata.py, render.yaml
- Excluded paths: none
- Runtime or test status: Focused worker tests passed; full worker pytest suite passed; ruff and mypy passed; safe MOCK_PROVIDERS one-shot worker smoke passed; no live orders placed.
- Artifacts reviewed: apps/worker/app/application/services/paper_health_models.py, apps/worker/app/application/services/paper_health_report_service.py, apps/worker/app/application/use_cases/run_trading_cycle.py, apps/worker/app/infrastructure/release_metadata.py, apps/worker/app/tools/paper_health_report.py, apps/worker/app/tools/write_release_metadata.py, render.yaml, artifacts/02_discovery/deep_review_input.jsonl, artifacts/02_discovery/work_ledger.jsonl, artifacts/02_discovery/finding_discovery_report.md, artifacts/03_coverage/reviewed_surfaces.md
- Scan context: Safety-first Korean stock auto-trading worker. This diff adds deploy revision observability to worker heartbeats and paper health reports without changing live order execution gates.

Limitations and exclusions:
- No Render redeploy was performed by this local scan.
- No live broker orders were placed.
- Hosted Supabase heartbeat release_sha observation still requires manual Render deploy after push.

### Scan Summary

| Field | Value |
| --- | --- |
| Reportable findings | 0 |
| Severity mix | none |
| Confidence mix | none |
| Coverage | complete |
| Validation mode | Parent-agent diff review with deterministic 7-row worklist and completion receipts; no candidates reached validation. |

Canonical artifacts: `scan-manifest.json`, `findings.json`, and `coverage.json`. This report is a deterministic projection of those files.

## Threat Model

Assets include broker credentials, accountSeq selection, Supabase service-role access, worker heartbeat/release metadata, live-order controls, order state, and release evidence artifacts.

### Assets

- Toss credentials and accountSeq
- Supabase service-role persistence
- worker heartbeat release metadata
- live order execution gate
- release evidence artifacts

### Trust Boundaries

- Render env vars to worker-only provider adapters
- Render build environment to generated release_metadata.json
- Worker persistence to Supabase service-role PostgREST
- Desktop cockpit to RLS-backed Supabase tables

### Attacker Capabilities

- Misconfigure release marker environment values
- Read RLS-authorized operational diagnostics from the cockpit
- Trigger scheduled worker cycles through configured runtime state

### Security Objectives

- Release markers must not persist secrets or arbitrary unbounded strings
- Any provider uncertainty fails closed and cannot enable live broker calls
- Only ExecutionService may call BrokerPort.place_order after RiskService approval
- UI code must not call broker/order APIs directly

### Assumptions

- Live order execution remains disabled unless explicit live gates and external evidence are satisfied
- APP_RELEASE_SHA and generated release metadata are deployment provenance markers, not authorization controls

## Findings

### No findings

No reportable findings survived the canonical discovery, validation, and reportability gates.

## Reviewed Surfaces

| Surface | Risk Area | Outcome | Notes |
| --- | --- | --- | --- |
| apps/worker/app/application/services/paper_health_models.py | paper health release metadata model | No issue found | Reviewed dataclass/report event details changes. The new fields are passive audit metadata only; they do not affect health PASS/WARN/FAIL decisions and carry only bounded strings supplied by heartbeat details. No broker/order permission path is changed. Evidence: artifacts/01_context/threat_model.md, artifacts/02_discovery/rank_input.jsonl, artifacts/02_discovery/deep_review_input.jsonl, artifacts/02_discovery/work_ledger.jsonl, artifacts/02_discovery/finding_discovery_report.md, artifacts/03_coverage/reviewed_surfaces.md, artifacts/04_reconciliation/dedupe_report.md, artifacts/05_findings/validation_summary.md, artifacts/05_findings/attack_path_analysis_report.md |
| apps/worker/app/application/services/paper_health_report_service.py | heartbeat details parsing for release metadata | No issue found | Reviewed extraction from worker_heartbeats.details. Only release_sha and release_source are read, only string values are accepted, and values are truncated to 80 characters before the report model. The metadata is not passed into findings/risk logic. Evidence: artifacts/01_context/threat_model.md, artifacts/02_discovery/rank_input.jsonl, artifacts/02_discovery/deep_review_input.jsonl, artifacts/02_discovery/work_ledger.jsonl, artifacts/02_discovery/finding_discovery_report.md, artifacts/03_coverage/reviewed_surfaces.md, artifacts/04_reconciliation/dedupe_report.md, artifacts/05_findings/validation_summary.md, artifacts/05_findings/attack_path_analysis_report.md |
| apps/worker/app/application/use_cases/run_trading_cycle.py | heartbeat persistence in trading cycle | No issue found | Reviewed heartbeat write path. The change replaces a literal cycle_id details object with worker_heartbeat_details(cycle_id); live reconciliation, RiskService evaluation, ExecutionService proposal, and BrokerPort boundaries are unchanged. Evidence: artifacts/01_context/threat_model.md, artifacts/02_discovery/rank_input.jsonl, artifacts/02_discovery/deep_review_input.jsonl, artifacts/02_discovery/work_ledger.jsonl, artifacts/02_discovery/finding_discovery_report.md, artifacts/03_coverage/reviewed_surfaces.md, artifacts/04_reconciliation/dedupe_report.md, artifacts/05_findings/validation_summary.md, artifacts/05_findings/attack_path_analysis_report.md |
| apps/worker/app/infrastructure/release_metadata.py | release metadata sanitization | No issue found | Reviewed env/file metadata loader. It accepts only 7-64 character hex SHA values from release-specific env names or generated metadata file, lowercases them, exposes a 12-character short SHA, and rejects non-uppercase source labels from the metadata file. Secret-like non-SHA values are not persisted. Evidence: artifacts/01_context/threat_model.md, artifacts/02_discovery/rank_input.jsonl, artifacts/02_discovery/deep_review_input.jsonl, artifacts/02_discovery/work_ledger.jsonl, artifacts/02_discovery/finding_discovery_report.md, artifacts/03_coverage/reviewed_surfaces.md, artifacts/04_reconciliation/dedupe_report.md, artifacts/05_findings/validation_summary.md, artifacts/05_findings/attack_path_analysis_report.md |
| apps/worker/app/tools/paper_health_report.py | paper health report release output | No issue found | Reviewed CLI formatting. release_sha/release_source pass through existing safe text handling, including secret-marker redaction and output length limits, and no service-role credentials or provider tokens are printed. Evidence: artifacts/01_context/threat_model.md, artifacts/02_discovery/rank_input.jsonl, artifacts/02_discovery/deep_review_input.jsonl, artifacts/02_discovery/work_ledger.jsonl, artifacts/02_discovery/finding_discovery_report.md, artifacts/03_coverage/reviewed_surfaces.md, artifacts/04_reconciliation/dedupe_report.md, artifacts/05_findings/validation_summary.md, artifacts/05_findings/attack_path_analysis_report.md |
| apps/worker/app/tools/write_release_metadata.py | Render build release metadata writer | No issue found | Reviewed build-time writer. It invokes git with an argument tuple, not shell interpolation; writes only generated_at, release_source, and a sanitized commit SHA; and falls back to GIT_REV_PARSE_UNAVAILABLE when git output is absent or unsafe. Evidence: artifacts/01_context/threat_model.md, artifacts/02_discovery/rank_input.jsonl, artifacts/02_discovery/deep_review_input.jsonl, artifacts/02_discovery/work_ledger.jsonl, artifacts/02_discovery/finding_discovery_report.md, artifacts/03_coverage/reviewed_surfaces.md, artifacts/04_reconciliation/dedupe_report.md, artifacts/05_findings/validation_summary.md, artifacts/05_findings/attack_path_analysis_report.md |
| render.yaml | Render build command release marker | No issue found | Reviewed worker build command. The added step runs the stdlib-only metadata writer before dependency install and leaves autoDeployTrigger off; no secrets or live-order controls are added to Render config. Evidence: artifacts/01_context/threat_model.md, artifacts/02_discovery/rank_input.jsonl, artifacts/02_discovery/deep_review_input.jsonl, artifacts/02_discovery/work_ledger.jsonl, artifacts/02_discovery/finding_discovery_report.md, artifacts/03_coverage/reviewed_surfaces.md, artifacts/04_reconciliation/dedupe_report.md, artifacts/05_findings/validation_summary.md, artifacts/05_findings/attack_path_analysis_report.md |

## Open Questions And Follow Up

- Has the pushed worker revision been manually redeployed on Render and observed in hosted Supabase heartbeat release_sha?
  - Follow-up prompt: After push, trigger Render manual deploy and verify public.worker_heartbeats.details.release_sha or release_sha_short matches the pushed commit.
- Do hosted provider-health rows still show any degraded provider after redeploy?
  - Follow-up prompt: Retain hosted Supabase provider-health evidence and keep live_order_allowed=false until degraded providers and remaining external evidence blockers are resolved.
