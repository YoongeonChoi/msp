# Database

Core tables:

- `bot_settings`
- `watchlist`
- `positions`
- `orders`
- `decision_snapshots`
- `features_daily`
- `fundamentals_quarterly`
- `news_events`
- `strategy_versions`
- `ai_upgrade_candidates`
- `api_health`
- `worker_heartbeats`
- `engine_events`
- `audit_logs`
- `outbox_events`

Rules:

- Store UTC `timestamptz`; display KST in UI.
- Keep queryable fields typed.
- Use JSONB for snapshots and extensible strategy params.
- Never store raw API secrets.
- Never store full OpenAI prompts by default.
- Never store full news article bodies.
- Unique `orders.idempotency_key`.
- RLS enabled for all exposed public tables.
- `bot_settings.id` is text and the singleton row must be `id='singleton'`.
- `worker_heartbeats` includes operational status fields: `memory_mb`, `last_loop_ms`, `message`, `details`, `created_at`.
- `api_health` includes provider status fields: `provider`, `healthy`, `status`, `message`, `error_code`, `checked_at`, `details`.
- `watchlist` supports upsert by `(symbol, market)`.
- `strategy_versions.version` is unique for deployment/research references.
- `decision_snapshots.decided_at` is the decision-time index column; existing rows may be backfilled from `created_at`.
- Worker writes `decision_snapshots.created_at` as the durable decision time so
  hosted projects still on the base schema can accept snapshots. Read paths may
  use `decided_at` when present and must fall back to `created_at`.

DB size query:

```sql
select pg_database_size(current_database());
```

## Schema Alignment Migration

`supabase/migrations/0005_schema_alignment.sql` aligns the current Render/Supabase/Worker/Paper Trading setup with application expectations. It is idempotent where PostgreSQL supports it, does not weaken RLS, does not add anon/public write policies, and does not touch live order execution.

The migration deletes only duplicate `watchlist` rows that block the `(symbol, market)` unique index. It keeps the latest row by `updated_at`, then `created_at`, then `id`.

## Verification SQL

Check the `bot_settings` singleton:

```sql
select id, enabled, mode, live_order_allowed from public.bot_settings;
```

Check latest worker heartbeats:

```sql
select * from public.worker_heartbeats order by created_at desc limit 10;
```

Check latest API health by provider:

```sql
select distinct on (provider) * from public.api_health order by provider, checked_at desc;
```

Check `watchlist` insert/upsert:

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

The RLS query should return no rows for application tables.

Check that order and decision counts did not increase unexpectedly while `enabled=false`:

```sql
select
  (select count(*) from public.orders) as orders_count,
  (select count(*) from public.decision_snapshots) as decision_snapshots_count;
```
