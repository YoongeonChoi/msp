# Supabase Setup

This procedure covers repository-local verification. Creating or changing a
hosted Supabase project is a separate change that requires explicit user
approval and dedicated staging credentials.

## PostgreSQL 17 replay boundary

Historical migration files are immutable. Their canonical UTF-8/LF SHA-256
inventory is `supabase/migration-checksums.v1.json`; a mismatch blocks the
migration safety check. Do not edit an old migration or use `migration repair`
to hide a schema/history mismatch.

Before any repository migration runner on PostgreSQL 17:

1. Acquire one approved external single-deployment mutex for the target project.
   Hold it without interruption through preflight, migration replay, and final
   verification. The SQL advisory lock protects concurrent preflight
   transactions only; it is released before replay.
2. Stop Worker and Desktop traffic and keep Paper disabled.
3. Record the exact release SHA, target project ref, PostgreSQL version, current
   `supabase_migrations.schema_migrations` inventory, and current pgcrypto
   schema/owner/version. Do not record credentials or JWTs.
4. Verify `supabase/migration-checksums.v1.json`, calculate the SHA-256 of
   `supabase/preflight/pgcrypto_replay_preflight.sql`, and retain both digests.
5. Fix one approved database-owner role and immutable connection profile for
   both preflight and replay. Its persistent role/database default must resolve
   `public` as the first effective non-system schema. Client/session overrides,
   including `PGOPTIONS`, URI `options`, and runner-side `SET search_path`, are
   prohibited. The durable scheduler tail also requires that the role which
   directly owns the new forced-RLS tables and security-definer routines has
   `rolsuper=true` or `rolbypassrls=true`; role membership does not inherit
   `BYPASSRLS`. Record a sanitized `current_user`, `rolsuper`, and
   `rolbypassrls` receipt and stop if the direct owner contract is not met.
   Do not grant this capability ad hoc during a hosted deployment. In that
   approved profile, immediately before the migration runner, execute:

   ```bash
   psql -X -v ON_ERROR_STOP=1 \
     -f supabase/preflight/pgcrypto_replay_preflight.sql
   ```

   Connection configuration must come from the approved secret store and must
   not be printed or committed. Record sanitized `current_user` and
   `current_schemas(false)` values from this connection. The preflight does not
   create pgcrypto or edit
   migration history. It serializes itself, requires PG17, and fails closed on
   an inconsistent ledger/sentinel, retained database without pgcrypto,
   unexpected owner/schema/ACL/version/member class, untrusted extension member
   owner, drift from the exact PG17 `pgcrypto 1.3` 36-member signature/metadata
   contract, non-relocatable extension, signature collision, or catalog-visible
   unsafe caller. Empty databases receive the same `public` owner and direct,
   inherited, and `SET ROLE` reachable `CREATE` checks before the no-extension
   no-op is accepted.
6. Run `supabase migration list --linked` and `supabase db push --dry-run`, then
   apply only the pending migrations through a controlled wrapper that uses the
   same approved role and connection profile. The actual replay connection must
   emit a sanitized `current_user` and `current_schemas(false)` start receipt
   before executing SQL; compare it with step 5 and require `public` first. A dry
   run lists pending files; it does not prove SQL execution or connection state.
   If the runner cannot enforce and report this contract, stop. Do not treat the
   preflight session as proof for the separate runner session.
7. Verify the final pgcrypto schema is `extensions`, both digest overloads retain
   their OIDs, `public.digest` is absent, runtime roles have no `CREATE` on
   `extensions`, and the migration inventory matches the approved release. Then
   release the external mutex.

The same preflight handles an empty database, a retained pre-convergence DB with
pgcrypto in `public`, a retained Supabase-like DB with pgcrypto in `extensions`,
and an already-converged DB. It returns a verified no-op for empty/no-extension,
public, and final states; only the pre-convergence `extensions` state is staged
temporarily to `public`. Static catalog-visible callers are checked after SQL
comments are removed. External dynamic SQL and prepared statements cannot be
proven safe by a catalog scan, so maintenance mode and Hosted Staging evidence
remain mandatory. `psql -f` and `supabase db push` use separate sessions, so the
preflight can validate only its own session state; the same-role/profile and
runner-start receipt requirements above are separate release gates, not an SQL
guarantee. Any ambiguous state stops the release.

## Migration order

`supabase/README.md` is the canonical list. Apply every file in this exact order:

1. `0001_schema.sql`
2. `0002_rls.sql`
3. `0003_realtime.sql`
4. `0004_retention.sql`
5. `0005_schema_alignment.sql`
6. `0006_outcome_tracking.sql`
7. `0007_backtest_runs.sql`
8. `0008_backtest_runs_rls.sql`
9. `0009_live_operations_hardening.sql`
10. `0010_security_definer_hardening.sql`
11. `0011_data_api_grants.sql`
12. `0012_runtime_safety_invariants.sql`
13. `0013_worker_deployment_lock.sql`
14. `0014_desktop_audit_summary.sql`
15. `0015_paper_order_execution_details.sql`
16. `0016_private_foundation.sql`
17. `0017_execution_accounting_truth.sql`
18. `0018_control_plane_api.sql`
19. `0019_rpc_access_contract.sql`
20. `0020_canonical_operations_contract.sql`
21. `0021_reconciliation_and_cutover.sql`
22. `0022_operational_workflows.sql`
23. `0023_operational_safety_closure.sql`
24. `0024_operational_upgrade_convergence.sql`
25. `20260714154520_control_qualification_workflow.sql`
26. `20260714155117_paper_execution_source.sql`
27. `20260714155744_cash_settlement_maturity.sql`
28. `20260714160105_operations_runtime_scheduler.sql`
29. `20260714161511_unknown_execution_resolution_v2.sql`
30. `20260714165910_unknown_resolution_desktop_projection.sql`
31. `20260715020752_kst_trading_date_convergence.sql`
32. `20260715041903_paper_bar_participation_guard.sql`
33. `20260715041909_operation_claim_fencing.sql`
34. `20260715041912_sell_cost_basis_checkpoint_guard.sql`
35. `20260715041915_paper_evidence_and_sell_reservation_guards.sql`
36. `20260718165749_pgcrypto_schema_convergence.sql`
37. `20260719001947_pit_candle_revision_store.sql`
38. `20260719010000_pit_daily_candle_timing_store.sql`
39. `20260719020000_pit_source_observation_occurrence_store.sql`
40. `20260719030000_pit_daily_candle_as_of_reader.sql`
41. `20260719040000_pit_calendar_observation_store.sql`
42. `20260719050000_pit_calendar_as_of_reader.sql`
43. `20260719060000_kr_calendar_collection_job_store.sql`
44. `20260719070000_kr_calendar_collection_job_conflict_boundary.sql`
45. `20260719080000_kr_calendar_collection_job_inspection.sql`
46. `20260719090000_pit_daily_candle_collection_job_store.sql`
47. `20260723162000_desktop_operations_sensitive_projection_gate.sql`
48. `20260724210000_durable_operations_scheduler.sql`
49. `seed.sql`

The first fifteen migrations are legacy-compatible history. Migration `0016`
starts the V2 private source of truth. Migrations `0017` through `0024` add the
balanced ledger, execution RPC boundary, strict Desktop contract, recovery,
maker-checker workflows, signal-only Realtime publication, cutover grants, and
DB-clock/fencing/reconciliation fail-closed closure. Migration `0024` also
converges populated operational rows, adds per-attempt delivery and
reconciliation fencing, and rejects tokenless legacy completion paths.
Legacy trading/control rows are frozen as `legacy_unreconciled`; no migration
may invent fills, cash, or positions for them.

The timestamp migrations extend that boundary in this order:

- `20260714154520` adds reviewed reference bundles and release-bound G1/G2/
  contract qualification workflows.
- `20260714155117` adds immutable Paper minute-bar fixtures, candidates, and
  restart-safe claim/load/complete work items.
- `20260714155744` reclassifies trade-date cash through settlement clearing,
  adds KST-dated obligations, pending debit/credit projections, and durable
  settlement claim/retry/dead-letter state.
- `20260714160105` records independent command, execution, settlement,
  reconciliation, and outbox scheduler-stage heartbeat evidence.
- `20260714161511` adds Unknown V2 evidence, maker/checker review, and the
  dedicated Worker `list/claim/apply` path; V1 remains evidence-only.
- `20260714165910` exposes the strict Desktop Unknown V2 case projection without
  exposing the private evidence tables.
- `20260715020752` converges reservation, Paper-candidate, Unknown-fill, and
  qualification calendar comparisons on an explicit `Asia/Seoul` date,
  including the 00:00-08:59 KST boundary.
- `20260715041903` adds current-intent-excluding Paper bar participation to the
  strict Worker bundle and serializes fill commits so aggregate account/symbol/
  completed-bar quantity cannot exceed one percent of bar volume. It also
  requires journal, projection, and provider-identity evidence in the contract
  qualification `ledger_invariants` check.
- `20260715041909` binds operation-command claim/ACK and reconciliation claim to
  the active account lease release and fencing token; legacy tokenless overloads
  remain fail-closed during a rolling Worker upgrade.
- `20260715041912` binds sell-fill `POSITION_COST` postings to the immutable
  moving-weighted-average checkpoint captured when the intent reserved shares;
  checkpoint/projection disagreement rolls the transaction back.
- `20260715041915` rejects mixed series/source/volume evidence for aggregate
  Paper fills in one completed minute and permits only one active sell
  reservation per account and symbol until its quantity reaches zero.
- `20260718165749` relocates `pgcrypto` to the locked `extensions` schema while
  preserving extension object identities and recompiles application routines to
  use the schema-qualified `extensions.digest` reference.
- `20260719001947` adds a private, append-only daily-candle revision ledger,
  mutable serialized stream heads, durable ambiguity quarantine, and one
  service-role-only Worker RPC.
- `20260719010000` adds append-only calendar revisions and exact daily-candle
  timing bindings. PostgreSQL derives the binding from immutable candle and
  calendar rows, records idempotent request receipts, and durably quarantines
  clock, recurrence, source, and binding conflicts. It does not wire collection,
  provide a durable as-of reader, or certify provider finality.
- `20260719020000_pit_source_observation_occurrence_store.sql` separates
  deduplicated candle/calendar content revisions from
  append-only observation occurrences and binds every timing revision to the
  exact two occurrences used to derive availability. It backfills only evidence
  retained by the old schema; discarded intermediate unchanged observations
  cannot be reconstructed.
- `20260719030000_pit_daily_candle_as_of_reader.sql` adds the service-role-only
  `worker_api.list_pit_daily_candles_as_of_v1` RPC. It reads one exact provider/
  `KR`/symbol/`1d`/`adjusted` series for at most 366 inclusive calendar days,
  accepts page sizes `25..100`, and rejects a snapshot with more than 1,000 raw
  candidates. A 15-minute PostgreSQL MVCC snapshot token and a full ordered
  raw-candidate manifest bind every page. The RPC writes zero domain rows and is
  not exposed to Desktop, `public`, authenticated clients, or Realtime.
- `20260719040000_pit_calendar_observation_store.sql` adds the service-role-only
  `worker_api.append_pit_kr_daily_session_observation_v1` RPC over the private
  calendar content and occurrence ledgers. It accepts canonical open and closed
  KR session observations, derives hashes and KST geometry in PostgreSQL,
  preserves later unchanged observations as occurrences, and durably
  quarantines clock regression, same-clock conflict, and historical hash
  recurrence. It is not wired into collection, scheduling, features,
  backtests, strategy, Desktop, or orders.
- `20260719050000_pit_calendar_as_of_reader.sql` adds the service-role-only
  `worker_api.list_pit_kr_daily_sessions_as_of_v1` RPC. It reads open and closed
  retained sessions for one exact provider and `KR` market over at most 366
  inclusive calendar days, accepts page sizes `25..100`, and rejects more than
  1,000 raw candidates. A 15-minute MVCC snapshot and complete ordered manifest
  bind exact immutable occurrence/content-revision lineage across every page;
  no mutable stream head is a read source and the RPC writes zero domain rows.
  It also converges the shared calendar identity/evidence hash helpers to
  explicit `YYYY-MM-DD` dates so results remain independent of session
  `DateStyle` without changing the existing ISO hash bytes.
- `20260719060000_kr_calendar_collection_job_store.sql` adds service-role-only
  durable snapshots and an append-only attempt ledger for the default-disabled
  manual KR calendar range job. Every mutation is bound to the spec SHA,
  revision, attempt, holder, target date, and canonical clock. There is no TTL
  takeover, automatic retry, runtime-container selection, or scheduler wiring.
- `20260719070000_kr_calendar_collection_job_conflict_boundary.sql` remaps only
  deterministic spec-hash, revision, clock-regression, and attempt-fence
  conflicts at the exposed Worker RPC boundary to bounded PostgREST `PT409`
  responses. The private state machine keeps its fail-closed checks; callers
  must reload state and must not auto-retry.
- `20260719080000_kr_calendar_collection_job_inspection.sql` adds one stable,
  service-role-only Worker RPC for exact job UUID inspection. A missing job
  returns `job_found=false` with a null snapshot; a present job returns the
  canonical snapshot. The RPC does not create, mutate, retry, or recover a job.
- `20260719090000_pit_daily_candle_collection_job_store.sql` adds a private,
  service-role-only job state machine for one manual PIT daily-candle request.
  It records the begin fence and canonical candidate required by the future
  collector protocol, and completion rechecks the exact immutable occurrence
  and content revision. The migration does not itself enforce provider/append
  call order because those ports are not fence-aware and no collector is wired.
  The job state has no TTL takeover or automatic retry transition, scheduler
  wiring, Desktop exposure, or order authority.
- `20260723162000_desktop_operations_sensitive_projection_gate.sql` preserves
  the strict Desktop v1 response shape while executing the private audit and
  reconciliation detail queries only for an `auditor`. Other admitted human
  roles receive exact empty arrays for both fields. The migration leaves the
  identifier-free reconciliation health signal available to minimum-status
  viewers and reasserts the invoker/definer and execute-grant contracts.
- `20260724210000_durable_operations_scheduler.sql` adds the private durable
  cadence boundary for exactly five operations jobs: commands, execution,
  settlement, reconciliation, and outbox. Database time owns due and expiry
  decisions; each inner run lease is capped by and bound to the current outer
  Worker account, holder, fencing token, and release SHA. One nonterminal run
  is allowed per definition. Commands must complete once in the current outer
  lease generation before execution can be claimed, including after restart;
  settlement and reconciliation must also exist, remain enabled and ready, and
  have no expired lease awaiting classification. The typed convergence RPC
  installs a desired digest only after the prior run is quiescent, may claim an
  existing non-effectful recovery run, and never creates an old-definition
  cadence run.
  Only command, reconciliation, and outbox polling failures may consume the
  bounded automatic retry budget. Execution and settlement expiry is an
  immutable dead letter and cannot be manually replayed without a future
  evidence-backed resolution contract. Manual replay for eligible jobs creates
  a new child run; it never mutates the source dead letter, and the exact request
  ID can recover the creation receipt after Worker takeover. No scheduler RPC
  accepts caller time, module names, function names, or executable payloads.

## Required project configuration

1. Expose only `api` and `worker_api` through the Data API. `api` is the
   authenticated Desktop boundary; `worker_api` is callable only with the
   server-side Worker credential. Do not expose `public` or `private`.
2. Keep explicit schema/table/function grants under migration control. RLS alone
   is not an API permission boundary.
3. Enable Supabase Auth TOTP and require AAL2 for operational mutations.
4. Enrol at least two distinct operating users and assign only the seven roles
   documented in `docs/SECURITY.md`.
5. Desktop receives only project URL and publishable key. Worker secrets stay in
   the server-side runtime.
6. Keep `supabase_realtime` limited to `api.control_plane_signal`. It is a
   monotonic invalidation signal, not a state source; clients refetch the strict
   snapshot after a signal. Do not publish raw order events, audit payloads,
   decision snapshots, accounting postings, or legacy `public` tables.
7. Keep `public.bot_settings.mode='paper'` and
   `public.bot_settings.live_order_allowed=false`.

## Local verification

With Docker available, run the repository verifier against a disposable
PostgreSQL instance:

```bash
python supabase/verify_g1_g2_migration.py
python supabase/verify_pit_candle_revision_store.py
python supabase/verify_pit_daily_candle_timing_store.py
python supabase/verify_pit_source_observation_occurrence_store.py
python supabase/verify_pit_daily_candle_as_of_reader.py
python supabase/verify_pit_calendar_observation_store.py
python supabase/verify_pit_calendar_as_of_reader.py
python supabase/verify_kr_calendar_collection_job_store.py
```

It must apply a raw fresh PG17 database and a Supabase-like fresh PG17 database
with preinstalled `extensions.pgcrypto` through the latest timestamp migration.
It must also apply the same tail to separate retained `0015` fixtures whose
pgcrypto starts in `public` and `extensions`, and converge the retained `0023`
operational fixture. The `0023` lane checks both the immediate `0024` state and
the intentional later `qualification_v1_required` fail-closed transition after
the complete tail. It then checks schema/grant/RLS assertions, Paper-only
constraints, ledger invariants, command separation, append-only audit behavior,
Paper source publication, KST cash settlement, Unknown V2 dedicated
maker/checker application, aggregate Paper bar participation, lease-bound
operation/reconciliation claims, and Worker RPC visibility. Also run the Python
contract tests and repository safety checks. The dedicated PIT verifier adds
Python/SQL canonical-hash vectors, exact replay and correction semantics,
concurrent first-write serialization, durable quarantine, direct-table denial,
and a populated pre-migration upgrade check. The timing verifier additionally
checks calendar revision semantics, exact immutable source binding, request
idempotency conflicts, derived availability time, and forged-source rejection.
The occurrence verifier additionally checks exact populated backfill, immutable
same-content re-observation, component-wise source-clock monotonicity, exact
occurrence foreign keys, and concurrent delivery convergence.
The as-of reader verifier and Worker unit tests additionally check the bounded
exact-series request, service-role-only RPC grant, zero-write reads,
deterministic paging under one 15-minute MVCC snapshot, complete raw-candidate
manifest binding, adapter/selector equivalence, and fail-closed ambiguity,
corruption, cursor, and concurrent-writer cases.
The independent calendar verifier additionally checks open/closed session
geometry, exact historical occurrence replay, later unchanged observations,
corrections, durable quarantine, fresh and populated upgrades, and concurrent
serialization with the existing timing RPC. It also verifies the current
fail-closed limitation that a new timing request cannot bind an older calendar
occurrence after that calendar stream head advances.
The Worker observation adapter additionally requires identity encoding, bounds
one RPC response to 64 KiB, and rejects duplicate JSON keys before receipt use.
The calendar as-of verifier additionally checks open and closed session reads,
the 366-day/page/candidate bounds, immutable revision/occurrence lineage,
15-minute snapshot/cursor/manifest continuity, `observed_at` cutoff semantics,
full as-of-eligible retained-timeline quarantine blocking, service-role-only
ACLs, and zero domain writes. Every page must be validated and buffered before
exposure; a cursor, manifest, payload, hash, lineage, or quarantine failure
returns no partial result. The official Worker adapter additionally rejects
non-identity response encoding, non-terminal short pages, excess continuations,
and RPC response bodies above 4 MiB before returning a fixed, payload-free
parser error.
The calendar collection-job verifier additionally checks reconnect durability,
concurrent create/begin, stale CAS zero-change, pause/rebegin, blocked-attempt
takeover denial, immutable occurrence binding, and canonical UTC. It also starts
the pinned local PostgREST image to verify the five mutation RPC envelopes and
the inspection RPC's missing/present envelope, invalid UUID rejection,
service-role/profile denial matrix, unchanged counts and existing-job
zero-write fingerprint, and bounded `PT409` stale-CAS response. A 366-day job
must finish with revision 733, 732
ledger events, 366 contiguous checkpoints, Python/SQL terminal-manifest parity,
an identity response below the 4 MiB adapter limit, forced RLS/ACL, append-only
history, and zero order-domain writes.

The application-layer calendar recovery assessment is read-only and performs
exactly one `KrCalendarCollectionJobInspectorPort.inspect_job` call for a valid
request. It canonicalizes the requested spec, requires the caller-provided spec
SHA to match, revalidates the returned snapshot, and then reports only a
conservative state classification and review direction. It performs no RPC
mutation and grants no retry, manual-execution, manual-recovery, scheduler, or
Production Live authority. The service-role-only inspection RPC and Supabase
inspector adapter are selected only by the default-disabled, read-only
`assess_kr_calendar_collection_job` command. It emits an execution precondition
only for `missing`, `ready`, or a `paused_retryable` snapshot whose state reason
is exactly `collection_failed_before_write`. The separate default-disabled
`run_kr_calendar_collection_job_once` mutation command requires the reviewed
spec SHA, classification, state reason, revision, confirmed count, and next
date; it rechecks those values after loading the mutation snapshot. It can
advance at most one date from `missing`/`ready`, or from recognized
`paused_retryable` after a second explicit review confirmation. It refuses
`paused_unrecognized`, `collecting`, `blocked_unknown`, `completed`, and any
assessment drift before provider collection. This does not add a main
runtime, scheduler, hosted operation, automatic retry, TTL takeover, or
unresolved-write recovery. The migration verifier therefore does not claim
that recovery is operational against a hosted project.

For the calendar reader, `occurrence.observed_at <= as_of` is the semantic
source cutoff. `received_at` remains lineage and cannot reconstruct which rows
were committed or visible in the database at a historical instant. It is not
wired into collection, the runtime container, scheduling, timing backfill, DQ,
dataset, research, features, backtests, strategy, Desktop, or orders. Passing
the verifier does not certify completeness, authenticity, provider finality,
corporate-action safety, Hosted Staging, or Production Live authorization.

For this reader, `evidence_available_at <= as_of` reconstructs eligibility from
the durable source evidence retained when the query runs. It is not a
`received_at <= as_of` filter and does not prove historical transaction commit
visibility. The Worker adapter must validate and buffer every page before
calling the existing selector exactly once; it must not expose a partial
selection. The reader is not wired into collection, the runtime container,
features, strategy, backtests, or orders. Passing the verifier does not certify
completeness, authenticity, provider finality, corporate-action safety, DQ or
feature readiness, Hosted Staging, or Production Live authorization.

Do not use the service role or a database owner session as evidence for the
authenticated/anonymous negative matrix. Hosted staging additionally requires
real Auth JWTs at AAL1/AAL2 for each role, two-person command tests, Supabase
advisors, and retained output bound to the tested migration SHA.

## Seed and opening journal

`paper-primary` receives one 10,000,000 KRW opening journal only through the
evidence-bound account-opening request, distinct risk-approver review, Worker
claim, and atomic ACK/application path. A second journal is rejected. It must
never be reset on a cycle or redeploy. Contract qualification uses
`contract-test-primary` and cannot affect Paper projections.

No secret, production account number, provider raw payload, or real customer
data belongs in seed data.

## Explicit Paper source publication

The Worker does not synthesize strategy candidates or market bars. To publish
one reviewed local fixture or approved read-evidence artifact, keep the normal
runtime configured for `paper`, set
`EXECUTION_V2_PAPER_SOURCE_INPUT_ENABLED=true` for that explicit invocation,
and pass the independently computed artifact SHA-256:

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

The publisher accepts only the strict `schema_version=1` shape, rejects an
unknown or duplicate field, and uses only
`worker_api.ingest_paper_bar_fixture_v1` and
`worker_api.enqueue_paper_execution_candidate_v1`. The DB still verifies the
active lease/fencing token, release, `control_epoch`, qualification, policy,
cost schedule, calendar, tick, volume, and corporate-action evidence. A local
file hash proves byte identity, not origin approval. This command is not an
automatic strategy/data producer.

## Local contract qualification

Run the fixed-artifact, zero-network simulator suite separately:

```powershell
cd apps/worker
py -m app.tools.run_contract_qualification_once
```

Its JSON manifest covers create replay, partial-to-terminal status, cancel,
fault injection, and a production-order-network request count of zero. It is
local evidence only. It does not call an official sandbox, register/finalize a
qualification in the database, or authorize Hosted Staging.
