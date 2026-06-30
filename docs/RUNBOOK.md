# Runbook

Worker down:

1. Keep `live_order_allowed=false`.
2. Check Render logs.
3. Check latest `worker_heartbeats`.
4. Restart worker.
5. Verify heartbeat before paper resume.

API outage:

1. Confirm `api_health`.
2. For Toss/Supabase outage, live orders remain blocked.
3. For OpenAI outage, use cached news risk or block affected new buys.

Toss read-only verification:

1. Confirm worker-side env vars are set in Render or local `.env`:

```text
TOSS_CLIENT_ID=...
TOSS_CLIENT_SECRET=...
TOSS_ACCOUNT_ID=<optional Toss accountSeq from GET /api/v1/accounts>
MOCK_PROVIDERS=false
```

`TOSS_ACCOUNT_ID` is optional only when `GET /api/v1/accounts` returns exactly
one account; the worker will infer that single `accountSeq`. If Toss returns no
account or more than one account, live/account-scoped calls fail closed until
`TOSS_ACCOUNT_ID` is set. When set, `TOSS_ACCOUNT_ID` must contain Toss
`accountSeq`, not a raw account number. Never put these values in Desktop env
vars.

2. Keep trading fail-closed:

```sql
update public.bot_settings
set enabled = false,
    mode = 'paper',
    live_order_allowed = false,
    updated_at = now()
where id = 'singleton'
returning id, enabled, mode, live_order_allowed;
```

3. Run the read-only command from `apps/worker`:

```bash
python -m app.tools.test_toss_readonly
```

On Windows where `python` points to the Microsoft Store shim, use:

```bash
py -m app.tools.test_toss_readonly
```

4. The command may call only verified read-only Toss endpoints. It must not call `POST /api/v1/orders`, cancel, or modify endpoints. It must not print `TOSS_CLIENT_SECRET`; account identifiers are masked.

Verified read-only Toss endpoints currently include:

- `GET /api/v1/accounts`
- `GET /api/v1/buying-power`
- `GET /api/v1/holdings`
- `GET /api/v1/prices`
- `GET /api/v1/candles`
- `GET /api/v1/market-calendar/KR`
- `GET /api/v1/orders`
- `GET /api/v1/orders/{orderId}`

5. Verify Toss health was recorded:

```sql
select distinct on (provider)
       provider,
       healthy,
       checked_at,
       message,
       error_code,
       details
from public.api_health
where provider = 'toss'
order by provider, checked_at desc;
```

6. Confirm no live order statuses were created:

```sql
select *
from public.orders
where status in ('sent', 'filled', 'partial_filled')
order by created_at desc
limit 20;
```

This query must return no rows after `test_toss_readonly`.

Live account fail-closed check:

1. Before enabling live controls for a controlled dry run, keep
   `LIVE_SYSTEM_ORDER_COUNT_SCOPE_ACCEPTED=false` unless the operator has recorded explicit acceptance that
   live operation is limited to system-originated orders. This is required while Toss broker-wide closed-order
   history remains unavailable for externally placed daily orders. The retained scope evidence must name the
   target deployment environment, and the final bundle rejects environment mixing: `staging` bundles require
   `deployment_environment=staging`, while `production-readiness` bundles require
   `deployment_environment=production`.

2. If that acceptance is missing, the worker must record
   `message='live_external_order_history_scope_not_accepted'`, persist a blocked live order with
   `daily_order_count_unverified`, and make no broker call.

3. If live controls are enabled after that acceptance, verify the system-created live order count sync:

```sql
select level, component, message, details, created_at
from public.engine_events
where component = 'live_account'
order by created_at desc
limit 20;
```

4. `message='live_system_order_count_sync_failed'` means the worker read Toss cash buying power and holdings,
   but blocked before broker execution because the local `orders` count for the current KST trading day could
   not be verified.

5. Do not override this by manually editing orders or risk snapshots. Keep `live_order_allowed=false` until
   the count sync failure is resolved. Toss broker-wide closed-order history is still not used for this gate
   while `status=CLOSED` remains documented as `400 closed-not-supported`.

Live approval audit check:

1. Verify each accepted live enable command has immutable review evidence:

```sql
select id,
       status,
       requested_by,
       reviewed_by,
       reviewed_at,
       applied_at,
       expires_at,
       payload
from public.manual_commands
where command_type = 'request_live_enable'
order by created_at desc
limit 20;
```

2. Accepted rows must have non-empty `provider_contract_version`, `risk_report_id`, and `release_version`
   in `payload`, `reviewed_by <> requested_by`, `expires_at > reviewed_at`, and `applied_at is null`.

3. If an operator needs different evidence or expiry, create a new `request_live_enable` row. Do not manually
   update an accepted or rejected row; the migration trigger rejects finalized-row mutation except the
   database-owned accepted-to-applied consumption during live enable.

4. After `live_order_allowed` is enabled, the accepted row used for that transition must move to
   `status='applied'` with `applied_at` populated. Re-enabling live after disabling it requires a new
   pending request and a new reviewer.

5. Confirm audit records exist for live control changes:

```sql
select action, target_table, target_id, actor_user_id, created_at
from public.audit_logs
where target_table in ('manual_commands', 'bot_settings')
order by created_at desc
limit 50;
```

Hosted Supabase readiness gate:

1. After applying migrations through `0011_data_api_grants.sql` to a hosted
   Supabase staging project, set these variables only in the worker/operator shell:

```bash
SUPABASE_URL=...
SUPABASE_PUBLISHABLE_KEY=...
SUPABASE_SECRET_KEY=...
```

`VITE_SUPABASE_PUBLISHABLE_KEY` may be used instead of `SUPABASE_PUBLISHABLE_KEY`
when reusing the desktop staging publishable key. Never put `SUPABASE_SECRET_KEY`
in desktop env, Git, docs, logs, seed data, or `render.yaml`.

2. Run the hosted verifier from the repository root:

```bash
python supabase/verify_hosted_live_readiness.py
```

On Windows:

```bash
py supabase\verify_hosted_live_readiness.py
```

3. The command must print:

```text
FINAL=PASS hosted_supabase_live_readiness postgrest=1 anon_rpc_denied=2 service_rpc_allowed=2 anon_table_denied=1 service_table_allowed=1 authenticated_table_allowed=2 realtime=1
```

4. Run the hosted live-enable user-session verifier with two different admin user
   access tokens from the hosted/staging project. Keep these JWTs in the
   operator shell only:

```bash
SUPABASE_LIVE_REQUESTER_JWT=...
SUPABASE_LIVE_REVIEWER_JWT=...
python supabase/verify_hosted_live_enable_flow.py
```

On Windows:

```bash
py supabase\verify_hosted_live_enable_flow.py
```

The command must print:

```text
FINAL=PASS hosted_live_enable_flow requester_admin=1 reviewer_admin=1 request_created=1 self_review_denied=1 review_accepted=1 activation_consumed_once=1 second_activation_denied=1
```

The final live-readiness evidence bundle verifier independently requires the exact
hosted Supabase metrics above. `hosted_supabase_live_readiness` must retain
`postgrest=1`, `anon_rpc_denied=2`, `service_rpc_allowed=2`,
`anon_table_denied=1`, `service_table_allowed=1`, and `realtime=1`.
`hosted_live_enable_flow` must retain all seven gate metrics with value `1`.
Missing, duplicate, unknown, or weaker hosted metrics block bundle verification
even when the line starts with `FINAL=PASS`.

Both hosted verifiers fail before network calls for obvious unsafe configuration:
non-positive timeout, reused `SUPABASE_PUBLISHABLE_KEY`/`SUPABASE_SECRET_KEY`,
identical requester/reviewer JWTs, or user JWTs that reuse the publishable or
secret key values. Failure output must not print the configured key or JWT values.

This proves the hosted RLS/user-session path, not only service-role SQL, can
create the live-enable request, block self-review, accept review by a different
admin, consume one approval during live activation, and deny a second activation
without a new approval. The verifier refuses to start if an unapplied accepted
live-enable command already exists, to avoid consuming an unrelated operator
approval.

5. `FINAL=SKIP hosted_supabase_env_missing` or
   `FINAL=SKIP hosted_live_enable_env_missing` means the hosted/staging evidence is
   absent. Treat the Supabase control-plane score as below 100 until a real hosted
   run returns both `FINAL=PASS` lines.

6. `FINAL=FAIL hosted_supabase_live_readiness` or `FINAL=FAIL hosted_live_enable_flow`
   means do not enable live mode. Inspect hosted migration state, API keys, admin
   user roles, RLS policies, PostgREST schema cache, function/table grants,
   live-enable trigger behavior, and Realtime publication settings before retrying.

Provider contract gap gate:

1. Before any live-readiness claim or live enable request, run the provider gap gate from `apps/worker`:

```bash
python -m app.tools.check_provider_contract_gaps \
  --system-order-scope-evidence path/to/system_order_scope_evidence.json \
  --provider-gap-evidence path/to/provider_gap_evidence.json
```

On Windows:

```bash
py -m app.tools.check_provider_contract_gaps `
  --system-order-scope-evidence path\to\system_order_scope_evidence.json `
  --provider-gap-evidence path\to\provider_gap_evidence.json
```

2. The command must print a counted final line before production live operation is considered ready:

```text
FINAL=PASS provider_contract_gaps total_gaps=18 blocking_unknown_gaps=0 invalid_status_gaps=0 warning_partial_gaps=1 warning_partial_gap_ids=toss:live-account-state-sync-for-scheduled-cycle:partial-system-only-accepted-fail-closed system_order_scope_accepted=1 provider_gap_evidence=1
```

`FINAL=FAIL provider_contract_gaps ...`, any nonzero `blocking_unknown_gaps`, or
any nonzero `invalid_status_gaps` means at least one row in `docs/API_GAPS.md`
still has `Status=unknown`, an unrecognized status typo, or an unaccepted
documented provider scope warning; do not enable live mode. With the documented
Toss partial warning still present, running this command without
`--system-order-scope-evidence` must return `FINAL=FAIL ... system_order_scope_accepted=0`.
Running it without a valid `--provider-gap-evidence` manifest must return
`FINAL=FAIL ... provider_gap_evidence=0`. That manifest must bind the exact
`docs/API_GAPS.md` SHA-256, every parsed provider gap id in order, and retained
HTTPS source artifacts with provider-matching gap coverage, unique retained URI,
unique SHA-256, non-future `captured_at`, and no secret-like keys.
The live-readiness bundle verifier rejects a bare `FINAL=PASS` because it hides
whether unknown, invalid, partial, system-order-scope acceptance, or provider
gap source evidence was present.

3. Only explicitly allowed `partial-*` statuses are warnings, not automatic pass-to-live
   evidence. A new or misspelled `partial-*` status must appear as
   `invalid_status_gaps>0` until `provider_gap_gate.py`, `docs/API_GAPS.md`, and
   the scorecard all document the exact fail-closed operational limitation. Keep
   the relevant runtime path fail-closed. The final bundle currently allows at
   most one `warning_partial_gaps` entry, and its
   `warning_partial_gap_ids` value must be exactly the documented Toss
   system-created-order scope limitation paired with retained
   `system_order_scope_evidence` and `system_order_scope_accepted=1` in the
   provider gap final line, plus `provider_gap_evidence=1`. `warning_partial_gaps>1`,
   `warning_partial_gaps=1` with any other id, `system_order_scope_accepted=0`,
   `provider_gap_evidence=0`, or a warning count/id mismatch blocks final bundle
   verification.

Live order reconciliation:

1. Do not create new live orders while reconciling:

```sql
update public.bot_settings
set enabled = false,
    updated_at = now()
where id = 'singleton'
returning id, enabled, mode, live_order_allowed;
```

2. Run the reconciliation command from `apps/worker`:

```bash
python -m app.tools.reconcile_live_orders_once --limit 50
```

On Windows:

```bash
py -m app.tools.reconcile_live_orders_once --limit 50
```

3. Review any rows still requiring manual investigation:

```sql
select id, symbol, status, provider_order_id, reason, provider_payload_summary, updated_at
from public.orders
where mode = 'live'
  and status = 'unknown_requires_manual_check'
order by updated_at desc
limit 50;
```

4. Review reconciliation events:

```sql
select level, message, details, created_at
from public.engine_events
where component = 'live_reconciliation'
order by created_at desc
limit 50;
```

Local live execution safety drill:

1. Run the local execution safety drill from `apps/worker`. It uses
   `ExecutionService`, `RiskService`, `InMemoryRepository`, and a local drill
   broker; it does not call Toss, Supabase, or live order endpoints.

```bash
python -m app.tools.run_live_execution_safety_drill_once
```

On Windows:

```bash
py -m app.tools.run_live_execution_safety_drill_once
```

2. The command must print:

```text
FINAL=PASS live_execution_safety_drill missing_evidence_blocked=1 pre_broker_manual_check=1 provider_result_recorded=1 duplicate_blocked=1 broker_calls=1
```

The final live-readiness evidence bundle verifier requires these exact counts.
Missing, duplicate, unknown, or weaker execution safety metrics block bundle
verification even if the line starts with `FINAL=PASS live_execution_safety_drill`.

3. This proves the local `ExecutionService` path blocks missing live decision
   evidence before broker placement, persists a manual-check row before a broker
   call, records the provider result back onto that same row, and blocks duplicate
   idempotency-key retry without a second broker call.

4. The live broker path must also reject mock/static or unready strategy
   features. Live mode must use `provider_live_v1` feature evidence with
   retained quote, fundamentals, news, positive PER/PBR valuation inputs, and
   market/sector provenance; mocked, unconfigured, missing provider, missing
   valuation, or missing market/sector inputs must keep `live_trading_ready=false`
   and record `live_feature_snapshot_not_ready`. Broker placement can continue
   only when `live_trading_ready=true` and `feature_source` is non-mock.

5. This is not a provider sandbox/live proof. Before live operation, still run a
   real provider lifecycle drill and audit the retained provider evidence.

Local live recovery drill:

1. Run the local service-path drill from `apps/worker`. It uses `InMemoryRepository`,
   `OrderReconciliationService`, and `LiveOrderCancellationService`; it does not call Toss,
   Supabase, `BrokerPort.place_order`, or live order endpoints.

```bash
python -m app.tools.run_live_recovery_drill_once
```

On Windows:

```bash
py -m app.tools.run_live_recovery_drill_once
```

2. The command must print:

```text
FINAL=PASS live_recovery_drill reconciled_updates=1 manual_check_events=2 cancel_confirmed=1 cancel_unknown=1 status_calls=4 cancel_calls=2 pending_order_blocked=1 manual_check_preserved=1
```

The final live-readiness evidence bundle verifier also requires these exact
recovery drill counts. Missing, duplicate, unknown, or weaker recovery metrics
block bundle verification even if the line starts with `FINAL=PASS live_recovery_drill`.

3. This proves the local operational recovery path still exercises:

- reconciliation of a live `sent` order to provider `filled`
- critical alert/audit when an existing `unknown_requires_manual_check` order remains unknown
- no automatic clearing of `unknown_requires_manual_check` when a later provider read reports a terminal
  status; review `live_order_manual_check_provider_status_observed` and complete operator recovery first
- operator cancel that only marks local `canceled` after provider `CANCELED` confirmation
- cancel confirmation failure that moves the local order to `unknown_requires_manual_check`
- fail-closed cycle blocking when pending live reconciliation remains after the reconciliation pass

4. This is not a provider sandbox/live proof. Before live operation, still run a real
   provider lifecycle drill and audit the resulting `orders`, `engine_events`, and `audit_logs`.

Provider lifecycle evidence verifier:

1. After a real Toss sandbox or live lifecycle drill, save a redacted JSON evidence file.
   The file must not contain `authorization`, tokens, client secrets, account numbers, raw
   provider order identifiers, raw provider responses, screenshots, chat exports, or audit
   row dumps. Provider order and cancel ids must be short redactions such as
   `toss_order_...abcd` or `redacted:<stable suffix>`. Raw drill artifacts must stay in
   retained artifact storage and be referenced only by URI plus SHA-256.

2. The evidence must cover all of these observed surfaces:

- `live_order_allowed_before=false` and `live_order_allowed_after=false`
- created local live order row with Korean stock symbol, action, order type, positive KRW amount,
  `created_at` inside the drill window, and redacted provider order id
- at least two provider status observations for the created `local_order_id`, including a
  strictly increasing `observed_at` sequence strictly after `created_at`, and a matching terminal
  provider/local pair such as `FILLED` -> `filled`, `CANCELED` -> `canceled`, or fail-closed
  rejected/unknown states. Every `provider_status` must be an uppercase known Toss status
  from the worker mapping (`PENDING`, `PENDING_CANCEL`, `PENDING_REPLACE`, `PARTIAL_FILLED`,
  `FILLED`, `CANCELED`, `REJECTED`, or `EXPIRED`); unknown raw status strings block evidence.
  Once a provider or local terminal status is observed, later observations must not regress
  to non-terminal status or change to a different terminal status.
- cancel probe for the same created `local_order_id` with `attempted_at` inside the drill window,
  `attempted_at` strictly after the created order `created_at`, provider `CANCELED`, local
  `canceled`, and a `CANCELED` -> `canceled` provider status observation after the cancel attempt
- no provider `FILLED`, `REJECTED`, or `EXPIRED` observation for the created
  `local_order_id` at or before the cancel attempt; irreversible terminal
  evidence blocks cancel proof even when a later `CANCELED` observation exists
- unknown-state recovery with `live_order_manual_check_still_unknown` and operator review evidence
  recorded after the latest provider status observation
- audit review after the unknown-state recovery operator review, covering at least
  3 orders, 2 engine events, and 2 audit logs
- `operator_reviewed_by` and `reviewed_by` must identify human logical operator
  handles, not email addresses, URLs, paths, raw contact values, or retained
  artifact references; automated identity segments such as `automation`, `bot`,
  `ci`, `github-actions`, `script`, `service-account`, or `system` block
  provider lifecycle evidence. Unknown-recovery review and repository audit
  review must also use distinct human logical handles; matching normalized
  identities fail the provider lifecycle verifier. The final bundle release
  reviewer must be a different human operator from these provider lifecycle
  reviewers; matching normalized identities are rejected as provider lifecycle
  self-review.
- retained HTTPS artifact evidence for `broker_order_receipt`, `provider_status_export`,
  `cancel_confirmation`, `unknown_recovery_review`, and `repository_audit_export`.
  Each artifact entry must include a `drill_id` matching the top-level evidence
  `drill_id`, a retained remote HTTPS URI with a valid DNS host or global IP
  and no private DNS suffix such as `.internal`, `.corp`, `.lan`,
  `.intranet`, `.home`, or `.private`, an artifact path without raw or
  percent-decoded `.`/`..` traversal or encoded slash/backslash separators, a unique retained
  artifact URI, a unique 64-hex SHA-256, and capture timestamp inside the drill window and strictly
  after its bound event: broker order `created_at`, latest provider status
  observation, the post-attempt `CANCELED` -> `canceled` observation,
  unknown-recovery `operator_reviewed_at`,
  or audit `reviewed_at`. Local, mock,
  sample, fixture, invalid-host, malformed-port, private-DNS, local/test/private/non-global-IP-host,
  non-HTTPS, credential-bearing, signed/query-string, path-traversal, or encoded-separator URIs,
  artifacts from a different drill, reused artifact URIs after
  hostname/default-HTTPS-port and unreserved percent-encoding canonicalization,
  or reused artifact hashes are rejected.
  After the retained artifacts are published, rerun the same verifier with
  `--verify-remote-artifacts`; this fetches each `evidence_artifacts[].uri`,
  rejects GitHub `blob` pages, caps downloaded bytes, and requires the remote
  artifact bytes to match the declared SHA-256 without echoing the URI, body, or
  hash value in failure output.

3. Run the verifier from `apps/worker`:

```bash
python -m app.tools.verify_provider_lifecycle_evidence --evidence path/to/provider_lifecycle_evidence.json
```

On Windows:

```bash
py -m app.tools.verify_provider_lifecycle_evidence --evidence path/to/provider_lifecycle_evidence.json
```

After the retained artifacts are published, run the release-byte check:

```bash
py -m app.tools.verify_provider_lifecycle_evidence --evidence path/to/provider_lifecycle_evidence.json --verify-remote-artifacts
```

4. The command must print:

```text
FINAL=PASS provider_lifecycle_evidence provider=toss environment=sandbox status_observations=... audit_logs_reviewed=... evidence_artifacts=5
```

For a production-readiness bundle, the retained provider evidence must instead
come from the live Toss lifecycle surface and print `environment=live`; sandbox
evidence is acceptable only for a `staging` bundle.

The final live-readiness evidence bundle verifier also requires these exact
provider lifecycle metrics: `provider=toss`, `environment=sandbox` or
`environment=live`, `status_observations>=2`, `audit_logs_reviewed>=2`, and
`evidence_artifacts=5`. The collector embeds the redacted provider lifecycle
evidence payload in the bundle, and the final verifier re-runs the provider
lifecycle validator, including human-only operator review checks and exact
final-output metric matching. It then binds the provider lifecycle environment to
the bundle target: `staging` requires `environment=sandbox`, and
`production-readiness` requires `environment=live`. Missing, duplicate, unknown,
weaker, or target-mismatched metrics block bundle verification even if the line
starts with `FINAL=PASS provider_lifecycle_evidence`.

5. `FINAL=FAIL provider_lifecycle_evidence` blocks live-mode consideration. The default verifier does not call
   Toss, Supabase, live order endpoints, or artifact storage; it only validates retained evidence from an already-run
   provider sandbox/live drill. With `--verify-remote-artifacts`, it calls only the retained HTTPS artifact
   URIs and requires the downloaded bytes to match the recorded hashes. Local fixtures or sample files are not provider proof. The verifier
   rejects unknown root, order, status, cancel, unknown-recovery, audit, and artifact fields,
   cancel probes for a different `local_order_id`,
   missing or pre-attempt canceled status observations,
   pre-cancel irreversible terminal observations,
  plus artifact timestamps not strictly after their bound lifecycle events, so
  ad hoc payloads cannot be embedded in the release evidence file.

Live external alert drill:

1. Run the local dry-run drill from `apps/worker`. It uses an in-memory repository and does not call Toss,
   Supabase, or live order endpoints. If `ALERT_WEBHOOK_URL` is unset, the command uses a mock external
   webhook transport. If `ALERT_WEBHOOK_URL` is set, it sends the same redacted alert payloads to that
   worker-side webhook URL:

```bash
python -m app.tools.run_live_alert_drill_once
```

On Windows:

```bash
py -m app.tools.run_live_alert_drill_once
```

2. The command must print:

```text
FINAL=PASS live_external_alert_drill delivered=4 max_latency_ms=...
```

The final live-readiness evidence bundle verifier requires `delivered=4` and
`max_latency_ms<=2000` for this local alert drill. Missing, duplicate, unknown, or
weaker alert metrics block bundle verification even if the line starts with
`FINAL=PASS live_external_alert_drill`.

3. This proves the worker alert path emits a `critical` `engine_events` entry and delivers redacted
   external alert payloads for:

- `live_order_manual_check_still_unknown`
- `worker_heartbeat_stale`
- `live_system_order_count_sync_failed`
- `live_external_order_history_scope_not_accepted`

4. The reconciliation part of the drill specifically proves
   `message='live_order_manual_check_still_unknown'` when a live order remains
   `unknown_requires_manual_check` and provider status lookup times out.

5. In a real incident, verify the same dashboard-visible event in Supabase:

```sql
select level, component, message, details, created_at
from public.engine_events
where component = 'live_reconciliation'
  and level = 'critical'
order by created_at desc
limit 20;
```

Live incident response drill:

1. The alert delivery drill above proves only that critical alerts can be
   delivered. Before treating the incident process as live-ready, run the
   response drill from `apps/worker`:

```bash
python -m app.tools.run_live_incident_response_drill_once
```

On Windows:

```bash
py -m app.tools.run_live_incident_response_drill_once
```

The non-blocking dry run must print:

```text
FINAL=PASS live_incident_delivery_drill delivered=4 max_latency_ms=... ack_required=false
```

2. To produce live-readiness evidence, set the real worker-side
   `ALERT_WEBHOOK_URL`, have the on-call operator watch the real incident
   channel, and run the ACK-gated mode:

```bash
python -m app.tools.run_live_incident_response_drill_once --require-ack --ack-timeout-sec 300
```

The operator must type the exact `ACK <drill_id>` phrase printed by the
command. The command must then print:

```text
FINAL=PASS live_incident_response_drill delivered=4 max_latency_ms=... acknowledged=true ack_latency_ms=... drill_id=...
```

The final live-readiness evidence bundle verifier requires `delivered=4`,
`max_latency_ms<=2000`, `acknowledged=true`, `ack_latency_ms<=300000`, and a
matching retained incident-channel `drill_id`. A slower incident delivery line
is not live-readiness evidence even if the human ACK arrives before the timeout.
The ACK must be attributed to a human operator; `operator_ack_by` identity
segments such as `automation`, `bot`, `ci`, `github-actions`, `script`,
`service-account`, or `system` are rejected as automated ACK evidence.
`operator_ack_by` must be an internal logical operator handle such as
`ops-admin-2`, not an email address, URL, path, webhook endpoint, raw contact
value, or retained artifact reference.

3. Save the command output, incident-channel message permalink or screenshot,
   `drill_id`, human operator identity, and response-time evidence with the
   live-readiness review. A local mock webhook or scripted stdin ACK is useful
   regression coverage, but it is not a real incident-channel proof.

4. Before assembling the live-readiness bundle, validate the retained incident
   evidence from `apps/worker`:

```bash
python -m app.tools.verify_incident_response_evidence \
  --incident-output-file path/to/incident_output.txt \
  --incident-channel-evidence path/to/incident_channel_evidence.json \
  --verify-remote-channel-evidence
```

On Windows:

```bash
py -m app.tools.verify_incident_response_evidence `
  --incident-output-file path\to\incident_output.txt `
  --incident-channel-evidence path\to\incident_channel_evidence.json `
  --verify-remote-channel-evidence
```

The command must print:

```text
FINAL=PASS incident_response_evidence delivered=4 max_latency_ms=... ack_latency_ms=... channel=... operator_ack_by=...
```

`FINAL=FAIL incident_response_evidence` blocks live-mode consideration. The
verifier rejects non-ACK dry runs, missing or slow delivery/latency metrics,
mock/local channel names or URIs, channel evidence whose `drill_id` does not
match the incident output, non-HTTPS, non-retained, invalid-host, malformed-port,
or local/test/private/non-global-IP-host evidence URIs, query-string
or fragment URIs, raw or percent-decoded path traversal, encoded slash/backslash
path separators, invalid SHA-256 values, missing human ACK metadata, automated
or email/URL/path-like ACK operator identities, and unexpected secret-like evidence keys. The captured
incident command
`FINAL=PASS` line is also a closed contract: its check-name token must be exactly
`live_incident_response_drill`, not a suffixed or preview variant, and it may contain
only `delivered`, `max_latency_ms`, `acknowledged`, `ack_latency_ms`, and `drill_id`
metrics, with `max_latency_ms<=2000` and `ack_latency_ms<=300000`. Extra or
duplicate metrics such as webhook URLs, channel payloads, or ad hoc operator
notes block live-mode consideration and must stay in retained channel evidence
artifacts. With `--verify-remote-channel-evidence`, the verifier also fetches
the retained incident-channel `evidence_uri`, rejects GitHub `blob` pages, caps
response size, and requires the downloaded bytes to match `evidence_sha256`
without printing the URI, body, or hash values on failure.

Live readiness evidence bundle:

1. After all individual gates above have passed against their real surfaces, create
   one redacted JSON evidence bundle. It must contain only single-line `FINAL=PASS`
   outputs and metadata. Do not include JWTs, API keys, webhook URLs, account
   numbers, provider raw ids, or screenshots containing secrets.
   Every collector-run gate command must also exit with process return code `0`;
   a nonzero exit status fails closed as `<check_name>_command_returncode_nonzero`
   before stdout/stderr final-line content is trusted.
   Collector-run gate output and the retained incident drill output must contain
   no non-empty non-final lines; debug logs, stack traces, warnings, or other
   side-channel text fail closed before the final line is trusted.

2. The bundle must include:

- hosted Supabase readiness output from the hosted/staging project
- hosted live-enable user-session output from two distinct admin users
- provider lifecycle verifier output from a retained Toss sandbox/live drill whose
  status observations match the created `local_order_id`, artifact entries match
  the evidence `drill_id`, artifact hashes are unique SHA-256 values, and
  artifact URIs are unique after hostname/default-HTTPS-port canonicalization
- ACK-gated incident response output from the real incident channel
- incident-channel evidence manifest with non-future retained remote HTTPS permalink/artifact hash,
  `captured_at` strictly after `operator_ack_at`, and human ACK metadata from a
  non-automated logical operator handle
- local migration, recovery, alert, and provider-gap gate outputs
- explicit `system_created_live_orders_only` scope acceptance while Toss
  broker-wide closed-order history remains unavailable, including retained
  operator evidence that `LIVE_SYSTEM_ORDER_COUNT_SCOPE_ACCEPTED=true` was
  confirmed for the deployment
- independent Codex Security replay summary with `scan_profile=security_diff_scan`,
  zero reportable findings, completion receipts exactly equal to every worklist
  row, threat-model and finding-discovery receipts, validation and attack-path
  receipts for every candidate finding, logical `scan_id`, retained remote HTTPS report URI, `report_path`,
  `report_sha256` matching the actual report file bytes, non-future `completed_at`, plus `source_head` and
  `source_diff_sha256` matching the current worktree. After commit/push publication,
  the retained security report URI must also pass byte-level remote verification
  with `--verify-remote-report-uri`; GitHub `blob` pages are not valid remote
  byte proof because they fetch rendered HTML rather than the retained markdown
  report bytes.

3. Prefer the collector from `apps/worker`. It runs the local and hosted command
   gates, reads the retained provider lifecycle evidence, incident output,
   incident-channel evidence manifest, and security scan summary, writes the
   bundle only after validation passes, and refuses to record the
   system-order-scope limitation unless the operator passes
   `--accept-system-order-scope`. The incident-channel evidence JSON must include
   `channel_name`, `drill_id`, `evidence_uri`, `evidence_sha256`,
   `operator_ack=true`, `operator_ack_by`, non-future `captured_at`, and
   `operator_ack_at`, with `drill_id` matching the incident output,
   `captured_at` strictly after `operator_ack_at`, and `operator_ack_by`
   identifying a human logical operator handle rather than an email address,
   URL, path, raw contact value, automation, bot, CI, script, service-account,
   or system identity. `channel_name` must be
   a logical channel identifier such as `ops-live-incidents`, not a URL, path,
   webhook endpoint, raw payload, or mock/local/test label; retained channel
   proof belongs in `evidence_uri` and `evidence_sha256`. The final bundle
   release reviewer must be a different human operator from `operator_ack_by`;
   matching normalized identities are rejected as incident ACK self-review. The security scan
   summary JSON must include `scan_id`, `report_path`, `completed_at`,
   `scan_profile=security_diff_scan`, `independent_replay=true`,
   `threat_model_receipt=true`, `finding_discovery_receipt=true`,
   `worklist_rows`, exact matching `completion_receipts`, `candidate_findings`,
   exact matching `validation_receipts`, exact matching `attack_path_receipts`,
   `reportable_findings=0`, `report_uri`, `report_sha256`, `source_head`, and
   `source_diff_sha256`. `scan_id` must be a lowercase logical scan identifier
   such as `msp-20260628-independent-replay`, not a URL, filesystem path,
   drive-qualified path, email/contact value, or retained artifact reference.
   The standalone security verifier, collector, and final bundle schema gate all
   reject absolute, drive-qualified, or `..`-escaping `report_path` values before
   they can be retained in release evidence. The standalone security verifier and the collector both
   read `report_path` and require its SHA-256 to equal `report_sha256` before
   source binding is collected; a missing, unreadable, absolute, directory-escaping,
   or mutated local report file blocks the gate first. The collector computes the current source binding from `git rev-parse HEAD`,
   `git diff --binary HEAD --`, and untracked source file contents, excluding tracked
   and untracked retained evidence files passed to the collector plus the local
   security report file referenced by `report_path`; mismatched security scan
   bindings and unavailable source-binding collection block bundle creation with
   fixed failure codes that do not print the expected or supplied hash value or
   local path details. The collector output file must be a new distinct local
   artifact path and must not equal any input evidence file or the local
   security `report_path`; duplicate paths fail before bundle output is written
   with `collector_artifact_paths_must_be_distinct`, without printing local path
   details. It must not already exist; existing output paths fail before bundle
   output is written with `collector_output_path_must_not_exist`, without
   printing local path details. The collector writes the final bundle with
   exclusive file creation, so an output file created concurrently during
   collection also fails with `collector_output_path_must_not_exist`. Other
   output write failures fail as `collector_output_path_unwritable`, without
   printing local path details. The system-order-scope evidence JSON must
   include `accepted=true`, `scope=system_created_live_orders_only`,
   `broker=toss`, `limitation=broker_wide_closed_order_history_unavailable`,
   `runtime_env_var=LIVE_SYSTEM_ORDER_COUNT_SCOPE_ACCEPTED`,
   `runtime_env_value_confirmed=true`, `deployment_environment`, `accepted_by`,
   non-future `accepted_at`, non-future `evidence_captured_at` strictly after
   `accepted_at`, `evidence_uri`, and `evidence_sha256`. The collector verifies
   that `accepted_by` matches `--system-order-scope-accepted-by` and reports only
   a fixed mismatch code on failure, not either operator value. `accepted_by`
   must identify a human logical operator handle, not an email address, URL,
   path, raw contact value, or retained artifact reference; identity segments
   such as `automation`, `bot`, `ci`, `github-actions`, `script`,
   `service-account`, or `system` are rejected. The final bundle release
   reviewer must be a different human operator from `accepted_by`; matching normalized identities are rejected as
   scope self-review. Before running the collector, validate the retained scope evidence
   file. The final bundle
   also binds the scope evidence to the bundle target: `staging` requires
   `deployment_environment=staging`, and `production-readiness` requires
   `deployment_environment=production`. The final bundle verifier also rejects
   reused retained evidence references across evidence classes: the
   incident-channel proof, system-order-scope proof, security report proof, and
   each provider lifecycle artifact must have distinct retained URIs and
   distinct SHA-256 hashes. Retained URI uniqueness is checked after
   hostname/default-HTTPS-port and unreserved percent-encoding canonicalization.

```bash
python -m app.tools.verify_system_order_scope_evidence --evidence path/to/system_order_scope_evidence.json --verify-remote-evidence
```

On Windows:

```bash
py -m app.tools.verify_system_order_scope_evidence --evidence path\to\system_order_scope_evidence.json --verify-remote-evidence
```

The verifier must print:

```text
FINAL=PASS system_order_scope_evidence scope=system_created_live_orders_only broker=toss deployment_environment=staging accepted_by=scope-admin
```

With `--verify-remote-evidence`, the verifier fetches the retained
system-order-scope `evidence_uri`, rejects GitHub `blob` pages, caps response
size, and requires the downloaded bytes to match `evidence_sha256` without
printing the URI, body, or hash values on failure.

Also validate the retained security scan evidence:

```bash
python -m app.tools.verify_security_scan_evidence --evidence path/to/security_scan_summary.json --repo-root .
python -m app.tools.verify_live_readiness_scorecard --scorecard docs/LIVE_READINESS_SCORECARD.md --security-evidence path/to/security_scan_summary.json --repo-root .
```

After the security report is committed and pushed to the declared raw
byte-addressed `report_uri`, run the remote publication gate as well:

```bash
python -m app.tools.verify_security_scan_evidence --evidence path/to/security_scan_summary.json --repo-root . --verify-remote-report-uri
```

On Windows:

```bash
py -m app.tools.verify_security_scan_evidence --evidence path\to\security_scan_summary.json --repo-root .
py -m app.tools.verify_live_readiness_scorecard --scorecard docs\LIVE_READINESS_SCORECARD.md --security-evidence path\to\security_scan_summary.json --repo-root .
```

After publication on Windows:

```bash
py -m app.tools.verify_security_scan_evidence --evidence path\to\security_scan_summary.json --repo-root . --verify-remote-report-uri
```

The security verifier must print:

```text
FINAL=PASS security_scan_evidence scan_id=abc46bf_20260630220125 worklist_rows=1 completion_receipts=1 candidate_findings=0 validation_receipts=0 attack_path_receipts=0 report_uri=https://...
FINAL=PASS live_readiness_scorecard scorecard_security_scan=1 worklist_rows=1 candidate_findings=0 reportable_findings=0
```

`FINAL=FAIL security_scan_evidence` blocks live-mode consideration. Run the verifier
with `--repo-root` so it recomputes the current `source_head`/`source_diff_sha256`
while excluding the summary and local report file; stale source bindings must fail
before bundle collection, and source-binding collection failures must fail closed
as `security_scan_evidence.source_binding_unavailable`. In addition to schema/source-binding checks, the verifier
reads `report_path` from the summary before source-binding collection and rejects
absolute paths, `..` escapes, unreadable reports, or `report_sha256` values that do not match the current
report file bytes. `report_path` must be a relative retained file path that stays
under the summary JSON directory; absolute, drive-qualified, and `..`-escaping
paths are rejected by the schema gate before file hashing or source binding.
Both `report_path` and `report_uri` must point
to the retained markdown security report (`.md` or `.markdown`), not the summary
JSON or another sidecar file. `report_uri` artifact paths must not contain raw
or percent-decoded `.`/`..` traversal or encoded slash/backslash separators.
When `--verify-remote-report-uri` is supplied, the verifier fetches the declared
URI, caps the remote response size, rejects GitHub `blob` page URLs, and requires
the downloaded bytes to hash to `report_sha256` without printing the URI, body,
local path, or hash values on failure.
`scan_id` must be a lowercase logical identifier,
not a URL, path, drive-qualified path, email/contact value, or retained artifact
reference. It also rejects future
`completed_at` and summary-only
security metadata by requiring the
`security_diff_scan` profile, threat-model and finding-discovery receipts, exact
worklist completion closure, and validation/attack-path receipts matching the
candidate finding count.

Then collect the bundle:

```bash
python -m app.tools.collect_live_readiness_evidence_bundle \
  --environment production-readiness \
  --reviewed-by release-admin \
  --provider-evidence path/to/provider_lifecycle_evidence.json \
  --provider-gap-evidence path/to/provider_gap_evidence.json \
  --incident-output-file path/to/incident_output.txt \
  --incident-channel-evidence path/to/incident_channel_evidence.json \
  --security-scan-summary path/to/security_scan_summary.json \
  --accept-system-order-scope \
  --system-order-scope-evidence path/to/system_order_scope_evidence.json \
  --system-order-scope-accepted-by scope-admin \
  --output path/to/live_readiness_evidence_bundle.json
```

On Windows:

```bash
py -m app.tools.collect_live_readiness_evidence_bundle `
  --environment production-readiness `
  --reviewed-by release-admin `
  --provider-evidence path\to\provider_lifecycle_evidence.json `
  --provider-gap-evidence path\to\provider_gap_evidence.json `
  --incident-output-file path\to\incident_output.txt `
  --incident-channel-evidence path\to\incident_channel_evidence.json `
  --security-scan-summary path\to\security_scan_summary.json `
  --accept-system-order-scope `
  --system-order-scope-evidence path\to\system_order_scope_evidence.json `
  --system-order-scope-accepted-by scope-admin `
  --output path\to\live_readiness_evidence_bundle.json
```

The collector must print:

```text
FINAL=PASS live_readiness_evidence_collector external_checks=4 local_checks=6 bundle_verified=1
```

The collector `--reviewed-by` value is copied into the final bundle
`reviewed_by` field and must identify a human release reviewer logical handle,
not an email address, URL, path, raw contact value, or retained artifact
reference. Automated identity segments such as `automation`, `bot`, `ci`,
`github-actions`, `script`, `service-account`, or `system` block the final bundle.
The release reviewer must
not match `external_checks.live_incident_response_drill.channel_evidence.operator_ack_by`
or `system_order_scope_acceptance.accepted_by`, and must not match provider
lifecycle `unknown_recovery.operator_reviewed_by` or `audit.reviewed_by`;
provider lifecycle `unknown_recovery.operator_reviewed_by` and `audit.reviewed_by`
must also differ from each other. Use distinct operators for provider lifecycle
unknown recovery review, provider lifecycle audit review, incident ACK, scope
acceptance, and final bundle review. Reusing one normalized identity across
provider lifecycle, incident ACK, and scope acceptance roles fails as
`bundle.evidence_operator_roles_must_be_distinct`.

4. Run the final bundle verifier from `apps/worker`:

```bash
python -m app.tools.verify_live_readiness_evidence_bundle --evidence path/to/live_readiness_evidence_bundle.json --verify-remote-provider-artifacts --verify-remote-incident-evidence --verify-remote-system-order-scope-evidence
```

On Windows:

```bash
py -m app.tools.verify_live_readiness_evidence_bundle --evidence path/to/live_readiness_evidence_bundle.json --verify-remote-provider-artifacts --verify-remote-incident-evidence --verify-remote-system-order-scope-evidence
```

The standalone security verifier and final bundle verifier both recompute the
current Git `HEAD` and tracked plus untracked worktree diff hash. The standalone
security verifier excludes the security summary and retained local security report
file; the final bundle verifier excludes the bundle file and retained local security
report file. Stale `security_scan.source_head` or `security_scan.source_diff_sha256`
values block the release bundle.
With `--verify-remote-provider-artifacts`, the final bundle verifier also fetches
the embedded provider lifecycle retained artifact URIs and requires the remote
bytes to match each declared SHA-256. With
`--verify-remote-incident-evidence` and
`--verify-remote-system-order-scope-evidence`, it also byte-verifies the retained
incident-channel proof and system-order-scope acceptance proof, rejects GitHub
`blob` pages, caps response size, and fails without printing URI, body, or hash
details when fetched bytes do not match the declared SHA-256. The final PASS line
must retain the three remote verification flags so local-only bundle verification
cannot be mistaken for post-publication release evidence.

5. The command must print:

```text
FINAL=PASS live_readiness_evidence_bundle external_checks=4 local_checks=6 security_scan=1 system_order_scope_accepted=1 provider_gap_evidence=1 remote_provider_artifacts=1 remote_incident_evidence=1 remote_system_order_scope_evidence=1
```

When run from a bundle file, the final verifier also reads `security_scan.report_path`
and requires the current report file bytes to hash to `security_scan.report_sha256`.
This keeps the final bundle gate aligned with the standalone security evidence
verifier and prevents a stale or mutated retained report from passing on summary
metadata alone.

6. `FINAL=FAIL live_readiness_evidence_collector` or
   `FINAL=FAIL live_readiness_evidence_bundle` blocks live-mode consideration.
   The collector/verifier rejects `FINAL=SKIP`, `FINAL=FAIL`, future bundle or
   retained evidence timestamps, bundle `reviewed_at` values at or before
   `generated_at`, collector-run commands with nonzero process exit status,
   stale command output
   outside the 24-hour evidence window, multi-line final output, any
   `FINAL=PASS` line whose check-name token is not exactly the expected gate name,
   missing or weak hosted Supabase final metrics, mutated live-enable migration output,
   missing or weak local execution safety, recovery,
   and alert drill metrics, local mock incident evidence, missing or mock/fixture
   incident-channel proof, incident evidence captured before the operator ACK,
   invalid incident evidence SHA-256,
   incident channel `drill_id` mismatches, sample/fixture provider evidence,
   status observations for the wrong local
   order, mismatched provider/local terminal status pairs, provider artifact
   `drill_id` mismatches, duplicate provider artifact SHA-256 values,
   reused bundle-level retained evidence URI/SHA-256 values, with retained URIs
   compared after hostname/default-HTTPS-port canonicalization, missing or
   weak system-order-scope acceptance evidence,
   local/sample/non-HTTPS scope evidence URI, invalid-host or malformed-port scope evidence URI,
   local/test/private/non-global-IP-host scope evidence URI,
   query/fragment-bearing scope evidence URI,
   invalid scope evidence SHA-256, system-order-scope evidence captured before
   acceptance, system-order-scope operator mismatch,
   scope deployment environment mismatch,
   provider lifecycle environment mismatch,
   additional provider partial warnings beyond the single documented Toss scope
   limitation,
   secret-like keys, unknown bundle/check/evidence fields outside the release
   evidence schema, unknown or duplicate incident final-output metrics, multi-line
   final output, mutated or suffixed final-output check names, security scan source binding mismatch, security report file
   hash mismatch, missing, weak, non-HTTPS, invalid-host, malformed-port, or
   local/test/private/non-global-IP-host retained security report URI, invalid security report SHA-256,
   URL/path/contact-like security `scan_id`, non-independent security replay, missing phase receipts, incomplete or
   over-counted worklist receipts, candidate validation/attack-path receipt
   mismatches, or any reportable security finding.

Manual live order cancel:

1. Stop new live decisions before canceling:

```sql
update public.bot_settings
set enabled = false,
    live_order_allowed = false,
    updated_at = now()
where id = 'singleton'
returning id, enabled, mode, live_order_allowed;
```

2. Choose exactly one local open live order. Use the local `orders.id`; do not pass a raw Toss
   `provider_order_id` to the tool:

```sql
select id, symbol, status, provider_order_id, created_at, updated_at
from public.orders
where mode = 'live'
  and status in ('sent', 'partial_filled')
order by created_at asc
limit 20;
```

3. Run the cancel command from `apps/worker`:

```bash
python -m app.tools.cancel_live_order_once --order-id ORDER_UUID
```

On Windows:

```bash
py -m app.tools.cancel_live_order_once --order-id ORDER_UUID
```

4. If the command returns `FINAL=PASS`, the worker received the cancel operation id and then confirmed
   the original provider order as `CANCELED`. Verify the row moved to `canceled` and that the payload
   keeps the original provider order id, the cancel operation id, and the confirmation summary:

```sql
select id, symbol, status, reason, provider_order_id, provider_payload_summary, updated_at
from public.orders
where id = 'ORDER_UUID';
```

5. If the row becomes `unknown_requires_manual_check`, do not retry blindly. Reconcile the provider order
   status first. This state can mean the cancel request timed out, the status confirmation timed out, or
   the provider did not confirm `CANCELED`. Then review `component='live_cancel'` events:

```sql
select level, message, details, created_at
from public.engine_events
where component = 'live_cancel'
order by created_at desc
limit 50;
```

Daily Paper Trading health report:

1. Keep live trading disabled:

```sql
update public.bot_settings
set mode = 'paper',
    live_order_allowed = false,
    updated_at = now()
where id = 'singleton'
returning id, enabled, mode, live_order_allowed;
```

2. Run the read-only operations report from `apps/worker`:

```bash
python -m app.tools.paper_health_report
```

On Windows:

```bash
py -m app.tools.paper_health_report
```

3. Interpret the final line:

- `FINAL=PASS`: no critical or warning finding
- `FINAL=WARN`: follow up on warnings; Paper Trading can remain fail-closed
- `FINAL=FAIL`: keep `live_order_allowed=false`, stop enabling paper cycles, and inspect findings

The report writes one `engine_events` row with `component='paper_ops'` and
`message='paper_health_report'`. Those diagnostic rows are excluded from the
repeated-critical-event count so the report cannot make future reports fail by
itself. Operational critical events from `worker`, provider, live execution, or
alerting components still count and must be investigated.

4. Verify the summary event:

```sql
select level, component, message, details, created_at
from public.engine_events
where component = 'paper_ops'
  and message = 'paper_health_report'
order by created_at desc
limit 5;
```

5. Confirm the report did not create decisions or orders by comparing counts before and after if investigating an anomaly:

```sql
select
  (select count(*) from public.orders) as orders_count,
  (select count(*) from public.decision_snapshots) as decision_snapshots_count;
```

6. Critical follow-up queries:

```sql
select *
from public.orders
where status in ('sent', 'filled', 'partial_filled')
order by created_at desc
limit 20;

select idempotency_key, count(*) as count
from public.orders
where idempotency_key is not null
group by idempotency_key
having count(*) > 1;

select *
from public.worker_heartbeats
order by created_at desc
limit 1;
```

See `docs/PAPER_TRADING_OPERATIONS.md` for the full PASS/WARN/FAIL policy.

DB full:

1. Disable bot.
2. Run retention dry-run.
3. Export long-term logs if needed.
4. Apply cleanup.

Schema drift after Render/Supabase connection:

1. Keep `bot_settings.enabled=false`.
2. Keep `live_order_allowed=false`.
3. Capture current counts:

```sql
select
  (select count(*) from public.orders) as orders_count,
  (select count(*) from public.decision_snapshots) as decision_snapshots_count;
```

4. Run pending migrations through `0005_schema_alignment.sql`.
5. Verify the singleton:

```sql
select id, enabled, mode, live_order_allowed from public.bot_settings;
```

6. Verify latest heartbeats:

```sql
select * from public.worker_heartbeats order by created_at desc limit 10;
```

7. Verify latest API health:

```sql
select distinct on (provider) * from public.api_health order by provider, checked_at desc;
```

After the provider-health diagnostics patch is deployed, unhealthy Toss-related
rows should include safe `details.error_type` and `details.reason` fields when
the provider can explain the failure. `details` must not contain credentials,
bearer tokens, account identifiers, raw provider payloads, or secret-looking
keys. Empty `details={}` on hosted Supabase means the deployed worker has not
yet emitted the patched diagnostic path or the provider returned no safe reason.

8. Verify watchlist upsert:

```sql
insert into public.watchlist (symbol, market, name, sector, enabled)
values ('005930', 'KR', '삼성전자', '반도체', true)
on conflict (symbol, market) do update set
  name = excluded.name,
  sector = excluded.sector,
  enabled = excluded.enabled,
  updated_at = now()
returning id, symbol, market, name, enabled;
```

9. Verify no app table has RLS disabled:

```sql
select tablename
from pg_tables
where schemaname = 'public'
  and rowsecurity = false;
```

10. Re-check order and decision counts while `enabled=false`:

```sql
select
  (select count(*) from public.orders) as orders_count,
  (select count(*) from public.decision_snapshots) as decision_snapshots_count;
```

If counts increased during schema alignment, stop the worker and inspect `engine_events` before resuming paper mode.

Paper trading readiness:

1. Keep live trading disabled:

```sql
update public.bot_settings
set enabled = false,
    mode = 'paper',
    live_order_allowed = false,
    updated_at = now()
where id = 'singleton'
returning id, enabled, mode, live_order_allowed;
```

2. Insert an initial watchlist symbol:

```sql
insert into public.watchlist (symbol, market, name, sector, enabled, notes)
values ('005930', 'KR', '삼성전자', '반도체', true, 'paper readiness seed')
on conflict (symbol, market) do update set
  name = excluded.name,
  sector = excluded.sector,
  enabled = excluded.enabled,
  notes = excluded.notes,
  updated_at = now()
returning id, symbol, market, name, sector, enabled;
```

3. Insert the initial weighted factor strategy:

```sql
insert into public.strategy_versions (
  version,
  version_name,
  status,
  strategy_type,
  weights,
  params
)
values (
  'strategy_v1_weighted_factor',
  'strategy_v1_weighted_factor',
  'active',
  'WeightedFactorStrategyV1',
  '{"technical":0.35,"fundamental":0.25,"market_sector":0.15,"news_event":0.15,"portfolio":0.10}'::jsonb,
  '{"buy_threshold":0.68,"sell_threshold":0.25}'::jsonb
)
on conflict (version) do update set
  version_name = excluded.version_name,
  status = 'active',
  strategy_type = excluded.strategy_type,
  weights = excluded.weights,
  params = excluded.params
returning id, version, version_name, status;
```

4. Enable paper mode only:

```sql
update public.bot_settings
set enabled = true,
    mode = 'paper',
    live_order_allowed = false,
    updated_at = now()
where id = 'singleton'
returning id, enabled, mode, live_order_allowed;
```

5. Run one safe cycle before enabling the continuous loop:

```bash
python -m app.tools.seed_watchlist_demo
python -m app.tools.seed_strategy_v1
python -m app.tools.run_paper_cycle_once
```

These commands do not print secrets. `run_paper_cycle_once` forces Paper Trading for one cycle and does not call Toss live order execution.

6. Check decisions:

```sql
select *
from public.decision_snapshots
order by decided_at desc
limit 20;
```

7. Check orders:

```sql
select *
from public.orders
order by created_at desc
limit 20;
```

8. Ensure no live order statuses exist:

```sql
select *
from public.orders
where status in ('sent', 'filled', 'partial_filled')
order by created_at desc
limit 20;
```

This query must return no rows during Paper Trading.

9. Optional grouped paper result check:

```sql
select status, mode, count(*) as order_count
from public.orders
group by status, mode
order by mode, status;
```

10. Disable after verification:

```sql
update public.bot_settings
set enabled = false,
    live_order_allowed = false,
    updated_at = now()
where id = 'singleton';
```

Paper outcome update:

1. Confirm paper safety remains locked:

```sql
select id, enabled, mode, live_order_allowed
from public.bot_settings
where id = 'singleton';
```

`mode` must be `paper` and `live_order_allowed` must be `false`.

2. Run the database-only outcome update from `apps/worker`:

```bash
python -m app.tools.update_outcomes_once
```

On Windows:

```bash
py -m app.tools.update_outcomes_once
```

3. Verify latest outcomes:

```sql
select *
from public.outcomes
order by updated_at desc
limit 20;
```

4. Join recent decisions and outcomes:

```sql
select d.symbol,
       d.action,
       d.final_score,
       o.return_1d,
       o.return_5d,
       o.return_20d
from public.decision_snapshots d
left join public.outcomes o on o.decision_id = d.id
order by d.decided_at desc
limit 30;
```

5. Confirm duplicate outcome rows were not created:

```sql
select decision_id, count(*) as outcome_rows
from public.outcomes
group by decision_id
having count(*) > 1;
```

This query must return no rows after migration `0006_outcome_tracking.sql`.

6. Confirm no live-like orders appeared:

```sql
select *
from public.orders
where status in ('sent', 'filled', 'partial_filled')
order by created_at desc
limit 20;
```

The outcome command must not create orders or decision snapshots. If counts changed unexpectedly, keep `live_order_allowed=false`, stop the worker, and inspect `engine_events`.

Lightweight backtest:

1. Apply migrations `0007_backtest_runs.sql` and `0008_backtest_runs_rls.sql`.

2. Keep live trading disabled:

```sql
update public.bot_settings
set mode = 'paper',
    live_order_allowed = false,
    updated_at = now()
where id = 'singleton'
returning id, enabled, mode, live_order_allowed;
```

3. Run the cached-data backtest from `apps/worker`:

```bash
python -m app.tools.run_backtest --strategy strategy_v1_weighted_factor --start YYYY-MM-DD --end YYYY-MM-DD
```

On Windows:

```bash
py -m app.tools.run_backtest --strategy strategy_v1_weighted_factor --start YYYY-MM-DD --end YYYY-MM-DD
```

4. Verify the result row:

```sql
select strategy,
       period_start,
       period_end,
       total_return,
       max_drawdown,
       sharpe_like,
       win_rate,
       number_of_trades,
       blocked_reason_counts,
       created_at
from public.backtest_runs
order by created_at desc
limit 10;
```

5. Confirm no live-like orders appeared:

```sql
select *
from public.orders
where status in ('sent', 'filled', 'partial_filled')
order by created_at desc
limit 20;
```

The backtest command must not create `orders`, call Toss, or update `strategy_versions.status`. If it fails with `FINAL=FAIL`, check `SUPABASE_URL`, `SUPABASE_SECRET_KEY`, migrations `0007_backtest_runs.sql` and `0008_backtest_runs_rls.sql`, and cached `features_daily` rows.

Strategy Lab verification:

1. Confirm the Desktop is using only publishable Supabase configuration:

```bash
cd apps/desktop
npm run typecheck
npm run build
```

2. Confirm `backtest_runs` admin read policy exists:

```sql
select policyname, roles, cmd
from pg_policies
where schemaname = 'public'
  and tablename = 'backtest_runs'
order by policyname;
```

Expected: `backtest_runs_admin_read` for `authenticated` select. Do not add anon/public write policies.

3. Confirm AI candidate approval did not deploy a strategy or enable live trading:

```sql
select id, enabled, mode, live_order_allowed
from public.bot_settings
where id = 'singleton';

select id, candidate_name, status, reviewed_at
from public.ai_upgrade_candidates
order by created_at desc
limit 20;

select version, status, created_at
from public.strategy_versions
order by created_at desc
limit 20;
```

`live_order_allowed` must remain `false`. Strategy Lab approval should change only candidate review state such as `approved_for_paper` or `rejected`.

4. Confirm no live-like orders appeared:

```sql
select *
from public.orders
where status in ('sent', 'filled', 'partial_filled')
order by created_at desc
limit 20;
```

The query should return no rows during Paper Trading validation.

Unknown broker status:

1. Stop new orders for same symbol.
2. Check official broker order status channel.
3. Mark manually reconciled only after evidence.

Secret leak:

1. Disable bot.
2. Rotate leaked key.
3. Review logs and Git history.
4. Re-enable paper only after verification.

Monthly AI candidate generation:

1. Keep live trading disabled:

```sql
update public.bot_settings
set live_order_allowed = false,
    updated_at = now()
where id = 'singleton';
```

2. Run the one-off command:

```bash
cd apps/worker
python -m app.tools.generate_monthly_research --month YYYY-MM
```

On Windows:

```bash
py -m app.tools.generate_monthly_research --month YYYY-MM
```

The legacy `generate_monthly_ai_candidate` command uses the same safe workflow, but `generate_monthly_research` is the preferred operations command.

3. Verify proposed candidates only:

```sql
select candidate_name,
       status,
       approval_required,
       created_at
from public.ai_upgrade_candidates
order by created_at desc
limit 10;
```

Every row created by this command must have `status='proposed'` and `approval_required=true`.

4. Confirm the singleton remained fail-closed for live trading:

```sql
select id, enabled, mode, live_order_allowed
from public.bot_settings
where id = 'singleton';
```

`live_order_allowed` must remain `false`. Monthly research must not change it.

5. Confirm no strategy was deployed:

```sql
select version, status, updated_at
from public.strategy_versions
order by created_at desc
limit 10;
```

The monthly AI command must not change strategy status.

6. Confirm no live orders were created:

```sql
select *
from public.orders
where status in ('sent', 'filled', 'partial_filled')
order by created_at desc
limit 20;
```

7. Review dataset quality warnings through the created engine event:

```sql
select level, component, message, details, created_at
from public.engine_events
where component = 'strategy_research'
  and message = 'monthly_ai_candidate_proposed'
order by created_at desc
limit 5;
```

8. If OpenAI output is rejected:

- check worker logs for `invalid_monthly_candidate_schema`
- keep the candidate absent or manual-review only
- do not retry with unsanitized data
- do not edit `strategy_versions` manually to force deployment

9. Batch API future workflow:

- use `AIBatchPort` only with sanitized monthly datasets
- validate each completed result with the same schema
- insert only `status='proposed'`
- never auto-approve, auto-promote, or create orders
