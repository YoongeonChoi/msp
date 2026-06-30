# Security Review: msp

## Scope

The scan reviewed the canonical include paths and exclusions listed below.

- Scan mode: working_tree
- Target kind: git_worktree
- Target ID: target_sha256_c5e6997502d8e2c5afba28f51f58e686
- Revision: 976ced92783109527decb1675d17d9b1526f2d52
- Snapshot digest: codex-security-snapshot/v1:sha256:c5e6997502d8e2c5afba28f51f58e686b61edda2b3c5f5ba310ffb6736e3a190
- Inventory strategy: diff
- Included paths: apps/worker/app/application/services/paper_health_models.py, apps/worker/app/application/services/paper_health_row_parsing.py, apps/worker/app/tools/paper_health_report.py
- Excluded paths: none
- Runtime or test status: Focused paper health report tests passed; full worker pytest suite, ruff, and mypy passed before final commit. paper_health_report now shows safe provider failure details and still redacts secret-like text.
- Artifacts reviewed: apps/worker/app/application/services/paper_health_models.py, apps/worker/app/application/services/paper_health_row_parsing.py, apps/worker/app/tools/paper_health_report.py, apps/worker/app/tests/unit/test_paper_health_report.py, artifacts/02_discovery/deep_review_input.jsonl, artifacts/02_discovery/work_ledger.jsonl, artifacts/02_discovery/finding_discovery_report.md, artifacts/03_coverage/reviewed_surfaces.md
- Scan context: Safety-first Korean stock auto-trading worker. This diff surfaces provider health failure details in the paper health report without changing live order execution gates.

Limitations and exclusions:
- No live broker orders were placed.
- Render CLI/MCP logs were unavailable in this local environment.
- Hosted Toss providers remain externally blocked by ProviderAuthError/toss_access_denied.

### Scan Summary

| Field | Value |
| --- | --- |
| Reportable findings | 0 |
| Severity mix | none |
| Confidence mix | none |
| Coverage | complete |
| Validation mode | Parent-agent diff review with deterministic 3-row worklist and completion receipts; no candidates reached validation. |

Canonical artifacts: `scan-manifest.json`, `findings.json`, and `coverage.json`. This report is a deterministic projection of those files.

## Threat Model

Assets include broker credentials, Supabase service-role access, worker/provider health diagnostics, live-order controls, and retained release evidence artifacts.

### Assets

- Toss credentials and accountSeq
- Supabase service-role persistence
- provider health diagnostics
- live order execution gate
- release evidence artifacts

### Trust Boundaries

- Render env vars to worker-only provider adapters
- Worker persistence to Supabase service-role PostgREST
- Desktop cockpit to RLS-backed Supabase tables
- Operator CLI output boundary for diagnostics

### Attacker Capabilities

- Influence provider health detail content through failed provider responses
- Read RLS-authorized operational diagnostics from the cockpit
- Trigger scheduled worker cycles through configured runtime state

### Security Objectives

- Provider diagnostics must not leak credentials or arbitrary unbounded strings
- Any provider uncertainty fails closed and cannot enable live broker calls
- Only ExecutionService may call BrokerPort.place_order after RiskService approval
- UI code must not call broker/order APIs directly

### Assumptions

- Live order execution remains disabled unless explicit live gates and external evidence are satisfied
- Provider health details are diagnostics only and are not authorization controls

## Findings

### No findings

No reportable findings survived the canonical discovery, validation, and reportability gates.

## Reviewed Surfaces

| Surface | Risk Area | Outcome | Notes |
| --- | --- | --- | --- |
| apps/worker/app/application/services/paper_health_models.py | paper health provider model | No issue found | Reviewed ProviderHealthSummary model expansion. The new detail_summary field is passive diagnostic metadata and does not affect PASS/WARN/FAIL decisions, risk gates, order placement, or broker boundaries. Evidence: artifacts/01_context/threat_model.md, artifacts/02_discovery/rank_input.jsonl, artifacts/02_discovery/deep_review_input.jsonl, artifacts/02_discovery/work_ledger.jsonl, artifacts/02_discovery/finding_discovery_report.md, artifacts/03_coverage/reviewed_surfaces.md, artifacts/04_reconciliation/dedupe_report.md, artifacts/05_findings/validation_summary.md, artifacts/05_findings/attack_path_analysis_report.md |
| apps/worker/app/application/services/paper_health_row_parsing.py | provider health detail parsing and allowlisting | No issue found | Reviewed api_health.details parsing. Only a fixed allowlist of provider failure keys is read, only strings and non-boolean numeric values are accepted, string values are single-line normalized and truncated to 160 characters, and unknown detail keys are ignored. Evidence: artifacts/01_context/threat_model.md, artifacts/02_discovery/rank_input.jsonl, artifacts/02_discovery/deep_review_input.jsonl, artifacts/02_discovery/work_ledger.jsonl, artifacts/02_discovery/finding_discovery_report.md, artifacts/03_coverage/reviewed_surfaces.md, artifacts/04_reconciliation/dedupe_report.md, artifacts/05_findings/validation_summary.md, artifacts/05_findings/attack_path_analysis_report.md |
| apps/worker/app/tools/paper_health_report.py | paper health report provider detail output | No issue found | Reviewed CLI provider formatting. The detail summary is appended only after passing through the existing _safe_text redaction path, so secret-like tokens are redacted before operator output. Evidence: artifacts/01_context/threat_model.md, artifacts/02_discovery/rank_input.jsonl, artifacts/02_discovery/deep_review_input.jsonl, artifacts/02_discovery/work_ledger.jsonl, artifacts/02_discovery/finding_discovery_report.md, artifacts/03_coverage/reviewed_surfaces.md, artifacts/04_reconciliation/dedupe_report.md, artifacts/05_findings/validation_summary.md, artifacts/05_findings/attack_path_analysis_report.md |

## Open Questions And Follow Up

- Do hosted provider-health rows still show Toss access denied after Render environment credentials and permissions are corrected?
  - Follow-up prompt: Run paper_health_report against hosted Supabase after Toss credential rotation and confirm provider detail output no longer reports ProviderAuthError/toss_access_denied.
- Do retained remote report bytes still match the final security_scan_summary.report_sha256 after push?
  - Follow-up prompt: After pushing this commit, run verify_security_scan_evidence with --verify-remote-report-uri against the final security_scan_summary.json.
