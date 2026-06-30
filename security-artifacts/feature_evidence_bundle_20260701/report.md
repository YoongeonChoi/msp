# Security Review: msp

## Scope

The scan reviewed the canonical include paths and exclusions listed below.

- Scan mode: working_tree
- Target kind: git_diff
- Target ID: target_sha256_6a17fb5fefcf6e2a087b5e4b0965898ead17781578a3633b0b46681e1d9dd60a
- Revision range: 0b2a0e1631b31b4f93eea333aa70d893acacfb82...0b2a0e1631b31b4f93eea333aa70d893acacfb82
- Snapshot digest: codex-security-snapshot/v1:sha256:7db085ec68f65b3dae578f8c7b7b6c0e5061a8de227872a94e2b5e82b6c1f3b8
- Inventory strategy: diff
- Included paths: apps/worker/app/tools/verify_live_readiness_evidence_bundle.py, apps/worker/app/tools/collect_live_readiness_evidence_bundle.py, apps/worker/app/tests/unit/test_live_readiness_evidence_bundle.py, apps/worker/app/tests/unit/test_live_readiness_evidence_collector.py, docs/LIVE_READINESS_SCORECARD.md, docs/LIVE_READINESS_WORK_SUMMARY.md, docs/RENDER_DEPLOYMENT.md, docs/RUNBOOK.md
- Excluded paths: none
- Runtime or test status: not recorded
- Artifacts reviewed: apps/worker/app/tools/verify_live_readiness_evidence_bundle.py, apps/worker/app/tools/collect_live_readiness_evidence_bundle.py, apps/worker/app/tests/unit/test_live_readiness_evidence_bundle.py, apps/worker/app/tests/unit/test_live_readiness_evidence_collector.py, docs/LIVE_READINESS_SCORECARD.md, docs/LIVE_READINESS_WORK_SUMMARY.md, docs/RENDER_DEPLOYMENT.md, docs/RUNBOOK.md, artifacts/01_context/threat_model.md, artifacts/02_discovery/deep_review_input.jsonl, artifacts/02_discovery/work_ledger.jsonl, artifacts/02_discovery/finding_discovery_report.md, artifacts/03_coverage/reviewed_surfaces.md
- Scan context: Diff scan of provider-live feature_evidence release-bundle gating, collector input handling, tests, and runbooks. No runtime deployment proof is claimed.

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

Primary assets are live order execution authority, worker-only provider/broker secrets, retained release evidence integrity, Supabase control plane, and operator approval history. Trust boundaries include desktop UI, worker-only broker paths, retained evidence manifests, remote artifact byte verification, and Render deployment.

## Findings

### No findings

No reportable findings survived the canonical discovery, validation, and reportability gates.

## Reviewed Surfaces

| Surface | Risk Area | Outcome | Notes |
| --- | --- | --- | --- |
| apps/worker/app/tools/verify_live_readiness_evidence_bundle.py | release bundle evidence validation and remote artifact byte-proof | No issue found | Reviewed feature_evidence schema enforcement, provider_live_v1/live_trading_ready requirements, symbol/provider/artifact validation, retained HTTPS URI restrictions, SHA-256 checks, bundle-level retained URI/SHA uniqueness, CLI flags, and remote artifact byte verification. No path bypass, secret echo, or live-order boundary change found. Evidence: artifacts/02_discovery/work_ledger.jsonl, artifacts/02_discovery/finding_discovery_report.md, artifacts/03_coverage/reviewed_surfaces.md |
| apps/worker/app/tools/collect_live_readiness_evidence_bundle.py | release evidence collector input boundary | No issue found | Reviewed required --feature-evidence input, distinct artifact path handling, sensitive-key scan, bundle embedding, source-binding exclusion, and collector PASS output. Collector only reads redacted evidence manifests and does not add broker/API calls. Evidence: artifacts/02_discovery/work_ledger.jsonl, artifacts/02_discovery/finding_discovery_report.md, artifacts/03_coverage/reviewed_surfaces.md |
| apps/worker/app/tests/unit/test_live_readiness_evidence_bundle.py | bundle gate regression coverage | No issue found | Reviewed tests for required feature evidence, unready/mock source rejection, incomplete artifact rejection, remote feature artifact SHA mismatch scoping, and updated PASS output flags. Tests avoid secret-bearing payloads and assert redacted failure reasons. Evidence: artifacts/02_discovery/work_ledger.jsonl, artifacts/02_discovery/finding_discovery_report.md, artifacts/03_coverage/reviewed_surfaces.md |
| apps/worker/app/tests/unit/test_live_readiness_evidence_collector.py | collector regression coverage | No issue found | Reviewed collector tests for required feature evidence, sensitive-key rejection, unready feature evidence rejection, source hash exclusions, and updated fixtures. No live provider or broker interaction is introduced by tests. Evidence: artifacts/02_discovery/work_ledger.jsonl, artifacts/02_discovery/finding_discovery_report.md, artifacts/03_coverage/reviewed_surfaces.md |
| docs/LIVE_READINESS_SCORECARD.md | release readiness documentation | No issue found | Reviewed scorecard wording for remote feature artifact flags and remaining real hosted/provider evidence gaps. Documentation does not claim runtime deployment or feature artifact publication. Evidence: artifacts/02_discovery/work_ledger.jsonl, artifacts/02_discovery/finding_discovery_report.md, artifacts/03_coverage/reviewed_surfaces.md |
| docs/LIVE_READINESS_WORK_SUMMARY.md | work summary evidence accounting | No issue found | Reviewed summary addition for feature evidence gate and local verification counts. No secrets, endpoints, or credentials are recorded. Evidence: artifacts/02_discovery/work_ledger.jsonl, artifacts/02_discovery/finding_discovery_report.md, artifacts/03_coverage/reviewed_surfaces.md |
| docs/RENDER_DEPLOYMENT.md | deployment runbook command accuracy | No issue found | Reviewed Render deployment command examples for --feature-evidence and --verify-remote-feature-artifacts, plus retained evidence uniqueness wording. No fake live order execution or Render secret is added. Evidence: artifacts/02_discovery/work_ledger.jsonl, artifacts/02_discovery/finding_discovery_report.md, artifacts/03_coverage/reviewed_surfaces.md |
| docs/RUNBOOK.md | operator runbook command accuracy | No issue found | Reviewed Linux/Windows collector/verifier commands and expected PASS lines for feature_evidence and remote_feature_artifacts. Runbook preserves real evidence and human-review requirements. Evidence: artifacts/02_discovery/work_ledger.jsonl, artifacts/02_discovery/finding_discovery_report.md, artifacts/03_coverage/reviewed_surfaces.md |

## Open Questions And Follow Up

- Has this working-tree change been committed, pushed, and redeployed to Render with a matching release_sha heartbeat?
  - Follow-up prompt: After commit/push and manual or hook-triggered Render deploy, run verify_worker_release_freshness until it returns FINAL=PASS for the final commit.
