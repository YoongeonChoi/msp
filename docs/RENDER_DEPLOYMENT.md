# Render Deployment

Use `render.yaml` worker service:

- `type: worker`
- `runtime: python`
- `region: singapore`
- `plan: starter`
- `rootDir: apps/worker`
- `autoDeployTrigger: "off"`
- `maxShutdownDelaySeconds: 120`

Before deploy:

1. Set `bot_settings.enabled=false`.
2. Set `live_order_allowed=false`.
3. Wait for heartbeat showing paused/disabled.
4. Deploy manually.
5. Verify heartbeat, including `details.release_sha` for the deployed commit.
6. Run smoke checks.
7. Re-enable paper first.
8. Keep `LIVE_SYSTEM_ORDER_COUNT_SCOPE_ACCEPTED=false` until an operator has recorded retained
   `system_order_scope_evidence.json` proving that production live operation is limited to
   system-originated orders while broker-wide Toss closed-order history is unavailable, including
   deployment environment, operator, runtime env confirmation, evidence URI, and SHA-256 hash.
9. Confirm the deployed live strategy feature builder emits `provider_live_v1`
   evidence from non-mock quote, fundamentals, and news providers with positive
   PER/PBR valuation inputs plus market/sector provenance. Live decisions must
   carry `feature_snapshot.raw.live_trading_ready=true` and a non-mock
   `feature_source`; otherwise `ExecutionService` must block before broker
   placement.
10. Consider live only after manual confirmation.

No secrets are stored in `render.yaml`; secret env vars use `sync: false`.
`ALERT_WEBHOOK_URL` is treated as a worker-only secret because provider tokens are
often embedded in webhook URLs.

## Startup Smoke And Schema Compatibility

Use a local Render-equivalent one-shot before or after a manual Render deploy:

```bash
ENV=production MOCK_PROVIDERS=false RUN_ONCE=true python -m app.main
```

If required Toss credentials are absent, startup must not crash during
`bootstrap()`. The one-shot run should exit cleanly after logging
`known_fail_closed` with safe message `toss_credentials_missing`, and no Toss
HTTP token request should be attempted. This proves the Render worker can stay
observable while live trading remains blocked until credentials and hosted
provider evidence are supplied.

If the worker exits during startup with a PostgREST `400`, inspect only the
redacted response body. Hosted projects that have not yet applied
`supabase/migrations/0005_schema_alignment.sql` may be missing
`strategy_versions.version` or `decision_snapshots.decided_at`. The worker keeps
runtime compatibility with those base-schema projects by loading strategies via
`version_name`/active rows and by writing decision time through
`decision_snapshots.created_at`, but the hosted migration still needs to be
applied and verified before live-readiness evidence can be accepted.

Because `autoDeployTrigger` remains `off`, a successful Git push does not update
Render by itself. Deploy the pushed commit manually, then verify a fresh
heartbeat whose `details.release_sha` or `details.release_sha_short` matches the
expected commit, and repeat the one-shot smoke against the deployed environment.
The Render build command writes `app/release_metadata.json` during build so the
worker can persist this release marker in `worker_heartbeats.details`; an
operator may also set `APP_RELEASE_SHA` to the expected commit to override the
build file explicitly. Invalid or secret-like values are ignored rather than
persisted.

If the operator uses a Render deploy hook instead of the dashboard, keep the
hook URL in the operator shell only as `RENDER_DEPLOY_HOOK_URL`. The URL is a
secret because it contains the deploy token. Triggering is still a manual action:
the helper refuses to call the hook unless `--yes` is present, appends the
current commit as `ref=<sha>`, accepts only `https://api.render.com/deploy/...`
hook URLs, and never prints the hook URL, token, response body, or full commit
hash.

```bash
python -m app.tools.trigger_render_deploy_hook --repo-root . --yes
python -m app.tools.verify_worker_release_freshness --repo-root .
```

```sql
select
  status,
  details->>'release_sha' as release_sha,
  details->>'release_source' as release_source,
  created_at
from public.worker_heartbeats
order by created_at desc
limit 1;
```

Before any live-mode consideration, run:

```bash
python -m app.tools.run_live_alert_drill_once
python -m app.tools.run_live_incident_response_drill_once --require-ack --ack-timeout-sec 300
python supabase/verify_hosted_live_readiness.py --env-file apps/worker/.env --env-file apps/desktop/.env.local
python supabase/verify_hosted_live_enable_flow.py --env-file apps/worker/.env --env-file apps/desktop/.env.local
python -m app.tools.verify_provider_lifecycle_evidence --evidence path/to/provider_lifecycle_evidence.json --verify-remote-artifacts
python -m app.tools.verify_incident_response_evidence --incident-output-file path/to/incident_output.txt --incident-channel-evidence path/to/incident_channel_evidence.json --verify-remote-channel-evidence
python -m app.tools.verify_system_order_scope_evidence --evidence path/to/system_order_scope_evidence.json --verify-remote-evidence
python -m app.tools.verify_security_scan_evidence --evidence path/to/security_scan_summary.json --repo-root .
python -m app.tools.verify_live_readiness_scorecard --scorecard docs/LIVE_READINESS_SCORECARD.md --security-evidence path/to/security_scan_summary.json --repo-root .
python -m app.tools.verify_worker_release_freshness --repo-root .
python -m app.tools.collect_live_readiness_evidence_bundle --environment production-readiness --reviewed-by release-admin --provider-evidence path/to/provider_lifecycle_evidence.json --provider-gap-evidence path/to/provider_gap_evidence.json --incident-output-file path/to/incident_output.txt --incident-channel-evidence path/to/incident_channel_evidence.json --security-scan-summary path/to/security_scan_summary.json --accept-system-order-scope --system-order-scope-evidence path/to/system_order_scope_evidence.json --system-order-scope-accepted-by scope-admin --output path/to/live_readiness_evidence_bundle.json
python -m app.tools.verify_live_readiness_evidence_bundle --evidence path/to/live_readiness_evidence_bundle.json --verify-remote-provider-artifacts --verify-remote-incident-evidence --verify-remote-system-order-scope-evidence
```

`--reviewed-by` becomes the final bundle `reviewed_by` value and must identify
a human release reviewer; identity segments such as `automation`, `bot`, `ci`,
`github-actions`, `script`, `service-account`, or `system` are rejected. It must
not match provider lifecycle reviewers, the retained incident ACK
`operator_ack_by`, or system-order-scope `accepted_by` identity. Provider
lifecycle `unknown_recovery.operator_reviewed_by` and `audit.reviewed_by` must
also be distinct normalized logical identities. Reusing one identity across
provider lifecycle, incident ACK, and scope acceptance evidence fails as
`bundle.evidence_operator_roles_must_be_distinct`.
The final bundle verifier also recomputes the current Git `HEAD` and tracked
plus untracked worktree diff hash, excluding the bundle and retained local
security report file, and rejects stale `security_scan.source_head` or
`security_scan.source_diff_sha256` values. If the Git source binding cannot be
collected, decoded, or read, the verifier fails closed with
`security_scan.source_binding_unavailable`; this is not live-readiness evidence.
`verify_worker_release_freshness` separately proves the manually deployed Render
worker is running the current commit by comparing hosted
`worker_heartbeats.details.release_sha` with local Git `HEAD` and by rejecting a
missing, mismatched, stale, or future heartbeat. A missing local Render CLI or a
successful push alone is not deploy freshness evidence.

The command must return `FINAL=PASS live_external_alert_drill` after delivering
four alert payloads for manual-check, stale-heartbeat, live-account-count failure,
and missing live-system-order-count scope acceptance scenarios, with
`delivered=4` and `max_latency_ms<=2000` retained in the final output line.

The incident response command must be run with the real Render
`ALERT_WEBHOOK_URL` and a human operator watching the incident channel. It must
return `FINAL=PASS live_incident_response_drill` with `delivered=4`,
`max_latency_ms<=2000`, `acknowledged=true`, and `ack_latency_ms<=300000`. A
local mock webhook, slow delivery line, scripted ACK, or ACK attributed to
automation instead of a human operator does not prove live incident readiness.
Retain a redacted incident-channel evidence manifest with
`channel_name`, `drill_id`, `evidence_uri`, `evidence_sha256`,
`operator_ack=true`, `operator_ack_by`, `captured_at`, and `operator_ack_at`;
the `drill_id` must match the command output, `captured_at` must be strictly after
`operator_ack_at`, and the bundle verifier requires this manifest in addition
to the command output. `operator_ack_by` must identify a human operator;
identity segments such as `automation`, `bot`, `ci`,
`github-actions`, `script`, `service-account`, or `system` are rejected.

The incident response evidence verifier must return
`FINAL=PASS incident_response_evidence` against the ACK-gated drill output and
retained incident-channel proof. `FINAL=FAIL incident_response_evidence`,
non-ACK dry runs, slow delivery metrics, future retained timestamps,
retained captures at or before `operator_ack_at`, mock/local channel evidence, weak,
non-HTTPS, invalid-host, or local/test/private/non-global-IP-host artifact URIs,
invalid SHA-256 values, channel `drill_id` mismatches,
missing human ACK metadata, automated ACK operator identities, extra incident
final-output metrics, suffixed incident check names, or duplicate incident final-output metrics are not
live-readiness evidence. The captured
`live_incident_response_drill` final line must use that exact check-name token and may contain only `delivered`,
`max_latency_ms`, `acknowledged`, `ack_latency_ms`, and `drill_id`, with
`max_latency_ms<=2000` and `ack_latency_ms<=300000`; raw channel payloads and
operator notes must stay in retained artifacts referenced by HTTPS URI and
SHA-256.
After retained incident-channel proof is published, rerun the verifier with
`--verify-remote-channel-evidence`; it rejects GitHub `blob` pages, caps response
size, and requires downloaded bytes to match `evidence_sha256` without printing
URI, body, or hash details on failure.

The hosted Supabase command must return `FINAL=PASS hosted_supabase_live_readiness`
against the staging/hosted project with `postgrest=1`, `anon_rpc_denied=2`,
`service_rpc_allowed=2`, `anon_table_denied=1`, `service_table_allowed=1`, and
`realtime=1`. `FINAL=SKIP hosted_supabase_env_missing` is not live-readiness
evidence.
`SUPABASE_URL` must be the official `https://<project_ref>.supabase.co` project
origin only: no local/test/private IP host, custom mock host, path, query/fragment,
or URL credentials. `SUPABASE_PUBLISHABLE_KEY` and `SUPABASE_SECRET_KEY` must be
distinct and the timeout must be positive before any hosted request is attempted.
This keeps a local mock, self-hosted endpoint, arbitrary HTTPS server, or reused
credential from satisfying the hosted gate.
The verifier also checks Data API table grants by requiring publishable-key access
to `bot_settings` to be denied while service-role access can select the singleton
row through PostgREST.

The hosted live-enable flow command must use two different hosted admin user JWTs
and return `FINAL=PASS hosted_live_enable_flow` with all seven gate metrics set to
`1`: requester admin, reviewer admin, request created, self-review denied, review
accepted, activation consumed once, and second activation denied. `FINAL=SKIP
hosted_live_enable_env_missing` is not live-readiness evidence.
Its `SUPABASE_URL` is subject to the same `.supabase.co` project origin restrictions, and
failure output must redact configured publishable/secret keys and both admin JWTs.
The requester/reviewer JWTs must be distinct from each other and must not reuse
the publishable or secret key values.

Also run the local execution-safety and recovery-path drills:

```bash
python -m app.tools.run_live_execution_safety_drill_once
python -m app.tools.run_live_recovery_drill_once
```

The execution safety command must return
`FINAL=PASS live_execution_safety_drill` before live-mode consideration, with
`missing_evidence_blocked=1`, `pre_broker_manual_check=1`,
`provider_result_recorded=1`, `duplicate_blocked=1`, and `broker_calls=1`
retained in the final output line. This is a local `ExecutionService` invariant
proof only; it does not replace a provider sandbox/live lifecycle drill.

The command must return `FINAL=PASS live_recovery_drill` before live-mode
consideration, with `reconciled_updates=1`, `manual_check_events=2`,
`cancel_confirmed=1`, `cancel_unknown=1`, `status_calls=4`, and `cancel_calls=2`
retained in the final output line. This is a local service-path proof only; it
does not replace a provider sandbox/live lifecycle drill.

The provider lifecycle evidence verifier must return
`FINAL=PASS provider_lifecycle_evidence` against a redacted evidence file retained
from a real Toss sandbox/live create/status/cancel/unknown-recovery/audit drill.
The file must include retained artifact entries for broker order receipt, provider
status export, cancel confirmation, unknown recovery review, and repository audit
export, each with a `drill_id` matching the evidence file, remote HTTPS artifact URI
with a valid DNS host or global IP, no local/test/private/non-global-IP host,
no private DNS suffix such as `.internal`, `.corp`, `.lan`, `.intranet`,
`.home`, or `.private`, no query/fragment, no URI credentials, unique retained
artifact URI, unique SHA-256, and capture timestamp strictly after its bound event, plus a matching terminal provider/local status
pair for the created `local_order_id`. The created order must include `created_at`,
status observations must be strictly increasing and strictly after `created_at`,
provider status values must be uppercase known Toss statuses from the worker mapping,
provider/local terminal statuses must not later regress or change terminal value,
and cancel proof must include `attempted_at` inside the drill window and strictly
after the created order `created_at` with exact provider `CANCELED` plus local
`canceled` for the same created `local_order_id`, backed by a `CANCELED` -> `canceled`
status observation after `attempted_at`, with no
provider `FILLED`, `REJECTED`, or `EXPIRED` observation for that order at or
before `attempted_at`. Artifact event anchors
are the created order `created_at`, latest provider status observation, the post-attempt
`CANCELED` -> `canceled` observation, unknown-recovery `operator_reviewed_at`,
and audit `reviewed_at`.
Unknown-recovery `operator_reviewed_by` and audit `reviewed_by` must identify
human operators; identity segments such as `automation`, `bot`, `ci`,
`github-actions`, `script`, `service-account`, or `system` are rejected. These
two provider lifecycle reviewers must also be distinct normalized logical
identities. Unknown-recovery `operator_reviewed_at` must be strictly after the
latest provider status observation, and audit `reviewed_at` must be strictly
after unknown-recovery `operator_reviewed_at`.
`FINAL=FAIL provider_lifecycle_evidence`,
local sample files, weak artifact manifests, status
observations for a different local order, out-of-order provider status timelines,
mismatched cancel/order IDs, missing or pre-attempt canceled status observations,
pre-cancel irreversible terminal observations,
mismatched terminal status pairs, unknown or lowercase provider status values,
artifact `drill_id`
mismatches, artifact timestamps not strictly after their bound events, duplicate artifact URIs after
hostname/default-HTTPS-port canonicalization, duplicate artifact hashes,
or unredacted provider identifiers are not
live-readiness evidence.
The final live-readiness evidence bundle verifier independently rejects weak
provider lifecycle PASS lines unless they contain only the allowed metrics and
prove `provider=toss`, `environment=sandbox` or `environment=live`,
`status_observations>=2`, `audit_logs_reviewed>=2`, and `evidence_artifacts=5`.
After retained provider lifecycle artifacts are published, rerun
`py -m app.tools.verify_provider_lifecycle_evidence --evidence <file> --verify-remote-artifacts`;
this rejects GitHub `blob` pages and requires downloaded artifact bytes to match
each declared SHA-256 before the evidence can be treated as durable release
proof.
The collector embeds the redacted `provider_lifecycle_evidence` payload in the
bundle, and the final verifier re-runs the provider lifecycle evidence validator,
including human-only operator review checks and exact final-output metric matching.
It also binds the provider lifecycle environment to the bundle target:
`staging` requires `environment=sandbox`, and `production-readiness` requires
`environment=live`.
The provider lifecycle evidence drill window and retained timestamps must not be
in the future beyond five minutes of clock skew. The provider lifecycle evidence
file is a closed schema: raw provider responses, screenshots, chat exports,
audit row dumps, and other ad hoc fields must remain in retained artifacts
referenced only by HTTPS URI and SHA-256.

The system-order-scope evidence verifier must return
`FINAL=PASS system_order_scope_evidence` against retained operator evidence for
the exact `system_created_live_orders_only` limitation. Local/sample/non-HTTPS
evidence URIs, invalid-host evidence URIs,
local/test/private/non-global-IP-host HTTPS evidence URIs,
query-string or fragment-bearing artifact URLs, invalid SHA-256 values,
missing `LIVE_SYSTEM_ORDER_COUNT_SCOPE_ACCEPTED=true` confirmation, missing or
future `evidence_captured_at`, evidence captured at or before `accepted_at`, or an
operator mismatch are not live-readiness evidence. `accepted_by` must identify a
human operator; identity segments such as `automation`, `bot`, `ci`,
`github-actions`, `script`, `service-account`, or `system` are rejected.
The final release bundle reviewer must be a different human operator from this
scope acceptance operator.
For the final release bundle, the scope evidence deployment environment must
match the bundle target: `staging` requires `deployment_environment=staging`,
and `production-readiness` requires `deployment_environment=production`.
After retained system-order-scope proof is published, rerun the verifier with
`--verify-remote-evidence`; it rejects GitHub `blob` pages, caps response size,
and requires downloaded bytes to match `evidence_sha256` without printing URI,
body, or hash details on failure.

The security scan evidence verifier must return
`FINAL=PASS security_scan_evidence` against the independent Codex Security replay
summary. The summary must bind to the current `source_head`/`source_diff_sha256`,
include `scan_profile=security_diff_scan`, zero reportable findings, completion
receipts exactly equal to every worklist row, threat-model and finding-discovery
receipts, validation and attack-path receipts matching the candidate finding
count, and a retained report artifact `report_uri` plus `report_path` whose
actual markdown report file bytes hash to `report_sha256`. The `report_path` and
`report_uri` must reference the retained `.md` or `.markdown` report, not the
security summary JSON or another sidecar file. `report_path` must be relative to
the security summary JSON directory and must not be absolute or escape that
directory. The collector validates that local retained report path and SHA-256
before collecting Git source binding, so artifact errors are fixed before stale
source binding errors. Summary-only replay metadata, future
`completed_at`, missing phase receipts, missing or mutated local markdown report files, local/sample/non-HTTPS
report URIs, invalid-host report URIs,
local/test/private/non-global-IP-host HTTPS report URIs, credential-bearing URIs, query-string or fragment-bearing artifact
URLs, invalid report SHA-256 values, stale source bindings, or unavailable source
binding collection block live-mode
consideration. Run this standalone verifier with `--repo-root` so it recomputes
the current Git source binding before bundle collection.

After the individual gates pass, collect their exact single-line `FINAL=PASS`
outputs, the real incident-channel evidence, the system-created-order scope
acceptance, and the independent Codex Security replay summary into a redacted
bundle. Prefer `collect_live_readiness_evidence_bundle`; it runs the local and
hosted command gates, reads the retained provider lifecycle, provider gap source,
incident-output, incident-channel, security, and system-order-scope evidence,
writes the bundle only after validation passes, and returns
`FINAL=PASS live_readiness_evidence_collector`. The bundle verifier must also
return `FINAL=PASS live_readiness_evidence_bundle`; when run from a bundle file,
it reads `security_scan.report_path`, rejects absolute or directory-escaping
report paths, and rechecks the local report file SHA-256 against
`security_scan.report_sha256`. With `--verify-remote-provider-artifacts`,
`--verify-remote-incident-evidence`, and
`--verify-remote-system-order-scope-evidence`, it also fetches the retained
provider lifecycle, incident-channel, and system-order-scope evidence bytes,
rejects GitHub `blob` pages, caps response size, and requires every downloaded
body to match its declared SHA-256. The final PASS line must include
`remote_provider_artifacts=1`, `remote_incident_evidence=1`, and
`remote_system_order_scope_evidence=1`; `0` on any remote flag means the bundle
was only locally validated and is not post-publication release evidence. It also rejects missing, duplicate, unknown,
or weak hosted Supabase, worker release freshness, and local execution/recovery/alert final-output metrics, multi-line
final output, any `FINAL=PASS` line whose check-name token is not exactly the
expected gate name, mutated live-enable migration output, and unknown root, check, and nested evidence fields
so raw logs, screenshots, or ad hoc evidence payloads must stay in retained
artifacts referenced only by HTTPS URI and SHA-256. Retained evidence references
must be distinct across the entire bundle: incident-channel proof,
system-order-scope proof, provider gap source proof, security report proof, and
every provider lifecycle artifact must not reuse the same retained URI or
SHA-256. The collector output
path must also be a new distinct local artifact and must not equal any input
evidence file or the local security `report_path`; duplicate local artifact
paths fail before bundle output is written with
`collector_artifact_paths_must_be_distinct`, without printing local path
details. Existing collector output paths fail before bundle output is written
with `collector_output_path_must_not_exist`, without printing local path
details. The collector writes the bundle with exclusive file creation, so an
output file created concurrently during collection also fails with
`collector_output_path_must_not_exist`; other output write failures fail as
`collector_output_path_unwritable`, without printing local path details. Collector
source-binding and system-order-scope operator mismatches must produce fixed
failure codes only; they must not print expected or supplied hashes or operator
values. Collector-run gate commands must exit with process return code `0`;
nonzero exit status fails closed as `<check_name>_command_returncode_nonzero`
before stdout/stderr final-line content is trusted. Collector-run gate output
and the retained incident drill output must contain no non-empty non-final
lines; debug logs, stack traces, warnings, or other side-channel text fail
closed before the final line is trusted.
`FINAL=SKIP`, local mock ACK evidence, future bundle or retained evidence timestamps,
bundle `reviewed_at` values at or before `generated_at`,
incident evidence captured at or before human ACK, system-order-scope evidence captured
at or before acceptance, missing incident-channel proof, sample
provider evidence, missing or weak system-order-scope acceptance evidence,
system-order-scope operator mismatch, a security scan `source_head`/
`source_diff_sha256` mismatch or unavailable source-binding collection against
the current worktree (retained evidence files and the `report_path` file
excluded), a security scan `report_sha256`
mismatch against the actual `report_path` file, missing security phase receipts,
security scan worklist/candidate receipt mismatches, provider artifact `drill_id`
mismatches, duplicate provider
artifact SHA-256 values, reused bundle-level retained evidence URI/SHA-256 values,
mismatched provider/local terminal status pairs,
status observations for the wrong local order, incident channel `drill_id`
mismatches, unknown or duplicate incident final-output metrics,
multi-line final output,
mutated or suffixed final-output check names,
additional provider partial warnings beyond the single documented Toss
system-created-order scope limitation, a provider partial warning count/id mismatch,
or a `warning_partial_gaps=1` line whose `warning_partial_gap_ids` does not exactly
name that Toss scope limitation, scope deployment environment mismatch,
provider lifecycle environment mismatch, security report artifact proof with a
missing, weak, non-HTTPS, invalid-host, or local/test/private/non-global-IP-host URI,
reused retained evidence URI after hostname/default-HTTPS-port canonicalization, or a partial,
summary-only, or non-independent security replay block live-mode consideration.
