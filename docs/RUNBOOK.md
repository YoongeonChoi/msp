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
TOSS_ACCOUNT_ID=<Toss accountSeq from GET /api/v1/accounts>
MOCK_PROVIDERS=false
```

`TOSS_ACCOUNT_ID` must contain Toss `accountSeq`, not a raw account number. Never put these values in Desktop env vars.

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
