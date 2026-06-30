# Live Readiness Work Summary

Generated: 2026-06-29 KST

This document summarizes the live-readiness hardening work completed in this
checkpoint. It is a handoff summary for commit review, not a live-trading
approval. The authoritative readiness score remains
`docs/LIVE_READINESS_SCORECARD.md`.

## 2026-06-30 Render Worker Follow-Up

- A local Render-equivalent smoke run with `ENV=production`,
  `MOCK_PROVIDERS=false`, and `RUN_ONCE=true` reproduced the hosted-worker
  startup failure against the configured hosted Supabase and provider keys.
- The first crash was a PostgREST `400` because the hosted
  `strategy_versions` schema did not yet expose the later `version` column.
  `SupabaseRepository.load_active_strategy_version()` now prefers the base
  `version_name` path, falls back to the active/paper row, and only probes the
  newer `version` column after those schema-compatible paths are exhausted.
- The next crash was a PostgREST `400` because the hosted
  `decision_snapshots` schema cache did not yet expose `decided_at`.
  `decision_to_row()` now writes the base `created_at` decision-time column,
  while read paths continue to tolerate `decided_at` when aligned migrations are
  present.
- The redacted Render-equivalent smoke now exits `0` and records heartbeat,
  health, decision snapshot, and feature observations without broker order
  placement. `render.yaml` still keeps `autoDeployTrigger: "off"`, so pushing
  this fix does not deploy it until an operator manually deploys Render.
- Hosted Supabase should still be migrated through
  `supabase/migrations/0005_schema_alignment.sql`; the runtime compatibility
  changes keep the worker alive on the base schema but do not replace migration
  verification.

## Current Status

The repository is materially stronger than the original MVP, but it is still
not approved for unattended live automatic trading. Local code paths, release
gates, evidence verifiers, desktop controls, and runbooks are now much stricter.
The remaining blockers are external evidence items that must be produced from
real hosted Supabase, real Toss sandbox/live lifecycle drills, real incident
channel ACK drills, and published retained artifacts.

## Worker Safety And Execution

- Live order creation remains owned by `ExecutionService`; desktop UI and AI
  output do not call broker order APIs directly.
- Live order decisions still pass through `RiskService` and fail closed when
  evidence is missing, stale, mocked, or provider-backed features are not ready.
- Allowed live order creation records a durable pre-broker manual-check row
  before calling `BrokerPort.place_order`, then updates the same row when the
  provider result is known.
- Duplicate retries are blocked by the idempotency key path, including crash,
  timeout, and unknown-result cases.
- Live cycles block new decisions when pending or
  `unknown_requires_manual_check` live orders remain after reconciliation.
- Manual-check reconciliation now preserves operator recovery requirements
  instead of automatically clearing an unknown order to a terminal state.
- Manual live cancellation confirms provider `CANCELED` before marking local
  orders `canceled`; uncertain cancel results stay in manual-check recovery.
- Scheduled live operation now requires explicit system-originated-order scope
  acceptance while Toss broker-wide closed-order history is unavailable.

## Provider And Feature Readiness

- Toss account, buying power, holdings, market data, order create, cancel, and
  status contracts are typed and tested.
- Naver Search News and OpenDART adapters now provide read-only typed evidence
  for provider-backed live features.
- OpenDART `CORPCODE.xml` ZIP/XML parsing rejects oversized data, DTD/entity
  declarations, and unsafe archive shapes before XML parsing.
- OpenAI structured-output usage now has a model allowlist and strict schema
  requirements so AI output remains advisory and cannot promote live execution.
- Live mode now requires `provider_live_v1` feature snapshots built from real
  provider evidence; mock/static paper features stay non-live-ready.

## Supabase Control Plane

- `0009_live_operations_hardening.sql` adds live-enable hardening around fresh
  evidence, distinct reviewer identity, immutable review evidence, audit
  triggers, RLS admin checks, and one-time accepted-to-applied consumption.
- `0010_security_definer_hardening.sql` hardens `SECURITY DEFINER` RPCs with
  explicit `search_path` and execute grants.
- `0011_data_api_grants.sql` makes Data API exposure explicit: public/anon
  grants are revoked, authenticated desktop access is RLS-backed, and worker
  `service_role` grants are explicit.
- Hosted Supabase verifiers now require PostgREST, RPC denial/allowance,
  Data API denial/allowance, two distinct admin JWT sessions, RLS-backed admin
  role reads, and Realtime handshake proof.
- Hosted live-enable flow verification requires requester/reviewer separation,
  self-review denial, accepted review, one-time activation consumption, and
  second-activation denial.

## Desktop Cockpit

- The desktop control page now exposes the live request, review, and activation
  path without storing broker secrets or directly calling broker APIs.
- Live activation stays disabled unless a fresh approval is present and the
  user confirmation phrase is valid.
- Dashboard data loading uses worker-compatible schema paths and keeps critical
  events visible in render fixtures.
- Desktop unit and browser tests cover approval helper logic, control page
  query/mutation states, dashboard rendering, schema compatibility, and a
  mocked Supabase live-gate flow across desktop and mobile viewports.

## Evidence And Release Gates

- `docs/LIVE_READINESS_SCORECARD.md` now records strict category scores and
  separates local proof from missing external proof.
- `apps/worker/app/tools/verify_live_readiness_scorecard.py` validates the
  scorecard against the retained security scan summary and rejects stale
  runbook PASS examples.
- `collect_live_readiness_evidence_bundle` and
  `verify_live_readiness_evidence_bundle` now require local gates, hosted
  Supabase proof, provider lifecycle proof, incident ACK proof, system-order
  scope proof, provider-gap evidence, and independent security replay evidence.
- Provider lifecycle, incident response, system-order-scope, provider-gap, and
  security scan evidence each have standalone verifiers with redaction,
  retained-artifact URI, SHA-256, timestamp, identity, and remote-byte-proof
  checks.
- Final bundle verification rejects weak, duplicate, unknown, local-only, stale,
  or side-channel-contaminated evidence lines even when they start with
  `FINAL=PASS`.
- `docs/RUNBOOK.md` now includes copyable gate commands and expected PASS line
  shapes for operators, while keeping `SKIP` and `FAIL` states release-blocking.

## Security Work

- The current retained Codex Security scan is
  `83add88_20260630113328`.
- The retained report is `security-artifacts/83add88_20260630113328/report.md`.
- The scan summary records 16 worklist rows, 16 completion receipts,
  0 promoted candidates, 0 validation receipts, 0 attack-path receipts, and
  0 surviving reportable findings.
- The delta scan covers the Render worker startup compatibility update,
  desktop dashboard/signal rendering, Toss holdings and market-data reads,
  Supabase persistence/schema fallback, feature/portfolio sync, risk policy
  scoping, run-cycle live-order gates, container/config wiring, and the
  recovery drill. The earlier `fb223a4_20260628182340` scan remains retained
  under `security-artifacts/` as broader historical baseline evidence.

## Verification Snapshot

The latest local verification recorded before this handoff included:

- `py -m pytest -q --tb=short` from `apps/worker`: `499 passed`
- `py -m ruff check .` from `apps/worker`: passed
- `py -m mypy .` from `apps/worker`: passed
- `npm run desktop:typecheck`: passed
- `npm run desktop:build`: passed
- `npm run desktop:test`: passed
- `npm run desktop:e2e`: `2 passed`
- Browser QA for `http://localhost:1420/?page=dashboard`: dashboard/control
  navigation rendered with no relevant console warnings or errors
- Scorecard/security evidence gates:
  `FINAL=PASS security_scan_evidence ... worklist_rows=16 ... candidate_findings=0`
  and
  `FINAL=PASS live_readiness_scorecard scorecard_security_scan=1 worklist_rows=16 candidate_findings=0 reportable_findings=0`

Run verification again after any source edit, commit publication, or external
evidence update.

## Remaining External Blockers

- Hosted Supabase/staging must return exact counted PASS lines for
  `hosted_supabase_live_readiness` and `hosted_live_enable_flow` using real
  Auth/RLS/PostgREST/Data API/Realtimes settings and two distinct admin users.
- Toss sandbox/live provider lifecycle evidence must prove create, status
  observations, cancel confirmation, unknown recovery, retained artifacts, and
  human audit review.
- A real incident channel ACK drill must prove delivery, latency, human
  acknowledgment, retained channel artifact, and remote SHA-256 byte match.
- System-originated-order scope acceptance must be exercised in a deployed
  environment, or Toss broker-wide closed-order history must be proven.
- Security and release artifacts must be published and re-verified through
  remote byte-fetch gates after push.
- The final live-readiness evidence bundle must pass with
  `remote_provider_artifacts=1`, `remote_incident_evidence=1`, and
  `remote_system_order_scope_evidence=1`.

Until those blockers are closed with retained external evidence, live operation
must remain disabled.
