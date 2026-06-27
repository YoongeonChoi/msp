alter table public.worker_heartbeats
  add column if not exists memory_mb numeric,
  add column if not exists last_loop_ms integer,
  add column if not exists message text;

alter table public.api_health
  add column if not exists message text,
  add column if not exists error_code text,
  add column if not exists checked_at timestamptz default now();

alter table public.api_health
  alter column checked_at set default now();

update public.api_health
set checked_at = now()
where checked_at is null;

alter table public.decision_snapshots
  add column if not exists decided_at timestamptz;

update public.decision_snapshots
set decided_at = created_at
where decided_at is null
  and created_at is not null;

alter table public.decision_snapshots
  alter column decided_at set default now();

alter table public.strategy_versions
  add column if not exists version text;

do $$
begin
  if exists (
    select 1
    from information_schema.columns
    where table_schema = 'public'
      and table_name = 'strategy_versions'
      and column_name = 'version_name'
  ) then
    execute 'update public.strategy_versions set version = coalesce(version, version_name, id::text) where version is null';
  else
    update public.strategy_versions
    set version = coalesce(version, id::text)
    where version is null;
  end if;
end $$;

with ranked_strategy_versions as (
  select
    id,
    row_number() over (
      partition by version
      order by created_at desc nulls last, id desc
    ) as duplicate_rank
  from public.strategy_versions
  where version is not null
)
update public.strategy_versions as strategy_versions
set version = strategy_versions.version || '_' || strategy_versions.id::text
from ranked_strategy_versions
where strategy_versions.id = ranked_strategy_versions.id
  and ranked_strategy_versions.duplicate_rank > 1;

with ranked_watchlist as (
  select
    ctid,
    row_number() over (
      partition by symbol, market
      order by updated_at desc nulls last, created_at desc nulls last, id desc
    ) as duplicate_rank
  from public.watchlist
)
delete from public.watchlist as watchlist
using ranked_watchlist
where watchlist.ctid = ranked_watchlist.ctid
  and ranked_watchlist.duplicate_rank > 1;

create index if not exists idx_worker_heartbeats_created_at
  on public.worker_heartbeats (created_at desc);

create index if not exists idx_api_health_provider_checked_at
  on public.api_health (provider, checked_at desc);

do $$
begin
  if exists (
    select 1
    from pg_index idx
    join pg_class idx_tbl on idx_tbl.oid = idx.indexrelid
    join pg_class data_tbl on data_tbl.oid = idx.indrelid
    join pg_namespace ns on ns.oid = data_tbl.relnamespace
    join pg_attribute attr on attr.attrelid = data_tbl.oid and attr.attname = 'enabled'
    where ns.nspname = 'public'
      and data_tbl.relname = 'watchlist'
      and idx_tbl.relname = 'idx_watchlist_enabled'
      and (
        idx.indnatts <> 1
        or idx.indkey[0] <> attr.attnum
      )
  ) then
    execute 'drop index public.idx_watchlist_enabled';
  end if;
end $$;

create index if not exists idx_watchlist_enabled
  on public.watchlist (enabled);

create index if not exists idx_orders_created_at
  on public.orders (created_at desc);

create index if not exists idx_orders_symbol_created_at
  on public.orders (symbol, created_at desc);

create index if not exists idx_decision_snapshots_symbol_time
  on public.decision_snapshots (symbol, decided_at desc);

do $$
begin
  if not exists (
    select 1
    from pg_index idx
    join pg_class tbl on tbl.oid = idx.indrelid
    join pg_namespace ns on ns.oid = tbl.relnamespace
    join pg_attribute attr1 on attr1.attrelid = tbl.oid and attr1.attname = 'symbol'
    join pg_attribute attr2 on attr2.attrelid = tbl.oid and attr2.attname = 'market'
    where ns.nspname = 'public'
      and tbl.relname = 'watchlist'
      and idx.indisunique
      and idx.indnatts = 2
      and idx.indkey[0] = attr1.attnum
      and idx.indkey[1] = attr2.attnum
  ) then
    execute 'create unique index idx_watchlist_symbol_market_unique on public.watchlist (symbol, market)';
  end if;
end $$;

do $$
begin
  if not exists (
    select 1
    from pg_index idx
    join pg_class tbl on tbl.oid = idx.indrelid
    join pg_namespace ns on ns.oid = tbl.relnamespace
    join pg_attribute attr on attr.attrelid = tbl.oid and attr.attname = 'version'
    where ns.nspname = 'public'
      and tbl.relname = 'strategy_versions'
      and idx.indisunique
      and idx.indnatts = 1
      and idx.indkey[0] = attr.attnum
  ) then
    execute 'create unique index idx_strategy_versions_version_unique on public.strategy_versions (version)';
  end if;
end $$;
