# Operations Runbook

## Non-negotiable boundary

The current release supports `paper` and local `contract_test` only. Keep
`live_order_allowed=false`; there is no production order create, cancel, modify,
credential, or network path. Real Toss use is read-only. The local simulator is
not an official sandbox.

Migration order and local database verification are maintained in
`supabase/README.md`; the V2 release train spans `0016_private_foundation.sql`
through `0024_operational_upgrade_convergence.sql`, followed in order by
`20260714154520_control_qualification_workflow.sql`,
`20260714155117_paper_execution_source.sql`,
`20260714155744_cash_settlement_maturity.sql`,
`20260714160105_operations_runtime_scheduler.sql`,
`20260714161511_unknown_execution_resolution_v2.sql`, and
`20260714165910_unknown_resolution_desktop_projection.sql`, followed by
`20260715020752_kst_trading_date_convergence.sql`,
`20260715041903_paper_bar_participation_guard.sql`,
`20260715041909_operation_claim_fencing.sql`,
`20260715041912_sell_cost_basis_checkpoint_guard.sql`,
`20260715041915_paper_evidence_and_sell_reservation_guards.sql`, and
`20260718165749_pgcrypto_schema_convergence.sql`, followed by
`20260719001947_pit_candle_revision_store.sql`,
`20260719010000_pit_daily_candle_timing_store.sql`,
`20260719020000_pit_source_observation_occurrence_store.sql`, and
`20260719030000_pit_daily_candle_as_of_reader.sql`, followed by
`20260719040000_pit_calendar_observation_store.sql`, and
`20260719050000_pit_calendar_as_of_reader.sql`, followed by
`20260719060000_kr_calendar_collection_job_store.sql`, followed by
`20260719070000_kr_calendar_collection_job_conflict_boundary.sql`, followed by
`20260719080000_kr_calendar_collection_job_inspection.sql`, followed by
`20260719090000_pit_daily_candle_collection_job_store.sql`, followed by
`20260723162000_desktop_operations_sensitive_projection_gate.sql`, followed by
`20260724210000_durable_operations_scheduler.sql`.

After the Desktop sensitive-projection migration, run the complete
`python supabase/verify_g1_g2_migration.py` verifier without
`--skip-postgrest`. Confirm that every admitted AAL2 non-auditor role receives
both `audit_events` and `reconciliation_cases` as exact empty arrays through
direct PostgreSQL and PostgREST, while an AAL2 `auditor` receives both
permissions and the exact known audit/reconciliation fixture. `anon` and
`service_role` must remain unable to call the Desktop RPC. Do not treat the
identifier-free reconciliation health state as auditor evidence; it remains
part of minimum-status availability.

After the durable operations scheduler migration, run its fresh-install and
populated-upgrade verifier:

```bash
python supabase/verify_durable_operations_scheduler.py
```

The verifier must finish with
`FINAL=PASS durable_operations_scheduler_verifier`. It uses disposable
PostgreSQL 17.6 containers and proves the following boundaries directly:

- the database, not a caller timestamp or `asyncio.sleep`, owns due time,
  retry availability, and lease expiry;
- the current outer Worker lease binds account, holder, fencing token, and
  release SHA, while every inner lease expires no later than that outer lease;
- a definition TTL and the outer lease must each provide at least ten database-
  clock seconds before a claim is issued; the Worker bounds the complete RPC to
  five seconds and keeps at least five seconds before starting a handler;
- a restart forces a command drain for the new outer fencing generation before
  execution can be claimed, even when the stored command cadence is not yet due;
- each definition has at most one pending, leased, or retry-wait run and stale
  completion, duplicate claim, wrong release, and old fencing tokens fail closed;
- command, reconciliation, and outbox polling use only their exact retry reason;
  execution and settlement never use scheduler-level automatic retry;
- expired execution or settlement is dead-lettered with an unknown effect and is
  not eligible for generic manual replay; reconciliation evidence must be added
  by a separate approved resolution contract before that policy can change;
- eligible manual replay binds source revision, definition and failure digests,
  failure reason, replay generation, request UUID, and explicit confirmation,
  creates a new child, and leaves the source terminal row immutable;
- a lost replay response can be recovered with the same semantic request under
  a later valid outer lease; changing any bound source field is rejected;
- forced RLS, zero table policies, zero runtime table grants, exact service-role
  RPC grants, empty function search paths, and zero order/trading side effects
  remain true on fresh and populated upgrades.

Do not start the normal scheduler by simply replacing definitions first. During
a rolling release, an older definition may still own a leased or retry-wait run.
Call `converge_scheduler_job_definition` for each fixed job and accept only its
exact `converged`, `claimed`, `wait`, or `manual_resolution` disposition.
`converged` requires the requested digest and no active run. `claimed` can
dispatch only the existing non-effectful recovery claim through the same fixed
job binding and result validator as a normal claim; it never creates an old-
definition cadence run. Re-query `wait` using database time and never turn its
timestamp into caller-side eligibility authority.

`scheduler_outer_lease_renewal_required` is not a sleep-until-expiry order.
Renew the same account/holder/fencing/release lease immediately, refresh the
local lease snapshot under the shared renewal/scheduler lock, and re-query the
convergence RPC. Never execute the claim using the pre-renewal snapshot.

Do not enter new execution claims until command, settlement, and reconciliation
barriers are healthy. A blocked execution or settlement remains a manual-
resolution incident, but commands, reconciliation, and outbox must keep making
bounded recovery/delivery progress. A missing, disabled, blocked, or expired
settlement or reconciliation definition closes new execution. An unexpired old
effectful run is a bounded wait; expiry is an unknown-effect dead letter, not
permission to delete state, rewrite a digest, or replay it. `asyncio.sleep` may
provide polling backpressure only; it is never cadence or expiry authority.

After the occurrence migration, confirm its dedicated fresh and populated
upgrade verifier passes. The upgrade can reconstruct original content
observations and the latest retained candle-head observation only; it cannot
invent intermediate unchanged observations that the previous schema discarded.
Do not reinterpret an existing quarantined request key. Its durable receipt must
remain stable, and a genuinely new observation requires a new request key.

After the as-of reader migration, run its dedicated disposable-database
verifier:

```bash
python supabase/verify_pit_daily_candle_as_of_reader.py
```

The RPC is a server-side `service_role` boundary for one exact provider/`KR`/
six-digit symbol/`1d`/`adjusted` series, an inclusive date range of at most 366
days, page size `25..100`, and no more than 1,000 raw candidates. It must remain
unavailable to Desktop, `public`, authenticated clients, and Realtime. The read
writes zero domain rows.

Treat `evidence_available_at <= as_of` as reconstruction from durable source
evidence retained when the read runs. Do not reinterpret it as
`received_at <= as_of` or as proof of which rows had committed at that
historical time. Multi-page reads must retain the same full raw-candidate
manifest and PostgreSQL MVCC snapshot token. The cursor expires after 15
minutes; on expiry, unresolved timeline ambiguity, corrupt binding, or manifest
drift, discard the entire read and start a new one. Never splice pages or expose
a partial selection. The Worker adapter performs this validation, buffers all
pages, and invokes the existing selector exactly once.

Treat the cursor as opaque within the trusted Worker-to-`worker_api` boundary.
Direct `service_role` callers must replay the server-issued object verbatim;
they must not rewrite its issuance time or any binding field. The official
adapter pins first-page metadata and rejects such drift before exposing a
result.

This reader is not connected to collection, the runtime container, features,
strategy, backtests, or orders. Its verifier does not establish completeness,
authenticity, provider finality, corporate-action safety, DQ approval, feature
readiness, Hosted Staging approval, or any Live authorization. Production Live
remains not authorized.

After the independent calendar observation migration, run its dedicated
disposable-database verifier:

```bash
python supabase/verify_pit_calendar_observation_store.py
```

The RPC accepts one canonical open or closed KR session observation and remains
available only to the server-side `service_role`. Confirm exact occurrence
replay, later unchanged occurrence storage, correction revisions, durable
quarantine, append-only ACLs, and serialization with the existing timing RPC.
It is not a calendar completeness, provider-finality, DQ, dataset, feature,
backtest, strategy, or order authorization boundary. The existing timing RPC
also remains fail closed when a new request tries to bind an older calendar
occurrence after the calendar stream head has advanced.

After the bounded calendar as-of reader migration, run its dedicated
disposable-database verifier:

```bash
python supabase/verify_pit_calendar_as_of_reader.py
```

The Worker-only RPC reads both open and closed sessions for one exact provider
and the `KR` market. Keep every request within 366 inclusive calendar days,
page size `25..100`, and 1,000 raw candidates. Keep the server-issued cursor
opaque: it binds the query, complete ordered candidate manifest, and one MVCC
snapshot for at most 15 minutes. The reader uses exact immutable occurrence and
content-revision lineage, never a mutable stream head. The migration also
converges shared calendar hash dates to explicit `YYYY-MM-DD`; a non-ISO
session `DateStyle` must produce the same identities, items, and manifest.

`observed_at <= as_of` is the only source-semantic cutoff. `received_at` is
lineage and cannot reconstruct past database visibility. On cursor expiry,
manifest drift, corrupt payload/hash/lineage, or any ambiguity in the full
as-of-eligible retained timeline, discard every buffered page and expose no
partial result. Also fail the whole read when a non-terminal page is shorter
than the requested page size, continuation exceeds the declared candidate
count, response encoding is not identity, or one RPC response exceeds 4 MiB.
The read writes zero domain rows and
is unavailable to Desktop, `public`, authenticated clients, and Realtime.

This verifier does not approve collection, runtime-container or scheduler
wiring, timing backfill, DQ, completeness, authenticity, provider finality,
corporate-action handling, dataset/research/feature/backtest/strategy/order use,
Hosted Staging, or Live. Production Live remains not authorized.

After the durable manual calendar collection-job migration, run its dedicated
disposable-database verifier:

```bash
python supabase/verify_kr_calendar_collection_job_store.py
```

The six Worker-only RPCs must remain service-role-only. Confirm reconnect
durability, concurrent create/begin serialization, exact spec/revision/attempt/
holder/target fencing, pause followed only by a new explicit manual attempt,
blocked-attempt takeover denial, immutable calendar occurrence binding,
canonical UTC timestamps, actual PostgREST `worker_api` profiles, bounded
`PT409` stale-CAS completion, a fully completed 366-day job, terminal-manifest
parity, response-size headroom, forced RLS, append-only attempt history, and zero
writes to trading/order rows. Never add TTL takeover or automatic retry for an
unresolved attempt.

For `inspect_kr_calendar_collection_job_v1`, confirm valid missing and present
job UUIDs return the exact `job_found`/`snapshot` envelope, invalid or non-v4
UUIDs fail closed, and only `service_role` can execute it through the actual
PostgREST `worker_api` profile. Compare the private job and attempt-ledger
fingerprint before and after inspection of an existing job; for missing or
invalid IDs, confirm the exact job/ledger counts and trading/order domain
snapshot are unchanged.

The durable mutation and inspector adapters are selected only by the separate
calendar assessment and one-date execution entry points. They are not part of
the normal runtime or scheduler. Both are false by default. The assessment
requires a Worker-only Supabase credential bound to a canonical
`https://<project>.supabase.co` origin. Execution additionally requires
read-only Toss credentials, a stable UUIDv4 holder, the assessed exact spec
SHA, and explicit operator confirmation.

First review the exact canonical job fields and the current read-only assessment:

- `missing`: confirm revision is absent, count is 0, and next date is the range start.
- `ready`: confirm the exact revision, confirmed count, and next date.
- `paused_retryable`: review the proven pre-write failure and confirm the same exact fields.
- `paused_unrecognized`: investigate the unrecognized pause reason without retrying it.
- `collecting`: investigate the in-flight attempt without retrying it.
- `blocked_unknown`: reconcile the unknown write outcome without retrying it.
- `completed`: no collection action is allowed.

Obtain those exact values through the supported read-only command. It calls the
inspector once, performs no mutation or Toss request, and emits `run_precondition`
only for `missing`, `ready`, or `paused_retryable`:

```powershell
cd apps/worker
$env:KR_CALENDAR_COLLECTION_ASSESSMENT_ENABLED="true"
$env:SUPABASE_URL="https://<project>.supabase.co"
$env:SUPABASE_SECRET_KEY="<worker-only-secret>"
python -m app.tools.assess_kr_calendar_collection_job `
  --job-id <job-uuid-v4> `
  --start-date YYYY-MM-DD `
  --end-date YYYY-MM-DD
```

Review the returned spec SHA and every `run_precondition` field. Do not run the
mutation command when `explicit_manual_invocation_candidate=false`.

For a reviewed `missing` example, run one date only:

```powershell
cd apps/worker
$env:KR_CALENDAR_COLLECTION_ASSESSMENT_ENABLED="true"
$env:KR_CALENDAR_COLLECTION_MANUAL_EXECUTION_ENABLED="true"
$env:KR_CALENDAR_COLLECTION_HOLDER_ID="<stable-uuid-v4>"
$env:MOCK_PROVIDERS="false"
$env:TOSS_CREDENTIAL_SCOPE="read_only"
$env:TOSS_ORDER_CAPABLE_CREDENTIALS="false"
$env:TOSS_CLIENT_ID="<read-only-client-id>"
$env:TOSS_CLIENT_SECRET="<read-only-client-secret>"
$env:SUPABASE_URL="https://<project>.supabase.co"
$env:SUPABASE_SECRET_KEY="<worker-only-secret>"
python -m app.tools.run_kr_calendar_collection_job_once `
  --job-id <job-uuid-v4> `
  --start-date YYYY-MM-DD `
  --end-date YYYY-MM-DD `
  --expected-spec-sha256 <64-lowercase-hex> `
  --expected-classification missing `
  --expected-confirmed-count 0 `
  --expected-next-date YYYY-MM-DD `
  --confirm-one-date-spec-sha256 <same-64-lowercase-hex>
```

For `ready` or `paused_retryable`, also pass the reviewed
`--expected-revision`. A paused retry additionally requires
`--expected-state-reason collection_failed_before_write` and
`--confirm-reviewed-paused-retryable-spec-sha256` with the same spec SHA.
`paused_unrecognized` never emits a run precondition. The command reassesses
once, then binds execution to the exact assessed state, reason, revision, count,
and next date. Any drift fails before attempt creation and provider collection.
Run the command again only after reviewing the new durable state; it never loops
over the remaining range.

These checks authorize only this one explicit date attempt. They do not approve
automatic retry, TTL takeover, direct private-table repair, unresolved-write
recovery, automatic backfill, dataset/DQ certification, research/feature/
backtest/strategy/order use, Hosted Staging, or Production Live. A spec,
assessment, snapshot, or adapter error after argument parsing returns only a
fixed `FINAL=FAIL` line on stdout and exit code 1. Invalid or incomplete CLI
arguments are rejected by `argparse` with usage/error text on stderr and exit
code 2. Successful assessment or execution emits its documented evidence on
stdout and exits with code 0.

## Start-of-day Paper checklist

1. Sign in with a personal Supabase Auth account, complete TOTP, and confirm
   AAL2. Never share an operator identity.
2. Confirm the status rail shows `PAPER`, `LIVE 금지`, the expected release SHA,
   a fresh heartbeat, current lease/fencing token, `enabled=false`, and the
   latest ledger checkpoint.
3. Confirm there is exactly one valid active lease and no unresolved stale
   Worker incident.
4. Check outbox oldest age, dead-letter count, and critical incident ACK state.
5. Check the latest reconciliation pass and manually review every unknown or
   quarantined order before requesting resume.
6. Have an `operator` request resume and a different `risk_approver` approve it
   with a fresh, command-bound step-up grant.
7. Do not report success until the command is `applied` and the runtime
   postcondition is visible.

## Emergency stop

1. From an online AAL2 `operator` session, submit `emergency_stop` once.
2. The control-plane receipt means only that the DB stored `enabled=false` and
   incremented `control_epoch`; it is not Worker confirmation.
3. Expect Worker observation within 10 seconds and runtime stop confirmation
   within 15 seconds.
4. If offline, show `전송되지 않음`. Do not queue or retry automatically.
5. If the runtime confirmation misses its target, open a critical incident,
   isolate the Worker credential, and keep Paper disabled.

## Unknown or quarantined order

1. Stop new dispatch for the affected account. Never resend an unknown intent.
2. Preserve intent, dispatch, observation, provider identity, cumulative fill,
   policy version, lease token, and correlation evidence.
3. Compare status monotonically: cumulative fill must not decrease, a terminal
   state must not regress, and provider identity must not change.
4. V1 unknown resolution is evidence-only. In V2, an `operator` submits the
   strict evidence/fill proposal and a different `risk_approver` reviews it.
   Unknown is not automatically terminal.
5. Confirm the Desktop case comes from `get_unknown_resolution_cases_v2`. After
   approval the Worker must use only the dedicated
   `list_unknown_resolution_v2` → `claim_unknown_resolution_v2` →
   `apply_unknown_resolution_v2` sequence. Generic operation-command ACK cannot
   apply the accounting change.
6. Verify command/work revisions, request/review hashes, terminal status,
   `control_epoch`, release, lease, fencing token, and claim expiry before
   application. A lost response permits only the exact post-state replay check;
   never resend blindly.
7. Apply only the reviewed fill manifest or verified no-fill terminal result
   tied to retained evidence. Reprocessing the same observation/fill must be an
   accounting no-op.
8. Close the case only after cash, reserved cash, available quantity, position,
   event chain, and audit hash are reconciled.

Reconciliation uses keyset pagination and priority, not a fixed oldest page.
One group of 50 stale rows must not starve newer dispatched orders.

## Lease or split-brain incident

1. Emergency-stop the account and capture both Worker identities and tokens.
2. Do not manually reuse or lower a fencing token.
3. Let the DB lease expire/reclaim according to policy; the new owner receives
   a strictly greater token.
4. Verify every old-token call to reserve, dispatch, observe, or account is
   rejected.
5. Reconcile in-flight intents before a maker/checker resume.

Only one scheduler is active. A warm standby may run only after fencing tests
pass. Outbox dispatchers are the only permitted parallel consumers and must use
row locking, leases, and recipient-side dedupe.

## Alert and incident handling

- `delivery_outbox` is at-least-once. Duplicate delivery is expected and the
  receiver deduplicates by the stable key.
- A dispatcher claims with `FOR UPDATE SKIP LOCKED`, records a lease, and uses
  bounded retry/backoff. Exhausted delivery moves to dead letter and opens or
  updates an incident.
- Critical delivery requires human ACK within five minutes. Delivery is not ACK.
- Escalate through an independent failure domain if the Worker heartbeat, lease,
  outbox age, or incident ACK is stale.
- Webhook secrets, account numbers, authorization data, and provider raw payloads
  never enter audit or outbox records.

### Receiver ACK key rotation

1. Generate a new random 32-byte key outside the repository and encode it as
   canonical padded base64. Assign a new bounded key ID.
2. Configure the receiver to accept both old and new IDs. Keep the old key active
   while completion-write retries may still return an exact cached ACK.
3. Move the old current ID/key to the matching `PREVIOUS` variables, place the new
   pair in `CURRENT`, and restart only after the full pair passes startup validation.
4. Confirm new deliveries are signed by the current ID and exact cached retries
   signed by the previous ID still authenticate. An item, URL, destination, or
   payload change must fail the cached ACK.
5. After the maximum retry/dead-letter and incident investigation window has
   elapsed with no old-key traffic, remove both previous variables together and
   then remove the old key from the receiver.

Never log or paste key values or raw key IDs. Bounded operational metrics may use
only the configured slot label (`current` or `previous`), never the configured or
receiver-provided ID itself.
Do not treat a signed receiver ACK as human incident ACK or immutable archive proof.

## Paper execution investigation

The v1 policy supports KRW whole-share `LIMIT` `DAY` buy/sell only. A fill can
start at the first complete one-minute bar after the decision, is capped at 1%
of bar volume, and applies 10 bps adverse slippage without crossing the limit.
Partial fill and expiry are valid. Short, margin, market, IOC/FOK, and modify are
not supported.

Missing or expired commission/tax/settlement evidence, price tick evidence,
bar volume, or corporate-action state blocks the fill. Never substitute zero or
a guessed default. The accounting cost basis is
`moving_weighted_average_v1`.

## Publish one Paper source artifact

The operations scheduler is a consumer, not an automatic strategy/bar producer.
Publish only a reviewed local fixture or approved read-evidence artifact. The
input gate is false by default and must be enabled for this explicit invocation:

```powershell
cd apps/worker
$env:EXECUTION_V2_ENABLED="true"
$env:EXECUTION_V2_WORKER_API_ENABLED="true"
$env:EXECUTION_V2_PAPER_SOURCE_INPUT_ENABLED="true"
$env:EXECUTION_V2_ENVIRONMENT="paper"
$env:EXECUTION_V2_ACCOUNT_ID="paper-primary"
$env:EXECUTION_V2_WORKER_ID="<active-lease-holder-uuid>"
$sourceSha = (Get-FileHash -Algorithm SHA256 .\paper-source.json).Hash.ToLowerInvariant()
py -m app.tools.publish_paper_execution_source_once `
  --input .\paper-source.json `
  --input-sha256 $sourceSha
```

Before accepting the receipt, verify the expected fixture, intent, command, and
idempotency fields. The artifact must use `local_fixture` or
`allowed_read_evidence`; a matching local hash proves byte identity, not data
origin approval. The DB remains responsible for account, release, active lease,
fencing token, `control_epoch`, qualification, policy, cost, and market-evidence
checks. Disable the input flag again when the explicit publication task ends.

## Cash settlement investigation

1. Treat fill-time position/cost/PnL as trade-date accounting and the cash leg
   as payable/receivable clearing until maturity.
2. Compare `settlement_date` with the DB's current `Asia/Seoul` date. Do not use
   the Worker's UTC calendar date as a substitute.
3. Verify `pending_debit_cash_krw`/`pending_credit_cash_krw`, the immutable
   obligation, claim token/revision, lease/release/fencing token, and the latest
   settlement event.
4. Exact completion replay must return the existing result without another
   journal. Typed retry uses bounded backoff; attempt exhaustion moves to
   `dead_letter` and produces incident/outbox evidence.
5. Do not manually promote a future receivable into settled cash or edit a
   settlement state row. Resolve projection conflicts with a reviewed forward
   fix while Paper remains disabled.

## Contract qualification

1. Fetch the official OpenAPI artifact through the approved release process and
   register URL, retrieval time, and SHA-256. Do not store credentials or a raw
   authenticated response.
2. Run local `contract_test` create → status → partial → terminal and cancel
   lifecycles, including timeout, duplicate, malformed response, and crash fault
   injection.
3. Assert production order host request count is zero.
4. A changed hash or unknown response blocks qualification; it never falls back
   to a production write.

Run the repository-local suite with:

```powershell
cd apps/worker
py -m app.tools.run_contract_qualification_once
```

The manifest must contain exactly the create, status partial/terminal, cancel,
fault-injection, and `production_order_network_zero` checks, with network
`request_count=0`. This command does not contact an official sandbox and does
not register or finalize the release-bound database qualification. Preserve the
manifest and its hashes for the later maker/checker workflow; do not mark the
gate complete from console output alone.

## Restore drill

1. Restore into an isolated environment with Paper disabled and no order-capable
   credential or production order network route.
2. Verify the last committed ledger checkpoint, audit hash chain, balanced
   postings, non-negative cash/reserves/quantities, and idempotent replay.
3. Reclaim a lease with a greater fencing token and reconcile all in-flight
   intents before considering resume.
4. Record start, data-ready, reconciliation-complete, and operator-ready times.
   Market-hours target is 30 minutes.
5. A failed RPO proof or missed RTO keeps G2 closed. Never hide it with an
   application rollback.

## Cutover and release

- Repository/local verification is not Hosted Staging approval.
- G0 remains open until responsible people and two distinct operating users are
  named and the internal-only/NO-LIVE boundary is approved.
- Stop the legacy Paper Worker, preserve legacy rows as
  `legacy_unreconciled`, create the single opening journal if absent, then switch
  V2 Worker and Desktop together.
- After the first V2 journal, use forward fixes and compensating entries only.
- Resume requires two different users and verified runtime postcondition.
- Final G1/G2 approval additionally requires the 24-hour fault soak, ten
  consecutive Paper/Shadow trading days, zero ledger imbalance/fictional sell/
  duplicate order or journal, no open Sev1/Sev2 or P0/P1 defects, alert ACK
  evidence, external immutable audit receipt, monitoring in a separate failure
  domain, hosted committed-ledger RPO proof, and the restore drill within the
  30-minute RTO.
- Hosted Staging migration/deployment requires a separate explicit approval and
  dedicated credentials. Until that approval and every item above have retained
  evidence, the G1/G2 operational release decision is not complete.

## Prohibited operator actions

- Editing ledger, audit, command, or lease rows to force a desired state.
- Treating `requested`, `approved`, or `claimed` as completed.
- Auto-retrying an unknown order or offline command.
- Adding legacy orders/positions to V2 balance or performance.
- Enabling a second scheduler before fencing qualification.
- Applying hosted migrations or deployments without explicit user approval.
