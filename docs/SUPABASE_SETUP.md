# Supabase Setup

1. Create Supabase project.
2. Enable Auth email provider suitable for one admin user.
3. Run migrations in order.
4. Run `seed.sql`.
5. Insert admin user into `user_roles`.
6. Use publishable key in desktop.
7. Use secret key only in Render worker env vars.
8. Verify RLS with non-admin authenticated user.
9. Enable Realtime only for lightweight control/status tables.

Desktop must not use service role or secret key.

## Desktop Admin Session

The desktop cockpit uses only `VITE_SUPABASE_URL` and
`VITE_SUPABASE_PUBLISHABLE_KEY`. It still needs a Supabase Auth session for an
admin user whose `auth.users.id` exists in `public.user_roles` with
`role='admin'`.

If the cockpit shows `권한 필요`, `모드: 권한 필요`, missing provider health,
empty-looking feature pages, or a stale-looking worker card while the worker
health tool shows fresh hosted rows, open `Settings` and sign in with the admin
Auth account. Without that session, RLS returns empty table results rather than
a hard query error, so `bot_settings`, `worker_heartbeats`, `api_health`, and
the feature tables can all look absent.

Worker-side verification remains service-role only:

```bash
cd apps/worker
py -m app.tools.paper_health_report
```

`bot_settings.enabled=false` stops order creation only. The worker cycle still
records heartbeat/provider health and can persist decision snapshots plus
feature observations; if those rows are invisible in the cockpit, check the
Supabase Auth/RLS session before treating it as a bot-stop issue.

## Migration Order

`supabase/README.md` is the canonical migration list. Before a new setup or a
manual migration apply, confirm this section still matches that list and run
every migration in this exact order:

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
16. `seed.sql`

`0012_runtime_safety_invariants.sql`는 live 승인 소비와 strategy/runtime 수치
불변식을 DB 경계에서 강제합니다. `0013_worker_deployment_lock.sql`는 service-role
RPC만 배포 잠금을 변경할 수 있게 하고, 배포 target을 관찰한 fresh/healthy
heartbeat 없이는 잠금을 해제하지 않습니다. 잠금 중에는 `enabled`와
`live_order_allowed`가 모두 false여야 합니다.
`0014_desktop_audit_summary.sql`은 desktop에서 원본 감사 snapshot을 읽지 못하게 하고,
admin 전용 `get_audit_log_summaries` RPC로 변경된 필드명만 제공합니다.
`0015_paper_order_execution_details.sql`은 paper/live 실행에 사용한 정수 `quantity`와
`price_krw`를 orders에 기록합니다. 두 컬럼은 기존 행 호환을 위해 nullable이며 값이
있을 때는 양수 제약을 적용합니다.

`0005_schema_alignment.sql` fixes known production drift:

- `worker_heartbeats.memory_mb`
- `worker_heartbeats.last_loop_ms`
- `worker_heartbeats.message`
- `api_health.message`
- `api_health.error_code`
- `api_health.checked_at`
- `watchlist(symbol, market)` unique upsert target
- `strategy_versions.version` unique reference
- operational indexes for paper trading verification

## Verification SQL

Check the singleton settings row:

```sql
select id, enabled, mode, live_order_allowed from public.bot_settings;
```

Check latest worker heartbeat rows:

```sql
select * from public.worker_heartbeats order by created_at desc limit 10;
```

Check latest API health by provider:

```sql
select distinct on (provider) * from public.api_health order by provider, checked_at desc;
```

Check watchlist upsert:

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

Check RLS disabled tables:

```sql
select tablename
from pg_tables
where schemaname = 'public'
  and rowsecurity = false;
```

Check orders and decisions did not increase unexpectedly while `enabled=false`:

```sql
select
  (select count(*) from public.orders) as orders_count,
  (select count(*) from public.decision_snapshots) as decision_snapshots_count;
```
