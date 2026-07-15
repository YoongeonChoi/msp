-- Make Paper participation deterministic across competing source series and
-- prevent independent sell reservations from pinning incompatible rounded
-- moving-weighted-average cost checkpoints for the same position.

begin;

-- Acquire the order-intent relation lock before blocking reservation inserts.
-- A running reserve RPC writes the intent before its reservation, so this
-- order prevents a migration/RPC lock-order inversion during index creation.
create index if not exists order_intents_active_sell_reservation_lookup_idx
on private.order_intents (account_id, symbol, id)
where side = 'sell';

-- Preserve an atomic rollout boundary.  Every transaction already inserting a
-- fill/reservation must finish before the preflight, and no old-trigger write
-- may enter between the preflight and the replacement trigger functions.
lock table private.fills in share row exclusive mode;
lock table private.order_reservations in share row exclusive mode;

do $$
begin
  if exists (
    select 1
    from (
      select
        fill.account_id,
        intent.symbol,
        date_trunc('minute', fill.filled_at) as completed_minute,
        count(*) as fill_count,
        count(candidate.intent_id) as evidenced_fill_count,
        count(distinct jsonb_build_array(
          candidate.fixture_series_id,
          series.source_kind,
          series.dataset_version,
          series.volume_source,
          series.volume_evidence_sha256,
          bar.source_sha256,
          bar.volume
        )) as evidence_identity_count
      from private.fills as fill
      join private.order_intents as intent
        on intent.id = fill.intent_id
       and intent.account_id = fill.account_id
       and intent.environment = 'paper'
      left join private.paper_execution_candidates as candidate
        on candidate.intent_id = intent.id
       and candidate.account_id = intent.account_id
      left join private.paper_bar_series as series
        on series.id = candidate.fixture_series_id
      left join private.paper_minute_bars as bar
        on bar.series_id = candidate.fixture_series_id
       and bar.completed_at = date_trunc('minute', fill.filled_at)
      where fill.broker = 'internal_paper'
      group by
        fill.account_id,
        intent.symbol,
        date_trunc('minute', fill.filled_at)
      having count(*) > 1
         and (
           count(candidate.intent_id) <> count(*)
           or count(bar.series_id) <> count(*)
           or count(distinct jsonb_build_array(
             candidate.fixture_series_id,
             series.source_kind,
             series.dataset_version,
             series.volume_source,
             series.volume_evidence_sha256,
             bar.source_sha256,
             bar.volume
           )) <> 1
         )
    ) as conflict
  ) then
    raise exception 'paper_bar_participation_committed_evidence_conflict'
      using errcode = '23514';
  end if;
end;
$$;

create or replace function private.enforce_paper_bar_participation_guard()
returns trigger
language plpgsql
security invoker
set search_path = ''
as $$
declare
  intent_symbol text;
  fixture_series_id_value uuid;
  series_source_kind text;
  series_dataset_version text;
  volume_source_value text;
  volume_evidence_sha256_value text;
  bar_source_sha256_value text;
  bar_completed_at timestamptz;
  bar_volume bigint;
  participation_capacity bigint;
  already_filled_quantity bigint;
begin
  if new.broker <> 'internal_paper' then
    return new;
  end if;

  select
    intent.symbol,
    candidate.fixture_series_id,
    series.source_kind,
    series.dataset_version,
    series.volume_source,
    series.volume_evidence_sha256,
    bar.source_sha256,
    bar.completed_at,
    bar.volume
  into
    intent_symbol,
    fixture_series_id_value,
    series_source_kind,
    series_dataset_version,
    volume_source_value,
    volume_evidence_sha256_value,
    bar_source_sha256_value,
    bar_completed_at,
    bar_volume
  from private.order_intents as intent
  join private.paper_execution_candidates as candidate
    on candidate.intent_id = intent.id
   and candidate.account_id = intent.account_id
  join private.paper_bar_series as series
    on series.id = candidate.fixture_series_id
   and series.environment = 'paper'
   and series.symbol = intent.symbol
  join private.paper_minute_bars as bar
    on bar.series_id = candidate.fixture_series_id
   and bar.completed_at = date_trunc('minute', new.filled_at)
  where intent.id = new.intent_id
    and intent.account_id = new.account_id
    and intent.environment = 'paper';

  if not found then
    raise exception 'paper_bar_participation_evidence_missing'
      using errcode = '23514';
  end if;

  perform pg_catalog.pg_advisory_xact_lock(
    pg_catalog.hashtextextended(
      new.account_id || '|' || intent_symbol || '|' ||
      private.utc_iso8601(bar_completed_at),
      61972411386421::bigint
    )
  );

  if exists (
    select 1
    from private.fills as fill
    join private.order_intents as filled_intent
      on filled_intent.id = fill.intent_id
     and filled_intent.account_id = fill.account_id
     and filled_intent.environment = 'paper'
    where fill.account_id = new.account_id
      and fill.broker = 'internal_paper'
      and filled_intent.symbol = intent_symbol
      and date_trunc('minute', fill.filled_at) = bar_completed_at
      and not exists (
        select 1
        from private.paper_execution_candidates as filled_candidate
        join private.paper_bar_series as filled_series
          on filled_series.id = filled_candidate.fixture_series_id
         and filled_series.environment = 'paper'
         and filled_series.symbol = filled_intent.symbol
        join private.paper_minute_bars as filled_bar
          on filled_bar.series_id = filled_candidate.fixture_series_id
         and filled_bar.completed_at = date_trunc('minute', fill.filled_at)
        where filled_candidate.intent_id = filled_intent.id
          and filled_candidate.account_id = filled_intent.account_id
      )
  ) then
    raise exception 'paper_bar_participation_committed_evidence_missing'
      using errcode = '23514';
  end if;

  if exists (
    select 1
    from private.fills as fill
    join private.order_intents as filled_intent
      on filled_intent.id = fill.intent_id
     and filled_intent.account_id = fill.account_id
     and filled_intent.environment = 'paper'
    join private.paper_execution_candidates as filled_candidate
      on filled_candidate.intent_id = filled_intent.id
     and filled_candidate.account_id = filled_intent.account_id
    join private.paper_bar_series as filled_series
      on filled_series.id = filled_candidate.fixture_series_id
    join private.paper_minute_bars as filled_bar
      on filled_bar.series_id = filled_candidate.fixture_series_id
     and filled_bar.completed_at = date_trunc('minute', fill.filled_at)
    where fill.account_id = new.account_id
      and fill.broker = 'internal_paper'
      and filled_intent.symbol = intent_symbol
      and date_trunc('minute', fill.filled_at) = bar_completed_at
      and (
        filled_candidate.fixture_series_id
          is distinct from fixture_series_id_value
        or filled_series.source_kind is distinct from series_source_kind
        or filled_series.dataset_version is distinct from series_dataset_version
        or filled_series.volume_source is distinct from volume_source_value
        or filled_series.volume_evidence_sha256
          is distinct from volume_evidence_sha256_value
        or filled_bar.source_sha256 is distinct from bar_source_sha256_value
        or filled_bar.volume is distinct from bar_volume
      )
  ) then
    raise exception 'paper_bar_participation_evidence_conflict'
      using errcode = '23514';
  end if;

  select coalesce(sum(fill.quantity), 0)
  into already_filled_quantity
  from private.fills as fill
  join private.order_intents as filled_intent
    on filled_intent.id = fill.intent_id
  where fill.account_id = new.account_id
    and fill.broker = 'internal_paper'
    and filled_intent.environment = 'paper'
    and filled_intent.symbol = intent_symbol
    and date_trunc('minute', fill.filled_at) = bar_completed_at;

  participation_capacity := bar_volume / 100;
  if already_filled_quantity + new.quantity > participation_capacity then
    raise exception 'paper_bar_participation_capacity_exceeded'
      using errcode = '23514';
  end if;
  return new;
end;
$$;

revoke all on function private.enforce_paper_bar_participation_guard()
from public, anon, authenticated, service_role;

comment on function private.enforce_paper_bar_participation_guard() is
  'Serializes Paper fills and requires one exact series/source/volume evidence identity per account, symbol, and completed minute.';

do $$
begin
  if exists (
    select 1
    from private.order_reservations as reservation
    join private.order_intents as intent
      on intent.id = reservation.intent_id
     and intent.account_id = reservation.account_id
     and intent.side = 'sell'
    left join lateral (
      select event.remaining_quantity
      from private.reservation_events as event
      where event.reservation_id = reservation.id
      order by event.event_sequence desc
      limit 1
    ) as latest on true
    where coalesce(latest.remaining_quantity, reservation.reserved_quantity) > 0
    group by reservation.account_id, intent.symbol
    having count(*) > 1
  ) then
    raise exception 'multiple_active_sell_reservations_present'
      using errcode = '23514';
  end if;
end;
$$;

create or replace function private.enforce_single_active_sell_reservation_v1()
returns trigger
language plpgsql
security invoker
set search_path = ''
as $$
declare
  intent_side text;
  intent_symbol text;
begin
  select intent.side, intent.symbol
  into intent_side, intent_symbol
  from private.order_intents as intent
  where intent.id = new.intent_id
    and intent.account_id = new.account_id
    and intent.environment = new.environment;

  if not found then
    raise exception 'sell_reservation_intent_identity_missing'
      using errcode = '23514';
  end if;
  if intent_side <> 'sell' then
    return new;
  end if;

  perform pg_catalog.pg_advisory_xact_lock(
    pg_catalog.hashtextextended(
      new.account_id || '|' || intent_symbol,
      78633021540117::bigint
    )
  );

  if exists (
    select 1
    from private.order_reservations as reservation
    join private.order_intents as intent
      on intent.id = reservation.intent_id
     and intent.account_id = reservation.account_id
     and intent.side = 'sell'
     and intent.symbol = intent_symbol
    left join lateral (
      select event.remaining_quantity
      from private.reservation_events as event
      where event.reservation_id = reservation.id
      order by event.event_sequence desc
      limit 1
    ) as latest on true
    where reservation.account_id = new.account_id
      and reservation.id <> new.id
      and coalesce(latest.remaining_quantity, reservation.reserved_quantity) > 0
  ) then
    raise exception 'active_sell_reservation_exists'
      using errcode = '23514';
  end if;
  return new;
end;
$$;

revoke all on function private.enforce_single_active_sell_reservation_v1()
from public, anon, authenticated, service_role;

create trigger enforce_single_active_sell_reservation_v1
before insert on private.order_reservations
for each row execute function private.enforce_single_active_sell_reservation_v1();

comment on function private.enforce_single_active_sell_reservation_v1() is
  'Serializes sell reservations and permits only one non-zero reservation per account and symbol until terminal consumption or release.';

do $$
begin
  if not exists (
    select 1
    from pg_catalog.pg_trigger as trigger
    join pg_catalog.pg_class as relation on relation.oid = trigger.tgrelid
    join pg_catalog.pg_namespace as namespace
      on namespace.oid = relation.relnamespace
    where namespace.nspname = 'private'
      and relation.relname = 'fills'
      and trigger.tgname = 'enforce_paper_bar_participation'
      and trigger.tgenabled = 'O'
      and not trigger.tgisinternal
  ) or not exists (
    select 1
    from pg_catalog.pg_trigger as trigger
    join pg_catalog.pg_class as relation on relation.oid = trigger.tgrelid
    join pg_catalog.pg_namespace as namespace
      on namespace.oid = relation.relnamespace
    where namespace.nspname = 'private'
      and relation.relname = 'order_reservations'
      and trigger.tgname = 'enforce_single_active_sell_reservation_v1'
      and trigger.tgenabled = 'O'
      and not trigger.tgisinternal
  ) then
    raise exception 'execution_conflict_guard_trigger_missing'
      using errcode = '23514';
  end if;
end;
$$;

commit;
