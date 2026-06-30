# Live Readiness Work Summary

Generated: 2026-06-30 KST

This document summarizes the live-readiness hardening work completed in this
checkpoint. It is a handoff summary for commit review, not a live-trading
approval. The authoritative readiness score remains
`docs/LIVE_READINESS_SCORECARD.md`.

## 2026-07-01 Continuous Worker Fail-Closed Loop

- `TradingLoop` now handles `KnownFailClosedError` inside continuous mode instead
  of letting a single expected provider/setup fail-closed condition terminate the
  Render background worker process.
- The loop records a warning `engine_events` row with `fail_closed=true` and
  `loop_continues=true`, then proceeds to the next interval. This keeps
  heartbeat/health observability alive while preserving fail-closed trading
  behavior.
- `RUN_ONCE=true` remains strict for operator smoke checks: known fail-closed
  errors still propagate to `main`, which records the existing
  `known_fail_closed` warning and exits cleanly for deploy validation.
- Focused verification:
  `py -m pytest app/tests/unit/test_trading_loop.py app/tests/integration/test_trading_cycle.py -q`
  returned `25 passed`; `ruff` and `mypy` passed for the changed files.
- Hosted Render was still observed on the older `release_sha=976ced927831...`
  before this change was deployed. Because auto deploy remains off, this code
  still requires a manual Render deploy hook or dashboard deploy plus
  `verify_worker_release_freshness` PASS before hosted freshness can be claimed.

## 2026-07-01 Render Redeploy Freshness Gate

- Added `app.tools.redeploy_render_worker`, an operator command that requires
  `--yes`, reads `RENDER_DEPLOY_HOOK_URL` only from the operator shell or
  `--hook-url`, triggers the Render deploy hook with `ref=<current HEAD>`, and
  polls hosted `worker_heartbeats` until `release_sha` matches the expected
  commit.
- The command refuses to trigger if hosted Supabase verification env is missing,
  so a hook HTTP 200 cannot be mistaken for a completed deploy. Output is limited
  to short SHA values, status code, heartbeat age, attempt count, and bounded
  `FINAL` reasons; it never prints the hook URL, token, response body, or full
  commit hash.
- Focused verification:
  `py -m pytest app/tests/unit/test_render_worker_redeploy.py app/tests/unit/test_render_deploy_hook.py app/tests/unit/test_worker_release_freshness.py -q`
  returned `21 passed`; `ruff` and `mypy` passed for the new tool and tests.

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
- Worker heartbeats now carry safe release metadata (`release_sha`,
  `release_sha_short`, and `release_source`) from `APP_RELEASE_SHA` or the
  build-generated `app/release_metadata.json`, so hosted Supabase can prove
  which pushed commit Render is actually running after a manual deploy.
- Hosted Supabase should still be migrated through
  `supabase/migrations/0005_schema_alignment.sql`; the runtime compatibility
  changes keep the worker alive on the base schema but do not replace migration
  verification.

## 2026-06-30 Hosted Paper Health Diagnostics Follow-Up

- Hosted Supabase paper health was checked with the server-side worker env. The
  worker heartbeat was fresh at the time of the check, carried
  `release_sha=976ced92783109527decb1675d17d9b1526f2d52`, and the safety flags
  remained disabled (`mode=paper`, `live_order_allowed=false`).
- The report still returned `FINAL=FAIL` because hosted data contained repeated
  operational `worker` critical events with `error_type='HTTPStatusError'` in
  the 24-hour window, and `toss` plus `toss_market_data` provider health was
  degraded.
- The latest hosted `api_health` rows for the Toss providers still had
  safe details showing `error_type=ProviderAuthError` and
  `reason=toss_access_denied`. `paper_health_report` now prints those
  allowlisted provider detail fields on degraded provider lines while still
  passing the output through secret redaction.
- The report continues to exclude its own `component='paper_ops'`,
  `message='paper_health_report'` diagnostic events from the
  repeated-critical-event count and from the rendered recent critical event
  list, preventing self-sustaining FAIL reports and keeping the triage list
  focused on worker/provider events.
  Operational critical events from `worker` and provider components still count
  and still block.
- Render was observed running the previously pushed release metadata commit.
  Because `render.yaml` keeps `autoDeployTrigger: "off"`, this new commit still
  requires a manual Render redeploy before hosted heartbeats can show the new
  `release_sha`.

## 2026-06-30 Worker Release Freshness Gate

- Added `app.tools.verify_worker_release_freshness`, a read-only hosted
  Supabase verifier that checks the latest `worker_heartbeats` row and compares
  `details.release_sha` with the current Git `HEAD`.
- The verifier prints one redacted `FINAL` line with only short commit prefixes,
  `heartbeat_age_sec`, and `max_age_sec`; missing Supabase env returns
  `FINAL=SKIP`, while missing, mismatched, stale, or future heartbeats return
  `FINAL=FAIL`.
- `collect_live_readiness_evidence_bundle` now runs this gate before the other
  local checks, and `verify_live_readiness_evidence_bundle` rejects bundle
  output where the observed short SHA differs from the expected short SHA, the
  heartbeat is older than the configured max age, or unknown metrics are added.
- A hosted check before this commit returned a release mismatch: the local
  expected short SHA was `2bac8362b504`, while hosted Render still reported
  `976ced927831`. That is useful negative evidence: the worker was running, but
  it was not proven to be running the latest pushed code.

## 2026-06-30 Hosted Supabase Env-File Verifier Follow-Up

- Added `supabase/_hosted_env.py`, a small helper for explicit operator
  `--env-file` loading in hosted Supabase verifier scripts.
- `supabase/verify_hosted_live_readiness.py` and
  `supabase/verify_hosted_live_enable_flow.py` now accept repeated `--env-file`
  arguments so ignored local worker/desktop env files can be merged without
  copying secrets into the shell.
- Precedence stays conservative: later env files override earlier env files,
  process environment overrides env-file values, and explicit CLI flags still
  override both.
- Unreadable env files emit only `env_file_unreadable` and do not print local
  paths. Existing failure output redaction still covers Supabase keys, bearer
  tokens, and JWT-like values.
- Local hosted verifier dry runs with `--env-file apps/worker/.env --env-file
  apps/desktop/.env.local` now narrow the remaining missing environment values
  to `SUPABASE_LIVE_REQUESTER_JWT` and `SUPABASE_LIVE_REVIEWER_JWT`.

## 2026-06-30 Render Deploy Hook Operator Gate

- Added `app.tools.trigger_render_deploy_hook`, a manual operator tool for
  triggering a Render deploy hook without enabling automatic Render deploys.
- The tool requires `--yes` before any network call, reads the secret-bearing
  hook URL from `RENDER_DEPLOY_HOOK_URL` or `--hook-url`, pins `ref` to the
  expected Git commit, and only allows `https://api.render.com/deploy/...`.
- Output is bounded to a single `FINAL` line and never prints the hook URL,
  secret query token, response body, or full commit hash.
- This reduces the Render rollout gap to an executable manual trigger plus the
  existing hosted heartbeat freshness proof. It does not claim deploy freshness
  until `verify_worker_release_freshness` observes the new commit in hosted
  Supabase.

## 2026-06-30 Provider Gap Remote Source Proof

- `verify_provider_gap_evidence` now has an optional remote-byte proof path for
  retained provider API gap source artifacts.
- `check_provider_contract_gaps --verify-remote-provider-gap-artifacts` fetches
  each `source_artifacts[].artifact_uri`, rejects GitHub `blob` pages, caps
  response size, and requires downloaded bytes to match
  `source_artifacts[].artifact_sha256`.
- The CLI still emits only the counted `FINAL=PASS/FAIL provider_contract_gaps`
  line, so failed remote source proof cannot leak artifact URI, response body,
  or hash details through operator output.
- `collect_live_readiness_evidence_bundle` can pass the same remote provider-gap
  source check with `--verify-remote-provider-gap-artifacts`, keeping the
  default local fixture flow offline while making published release evidence
  stricter.

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
  provider evidence. Mock/static paper features, missing provider valuation
  inputs, and missing market/sector evidence stay non-live-ready and stop
  before live order proposal creation.

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
- Dashboard and feature-page data loading use worker-compatible schema paths,
  keep critical events visible in render fixtures, and now surface missing
  Supabase Auth admin sessions as `권한 필요` instead of making
  worker/provider rows or feature tables look silently absent.
- The cockpit labels now separate `거래 봇` from `데이터 수집`: `enabled=false`
  is presented as an order-creation stop, not a reason for cached Supabase
  data, provider health, or feature pages to disappear.
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
  scope proof, worker release freshness proof, provider-gap evidence, and
  independent security replay evidence.
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
  `paper_health_self_noise_20260630151326`.
- The retained report is
  `security-artifacts/paper_health_self_noise_20260630151326/report.md`.
- The scan summary records 2 worklist rows, 2 completion receipts,
  0 promoted candidates, 0 validation receipts, 0 attack-path receipts, and
  0 surviving reportable findings.
- The delta scan covers paper health self-diagnostic filtering: only
  `paper_ops/paper_health_report` rows are removed from rendered recent critical
  event output, while worker/provider/live execution/alerting critical events
  still render and still block. The live-order boundary is unchanged:
  `RiskService`, `ExecutionService`, `BrokerPort`, and desktop broker/order API
  paths were not modified.
  The earlier `fb223a4_20260628182340`, `83add88_20260630113328`,
  `c288dcd_20260630120402`, `93e239b_20260630211736`, and
  `3649a5f_20260630214017` scans, plus `abc46bf_20260630220125` and
  `release_metadata_20260630132334`, `provider_detail_20260630133946`,
  `hosted_env_files_20260630142933`, `release_freshness_20260630140851`,
  `render_deploy_hook_20260630144442`, and
  `provider_gap_remote_artifacts_20260630150324`, remain retained under
  `security-artifacts/` as broader historical baseline evidence.

## Verification Snapshot

Current feature-evidence bundle gate update:

- `collect_live_readiness_evidence_bundle` now requires `--feature-evidence`
  and embeds a provider-live feature evidence manifest in the release bundle.
- `verify_live_readiness_evidence_bundle` now requires `feature_evidence=1`,
  rejects mock/unready/non-`provider_live_v1` feature payloads, requires
  quote/fundamentals/news/market-sector/snapshot retained artifacts per symbol,
  and can verify those published artifact bytes with
  `--verify-remote-feature-artifacts`.
- Latest local verification from `apps/worker` after this update:
  `py -m pytest -q` returned `553 passed`, the focused bundle/collector tests
  returned `178 passed`, `ruff` passed on the changed files, and `mypy` reported
  no issues on the changed source/test files.

The latest local verification recorded before this handoff included:

- `py -m pytest -q --tb=short` from `apps/worker`: `543 passed`
- `py -m pytest app/tests/unit/test_provider_gap_gate.py app/tests/unit/test_live_readiness_evidence_collector.py app/tests/unit/test_live_readiness_evidence_bundle.py -q`
  from `apps/worker`: `183 passed`
- `py -m ruff check . ../../supabase` from `apps/worker`: passed
- `py -m mypy . ../../supabase/_hosted_env.py ../../supabase/verify_hosted_live_readiness.py ../../supabase/verify_hosted_live_enable_flow.py`
  from `apps/worker`: `238 source files`
- `py -m pytest app/tests/contract/test_verify_hosted_live_readiness.py app/tests/contract/test_verify_hosted_live_enable_flow.py -q --tb=short`
  from `apps/worker`: `34 passed`
- `py -m pytest app/tests/unit/test_render_deploy_hook.py -q --tb=short`
  from `apps/worker`: `7 passed`
- `py supabase/verify_hosted_live_readiness.py --env-file apps/worker/.env --env-file apps/desktop/.env.local`:
  expected `FINAL=SKIP hosted_supabase_env_missing` with only
  `SUPABASE_LIVE_REQUESTER_JWT,SUPABASE_LIVE_REVIEWER_JWT` missing
- `py supabase/verify_hosted_live_enable_flow.py --env-file apps/worker/.env --env-file apps/desktop/.env.local`:
  expected `FINAL=SKIP hosted_live_enable_env_missing` with only
  `SUPABASE_LIVE_REQUESTER_JWT,SUPABASE_LIVE_REVIEWER_JWT` missing
- `py -m app.tools.paper_health_report` from `apps/worker`: expected
  `FINAL=FAIL` against hosted data, with degraded provider lines including
  `error_type=ProviderAuthError reason=toss_access_denied`; the rendered recent
  critical event list excludes self-generated `paper_ops: paper_health_report`
  rows and shows only operational worker/provider events
- `py -m app.tools.verify_worker_release_freshness --repo-root ...` from
  `apps/worker`: expected `FINAL=FAIL` before manual Render redeploy, with
  `reason=release_sha_mismatch`
- `py -m app.tools.trigger_render_deploy_hook --expected-sha ...` from
  `apps/worker`: expected `FINAL=SKIP render_deploy_hook` with
  `reason=render_deploy_hook_env_missing` when no operator hook URL is present
- `py -m app.tools.redeploy_render_worker --repo-root ... --yes` from
  `apps/worker`: expected `FINAL=SKIP render_worker_redeploy` with
  `reason=render_deploy_hook_env_missing` when no operator hook URL is present
- Scorecard/security evidence gates:
  retained scan report prepared for
  `paper_health_self_noise_20260630151326`; regenerate the source-bound
  `security_scan_summary.json` after the final commit hash exists,
  then run `verify_security_scan_evidence` and `verify_live_readiness_scorecard`
  before release bundle assembly.

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
  `feature_evidence=1`, `remote_provider_artifacts=1`,
  `remote_incident_evidence=1`, `remote_system_order_scope_evidence=1`, and
  `remote_feature_artifacts=1`.

Until those blockers are closed with retained external evidence, live operation
must remain disabled.
