# Live Readiness Scorecard

Generated: 2026-06-30 KST

This scorecard is intentionally strict. A category reaches 100 only when the
current repository state and an observed runtime surface prove the requirement.
Passing tests alone is not enough.

| Category | Current score | Evidence | Remaining gap before 100 |
| --- | ---: | --- | --- |
| Live execution safety | 97 | `ExecutionService` is the only live order path; live orders run final `RiskService`; Toss create/cancel use verified contracts; uncertain create/cancel statuses fail to `unknown_requires_manual_check`. Allowed live order creation now persists the final idempotency key as a durable `unknown_requires_manual_check` row with `reason='live_broker_order_result_pending'` before `BrokerPort.place_order`; broker success updates that same row with provider status/id/payload, while crash, timeout, or unknown result remains visible for reconciliation and duplicate retry is blocked by the preexisting idempotency key. `run_live_execution_safety_drill_once` now turns those local execution invariants into a release-bundle local gate, requiring `missing_evidence_blocked=1`, `pre_broker_manual_check=1`, `provider_result_recorded=1`, `duplicate_blocked=1`, and `broker_calls=1`; `verify_live_readiness_evidence_bundle` rejects missing, duplicate, unknown, or weaker execution-safety metrics. Scheduled live cycles now fail closed unless `LIVE_SYSTEM_ORDER_COUNT_SCOPE_ACCEPTED=true` records operator acceptance that production live operation is limited to system-originated orders while Toss broker-wide closed-order history remains unavailable; the new integration test proves missing acceptance records `live_external_order_history_scope_not_accepted`, blocks with `daily_order_count_unverified`, and makes zero broker calls. `verify_system_order_scope_evidence` now standalone-validates the retained scope evidence manifest, and `collect_live_readiness_evidence_bundle` also requires the exact `system_created_live_orders_only` scope, Toss closed-order-history limitation, deployment environment, operator, `LIVE_SYSTEM_ORDER_COUNT_SCOPE_ACCEPTED` confirmation, non-future `evidence_captured_at` strictly after `accepted_at`, retained remote HTTPS artifact URI with a valid DNS host or global IP, no local/test/private/non-global-IP host or private DNS suffix, no malformed port, no query/fragment, no URI credentials, and SHA-256 before the bundle can pass. `verify_live_readiness_evidence_bundle` now binds that retained scope evidence to the bundle target, rejecting `staging` bundles unless `deployment_environment=staging` and rejecting `production-readiness` bundles unless `deployment_environment=production`. | No provider sandbox/live end-to-end order drill was run in this session; Toss broker-wide closed-order history remains unavailable for external daily-order counting, and the system-originated-order acceptance plus pre-broker manual-check behavior have not been exercised in a real deployed environment with retained evidence. |
| Supabase live control plane | 99 | `0009_live_operations_hardening.sql` enforces fresh evidence, different reviewer, immutable review evidence, RLS admin checks, audit triggers, and one-time `accepted` -> `applied` live-enable consumption. `0010_security_definer_hardening.sql` hardens existing `security definer` RPCs with `search_path = ''` and explicit `execute` grants. `0011_data_api_grants.sql` now makes Supabase Data API exposure explicit: `public`/`anon` table and function access is revoked, future default table/function grants are revoked, authenticated desktop users receive only RLS-backed table privileges, and worker `service_role` receives explicit table/sequence/function grants. `test_data_api_grants_migration.py` proves every current `public` table is covered by authenticated `select`, write grants match existing RLS write policy surfaces, `anon` is not granted table access, and `SECURITY DEFINER` execute grants remain explicit. `supabase/verify_live_enable_migration.py` applied all migrations and seed data against a disposable `postgres:16-alpine` container and returned `FINAL=PASS live_enable_consumed_once rpc_hardening`, including `anon`/`authenticated` denial for retention/database-size RPCs and `service_role` dry-run success. `supabase/verify_hosted_live_readiness.py` now codifies the hosted/staging gate for PostgREST root, publishable-key RPC denial, secret-key RPC success, publishable-key Data API denial on `bot_settings`, service-role Data API select on `bot_settings`, two distinct admin JWT Auth user lookups, two RLS-backed authenticated `user_roles` reads proving `admin` role visibility for requester and reviewer, and Realtime WebSocket handshake without printing secrets; local env-free execution correctly returns `FINAL=SKIP hosted_supabase_env_missing` including missing `SUPABASE_LIVE_REQUESTER_JWT` and `SUPABASE_LIVE_REVIEWER_JWT`, and contract tests cover the mocked hosted pass path plus rejection of non-HTTPS, local/test/private IP, non-`.supabase.co`, credential-bearing, path-bearing, query/fragment-bearing URLs, reused publishable/secret keys, and JWTs that reuse Supabase key values. `supabase/verify_hosted_live_enable_flow.py` codifies the hosted user-session gate with two different admin JWTs: requester creates `request_live_enable`, self-review is denied, reviewer accepts, live activation consumes the approval exactly once, and second activation is denied; local env-free execution returns `FINAL=SKIP hosted_live_enable_env_missing`, and contract tests cover mocked hosted pass/fail paths, matching `.supabase.co` project-origin restrictions, and configured key/JWT redaction from failure output. `verify_live_readiness_evidence_bundle` now rejects bare or weak hosted PASS lines, requiring `postgrest=1`, `anon_rpc_denied=2`, `service_rpc_allowed=2`, `anon_table_denied=1`, `service_table_allowed=1`, `authenticated_table_allowed=2`, `realtime=1`, and all seven hosted live-enable flow metrics set to `1`, with duplicate or unknown hosted metrics rejected without leaking metric values. It also requires the local migration output to exactly match `FINAL=PASS live_enable_consumed_once rpc_hardening`, so extra ad hoc tokens cannot be smuggled into the release bundle. | Local disposable Postgres and executable hosted gates are proven and now fail before network calls for local/mock/arbitrary HTTPS URLs; a real hosted Supabase/staging run with real Auth/RLS/PostgREST/Data API grants, Realtime settings, and two distinct admin user sessions still must return both exact counted `FINAL=PASS hosted_supabase_live_readiness` and `FINAL=PASS hosted_live_enable_flow` lines before this category can reach 100. |
| Reconciliation and manual recovery | 97 | Worker reconciliation reads Toss order status before new live order proposals; manual cancel confirms original order `CANCELED` before local `canceled`; runbook covers unknown states. `run_live_recovery_drill_once` now exercises the actual `OrderReconciliationService` and `LiveOrderCancellationService` paths with no live placement: provider `filled` reconciliation, still-unknown manual-check escalation, no automatic clearing of an existing `unknown_requires_manual_check` order when a later provider read reports `filled`, cancel confirmation to local `canceled`, and cancel confirmation failure to `unknown_requires_manual_check`. Latest local run returned `FINAL=PASS live_recovery_drill reconciled_updates=1 manual_check_events=2 cancel_confirmed=1 cancel_unknown=1 status_calls=4 cancel_calls=2 pending_order_blocked=1 manual_check_preserved=1`. The final bundle verifier now rechecks those exact recovery metrics, including `manual_check_preserved=1`, and rejects missing, duplicate, unknown, or weaker local recovery PASS lines. Existing manual-check orders now record `live_order_manual_check_provider_status_observed` and remain in manual recovery instead of being transiently or durably rewritten to `sent`, `partial_filled`, `filled`, `canceled`, or `rejected` by reconciliation alone. Live cycles also recheck pending live reconciliation rows after the reconciliation pass; any remaining `sent`, `partial_filled`, or `unknown_requires_manual_check` live order records `live_pending_reconciliation_blocks_new_live_orders`, sends the critical event/alert path, still writes decision snapshots for observability, and returns before new live order proposals or broker calls. `run_live_recovery_drill_once` and the final bundle verifier now require `pending_order_blocked=1`. `verify_provider_lifecycle_evidence` now adds a fail-closed verifier for a retained Toss sandbox/live evidence artifact: redacted provider ids, disabled live gate before/after, non-future drill window, created-order `created_at`, provider status observations bound to the created `local_order_id` with strictly increasing `observed_at` timestamps strictly after creation, uppercase known Toss provider statuses only, matching terminal provider/local status pairs with no later regression or terminal-value change, cancel confirmation with `attempted_at` strictly after created-order `created_at`, exact provider `CANCELED`, post-attempt `CANCELED` -> `canceled` status proof, and no pre-cancel `FILLED`/`REJECTED`/`EXPIRED` provider terminal observation, unknown-state human operator review strictly after the latest provider status observation, audit review strictly after that recovery review, distinct human audit reviewer identity, audit review counts, and retained artifact proof for broker order receipt, provider status export, cancel confirmation, unknown recovery review, and repository audit export with matching artifact `drill_id`, remote HTTPS artifact URI with a valid DNS host or global IP, no local/test/private/non-global-IP host or private DNS suffix, no malformed port, no query/fragment, no URI credentials, unique retained artifact URI after hostname/default-HTTPS-port canonicalization, unique SHA-256, and `captured_at` strictly after the artifact's bound lifecycle event; cancel-confirmation artifacts are anchored to the post-attempt `CANCELED` -> `canceled` observation, not merely the cancel attempt. Automated review identity segments such as `automation`, `bot`, `ci`, `github-actions`, `script`, `service-account`, or `system` are rejected without echoing the supplied identity. It also rejects unknown root, order, status, cancel, recovery, audit, and artifact fields so raw provider responses or audit dumps must stay in retained artifacts referenced by URI and SHA-256. When release evidence has been published, `--verify-remote-artifacts` fetches each retained artifact URI, rejects GitHub `blob` pages, caps response size, and requires the downloaded bytes to match the declared SHA-256 without leaking URI/body/hash details. `collect_live_readiness_evidence_bundle` now embeds the redacted provider lifecycle evidence payload, and `verify_live_readiness_evidence_bundle` re-runs the provider lifecycle validator against that payload, rejects missing provider payloads, rejects final-output/payload mismatches, and separately enforces provider lifecycle final-output metrics so they prove `provider=toss`, sandbox/live environment, at least two status observations, at least two reviewed audit logs, and exactly five retained artifacts. It also binds provider lifecycle evidence to the bundle target, rejecting `staging` bundles unless provider `environment=sandbox` and rejecting `production-readiness` bundles unless provider `environment=live`. | Provider sandbox/live lifecycle drill with real provider order states and human operator audit review has not been executed, so no real evidence file has returned `FINAL=PASS provider_lifecycle_evidence ... evidence_artifacts=5`; post-publication remote artifact byte verification must also pass before this category can reach 100. |
| Desktop cockpit | 100 | Browser QA confirmed `?page=control`, `?page=dashboard`, and `?page=signals` render with no relevant console warnings/errors; the live activation path stays disabled without fresh approval even after typing the confirmation phrase. The hosted dashboard `decision_snapshots` 400 was fixed by using the worker-compatible `created_at` schema path, and `http://localhost:1420/?page=dashboard` currently returns HTTP 200. `npm run desktop:test` covers approval policy helpers, `ControlPage` live approval query states, rendered live request form valid/missing/too-short/too-long input paths, hydrated request/approve/reject/activate mutation payloads and failure messages against a mocked data API, self-review blocking, `DashboardPage` critical event visibility, and a schema compatibility guard for decision snapshot reads. `npm run desktop:e2e` now starts an isolated Vite server on `127.0.0.1:1431` with mocked Supabase Auth/REST responses and proves the real browser live gate flow across desktop and mobile viewports: no fresh approval keeps activation disabled, request payloads are written to `manual_commands`, reviewer approval creates a fresh approval, and activation writes the gated `bot_settings` patch with no console/page errors. | None for the current repository state and observed desktop runtime surface. Hosted Supabase/staging verification is tracked under the Supabase control-plane category. |
| Security posture | 99 | RLS policies remain admin-scoped, no anon/public writes were added, secrets remain placeholder/test-only, and worker-only broker boundary is preserved. The current Codex Security delta scan `hosted_env_files_20260630142933` is finalized with retained report path `report.md` and 3 worklist rows, 3 completion receipts, 0 promoted candidates, 0 validation receipts, 0 attack-path receipts, and 0 surviving reportable findings. The scan reviewed explicit hosted verifier env-file loading: env files are opt-in via `--env-file`, process env and CLI args keep precedence, unreadable files emit only `env_file_unreadable`, and secret/JWT redaction remains in verifier failure output. The unchanged live-order boundary remains intact: no `RiskService`, `ExecutionService`, `BrokerPort`, or desktop broker/order API path changed. Worker verification returned `531 passed`; focused hosted verifier contract tests returned `34 passed`; `ruff` and `mypy` passed. The broader retained scans `fb223a4_20260628182340`, `83add88_20260630113328`, `c288dcd_20260630120402`, `93e239b_20260630211736`, `3649a5f_20260630214017`, `abc46bf_20260630220125`, `release_metadata_20260630132334`, `provider_detail_20260630133946`, and `release_freshness_20260630140851` remain under `security-artifacts/` as historical baseline evidence, while current source binding uses the `hosted_env_files_20260630142933` summary. `verify_security_scan_evidence` still requires `scan_profile=security_diff_scan`, zero reportable findings, exact completion receipts for every worklist row, threat-model and finding-discovery receipts, validation and attack-path receipts matching the candidate finding count, current `source_head`/`source_diff_sha256`, a relative report path, a matching `report_sha256`, and matching repo-retained/remote report bytes before source binding can pass. | The local source-bound delta scan is complete for the latest committed code path, but strict release readiness still requires post-publication `--verify-remote-report-uri` PASS after this update is pushed, plus manual Render redeploy observation for the new commit through hosted Supabase heartbeat/provider-health rows and final bundle PASS with real hosted/provider/incident/scope artifacts and `remote_provider_artifacts=1`, `remote_incident_evidence=1`, and `remote_system_order_scope_evidence=1`. Hosted Supabase role/RPC verification is tracked under the Supabase control-plane category. |
| Observability and runbooks | 99 | `engine_events`, `audit_logs`, live reconciliation, manual cancel, live approval audit, and Emergency Stop flows are documented. `worker_heartbeats.details` now includes safe release metadata (`release_sha`, `release_sha_short`, `release_source`) from `APP_RELEASE_SHA` or the build-generated `app/release_metadata.json`, and `paper_health_report` prints those fields so a manual Render deploy can be tied to the exact pushed commit without exposing secrets. `verify_worker_release_freshness` now turns that hosted heartbeat into a release-blocking local gate by comparing `worker_heartbeats.details.release_sha` with current Git `HEAD` and rejecting missing, mismatched, stale, or future heartbeats; the final bundle requires this PASS line as the seventh local check. `WebhookAlertNotifier` can send redacted critical engine events to `ALERT_WEBHOOK_URL`; `run_live_alert_drill_once` now covers `unknown_requires_manual_check`, stale worker heartbeat, live account count failure, and missing live system-order-scope acceptance, returning `FINAL=PASS live_external_alert_drill delivered=4 max_latency_ms=1` in the latest local run. The final bundle verifier now requires the local alert PASS line to retain `delivered=4` and `max_latency_ms<=2000`, rejecting missing, duplicate, unknown, or weaker local alert metrics. `run_live_incident_response_drill_once` adds an ACK-gated incident response gate for the same alert family; the local dry run returned `FINAL=PASS live_incident_delivery_drill delivered=4 max_latency_ms=1 ack_required=false`, and the ACK-gated output now must retain `delivered=4`, `max_latency_ms<=2000`, `acknowledged=true`, `ack_latency_ms<=300000`, and `drill_id`. `verify_incident_response_evidence` now validates ACK-gated drill output plus retained incident-channel proof before bundle assembly, rejecting non-ACK dry runs, suffixed ACK-gated check names, missing or slow delivery/latency metrics, mock/local channel evidence, URL/path/webhook-like channel names, channel `drill_id` mismatches, future retained timestamps, retained captures at or before `operator_ack_at`, weak, non-HTTPS, invalid-host, malformed-port, or local/test/private/non-global-IP-host retained URIs, invalid SHA-256 values, missing human ACK metadata, ACK identity segments such as `automation`, `bot`, `ci`, `github-actions`, `script`, `service-account`, or `system`, email/URL/path-like ACK operator identities, unknown or duplicate final-output metrics, and unexpected secret-like evidence keys. `verify_system_order_scope_evidence` adds the same pre-collector standalone gate for the retained system-order-scope acceptance file, rejecting future acceptance and capture timestamps, retained scope evidence captures at or before `accepted_at`, local/sample/non-HTTPS, invalid-host, malformed-port, or local/test/private/non-global-IP-host URIs, query/fragment-bearing artifact URLs, invalid SHA-256 values, missing runtime env confirmation, automated or email/URL/path-like `accepted_by` identities, and secret-like keys. `collect_live_readiness_evidence_bundle` now runs the local/hosted gates, reads retained provider evidence, ACK-gated incident output, incident-channel evidence, security evidence, and system-order-scope acceptance evidence, writes a bundle only after validation passes, and requires explicit `--accept-system-order-scope`; `verify_live_readiness_evidence_bundle` then enforces one release-blocking `FINAL=PASS live_readiness_evidence_bundle` gate with a 24-hour evidence window, non-future bundle timestamps, and `reviewed_at` strictly after `generated_at`, rejecting automated bundle reviewer identities, `SKIP`, missing or weak hosted Supabase output metrics, stale or mismatched worker release freshness proof, mutated local migration output, missing or weak local execution-safety/recovery/alert/scorecard metrics, mock ACK, automated or email/URL/path-like ACK/operator acceptance identities, slow incident delivery, missing/mock incident-channel proof, URL/path/webhook-like incident channel names, incident `drill_id` mismatch, invalid incident evidence SHA-256, incident evidence captured at or before human ACK, system-order-scope evidence captured at or before acceptance, unknown or duplicate incident final-output metrics, weak, non-HTTPS, invalid-host, malformed-port, or local/test/private/non-global-IP-host artifact URIs, sample evidence, secret-like keys, unknown bundle/check/evidence fields, incomplete security receipts, or reportable findings. `DashboardPage` render fixtures prove critical events remain visible in the cockpit. | External webhook delivery and ACK timing are executable and collector/bundle-gated, but the ACK-gated drill has not been run against the real incident channel with a real human operator and retained response-time evidence plus incident-channel proof manifest. A manual Render deploy still must be observed in hosted Supabase with the expected `release_sha` heartbeat before deploy freshness can be claimed; the latest hosted observation before this update still showed an older Render release, so freshness is intentionally not claimed. The final collector/bundle also has not passed with real hosted/provider/security/scope evidence. |
| Provider/data completeness | 96 | Toss account, buying power, holdings, price, candle, calendar, order create, cancel, and status contracts are typed and tested. Naver Search News now has a read-only typed adapter with official request/error mapping. OpenDART now parses official `CORPCODE.xml` ZIP data, rejects oversized ZIP/XML plus DTD/entity declarations before XML parsing, and maps official financial statement account rows into partial fundamentals. OpenAI structured-output requests now use a verified model allowlist, `gpt-5.5` default, and an explicit strict JSON schema where `approval_required` and fixed strategy params are required. Supabase Free DB budget is verified at 500MB with a 450MB warning. `py -m app.tools.check_provider_contract_gaps` now returns `FINAL=PASS provider_contract_gaps total_gaps=18 blocking_unknown_gaps=0 invalid_status_gaps=0 warning_partial_gaps=1 warning_partial_gap_ids=toss:live-account-state-sync-for-scheduled-cycle:partial-system-only-accepted-fail-closed system_order_scope_accepted=1 provider_gap_evidence=1`; the final bundle verifier rejects bare provider-gap PASS output, any nonzero `blocking_unknown_gaps`, any nonzero `invalid_status_gaps`, `warning_partial_gaps>1`, any missing warning id, any warning count/id mismatch, `provider_gap_evidence=0`, or `warning_partial_gaps=1` unless `warning_partial_gap_ids` exactly names the documented Toss system-created-order scope limitation, retained `system_order_scope_evidence` is accepted, and retained `provider_gap_evidence` binds the exact `docs/API_GAPS.md` SHA-256 plus every parsed provider gap id to provider-matching source artifacts with unique retained URI/SHA-256 pairs and non-future capture timestamps, so provider status typos, undocumented partial uncertainty swaps, and detached provider-source assertions cannot silently pass. The single remaining Toss partial warning stays visible in release evidence and remains explicitly fail-closed unless `LIVE_SYSTEM_ORDER_COUNT_SCOPE_ACCEPTED=true` records that live operation is system-originated only. Default mock/static paper features remain non-live-ready, and live mode now calls `FeatureService.build_live_features` to construct `provider_live_v1` snapshots from non-mock quote, OpenDART fundamentals, and Naver news evidence. Mocked, unconfigured, missing, provider-error, missing valuation, or missing market/sector evidence inputs keep `live_trading_ready=false` with retained `feature_unready_reasons` and emit `live_feature_snapshot_not_ready`; `ExecutionService` still blocks broker placement unless `live_trading_ready=true` and `feature_source` is non-mock. `provider_live_v1` now requires positive PER/PBR valuation inputs plus verified market/sector evidence before live proposal creation; focused tests prove mocked provider inputs and provider-backed Toss/OpenDART/Naver fixtures without sector evidence make no broker call, while the separate verified live-ready execution fixture still reaches the sent live-order path. | Toss broker-wide closed-order history remains unavailable for externally placed daily order counts; real hosted/provider credential proof and published retained feature-evidence artifacts are still required before release; OpenDART PER/PBR remain `None` because they require market capitalization/price data outside OpenDART financial statements; direct KRX listing/sector APIs remain intentionally unused/fail-closed rather than production-proven. |

Release evidence operator identities must be internal logical handles across
provider lifecycle review, incident ACK, scope acceptance, and final bundle
review. Email addresses, URLs, paths, raw contact values, retained artifact
references, and automated identity segments are not valid human operator proof.
Security scan `scan_id` values follow the same artifact-boundary principle:
they must be lowercase logical scan identifiers, while report locations belong
only in retained `report_uri`/`report_path` fields.
Security scan `report_path` values must be relative retained markdown paths
under the security summary directory; absolute, drive-qualified, and
`..`-escaping path shapes are rejected before report hashing or source binding.
Published incident-channel and system-order-scope retained evidence now follows
the same post-publication byte-proof rule as provider lifecycle artifacts and
security reports: the standalone verifiers and final bundle verifier can fetch
the declared retained HTTPS URI, reject GitHub `blob` pages, cap response size,
and compare downloaded bytes to the declared SHA-256 without leaking URI, body,
or hash values in failures. The final bundle PASS line must show
`remote_provider_artifacts=1`, `remote_incident_evidence=1`, and
`remote_system_order_scope_evidence=1` when those post-publication byte checks
were actually run.
Standalone and bundled security scan evidence validation must classify
secret-like unknown keys as `sensitive_key_not_allowed` before release evidence
can pass, and failure output must not echo supplied secret values.
Standalone and bundled incident-channel evidence validation must classify
secret-like unknown keys as `sensitive_key_not_allowed` before release evidence
can pass, while still reporting the schema unknown-key failure and never echoing
the supplied secret value.
The ACK-gated incident drill runner writes the interactive ACK prompt to stderr,
so the retained stdout artifact remains a single `FINAL=PASS
live_incident_response_drill ...` line that can pass standalone evidence
verification without non-final side-channel output.
The provider contract gap CLI writes only its single
`FINAL=PASS/FAIL provider_contract_gaps ...` gate line to stdout; detailed human
reports remain formatter output, not collector-run command output. When the
documented Toss partial warning remains, release PASS requires retained
system-order-scope evidence plus retained provider source evidence, and emits
`system_order_scope_accepted=1 provider_gap_evidence=1`.
Hosted Supabase readiness and live-enable flow verifiers follow the same
collector contract on PASS: stdout contains only the counted `FINAL=PASS ...`
line, so real hosted evidence cannot be rejected as side-channel output.
Hosted Supabase readiness evidence must include two distinct admin user JWTs:
the verifier must prove both Auth `/user` identity resolution and two
RLS-backed authenticated `user_roles` reads before emitting
`authenticated_table_allowed=2`.
All retained release evidence URIs must use artifact paths without raw or
percent-decoded `.`/`..` traversal or encoded slash/backslash separators.
GitHub security scan `report_uri` values verified with `--repo-root` must map
to a repository-retained artifact path whose bytes match `report_sha256`; a
missing or mismatched repo artifact is not durable release evidence. Remote
publication checks must additionally run with `--verify-remote-report-uri`
against a byte-addressed report URL; GitHub `blob` page URLs are not valid
remote byte proof.
Retained SHA-256 evidence references are compared case-insensitively, so
uppercase/lowercase variants of the same digest do not count as distinct proof.
Provider lifecycle unknown-recovery evidence must bind back to the created
local order ID; recovery proof for any other order is not release evidence.
Its `final_status` must also match the latest local provider-status observation,
so a recovered order cannot be reported with a stale or contradictory outcome.
The created order's `status_after_create` must match the first local status
observation in the provider lifecycle sequence.
Provider lifecycle provider identifiers must use a constrained redaction-token
format such as `toss_order_...abcd` or `redacted:<hex-digest>`; a raw provider
identifier with only a `redacted:` prefix is not valid retained evidence.
Standalone and bundled system-order-scope evidence validation must classify
secret-like unknown keys as `sensitive_key_not_allowed` before release evidence
can pass, and failure output must not echo supplied secret values.

## Next Strict Iteration

1. Apply migrations through `0011_data_api_grants.sql` to a hosted
   Supabase/staging project, then run `python supabase/verify_hosted_live_readiness.py`
   and `python supabase/verify_hosted_live_enable_flow.py` with an official
   `https://<project_ref>.supabase.co` `SUPABASE_URL` that has no credentials, path,
   query, or fragment, distinct publishable/secret keys, and distinct requester/reviewer
   admin JWTs that do not reuse Supabase key values until they return
   `FINAL=PASS hosted_supabase_live_readiness` with `postgrest=1`,
   `anon_rpc_denied=2`, `service_rpc_allowed=2`, `anon_table_denied=1`,
   `service_table_allowed=1`, `authenticated_table_allowed=2`, and `realtime=1`, plus
   `FINAL=PASS hosted_live_enable_flow` with all seven gate metrics set to `1`.
2. Keep the current Codex Security scan summary bound to the final source state:
   after any repository edit, regenerate
   `C:\Users\choey\.tmp\codex-security-scans\msp\hosted_env_files_20260630142933\security_scan_summary.json`
   with current `source_head`, `source_diff_sha256`,
   `threat_model_receipt=true`, `finding_discovery_receipt=true`,
   `worklist_rows=3`, `completion_receipts=3`, `candidate_findings=0`,
   `validation_receipts=0`, `attack_path_receipts=0`,
   `reportable_findings=0`, a lowercase logical `scan_id`, retained remote
   HTTPS `report_uri` without raw or percent-decoded path traversal or encoded
   path separators, a relative retained `report_path` that stays under the
   security summary directory without absolute, drive-qualified, or
   `..`-escaping path shapes, and `report_sha256` of the actual markdown report
   file bytes, and, when `report_uri` is a GitHub blob/raw URL, a matching
   repo-retained artifact under `security-artifacts/` whose bytes equal
   `report_sha256`. After commit/push publication, run the same verifier with
   `--verify-remote-report-uri` against the declared raw GitHub `report_uri` and
   compare the downloaded bytes to the same `report_sha256`.
   Then run
   `python -m app.tools.verify_security_scan_evidence --evidence ... --repo-root ...`
   and
   `python -m app.tools.verify_live_readiness_scorecard --scorecard docs/LIVE_READINESS_SCORECARD.md --security-evidence ... --repo-root ...`
   so the standalone gate, scorecard gate, and collector reject stale,
   unavailable-source-binding, summary-only replay evidence, or scorecard/security-summary count drift
   without leaking local path or hash details.
3. Execute a provider sandbox/live order lifecycle drill covering create,
   status reconciliation, cancel, unknown-state recovery, and audit review.
   Then run `python -m app.tools.verify_provider_lifecycle_evidence --evidence ...`
   until it returns `FINAL=PASS provider_lifecycle_evidence` against the retained
   redacted evidence file with `created_at`, strictly increasing status
   observations strictly after creation bound to the created `local_order_id`,
   `attempted_at` cancel proof strictly after created-order `created_at` for the same created `local_order_id` with exact
   provider `CANCELED` plus a post-attempt `CANCELED` -> `canceled` status
   observation, no pre-cancel `FILLED`/`REJECTED`/`EXPIRED` provider terminal
   observation, and all five
   retained remote HTTPS artifact proofs using public retained DNS/global-IP hosts and no raw or percent-decoded path traversal or encoded path separators
   rather than private suffixes such as `.internal`, `.corp`, `.lan`,
   `.intranet`, `.home`, or `.private`,
   each bound to the evidence `drill_id` with a unique retained URI, unique SHA-256, and `captured_at`
   strictly after its corresponding lifecycle event; the `cancel_confirmation`
   artifact must be captured after the post-attempt `CANCELED` -> `canceled`
   status observation, not merely after the cancel attempt. The final bundle
   must also retain `status_observations>=2`, `audit_logs_reviewed>=2`, and
   `evidence_artifacts=5` in the provider lifecycle PASS line. The local service-path
   recovery drill is now complete, but it is not a provider proof.
4. Execute `python -m app.tools.run_live_incident_response_drill_once --require-ack`
   against the real incident channel and retain `delivered=4`,
   `max_latency_ms<=2000`, the human operator acknowledgment,
   `ack_latency_ms<=300000`, output `drill_id`, and incident-channel evidence manifest
   with matching `drill_id`, remote HTTPS artifact URI, SHA-256, and
   `captured_at` strictly after `operator_ack_at`. Then run
   `python -m app.tools.verify_incident_response_evidence
   --incident-output-file ... --incident-channel-evidence ... --verify-remote-channel-evidence` until it returns
   `FINAL=PASS incident_response_evidence`.
5. Validate the retained system-order-scope acceptance evidence with
   `python -m app.tools.verify_system_order_scope_evidence --evidence ... --verify-remote-evidence` until
   it returns `FINAL=PASS system_order_scope_evidence` against a deployed
   environment proof with a human logical `accepted_by` handle and no
   local, sample, non-HTTPS, malformed-port, local/test/private/non-global-IP-host, or query-bearing artifact URI,
   plus `evidence_captured_at` strictly after `accepted_at`; the final bundle
   release reviewer must be a different human operator from this `accepted_by`.
6. Assemble the retained hosted, provider, provider-gap source, incident,
   local, system-order-scope, and independent security replay outputs with
   `python -m app.tools.collect_live_readiness_evidence_bundle --provider-gap-evidence ... --incident-channel-evidence ... --system-order-scope-evidence ... --output ...`,
   then verify the generated bundle with
   `python -m app.tools.verify_live_readiness_evidence_bundle --evidence ... --verify-remote-provider-artifacts --verify-remote-incident-evidence --verify-remote-system-order-scope-evidence`
   until it returns `FINAL=PASS live_readiness_evidence_bundle` with
   `remote_provider_artifacts=1`, `remote_incident_evidence=1`, and
   `remote_system_order_scope_evidence=1`, with every
   provider lifecycle reviewer, incident ACK operator, scope acceptance operator,
   and final bundle reviewer represented by distinct human logical handles;
   provider lifecycle `unknown_recovery.operator_reviewed_by` and
   `audit.reviewed_by` must also be different normalized identities
   (`bundle.evidence_operator_roles_must_be_distinct` on provider/incident/scope
   reuse), and every
   collected collector-run command exiting with return code `0`, every
   `FINAL=PASS <check_name>` line staying single-line and retaining the exact
   expected check-name token, and no reused retained URI or SHA-256 across
   incident-channel, system-order-scope, security report, or provider lifecycle
   artifact evidence; retained URI reuse is checked after hostname/default-HTTPS-port
   plus unreserved percent-encoding canonicalization and is now pinned by bundle-level regression tests that
   reject cross-evidence URI/SHA reuse without leaking supplied values. The collector `--output` path must be a new distinct file
   and must not equal any input evidence file or the local security
   `report_path`; duplicate local artifact paths fail with the fixed
   `collector_artifact_paths_must_be_distinct` code before bundle output is
   written. If the collector `--output` path already exists, collection fails
   with `collector_output_path_must_not_exist` before bundle output is written.
   The collector writes the bundle with exclusive file creation, so concurrent
   output creation also fails with `collector_output_path_must_not_exist`; other
   output write failures fail as `collector_output_path_unwritable`. A
   collector-run command with nonzero process exit status fails with
   `<check_name>_command_returncode_nonzero` before stdout/stderr final-line
   content is trusted. Collector-run gate output and the retained incident drill
   output must contain no non-empty non-final lines; such side-channel output
   fails with `<check_name>_non_final_output_lines_not_allowed` or
   `incident_output_non_final_lines_not_allowed`.
7. Retire the remaining provider warning by either proving a broker-wide
   closed-order history source for externally placed daily orders or exercising
   the `LIVE_SYSTEM_ORDER_COUNT_SCOPE_ACCEPTED=true` system-originated-order
   acceptance in a real deployed environment with an operator record. Until the
   warning is retired, the release collector must pass
   `--system-order-scope-evidence ...` and `--provider-gap-evidence ...` to
   `check_provider_contract_gaps`, and the retained provider gap final line must include
   `system_order_scope_accepted=1 provider_gap_evidence=1`.

