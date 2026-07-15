# Database

## Schema ownership

- `private`: G1/G2 source-of-truth tables. It is not a Desktop Data API schema.
- `api`: strict, data-minimized Desktop projections and authenticated RPC
  wrappers.
- `worker_api`: the reviewed, explicit Worker RPC allowlist: heartbeat and lease
  lifecycle, Paper source ingest/claim, intent/observation, cash settlement,
  reconciliation, Unknown V2 application, command ACK, and outbox delivery.
- `public`: frozen legacy research/control compatibility during cutover; it is
  not exposed by the V2 Data API configuration.

`public.orders`, `public.positions`, `public.manual_commands`, and
`public.audit_logs` are frozen `legacy_unreconciled` evidence. They cannot create
V2 cash, fills, positions, or performance.

## V2 source-of-truth model

Execution and accounting:

- `trading_accounts`, `execution_controls`, `worker_leases`, `account_snapshots`
- `order_intents`, `order_reservations`, `order_attempts`,
  `execution_observations`, `fills`
- `accounting_transactions`, `accounting_postings`, `position_movements`
- cash and position projections, reconciliation runs/breaks
- `paper_bar_series`, `paper_bar_fixture_sets`, `paper_minute_bars`,
  `paper_execution_candidates`, `paper_execution_work_items`
- `cash_settlement_obligations`, `cash_settlement_state`,
  `cash_settlement_events`, `cash_settlement_cutovers`

Control and operations:

- `operation_commands`, `operation_command_reviews`,
  `operation_command_events`
- `role_assignments`, `access_change_requests`, `step_up_grants`
- `incidents`, `delivery_outbox`, `audit_events`, `control_evidence`
- `paper_execution_policies`, `execution_cost_schedules`,
  `provider_contract_registry`
- `control_approval_requests`, `control_approval_reviews`,
  `reference_bundle_materializations`, `qualification_runs`,
  `qualification_finalizations`
- `unknown_execution_resolution_requests_v2`, fill proposals, reviews, work
  items, applications, and application fills

Rules:

- Store instants as UTC `timestamptz`; derive Korean market trade/settlement
  dates with `Asia/Seoul` and display KST in the UI.
- Use typed query fields and bounded JSON objects only for extensible summaries.
- Every exposed table has RLS and explicit grants; `api` views use
  `security_invoker=true`.
- Every definer implementation is in non-exposed `private`, uses
  `search_path=''`, schema-qualified objects, caller validation, and a narrow
  `EXECUTE` grant.
- `accounting_transactions` must have at least two postings and equal debit and
  credit totals at commit.
- Cash/reserved cash and total/reserved/available quantity cannot be negative.
- Semantic intent, observation/fill identity, accounting source, and outbox
  dedupe keys are unique.
- Immutable order, fill, posting, policy evidence, and audit history cannot be
  updated or deleted.
- The audit hash chain is serialized and exported only through a redacted
  transactional outbox record.
- Never store API keys, authorization headers, account numbers, webhook secrets,
  provider raw payloads, full prompts, or full news bodies.

## Post-`0024` migration tail

The timestamp migration tail is part of the required schema, not an optional
fixture:

1. `20260714154520_control_qualification_workflow.sql`
2. `20260714155117_paper_execution_source.sql`
3. `20260714155744_cash_settlement_maturity.sql`
4. `20260714160105_operations_runtime_scheduler.sql`
5. `20260714161511_unknown_execution_resolution_v2.sql`
6. `20260714165910_unknown_resolution_desktop_projection.sql`
7. `20260715020752_kst_trading_date_convergence.sql`

The Desktop-projection migration exposes only
`api.get_unknown_resolution_cases_v2`; source evidence and application records
remain in `private`. The final convergence migration replaces session-timezone
date casts in reservation, Paper candidate, Unknown-fill, and qualification
calendar comparisons with explicit `Asia/Seoul` dates.

## Cash settlement truth

Position quantity, cost basis, and realized PnL remain trade-date accounting.
At fill time the cash leg moves through
`CASH_SETTLEMENT_PAYABLE` or `CASH_SETTLEMENT_RECEIVABLE`, and the projection
records `pending_debit_cash_krw` or `pending_credit_cash_krw`. An immutable
obligation becomes due when its `settlement_date` is no later than the current
`Asia/Seoul` date.

The Worker uses the dedicated due/list, claim, complete, and fail RPCs. Claim
ownership is bound to the current lease, release, fencing token, and DB clock.
An exact completion replay is a no-op; retry exhaustion moves the obligation to
`dead_letter` and records incident/outbox evidence. A future-dated credit is not
reported as settled cash.

## Unknown V2 accounting closure

An unknown observation first remains quarantined and cannot be resent. V1
resolution rows are evidence-only. V2 requires a strict terminal proposal
(`filled`, `canceled`, `expired`, or `rejected`), evidence hashes, an operator
request, and a review by a different risk approver. The Worker then uses only
`list_unknown_resolution_v2`, `claim_unknown_resolution_v2`, and
`apply_unknown_resolution_v2` with command/work revisions, `control_epoch`,
release, lease, and fencing-token CAS.

Generic operation-command ACK cannot apply this accounting change. Verified
missing fills are inserted through the same immutable observation/accounting/
settlement path; a no-fill terminal resolution releases only the remaining
reserve. Reapplying the exact accepted receipt does not create a second fill,
journal, or settlement obligation.

## Opening state

`paper-primary` starts `pending_open`. An evidence-bound request, distinct
`risk_approver` review, Worker claim, and atomic command ACK create its single
10,000,000 KRW opening transaction. Reapplying a migration, restarting a Worker,
or running a cycle cannot create another opening journal or reset the balance.
`contract-test-primary` is isolated and does not contribute to Paper cash,
positions, or performance.

## Legacy and cutover

The expand migration first creates V2 schemas and permanently constrains legacy
`bot_settings` to `mode='paper'` and `live_order_allowed=false`. At coordinated
cutover the legacy Worker stops, the opening journal is verified/created once,
and V2 Worker/Desktop switch together. After a V2 journal exists, use only
forward migrations and compensating transactions.

Migration order and disposable PostgreSQL assertions are documented in
[Supabase Setup](SUPABASE_SETUP.md) and `supabase/README.md`.

Database size remains an operational budget input:

```sql
select pg_database_size(current_database());
```

The Free-plan budget is treated as 500 MB until the hosted plan is verified;
alerts begin before that internal cap.
