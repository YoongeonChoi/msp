# Supabase Setup

This procedure covers repository-local verification. Creating or changing a
hosted Supabase project is a separate change that requires explicit user
approval and dedicated staging credentials.

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
39. `seed.sql`

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
```

It must apply a fresh database through the latest timestamp migration, apply the
same tail to the retained `0015` fixture path, and converge the retained `0023`
operational fixture. It then checks schema/grant/RLS assertions, Paper-only
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
