begin;

set local lock_timeout = '5s';
set local statement_timeout = '90s';

-- Forward-only dependencies:
--   20260719001947_pit_candle_revision_store.sql
--   20260719010000_pit_daily_candle_timing_store.sql
do $$
begin
  if to_regprocedure('private.require_service_role()') is null
     or to_regprocedure('private.jsonb_exact_keys_v1(jsonb,text[])') is null
     or to_regprocedure('private.reject_append_only_mutation()') is null
     or to_regprocedure('private.pit_canonical_timestamp_v1(timestamptz)') is null
     or to_regprocedure('private.pit_sha256_text_v1(text)') is null
     or to_regprocedure(
       'private.append_pit_candle_observation_v1_impl(jsonb)'
     ) is null
     or to_regprocedure(
       'private.append_pit_daily_candle_timing_evidence_v1_impl(text,jsonb,jsonb)'
     ) is null
     or to_regclass('private.pit_candle_stream_heads') is null
     or to_regclass('private.pit_candle_observation_revisions') is null
     or to_regclass('private.pit_calendar_stream_heads') is null
     or to_regclass('private.pit_calendar_content_revisions') is null
     or to_regclass('private.pit_daily_candle_timing_heads') is null
     or to_regclass('private.pit_daily_candle_timing_revisions') is null then
    raise exception 'pit_source_occurrence_store_dependency_missing'
      using errcode = '55000';
  end if;
end;
$$;

-- Block all source/timing writers while exact historical bindings are
-- backfilled. A busy database fails the migration instead of mixing schemas.
lock table
  private.pit_candle_stream_heads,
  private.pit_candle_observation_revisions,
  private.pit_candle_observation_quarantine,
  private.pit_calendar_stream_heads,
  private.pit_calendar_content_revisions,
  private.pit_calendar_observation_quarantine,
  private.pit_daily_candle_timing_heads,
  private.pit_daily_candle_timing_revisions,
  private.pit_daily_candle_timing_quarantine,
  private.pit_daily_candle_timing_request_receipts,
  private.pit_daily_candle_timing_request_ledger
in share row exclusive mode;

alter table private.pit_candle_observation_revisions
  add constraint pit_candle_revision_occurrence_fk_key
  unique (
    id,
    idempotency_key,
    canonical_observation_sha256
  );

alter table private.pit_calendar_content_revisions
  add constraint pit_calendar_revision_occurrence_fk_key
  unique (
    id,
    calendar_idempotency_key,
    canonical_evidence_sha256
  );

create table private.pit_candle_observation_occurrences (
  id uuid primary key default pg_catalog.gen_random_uuid(),
  idempotency_key text not null
    check (idempotency_key ~ '^[0-9a-f]{64}$'),
  canonical_observation_sha256 text not null
    check (canonical_observation_sha256 ~ '^[0-9a-f]{64}$'),
  content_revision_id uuid not null,
  observed_at timestamptz not null,
  observation_payload jsonb not null
    check (jsonb_typeof(observation_payload) = 'object')
    check (pg_catalog.octet_length(observation_payload::text) <= 4096),
  record_origin text not null check (
    record_origin in (
      'content_revision_backfill',
      'stream_head_recovery',
      'rpc'
    )
  ),
  received_at timestamptz not null default clock_timestamp(),
  unique (idempotency_key, observed_at),
  unique (id, content_revision_id),
  constraint pit_candle_occurrence_revision_fk
    foreign key (
      content_revision_id,
      idempotency_key,
      canonical_observation_sha256
    )
    references private.pit_candle_observation_revisions (
      id,
      idempotency_key,
      canonical_observation_sha256
    ),
  check (
    observation_payload->>'canonical_observation_sha256'
      = canonical_observation_sha256
  ),
  check (
    observation_payload->>'observed_at'
      = private.pit_canonical_timestamp_v1(observed_at)
  )
);

create index pit_candle_observation_occurrences_as_of_idx
  on private.pit_candle_observation_occurrences (
    idempotency_key,
    observed_at desc
  );

create table private.pit_calendar_observation_occurrences (
  id uuid primary key default pg_catalog.gen_random_uuid(),
  calendar_idempotency_key text not null
    check (calendar_idempotency_key ~ '^[0-9a-f]{64}$'),
  canonical_evidence_sha256 text not null
    check (canonical_evidence_sha256 ~ '^[0-9a-f]{64}$'),
  content_revision_id uuid not null,
  observed_at timestamptz not null,
  observation_payload jsonb not null
    check (jsonb_typeof(observation_payload) = 'object')
    check (pg_catalog.octet_length(observation_payload::text) <= 8192),
  record_origin text not null check (
    record_origin in (
      'content_revision_backfill',
      'stream_head_recovery',
      'rpc'
    )
  ),
  received_at timestamptz not null default clock_timestamp(),
  unique (calendar_idempotency_key, observed_at),
  unique (id, content_revision_id),
  constraint pit_calendar_occurrence_revision_fk
    foreign key (
      content_revision_id,
      calendar_idempotency_key,
      canonical_evidence_sha256
    )
    references private.pit_calendar_content_revisions (
      id,
      calendar_idempotency_key,
      canonical_evidence_sha256
    ),
  check (
    observation_payload->>'canonical_evidence_sha256'
      = canonical_evidence_sha256
  ),
  check (
    observation_payload->>'observed_at'
      = private.pit_canonical_timestamp_v1(observed_at)
  )
);

create index pit_calendar_observation_occurrences_as_of_idx
  on private.pit_calendar_observation_occurrences (
    calendar_idempotency_key,
    observed_at desc
  );

-- Every immutable content revision is also its first known occurrence.
insert into private.pit_candle_observation_occurrences (
  idempotency_key,
  canonical_observation_sha256,
  content_revision_id,
  observed_at,
  observation_payload,
  record_origin,
  received_at
)
select
  revision.idempotency_key,
  revision.canonical_observation_sha256,
  revision.id,
  revision.observed_at,
  revision.candle_payload,
  'content_revision_backfill',
  revision.received_at
from private.pit_candle_observation_revisions as revision;

insert into private.pit_calendar_observation_occurrences (
  calendar_idempotency_key,
  canonical_evidence_sha256,
  content_revision_id,
  observed_at,
  observation_payload,
  record_origin,
  received_at
)
select
  revision.calendar_idempotency_key,
  revision.canonical_evidence_sha256,
  revision.id,
  revision.observed_at,
  revision.calendar_payload,
  'content_revision_backfill',
  revision.received_at
from private.pit_calendar_content_revisions as revision;

-- Older candle code remembered only the last clock for same-content
-- re-observations. Recover that last known occurrence without inventing any
-- unknown intermediate observations.
insert into private.pit_candle_observation_occurrences (
  idempotency_key,
  canonical_observation_sha256,
  content_revision_id,
  observed_at,
  observation_payload,
  record_origin,
  received_at
)
select
  head.idempotency_key,
  revision.canonical_observation_sha256,
  revision.id,
  head.last_seen_observed_at,
  pg_catalog.jsonb_set(
    revision.candle_payload,
    '{observed_at}',
    pg_catalog.to_jsonb(
      private.pit_canonical_timestamp_v1(head.last_seen_observed_at)
    ),
    false
  ),
  'stream_head_recovery',
  head.updated_at
from private.pit_candle_stream_heads as head
join private.pit_candle_observation_revisions as revision
  on revision.idempotency_key = head.idempotency_key
 and revision.revision = head.latest_revision
where head.last_seen_observed_at > revision.observed_at;

-- This path is defensive for databases upgraded from an intermediate build.
insert into private.pit_calendar_observation_occurrences (
  calendar_idempotency_key,
  canonical_evidence_sha256,
  content_revision_id,
  observed_at,
  observation_payload,
  record_origin,
  received_at
)
select
  head.calendar_idempotency_key,
  revision.canonical_evidence_sha256,
  revision.id,
  head.last_seen_observed_at,
  pg_catalog.jsonb_set(
    revision.calendar_payload,
    '{observed_at}',
    pg_catalog.to_jsonb(
      private.pit_canonical_timestamp_v1(head.last_seen_observed_at)
    ),
    false
  ),
  'stream_head_recovery',
  head.updated_at
from private.pit_calendar_stream_heads as head
join private.pit_calendar_content_revisions as revision
  on revision.calendar_idempotency_key = head.calendar_idempotency_key
 and revision.revision = head.latest_revision
where head.last_seen_observed_at > revision.observed_at;

do $$
begin
  if exists (
    select 1
    from private.pit_candle_observation_revisions as revision
    where not exists (
      select 1
      from private.pit_candle_observation_occurrences as occurrence
      where occurrence.content_revision_id = revision.id
        and occurrence.observed_at = revision.observed_at
    )
  ) or exists (
    select 1
    from private.pit_calendar_content_revisions as revision
    where not exists (
      select 1
      from private.pit_calendar_observation_occurrences as occurrence
      where occurrence.content_revision_id = revision.id
        and occurrence.observed_at = revision.observed_at
    )
  ) then
    raise exception 'pit_source_occurrence_revision_backfill_incomplete'
      using errcode = '55000';
  end if;

  if exists (
    select 1
    from private.pit_candle_stream_heads as head
    join private.pit_candle_observation_revisions as revision
      on revision.idempotency_key = head.idempotency_key
     and revision.revision = head.latest_revision
    where not exists (
      select 1
      from private.pit_candle_observation_occurrences as occurrence
      where occurrence.content_revision_id = revision.id
        and occurrence.observed_at = head.last_seen_observed_at
    )
  ) or exists (
    select 1
    from private.pit_calendar_stream_heads as head
    join private.pit_calendar_content_revisions as revision
      on revision.calendar_idempotency_key = head.calendar_idempotency_key
     and revision.revision = head.latest_revision
    where not exists (
      select 1
      from private.pit_calendar_observation_occurrences as occurrence
      where occurrence.content_revision_id = revision.id
        and occurrence.observed_at = head.last_seen_observed_at
    )
  ) then
    raise exception 'pit_source_occurrence_head_backfill_incomplete'
      using errcode = '55000';
  end if;
end;
$$;

-- Existing timing revisions are append-only in normal operation. Temporarily
-- remove the exact guard while this locked migration adds deterministic source
-- occurrence foreign keys, then restore it before the transaction commits.
drop trigger reject_pit_daily_candle_timing_revision_mutation
  on private.pit_daily_candle_timing_revisions;

alter table private.pit_daily_candle_timing_revisions
  add column candle_occurrence_id uuid,
  add column calendar_occurrence_id uuid;

update private.pit_daily_candle_timing_revisions as timing
set candle_occurrence_id = (
      select occurrence.id
      from private.pit_candle_observation_occurrences as occurrence
      where occurrence.content_revision_id = timing.candle_revision_id
        and occurrence.observed_at =
          (timing.timing_payload->>'candle_observed_at')::timestamptz
    ),
    calendar_occurrence_id = (
      select occurrence.id
      from private.pit_calendar_observation_occurrences as occurrence
      where occurrence.content_revision_id = timing.calendar_revision_id
        and occurrence.observed_at =
          (timing.timing_payload->>'calendar_observed_at')::timestamptz
    );

do $$
begin
  if exists (
    select 1
    from private.pit_daily_candle_timing_revisions
    where candle_occurrence_id is null
       or calendar_occurrence_id is null
  ) then
    raise exception 'pit_timing_occurrence_backfill_incomplete'
      using errcode = '55000';
  end if;
end;
$$;

alter table private.pit_daily_candle_timing_revisions
  alter column candle_occurrence_id set not null,
  alter column calendar_occurrence_id set not null;

do $$
declare
  old_constraint_name text;
  old_constraint_count bigint;
begin
  select count(*), min(constraint_row.conname::text)
  into old_constraint_count, old_constraint_name
  from pg_catalog.pg_constraint as constraint_row
  where constraint_row.conrelid =
      'private.pit_daily_candle_timing_revisions'::regclass
    and constraint_row.contype = 'u'
    and (
      select pg_catalog.array_agg(
        attribute.attname::text
        order by key_column.ordinality
      )
      from pg_catalog.unnest(constraint_row.conkey)
        with ordinality as key_column(attnum, ordinality)
      join pg_catalog.pg_attribute as attribute
        on attribute.attrelid = constraint_row.conrelid
       and attribute.attnum = key_column.attnum
    ) = array['candle_revision_id', 'calendar_revision_id'];

  if old_constraint_count <> 1 or old_constraint_name is null then
    raise exception 'pit_timing_content_pair_constraint_ambiguous'
      using errcode = '55000';
  end if;

  execute pg_catalog.format(
    'alter table private.pit_daily_candle_timing_revisions drop constraint %I',
    old_constraint_name
  );
end;
$$;

alter table private.pit_daily_candle_timing_revisions
  add constraint pit_timing_candle_occurrence_revision_fk
    foreign key (candle_occurrence_id, candle_revision_id)
    references private.pit_candle_observation_occurrences (
      id,
      content_revision_id
    ),
  add constraint pit_timing_calendar_occurrence_revision_fk
    foreign key (calendar_occurrence_id, calendar_revision_id)
    references private.pit_calendar_observation_occurrences (
      id,
      content_revision_id
    ),
  add constraint pit_timing_occurrence_pair_key
    unique (candle_occurrence_id, calendar_occurrence_id);

create index pit_daily_candle_timing_revisions_candle_occurrence_idx
  on private.pit_daily_candle_timing_revisions (
    candle_occurrence_id,
    candle_revision_id
  );

create index pit_daily_candle_timing_revisions_calendar_occurrence_idx
  on private.pit_daily_candle_timing_revisions (
    calendar_occurrence_id,
    calendar_revision_id
  );

create trigger reject_pit_daily_candle_timing_revision_mutation
  before update or delete on private.pit_daily_candle_timing_revisions
  for each row execute function private.reject_append_only_mutation();

create trigger reject_pit_candle_occurrence_mutation
  before update or delete on private.pit_candle_observation_occurrences
  for each row execute function private.reject_append_only_mutation();

create trigger reject_pit_calendar_occurrence_mutation
  before update or delete on private.pit_calendar_observation_occurrences
  for each row execute function private.reject_append_only_mutation();

alter table private.pit_candle_observation_occurrences enable row level security;
alter table private.pit_calendar_observation_occurrences enable row level security;

revoke all on table
  private.pit_candle_observation_occurrences,
  private.pit_calendar_observation_occurrences
from public, anon, authenticated, service_role;

create or replace function private.put_pit_candle_observation_occurrence_v1(
  p_idempotency_key text,
  p_canonical_observation_sha256 text,
  p_content_revision_id uuid,
  p_observed_at timestamptz,
  p_observation_payload jsonb
)
returns table (
  occurrence_id uuid,
  occurrence_inserted boolean
)
language plpgsql
volatile
security definer
set search_path = ''
as $$
declare
  stored private.pit_candle_observation_occurrences%rowtype;
begin
  insert into private.pit_candle_observation_occurrences (
    idempotency_key,
    canonical_observation_sha256,
    content_revision_id,
    observed_at,
    observation_payload,
    record_origin
  ) values (
    p_idempotency_key,
    p_canonical_observation_sha256,
    p_content_revision_id,
    p_observed_at,
    p_observation_payload,
    'rpc'
  )
  on conflict (idempotency_key, observed_at) do nothing
  returning * into stored;

  if stored.id is not null then
    return query select stored.id, true;
    return;
  end if;

  select occurrence.* into strict stored
  from private.pit_candle_observation_occurrences as occurrence
  where occurrence.idempotency_key = p_idempotency_key
    and occurrence.observed_at = p_observed_at;

  if stored.canonical_observation_sha256 <>
       p_canonical_observation_sha256
     or stored.content_revision_id <> p_content_revision_id
     or stored.observation_payload <> p_observation_payload then
    raise exception 'pit_candle_observation_occurrence_conflict'
      using errcode = '55000';
  end if;

  return query select stored.id, false;
end;
$$;

create or replace function private.put_pit_calendar_observation_occurrence_v1(
  p_calendar_idempotency_key text,
  p_canonical_evidence_sha256 text,
  p_content_revision_id uuid,
  p_observed_at timestamptz,
  p_observation_payload jsonb
)
returns table (
  occurrence_id uuid,
  occurrence_inserted boolean
)
language plpgsql
volatile
security definer
set search_path = ''
as $$
declare
  stored private.pit_calendar_observation_occurrences%rowtype;
begin
  insert into private.pit_calendar_observation_occurrences (
    calendar_idempotency_key,
    canonical_evidence_sha256,
    content_revision_id,
    observed_at,
    observation_payload,
    record_origin
  ) values (
    p_calendar_idempotency_key,
    p_canonical_evidence_sha256,
    p_content_revision_id,
    p_observed_at,
    p_observation_payload,
    'rpc'
  )
  on conflict (calendar_idempotency_key, observed_at) do nothing
  returning * into stored;

  if stored.id is not null then
    return query select stored.id, true;
    return;
  end if;

  select occurrence.* into strict stored
  from private.pit_calendar_observation_occurrences as occurrence
  where occurrence.calendar_idempotency_key = p_calendar_idempotency_key
    and occurrence.observed_at = p_observed_at;

  if stored.canonical_evidence_sha256 <> p_canonical_evidence_sha256
     or stored.content_revision_id <> p_content_revision_id
     or stored.observation_payload <> p_observation_payload then
    raise exception 'pit_calendar_observation_occurrence_conflict'
      using errcode = '55000';
  end if;

  return query select stored.id, false;
end;
$$;

create or replace function private.append_pit_candle_observation_v1_impl(
  p_candle jsonb
)
returns table (
  status text,
  idempotency_key text,
  canonical_observation_sha256 text,
  revision bigint,
  inserted boolean,
  stored_observed_at timestamptz,
  quarantine_id uuid,
  reason_code text
)
language plpgsql
volatile
security definer
set search_path = ''
as $$
declare
  provider_value text;
  symbol_value text;
  market_value text;
  interval_value text;
  adjusted_value boolean;
  provider_event_at_value timestamptz;
  observed_at_value timestamptz;
  currency_value text;
  open_value numeric;
  high_value numeric;
  low_value numeric;
  close_value numeric;
  volume_value numeric;
  contract_sha256_value text;
  candidate_sha256_value text;
  expected_idempotency_key text;
  expected_candidate_sha256 text;
  head private.pit_candle_stream_heads%rowtype;
  next_revision bigint;
  quarantine_id_value uuid;
  quarantine_reason_value text;
  content_revision_row private.pit_candle_observation_revisions%rowtype;
  occurrence_id_value uuid;
  occurrence_inserted_value boolean;
begin
  perform private.require_service_role();

  if p_candle is null
     or pg_catalog.octet_length(p_candle::text) > 4096
     or private.jsonb_exact_keys_v1(
       p_candle,
       array[
         'adjusted', 'canonical_observation_sha256', 'close_krw', 'currency',
         'high_krw', 'interval', 'low_krw', 'market', 'observed_at',
         'open_krw', 'provider', 'provider_contract_sha256',
         'provider_event_at', 'schema_version', 'symbol', 'volume'
       ]
     ) is distinct from true then
    raise exception 'pit_candle_payload_shape_invalid' using errcode = '22023';
  end if;

  if jsonb_typeof(p_candle->'schema_version') is distinct from 'number'
     or p_candle->>'schema_version' <> '1'
     or jsonb_typeof(p_candle->'provider') is distinct from 'string'
     or (p_candle->>'provider') !~ '^[a-z][a-z0-9._-]{0,63}$'
     or jsonb_typeof(p_candle->'symbol') is distinct from 'string'
     or (p_candle->>'symbol') !~ '^[0-9]{6}$'
     or jsonb_typeof(p_candle->'market') is distinct from 'string'
     or p_candle->>'market' <> 'KR'
     or jsonb_typeof(p_candle->'interval') is distinct from 'string'
     or p_candle->>'interval' <> '1d'
     or jsonb_typeof(p_candle->'adjusted') is distinct from 'boolean'
     or jsonb_typeof(p_candle->'provider_event_at') is distinct from 'string'
     or jsonb_typeof(p_candle->'observed_at') is distinct from 'string'
     or jsonb_typeof(p_candle->'currency') is distinct from 'string'
     or p_candle->>'currency' <> 'KRW'
     or jsonb_typeof(p_candle->'provider_contract_sha256')
       is distinct from 'string'
     or (p_candle->>'provider_contract_sha256') !~ '^[0-9a-f]{64}$'
     or jsonb_typeof(p_candle->'canonical_observation_sha256')
       is distinct from 'string'
     or (p_candle->>'canonical_observation_sha256') !~ '^[0-9a-f]{64}$' then
    raise exception 'pit_candle_payload_value_invalid' using errcode = '22023';
  end if;

  if jsonb_typeof(p_candle->'open_krw') is distinct from 'number'
     or (p_candle->>'open_krw') !~ '^[1-9][0-9]*$'
     or jsonb_typeof(p_candle->'high_krw') is distinct from 'number'
     or (p_candle->>'high_krw') !~ '^[1-9][0-9]*$'
     or jsonb_typeof(p_candle->'low_krw') is distinct from 'number'
     or (p_candle->>'low_krw') !~ '^[1-9][0-9]*$'
     or jsonb_typeof(p_candle->'close_krw') is distinct from 'number'
     or (p_candle->>'close_krw') !~ '^[1-9][0-9]*$'
     or jsonb_typeof(p_candle->'volume') is distinct from 'number'
     or (p_candle->>'volume') !~ '^(0|[1-9][0-9]*)$' then
    raise exception 'pit_candle_numeric_value_invalid' using errcode = '22023';
  end if;

  begin
    provider_event_at_value := (p_candle->>'provider_event_at')::timestamptz;
    observed_at_value := (p_candle->>'observed_at')::timestamptz;
    open_value := (p_candle->>'open_krw')::numeric;
    high_value := (p_candle->>'high_krw')::numeric;
    low_value := (p_candle->>'low_krw')::numeric;
    close_value := (p_candle->>'close_krw')::numeric;
    volume_value := (p_candle->>'volume')::numeric;
  exception
    when others then
      raise exception 'pit_candle_typed_value_invalid' using errcode = '22023';
  end;

  if private.pit_canonical_timestamp_v1(provider_event_at_value)
       <> p_candle->>'provider_event_at'
     or private.pit_canonical_timestamp_v1(observed_at_value)
       <> p_candle->>'observed_at'
     or observed_at_value < provider_event_at_value
     or high_value < greatest(open_value, close_value)
     or low_value > least(open_value, close_value) then
    raise exception 'pit_candle_domain_value_invalid' using errcode = '22023';
  end if;

  provider_value := p_candle->>'provider';
  symbol_value := p_candle->>'symbol';
  market_value := p_candle->>'market';
  interval_value := p_candle->>'interval';
  adjusted_value := (p_candle->>'adjusted')::boolean;
  currency_value := p_candle->>'currency';
  contract_sha256_value := p_candle->>'provider_contract_sha256';
  candidate_sha256_value := p_candle->>'canonical_observation_sha256';

  expected_idempotency_key := private.pit_candle_identity_sha256_v1(
    provider_value,
    symbol_value,
    market_value,
    interval_value,
    adjusted_value,
    provider_event_at_value
  );
  expected_candidate_sha256 := private.pit_candle_observation_sha256_v1(
    provider_value,
    symbol_value,
    market_value,
    interval_value,
    adjusted_value,
    provider_event_at_value,
    currency_value,
    open_value,
    high_value,
    low_value,
    close_value,
    volume_value,
    contract_sha256_value
  );

  if candidate_sha256_value <> expected_candidate_sha256 then
    raise exception 'pit_candle_canonical_observation_sha256_mismatch'
      using errcode = '22023';
  end if;

  perform pg_catalog.pg_advisory_xact_lock(
    pg_catalog.hashtextextended(expected_idempotency_key, 749132011::bigint)
  );

  select stream.* into head
  from private.pit_candle_stream_heads as stream
  where stream.idempotency_key = expected_idempotency_key
  for update;

  if not found then
    insert into private.pit_candle_stream_heads (
      idempotency_key,
      provider,
      symbol,
      market,
      interval,
      adjusted,
      provider_event_at,
      latest_revision,
      latest_canonical_observation_sha256,
      latest_observed_at,
      last_seen_observed_at
    ) values (
      expected_idempotency_key,
      provider_value,
      symbol_value,
      market_value,
      interval_value,
      adjusted_value,
      provider_event_at_value,
      1,
      candidate_sha256_value,
      observed_at_value,
      observed_at_value
    );

    insert into private.pit_candle_observation_revisions (
      idempotency_key,
      revision,
      canonical_observation_sha256,
      observed_at,
      candle_payload
    ) values (
      expected_idempotency_key,
      1,
      candidate_sha256_value,
      observed_at_value,
      p_candle
    )
    returning * into content_revision_row;

    select result.occurrence_id, result.occurrence_inserted
    into occurrence_id_value, occurrence_inserted_value
    from private.put_pit_candle_observation_occurrence_v1(
      expected_idempotency_key,
      candidate_sha256_value,
      content_revision_row.id,
      observed_at_value,
      p_candle
    ) as result;

    if occurrence_id_value is null
       or occurrence_inserted_value is distinct from true then
      raise exception 'pit_candle_occurrence_initial_insert_failed'
        using errcode = '55000';
    end if;

    return query select
      'stored'::text,
      expected_idempotency_key,
      candidate_sha256_value,
      1::bigint,
      true,
      observed_at_value,
      null::uuid,
      null::text;
    return;
  end if;

  if head.provider <> provider_value
     or head.symbol <> symbol_value
     or head.market <> market_value
     or head.interval <> interval_value
     or head.adjusted <> adjusted_value
     or head.provider_event_at <> provider_event_at_value then
    raise exception 'pit_candle_identity_hash_collision' using errcode = '22023';
  end if;

  if observed_at_value < head.last_seen_observed_at then
    quarantine_reason_value :=
      'candle_observation_store_observation_time_regressed';
  elsif candidate_sha256_value = head.latest_canonical_observation_sha256 then
    select revision_row.* into strict content_revision_row
    from private.pit_candle_observation_revisions as revision_row
    where revision_row.idempotency_key = expected_idempotency_key
      and revision_row.revision = head.latest_revision;

    select result.occurrence_id, result.occurrence_inserted
    into occurrence_id_value, occurrence_inserted_value
    from private.put_pit_candle_observation_occurrence_v1(
      expected_idempotency_key,
      candidate_sha256_value,
      content_revision_row.id,
      observed_at_value,
      p_candle
    ) as result;

    if occurrence_id_value is null then
      raise exception 'pit_candle_occurrence_replay_failed'
        using errcode = '55000';
    end if;

    if occurrence_inserted_value
       and observed_at_value > head.last_seen_observed_at then
      update private.pit_candle_stream_heads as stream
      set last_seen_observed_at = observed_at_value,
          updated_at = clock_timestamp()
      where stream.idempotency_key = expected_idempotency_key;
    end if;

    return query select
      'replayed'::text,
      expected_idempotency_key,
      candidate_sha256_value,
      head.latest_revision,
      false,
      head.latest_observed_at,
      null::uuid,
      null::text;
    return;
  elsif exists (
    select 1
    from private.pit_candle_observation_revisions as historical
    where historical.idempotency_key = expected_idempotency_key
      and historical.canonical_observation_sha256 = candidate_sha256_value
  ) then
    quarantine_reason_value :=
      'candle_observation_store_historical_hash_recurrence_ambiguous';
  elsif observed_at_value <= head.last_seen_observed_at then
    quarantine_reason_value :=
      'candle_observation_store_revision_time_not_increasing';
  end if;

  if quarantine_reason_value is not null then
    quarantine_id_value := private.quarantine_pit_candle_observation_v1(
      expected_idempotency_key,
      quarantine_reason_value,
      candidate_sha256_value,
      observed_at_value,
      p_candle,
      head.latest_revision,
      head.latest_canonical_observation_sha256,
      head.last_seen_observed_at
    );

    return query select
      'quarantined'::text,
      expected_idempotency_key,
      candidate_sha256_value,
      head.latest_revision,
      false,
      head.latest_observed_at,
      quarantine_id_value,
      quarantine_reason_value;
    return;
  end if;

  next_revision := head.latest_revision + 1;
  insert into private.pit_candle_observation_revisions (
    idempotency_key,
    revision,
    canonical_observation_sha256,
    observed_at,
    candle_payload
  ) values (
    expected_idempotency_key,
    next_revision,
    candidate_sha256_value,
    observed_at_value,
    p_candle
  )
  returning * into content_revision_row;

  select result.occurrence_id, result.occurrence_inserted
  into occurrence_id_value, occurrence_inserted_value
  from private.put_pit_candle_observation_occurrence_v1(
    expected_idempotency_key,
    candidate_sha256_value,
    content_revision_row.id,
    observed_at_value,
    p_candle
  ) as result;

  if occurrence_id_value is null
     or occurrence_inserted_value is distinct from true then
    raise exception 'pit_candle_occurrence_revision_insert_failed'
      using errcode = '55000';
  end if;

  update private.pit_candle_stream_heads as stream
  set latest_revision = next_revision,
      latest_canonical_observation_sha256 = candidate_sha256_value,
      latest_observed_at = observed_at_value,
      last_seen_observed_at = observed_at_value,
      updated_at = clock_timestamp()
  where stream.idempotency_key = expected_idempotency_key;

  return query select
    'stored'::text,
    expected_idempotency_key,
    candidate_sha256_value,
    next_revision,
    true,
    observed_at_value,
    null::uuid,
    null::text;
end;
$$;

create or replace function private.append_pit_daily_candle_timing_evidence_v1_impl(
  p_request_idempotency_key text,
  p_calendar jsonb,
  p_timing jsonb
)
returns table (
  status text,
  request_idempotency_key text,
  timing_idempotency_key text,
  canonical_timing_evidence_sha256 text,
  calendar_revision bigint,
  timing_revision bigint,
  calendar_inserted boolean,
  timing_inserted boolean,
  evidence_available_at timestamptz,
  quarantine_id uuid,
  reason_code text
)
language plpgsql
volatile
security definer
set search_path = ''
as $$
declare
  request_sha_value text;
  original_request_sha_value text;
  original_receipt_id_value uuid;
  receipt private.pit_daily_candle_timing_request_receipts%rowtype;
  calendar_provider_value text;
  calendar_market_value text;
  calendar_session_date_value date;
  calendar_is_open_value boolean;
  calendar_regular_start_value timestamptz;
  calendar_regular_end_value timestamptz;
  calendar_next_date_value date;
  calendar_next_start_value timestamptz;
  calendar_next_end_value timestamptz;
  calendar_observed_at_value timestamptz;
  calendar_contract_sha_value text;
  calendar_candidate_sha_value text;
  calendar_key_value text;
  expected_calendar_sha_value text;
  timing_candle_key_value text;
  timing_calendar_key_value text;
  timing_key_value text;
  timing_candidate_sha_value text;
  timing_candle_observed_at_value timestamptz;
  timing_calendar_observed_at_value timestamptz;
  timing_available_at_value timestamptz;
  expected_available_at_value timestamptz;
  expected_timing_sha_value text;
  candle_revision private.pit_candle_observation_revisions%rowtype;
  candle_occurrence private.pit_candle_observation_occurrences%rowtype;
  calendar_head private.pit_calendar_stream_heads%rowtype;
  calendar_revision_row private.pit_calendar_content_revisions%rowtype;
  calendar_occurrence private.pit_calendar_observation_occurrences%rowtype;
  timing_head private.pit_daily_candle_timing_heads%rowtype;
  latest_timing_revision private.pit_daily_candle_timing_revisions%rowtype;
  latest_candle_occurrence private.pit_candle_observation_occurrences%rowtype;
  latest_calendar_occurrence private.pit_calendar_observation_occurrences%rowtype;
  next_revision_value bigint;
  calendar_inserted_value boolean := false;
  calendar_occurrence_inserted_value boolean := false;
  timing_inserted_value boolean := false;
  calendar_occurrence_id_value uuid;
  quarantine_id_value uuid;
  quarantine_reason_value text;
begin
  perform private.require_service_role();

  if p_request_idempotency_key is null
     or p_request_idempotency_key !~ '^[0-9a-f]{64}$' then
    raise exception 'pit_timing_request_idempotency_key_invalid'
      using errcode = '22023';
  end if;

  if p_calendar is null
     or p_timing is null
     or pg_catalog.octet_length(p_calendar::text) > 8192
     or pg_catalog.octet_length(p_timing::text) > 12288
     or private.jsonb_exact_keys_v1(
       p_calendar,
       array[
         'canonical_evidence_sha256', 'is_open', 'market',
         'next_business_date', 'next_regular_end_at',
         'next_regular_start_at', 'observed_at', 'provider',
         'provider_contract_sha256', 'regular_end_at', 'regular_start_at',
         'schema_version', 'session_date'
       ]
     ) is distinct from true
     or private.jsonb_exact_keys_v1(
       p_timing,
       array[
         'adjusted', 'calendar_canonical_evidence_sha256',
         'calendar_idempotency_key', 'calendar_observed_at',
         'calendar_provider_contract_sha256',
         'candle_canonical_observation_sha256',
         'candle_idempotency_key', 'candle_observed_at',
         'candle_provider_contract_sha256', 'candle_provider_event_at',
         'canonical_timing_evidence_sha256', 'check_kind', 'cutoff_at',
         'evidence_available_at', 'interval', 'market',
         'next_business_date', 'provider', 'regular_end_at',
         'regular_start_at', 'schema_version', 'session_date', 'symbol'
       ]
     ) is distinct from true then
    raise exception 'pit_daily_candle_timing_payload_shape_invalid'
      using errcode = '22023';
  end if;

  if jsonb_typeof(p_calendar->'schema_version') is distinct from 'number'
     or p_calendar->>'schema_version' <> '1'
     or jsonb_typeof(p_calendar->'provider') is distinct from 'string'
     or (p_calendar->>'provider') !~ '^[a-z][a-z0-9._-]{0,63}$'
     or jsonb_typeof(p_calendar->'market') is distinct from 'string'
     or p_calendar->>'market' <> 'KR'
     or jsonb_typeof(p_calendar->'session_date') is distinct from 'string'
     or (p_calendar->>'session_date') !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
     or jsonb_typeof(p_calendar->'is_open') is distinct from 'boolean'
     or jsonb_typeof(p_calendar->'next_business_date') is distinct from 'string'
     or (p_calendar->>'next_business_date') !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
     or jsonb_typeof(p_calendar->'next_regular_start_at') is distinct from 'string'
     or jsonb_typeof(p_calendar->'next_regular_end_at') is distinct from 'string'
     or jsonb_typeof(p_calendar->'observed_at') is distinct from 'string'
     or jsonb_typeof(p_calendar->'provider_contract_sha256') is distinct from 'string'
     or (p_calendar->>'provider_contract_sha256') !~ '^[0-9a-f]{64}$'
     or jsonb_typeof(p_calendar->'canonical_evidence_sha256') is distinct from 'string'
     or (p_calendar->>'canonical_evidence_sha256') !~ '^[0-9a-f]{64}$'
     or jsonb_typeof(p_calendar->'regular_start_at') not in ('string', 'null')
     or jsonb_typeof(p_calendar->'regular_end_at') not in ('string', 'null') then
    raise exception 'pit_calendar_payload_value_invalid' using errcode = '22023';
  end if;

  if jsonb_typeof(p_timing->'schema_version') is distinct from 'number'
     or p_timing->>'schema_version' <> '1'
     or jsonb_typeof(p_timing->'check_kind') is distinct from 'string'
     or p_timing->>'check_kind'
       <> 'kr_daily_candle_observed_at_not_before_next_regular_start'
     or jsonb_typeof(p_timing->'provider') is distinct from 'string'
     or (p_timing->>'provider') !~ '^[a-z][a-z0-9._-]{0,63}$'
     or jsonb_typeof(p_timing->'market') is distinct from 'string'
     or p_timing->>'market' <> 'KR'
     or jsonb_typeof(p_timing->'symbol') is distinct from 'string'
     or (p_timing->>'symbol') !~ '^[0-9]{6}$'
     or jsonb_typeof(p_timing->'interval') is distinct from 'string'
     or p_timing->>'interval' <> '1d'
     or jsonb_typeof(p_timing->'adjusted') is distinct from 'boolean'
     or jsonb_typeof(p_timing->'session_date') is distinct from 'string'
     or (p_timing->>'session_date') !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
     or jsonb_typeof(p_timing->'next_business_date') is distinct from 'string'
     or (p_timing->>'next_business_date') !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
     or jsonb_typeof(p_timing->'candle_provider_event_at') is distinct from 'string'
     or jsonb_typeof(p_timing->'regular_start_at') is distinct from 'string'
     or jsonb_typeof(p_timing->'regular_end_at') is distinct from 'string'
     or jsonb_typeof(p_timing->'cutoff_at') is distinct from 'string'
     or jsonb_typeof(p_timing->'candle_observed_at') is distinct from 'string'
     or jsonb_typeof(p_timing->'calendar_observed_at') is distinct from 'string'
     or jsonb_typeof(p_timing->'evidence_available_at') is distinct from 'string'
     or jsonb_typeof(p_timing->'candle_idempotency_key') is distinct from 'string'
     or (p_timing->>'candle_idempotency_key') !~ '^[0-9a-f]{64}$'
     or jsonb_typeof(p_timing->'calendar_idempotency_key') is distinct from 'string'
     or (p_timing->>'calendar_idempotency_key') !~ '^[0-9a-f]{64}$'
     or jsonb_typeof(p_timing->'candle_provider_contract_sha256') is distinct from 'string'
     or (p_timing->>'candle_provider_contract_sha256') !~ '^[0-9a-f]{64}$'
     or jsonb_typeof(p_timing->'calendar_provider_contract_sha256') is distinct from 'string'
     or (p_timing->>'calendar_provider_contract_sha256') !~ '^[0-9a-f]{64}$'
     or jsonb_typeof(p_timing->'candle_canonical_observation_sha256') is distinct from 'string'
     or (p_timing->>'candle_canonical_observation_sha256') !~ '^[0-9a-f]{64}$'
     or jsonb_typeof(p_timing->'calendar_canonical_evidence_sha256') is distinct from 'string'
     or (p_timing->>'calendar_canonical_evidence_sha256') !~ '^[0-9a-f]{64}$'
     or jsonb_typeof(p_timing->'canonical_timing_evidence_sha256') is distinct from 'string'
     or (p_timing->>'canonical_timing_evidence_sha256') !~ '^[0-9a-f]{64}$' then
    raise exception 'pit_daily_candle_timing_payload_value_invalid'
      using errcode = '22023';
  end if;

  begin
    calendar_session_date_value := (p_calendar->>'session_date')::date;
    calendar_is_open_value := (p_calendar->>'is_open')::boolean;
    calendar_regular_start_value := case
      when jsonb_typeof(p_calendar->'regular_start_at') = 'null' then null
      else (p_calendar->>'regular_start_at')::timestamptz
    end;
    calendar_regular_end_value := case
      when jsonb_typeof(p_calendar->'regular_end_at') = 'null' then null
      else (p_calendar->>'regular_end_at')::timestamptz
    end;
    calendar_next_date_value := (p_calendar->>'next_business_date')::date;
    calendar_next_start_value := (p_calendar->>'next_regular_start_at')::timestamptz;
    calendar_next_end_value := (p_calendar->>'next_regular_end_at')::timestamptz;
    calendar_observed_at_value := (p_calendar->>'observed_at')::timestamptz;
    timing_candle_observed_at_value := (p_timing->>'candle_observed_at')::timestamptz;
    timing_calendar_observed_at_value := (p_timing->>'calendar_observed_at')::timestamptz;
    timing_available_at_value := (p_timing->>'evidence_available_at')::timestamptz;
  exception
    when others then
      raise exception 'pit_daily_candle_timing_typed_value_invalid'
        using errcode = '22023';
  end;

  if calendar_session_date_value::text <> p_calendar->>'session_date'
     or calendar_next_date_value::text <> p_calendar->>'next_business_date'
     or private.pit_canonical_timestamp_v1(calendar_next_start_value)
       <> p_calendar->>'next_regular_start_at'
     or private.pit_canonical_timestamp_v1(calendar_next_end_value)
       <> p_calendar->>'next_regular_end_at'
     or private.pit_canonical_timestamp_v1(calendar_observed_at_value)
       <> p_calendar->>'observed_at'
     or (
       calendar_regular_start_value is not null
       and private.pit_canonical_timestamp_v1(calendar_regular_start_value)
         <> p_calendar->>'regular_start_at'
     )
     or (
       calendar_regular_end_value is not null
       and private.pit_canonical_timestamp_v1(calendar_regular_end_value)
         <> p_calendar->>'regular_end_at'
     )
     or private.pit_canonical_timestamp_v1(timing_candle_observed_at_value)
       <> p_timing->>'candle_observed_at'
     or private.pit_canonical_timestamp_v1(timing_calendar_observed_at_value)
       <> p_timing->>'calendar_observed_at'
     or private.pit_canonical_timestamp_v1(timing_available_at_value)
       <> p_timing->>'evidence_available_at' then
    raise exception 'pit_daily_candle_timing_canonical_value_invalid'
      using errcode = '22023';
  end if;

  calendar_provider_value := p_calendar->>'provider';
  calendar_market_value := p_calendar->>'market';
  calendar_contract_sha_value := p_calendar->>'provider_contract_sha256';
  calendar_candidate_sha_value := p_calendar->>'canonical_evidence_sha256';
  timing_candle_key_value := p_timing->>'candle_idempotency_key';
  timing_calendar_key_value := p_timing->>'calendar_idempotency_key';
  timing_candidate_sha_value := p_timing->>'canonical_timing_evidence_sha256';
  calendar_key_value := private.pit_calendar_identity_sha256_v1(
    calendar_provider_value,
    calendar_market_value,
    calendar_session_date_value
  );
  timing_key_value := private.pit_daily_candle_timing_identity_sha256_v1(
    timing_candle_key_value,
    timing_calendar_key_value
  );
  expected_calendar_sha_value :=
    private.pit_calendar_canonical_evidence_sha256_v1(
      calendar_provider_value,
      calendar_market_value,
      calendar_session_date_value,
      calendar_is_open_value,
      calendar_regular_start_value,
      calendar_regular_end_value,
      calendar_next_date_value,
      calendar_next_start_value,
      calendar_next_end_value,
      calendar_contract_sha_value
    );
  request_sha_value := private.pit_daily_candle_timing_request_sha256_v1(
    p_calendar,
    p_timing
  );

  perform pg_catalog.pg_advisory_xact_lock(
    pg_catalog.hashtextextended(p_request_idempotency_key, 813004911::bigint)
  );

  select ledger.original_request_sha256, ledger.original_receipt_id
  into original_request_sha_value, original_receipt_id_value
  from private.pit_daily_candle_timing_request_ledger as ledger
  where ledger.request_idempotency_key = p_request_idempotency_key;

  if found then
    if original_request_sha_value = request_sha_value then
      select stored.* into strict receipt
      from private.pit_daily_candle_timing_request_receipts as stored
      where stored.id = original_receipt_id_value;
    else
      select stored.* into receipt
      from private.pit_daily_candle_timing_request_receipts as stored
      where stored.request_idempotency_key = p_request_idempotency_key
        and stored.request_sha256 = request_sha_value;

      if not found then
        quarantine_id_value := private.quarantine_pit_daily_candle_timing_v1(
          p_request_idempotency_key,
          request_sha_value,
          timing_key_value,
          'pit_timing_request_idempotency_conflict',
          timing_candidate_sha_value,
          p_timing,
          null,
          null,
          null
        );
        receipt := private.put_pit_daily_candle_timing_receipt_v1(
          p_request_idempotency_key,
          request_sha_value,
          'quarantined',
          timing_key_value,
          timing_candidate_sha_value,
          null,
          null,
          false,
          false,
          timing_available_at_value,
          quarantine_id_value,
          'pit_timing_request_idempotency_conflict',
          false
        );
      end if;
    end if;

    return query select
      receipt.status,
      receipt.request_idempotency_key,
      receipt.timing_idempotency_key,
      receipt.canonical_timing_evidence_sha256,
      receipt.calendar_revision,
      receipt.timing_revision,
      receipt.calendar_inserted,
      receipt.timing_inserted,
      receipt.evidence_available_at,
      receipt.quarantine_id,
      receipt.reason_code;
    return;
  end if;

  if expected_calendar_sha_value <> calendar_candidate_sha_value
     or timing_calendar_key_value <> calendar_key_value
     or p_timing->>'calendar_canonical_evidence_sha256'
       <> calendar_candidate_sha_value
     or p_timing->>'calendar_provider_contract_sha256'
       <> calendar_contract_sha_value
     or p_timing->>'calendar_observed_at'
       <> p_calendar->>'observed_at'
     or p_timing->>'provider' <> calendar_provider_value
     or p_timing->>'market' <> calendar_market_value
     or p_timing->>'session_date' <> p_calendar->>'session_date'
     or p_timing->>'regular_start_at' is distinct from
       p_calendar->>'regular_start_at'
     or p_timing->>'regular_end_at' is distinct from
       p_calendar->>'regular_end_at'
     or p_timing->>'next_business_date' <> p_calendar->>'next_business_date'
     or p_timing->>'cutoff_at' <> p_calendar->>'next_regular_start_at'
     or not calendar_is_open_value
     or calendar_regular_start_value is null
     or calendar_regular_end_value is null then
    quarantine_reason_value := 'pit_timing_source_binding_mismatch';
  end if;

  select occurrence.* into candle_occurrence
  from private.pit_candle_observation_occurrences as occurrence
  where occurrence.idempotency_key = timing_candle_key_value
    and occurrence.canonical_observation_sha256 =
      p_timing->>'candle_canonical_observation_sha256'
    and occurrence.observed_at = timing_candle_observed_at_value;

  if not found then
    quarantine_reason_value := coalesce(
      quarantine_reason_value,
      'pit_timing_candle_revision_missing'
    );
  else
    select source.* into strict candle_revision
    from private.pit_candle_observation_revisions as source
    where source.id = candle_occurrence.content_revision_id;

    if quarantine_reason_value is null and (
      candle_occurrence.observation_payload->>'provider'
        <> p_timing->>'provider'
      or candle_occurrence.observation_payload->>'market'
        <> p_timing->>'market'
      or candle_occurrence.observation_payload->>'symbol'
        <> p_timing->>'symbol'
      or candle_occurrence.observation_payload->>'interval'
        <> p_timing->>'interval'
      or candle_occurrence.observation_payload->>'adjusted'
        <> p_timing->>'adjusted'
      or candle_occurrence.observation_payload->>'provider_event_at'
        <> p_timing->>'candle_provider_event_at'
      or candle_occurrence.observation_payload->>'provider_contract_sha256'
        <> p_timing->>'candle_provider_contract_sha256'
    ) then
      quarantine_reason_value := 'pit_timing_source_binding_mismatch';
    end if;
  end if;

  if quarantine_reason_value is null and (
    calendar_next_date_value <= calendar_session_date_value
    or calendar_regular_start_value >= calendar_regular_end_value
    or calendar_regular_end_value >= calendar_next_start_value
    or calendar_next_start_value >= calendar_next_end_value
    or (calendar_regular_start_value at time zone 'Asia/Seoul')::date
      <> calendar_session_date_value
    or (calendar_regular_end_value at time zone 'Asia/Seoul')::date
      <> calendar_session_date_value
    or (calendar_next_start_value at time zone 'Asia/Seoul')::date
      <> calendar_next_date_value
    or (calendar_next_end_value at time zone 'Asia/Seoul')::date
      <> calendar_next_date_value
    or (
      (candle_occurrence.observation_payload->>'provider_event_at')::timestamptz
      at time zone 'Asia/Seoul'
    )::date <> calendar_session_date_value
    or timing_candle_observed_at_value < calendar_next_start_value
    or timing_calendar_observed_at_value < calendar_next_start_value
  ) then
    quarantine_reason_value := 'pit_timing_source_binding_mismatch';
  end if;

  if quarantine_reason_value is null then
    expected_available_at_value := greatest(
      candle_occurrence.observed_at,
      calendar_observed_at_value
    );

    if timing_available_at_value <> expected_available_at_value then
      quarantine_reason_value := 'pit_timing_available_at_mismatch';
    else
      expected_timing_sha_value :=
        private.pit_daily_candle_timing_evidence_sha256_v1(
          candle_occurrence.observation_payload->>'provider',
          candle_occurrence.observation_payload->>'market',
          candle_occurrence.observation_payload->>'symbol',
          candle_occurrence.observation_payload->>'interval',
          (candle_occurrence.observation_payload->>'adjusted')::boolean,
          calendar_session_date_value,
          (candle_occurrence.observation_payload->>'provider_event_at')::timestamptz,
          calendar_regular_start_value,
          calendar_regular_end_value,
          calendar_next_date_value,
          calendar_next_start_value,
          candle_occurrence.observed_at,
          calendar_observed_at_value,
          expected_available_at_value,
          candle_revision.idempotency_key,
          calendar_key_value,
          candle_occurrence.observation_payload->>'provider_contract_sha256',
          calendar_contract_sha_value,
          candle_revision.canonical_observation_sha256,
          calendar_candidate_sha_value
        );

      if expected_timing_sha_value <> timing_candidate_sha_value then
        quarantine_reason_value := 'pit_timing_canonical_sha256_mismatch';
      end if;
    end if;
  end if;

  if quarantine_reason_value is not null then
    quarantine_id_value := private.quarantine_pit_daily_candle_timing_v1(
      p_request_idempotency_key,
      request_sha_value,
      timing_key_value,
      quarantine_reason_value,
      timing_candidate_sha_value,
      p_timing,
      null,
      null,
      null
    );
    receipt := private.put_pit_daily_candle_timing_receipt_v1(
      p_request_idempotency_key,
      request_sha_value,
      'quarantined',
      timing_key_value,
      timing_candidate_sha_value,
      null,
      null,
      false,
      false,
      timing_available_at_value,
      quarantine_id_value,
      quarantine_reason_value,
      true
    );
    return query select
      receipt.status,
      receipt.request_idempotency_key,
      receipt.timing_idempotency_key,
      receipt.canonical_timing_evidence_sha256,
      receipt.calendar_revision,
      receipt.timing_revision,
      receipt.calendar_inserted,
      receipt.timing_inserted,
      receipt.evidence_available_at,
      receipt.quarantine_id,
      receipt.reason_code;
    return;
  end if;

  perform pg_catalog.pg_advisory_xact_lock(
    pg_catalog.hashtextextended(calendar_key_value, 813004912::bigint)
  );

  select stream.* into calendar_head
  from private.pit_calendar_stream_heads as stream
  where stream.calendar_idempotency_key = calendar_key_value
  for update;

  if not found then
    insert into private.pit_calendar_stream_heads (
      calendar_idempotency_key,
      provider,
      market,
      session_date,
      latest_revision,
      latest_canonical_evidence_sha256,
      latest_observed_at,
      last_seen_observed_at
    ) values (
      calendar_key_value,
      calendar_provider_value,
      calendar_market_value,
      calendar_session_date_value,
      1,
      calendar_candidate_sha_value,
      calendar_observed_at_value,
      calendar_observed_at_value
    );

    insert into private.pit_calendar_content_revisions (
      calendar_idempotency_key,
      revision,
      canonical_evidence_sha256,
      observed_at,
      calendar_payload
    ) values (
      calendar_key_value,
      1,
      calendar_candidate_sha_value,
      calendar_observed_at_value,
      p_calendar
    )
    returning * into calendar_revision_row;

    select result.occurrence_id, result.occurrence_inserted
    into calendar_occurrence_id_value, calendar_occurrence_inserted_value
    from private.put_pit_calendar_observation_occurrence_v1(
      calendar_key_value,
      calendar_candidate_sha_value,
      calendar_revision_row.id,
      calendar_observed_at_value,
      p_calendar
    ) as result;

    if calendar_occurrence_id_value is null
       or calendar_occurrence_inserted_value is distinct from true then
      raise exception 'pit_calendar_occurrence_initial_insert_failed'
        using errcode = '55000';
    end if;
    calendar_inserted_value := true;
  else
    if calendar_head.provider <> calendar_provider_value
       or calendar_head.market <> calendar_market_value
       or calendar_head.session_date <> calendar_session_date_value then
      raise exception 'pit_calendar_identity_hash_collision'
        using errcode = '22023';
    end if;

    quarantine_reason_value := null;
    if calendar_observed_at_value < calendar_head.last_seen_observed_at then
      quarantine_reason_value := 'pit_calendar_observation_time_regressed';
    elsif calendar_candidate_sha_value =
      calendar_head.latest_canonical_evidence_sha256 then
      select revision_row.* into strict calendar_revision_row
      from private.pit_calendar_content_revisions as revision_row
      where revision_row.calendar_idempotency_key = calendar_key_value
        and revision_row.revision = calendar_head.latest_revision;

      select result.occurrence_id, result.occurrence_inserted
      into calendar_occurrence_id_value, calendar_occurrence_inserted_value
      from private.put_pit_calendar_observation_occurrence_v1(
        calendar_key_value,
        calendar_candidate_sha_value,
        calendar_revision_row.id,
        calendar_observed_at_value,
        p_calendar
      ) as result;

      if calendar_occurrence_id_value is null then
        raise exception 'pit_calendar_occurrence_replay_failed'
          using errcode = '55000';
      end if;

      if calendar_occurrence_inserted_value
         and calendar_observed_at_value >
           calendar_head.last_seen_observed_at then
        update private.pit_calendar_stream_heads as stream
        set last_seen_observed_at = calendar_observed_at_value,
            updated_at = clock_timestamp()
        where stream.calendar_idempotency_key = calendar_key_value;
      end if;
    elsif exists (
      select 1
      from private.pit_calendar_content_revisions as historical
      where historical.calendar_idempotency_key = calendar_key_value
        and historical.canonical_evidence_sha256 = calendar_candidate_sha_value
    ) then
      quarantine_reason_value :=
        'pit_calendar_historical_hash_recurrence_ambiguous';
    elsif calendar_observed_at_value <= calendar_head.last_seen_observed_at then
      quarantine_reason_value := 'pit_calendar_revision_time_not_increasing';
    else
      next_revision_value := calendar_head.latest_revision + 1;
      insert into private.pit_calendar_content_revisions (
        calendar_idempotency_key,
        revision,
        canonical_evidence_sha256,
        observed_at,
        calendar_payload
      ) values (
        calendar_key_value,
        next_revision_value,
        calendar_candidate_sha_value,
        calendar_observed_at_value,
        p_calendar
      )
      returning * into calendar_revision_row;

      select result.occurrence_id, result.occurrence_inserted
      into calendar_occurrence_id_value, calendar_occurrence_inserted_value
      from private.put_pit_calendar_observation_occurrence_v1(
        calendar_key_value,
        calendar_candidate_sha_value,
        calendar_revision_row.id,
        calendar_observed_at_value,
        p_calendar
      ) as result;

      if calendar_occurrence_id_value is null
         or calendar_occurrence_inserted_value is distinct from true then
        raise exception 'pit_calendar_occurrence_revision_insert_failed'
          using errcode = '55000';
      end if;

      update private.pit_calendar_stream_heads as stream
      set latest_revision = next_revision_value,
          latest_canonical_evidence_sha256 = calendar_candidate_sha_value,
          latest_observed_at = calendar_observed_at_value,
          last_seen_observed_at = calendar_observed_at_value,
          updated_at = clock_timestamp()
      where stream.calendar_idempotency_key = calendar_key_value;
      calendar_inserted_value := true;
    end if;

    if quarantine_reason_value is not null then
      quarantine_id_value := private.quarantine_pit_calendar_observation_v1(
        p_request_idempotency_key,
        request_sha_value,
        calendar_key_value,
        quarantine_reason_value,
        calendar_candidate_sha_value,
        calendar_observed_at_value,
        p_calendar,
        calendar_head.latest_revision,
        calendar_head.latest_canonical_evidence_sha256,
        calendar_head.last_seen_observed_at
      );
      receipt := private.put_pit_daily_candle_timing_receipt_v1(
        p_request_idempotency_key,
        request_sha_value,
        'quarantined',
        timing_key_value,
        timing_candidate_sha_value,
        calendar_head.latest_revision,
        null,
        false,
        false,
        timing_available_at_value,
        quarantine_id_value,
        quarantine_reason_value,
        true
      );
      return query select
        receipt.status,
        receipt.request_idempotency_key,
        receipt.timing_idempotency_key,
        receipt.canonical_timing_evidence_sha256,
        receipt.calendar_revision,
        receipt.timing_revision,
        receipt.calendar_inserted,
        receipt.timing_inserted,
        receipt.evidence_available_at,
        receipt.quarantine_id,
        receipt.reason_code;
      return;
    end if;
  end if;

  select occurrence.* into calendar_occurrence
  from private.pit_calendar_observation_occurrences as occurrence
  where occurrence.calendar_idempotency_key = calendar_key_value
    and occurrence.canonical_evidence_sha256 = calendar_candidate_sha_value
    and occurrence.observed_at = calendar_observed_at_value;

  if not found then
    quarantine_reason_value := 'pit_timing_calendar_revision_missing';
  else
    select source.* into strict calendar_revision_row
    from private.pit_calendar_content_revisions as source
    where source.id = calendar_occurrence.content_revision_id;

    expected_available_at_value := greatest(
      candle_occurrence.observed_at,
      calendar_occurrence.observed_at
    );

    if p_timing->>'candle_observed_at'
         <> private.pit_canonical_timestamp_v1(candle_occurrence.observed_at)
       or p_timing->>'calendar_observed_at'
         <> private.pit_canonical_timestamp_v1(calendar_occurrence.observed_at)
       or p_timing->>'candle_canonical_observation_sha256'
         <> candle_occurrence.canonical_observation_sha256
       or p_timing->>'calendar_canonical_evidence_sha256'
         <> calendar_occurrence.canonical_evidence_sha256 then
      quarantine_reason_value := 'pit_timing_source_binding_mismatch';
    elsif timing_available_at_value <> expected_available_at_value then
      quarantine_reason_value := 'pit_timing_available_at_mismatch';
    else
      expected_timing_sha_value :=
        private.pit_daily_candle_timing_evidence_sha256_v1(
          candle_occurrence.observation_payload->>'provider',
          candle_occurrence.observation_payload->>'market',
          candle_occurrence.observation_payload->>'symbol',
          candle_occurrence.observation_payload->>'interval',
          (candle_occurrence.observation_payload->>'adjusted')::boolean,
          calendar_session_date_value,
          (
            candle_occurrence.observation_payload->>'provider_event_at'
          )::timestamptz,
          calendar_regular_start_value,
          calendar_regular_end_value,
          calendar_next_date_value,
          calendar_next_start_value,
          candle_occurrence.observed_at,
          calendar_occurrence.observed_at,
          expected_available_at_value,
          candle_occurrence.idempotency_key,
          calendar_occurrence.calendar_idempotency_key,
          candle_occurrence.observation_payload->>'provider_contract_sha256',
          calendar_contract_sha_value,
          candle_occurrence.canonical_observation_sha256,
          calendar_occurrence.canonical_evidence_sha256
        );

      if expected_timing_sha_value <> timing_candidate_sha_value then
        quarantine_reason_value := 'pit_timing_canonical_sha256_mismatch';
      end if;
    end if;
  end if;

  if quarantine_reason_value is not null then
    quarantine_id_value := private.quarantine_pit_daily_candle_timing_v1(
      p_request_idempotency_key,
      request_sha_value,
      timing_key_value,
      quarantine_reason_value,
      timing_candidate_sha_value,
      p_timing,
      null,
      null,
      null
    );
    receipt := private.put_pit_daily_candle_timing_receipt_v1(
      p_request_idempotency_key,
      request_sha_value,
      'quarantined',
      timing_key_value,
      timing_candidate_sha_value,
      case
        when calendar_revision_row.id is null then null
        else calendar_revision_row.revision
      end,
      null,
      calendar_inserted_value,
      false,
      timing_available_at_value,
      quarantine_id_value,
      quarantine_reason_value,
      true
    );
    return query select
      receipt.status,
      receipt.request_idempotency_key,
      receipt.timing_idempotency_key,
      receipt.canonical_timing_evidence_sha256,
      receipt.calendar_revision,
      receipt.timing_revision,
      receipt.calendar_inserted,
      receipt.timing_inserted,
      receipt.evidence_available_at,
      receipt.quarantine_id,
      receipt.reason_code;
    return;
  end if;

  perform pg_catalog.pg_advisory_xact_lock(
    pg_catalog.hashtextextended(timing_key_value, 813004913::bigint)
  );

  select stream.* into timing_head
  from private.pit_daily_candle_timing_heads as stream
  where stream.timing_idempotency_key = timing_key_value
  for update;

  if not found then
    insert into private.pit_daily_candle_timing_heads (
      timing_idempotency_key,
      candle_idempotency_key,
      calendar_idempotency_key,
      latest_revision,
      latest_canonical_timing_evidence_sha256,
      latest_evidence_available_at,
      last_seen_evidence_available_at
    ) values (
      timing_key_value,
      timing_candle_key_value,
      calendar_key_value,
      1,
      timing_candidate_sha_value,
      expected_available_at_value,
      expected_available_at_value
    );

    insert into private.pit_daily_candle_timing_revisions (
      timing_idempotency_key,
      revision,
      canonical_timing_evidence_sha256,
      candle_revision_id,
      calendar_revision_id,
      candle_occurrence_id,
      calendar_occurrence_id,
      evidence_available_at,
      timing_payload
    ) values (
      timing_key_value,
      1,
      timing_candidate_sha_value,
      candle_revision.id,
      calendar_revision_row.id,
      candle_occurrence.id,
      calendar_occurrence.id,
      expected_available_at_value,
      p_timing
    );
    next_revision_value := 1;
    timing_inserted_value := true;
  else
    if timing_head.candle_idempotency_key <> timing_candle_key_value
       or timing_head.calendar_idempotency_key <> calendar_key_value then
      raise exception 'pit_daily_candle_timing_identity_hash_collision'
        using errcode = '22023';
    end if;

    select revision_row.* into strict latest_timing_revision
    from private.pit_daily_candle_timing_revisions as revision_row
    where revision_row.timing_idempotency_key = timing_key_value
      and revision_row.revision = timing_head.latest_revision;

    select occurrence.* into strict latest_candle_occurrence
    from private.pit_candle_observation_occurrences as occurrence
    where occurrence.id = latest_timing_revision.candle_occurrence_id;

    select occurrence.* into strict latest_calendar_occurrence
    from private.pit_calendar_observation_occurrences as occurrence
    where occurrence.id = latest_timing_revision.calendar_occurrence_id;

    quarantine_reason_value := null;
    if timing_candidate_sha_value =
      timing_head.latest_canonical_timing_evidence_sha256 then
      if latest_timing_revision.candle_occurrence_id <> candle_occurrence.id
         or latest_timing_revision.calendar_occurrence_id <>
           calendar_occurrence.id then
        raise exception 'pit_timing_occurrence_hash_collision'
          using errcode = '55000';
      end if;
      next_revision_value := timing_head.latest_revision;
    elsif exists (
      select 1
      from private.pit_daily_candle_timing_revisions as historical
      where historical.timing_idempotency_key = timing_key_value
        and historical.canonical_timing_evidence_sha256 =
          timing_candidate_sha_value
    ) then
      quarantine_reason_value :=
        'pit_timing_historical_hash_recurrence_ambiguous';
    elsif candle_occurrence.observed_at <
        latest_candle_occurrence.observed_at
       or calendar_occurrence.observed_at <
        latest_calendar_occurrence.observed_at then
      quarantine_reason_value := 'pit_timing_observation_time_regressed';
    elsif expected_available_at_value <
      timing_head.last_seen_evidence_available_at then
      raise exception 'pit_timing_occurrence_clock_invariant_failed'
        using errcode = '55000';
    else
      next_revision_value := timing_head.latest_revision + 1;
      insert into private.pit_daily_candle_timing_revisions (
        timing_idempotency_key,
        revision,
        canonical_timing_evidence_sha256,
        candle_revision_id,
        calendar_revision_id,
        candle_occurrence_id,
        calendar_occurrence_id,
        evidence_available_at,
        timing_payload
      ) values (
        timing_key_value,
        next_revision_value,
        timing_candidate_sha_value,
        candle_revision.id,
        calendar_revision_row.id,
        candle_occurrence.id,
        calendar_occurrence.id,
        expected_available_at_value,
        p_timing
      );

      update private.pit_daily_candle_timing_heads as stream
      set latest_revision = next_revision_value,
          latest_canonical_timing_evidence_sha256 = timing_candidate_sha_value,
          latest_evidence_available_at = expected_available_at_value,
          last_seen_evidence_available_at = expected_available_at_value,
          updated_at = clock_timestamp()
      where stream.timing_idempotency_key = timing_key_value;
      timing_inserted_value := true;
    end if;

    if quarantine_reason_value is not null then
      quarantine_id_value := private.quarantine_pit_daily_candle_timing_v1(
        p_request_idempotency_key,
        request_sha_value,
        timing_key_value,
        quarantine_reason_value,
        timing_candidate_sha_value,
        p_timing,
        timing_head.latest_revision,
        timing_head.latest_canonical_timing_evidence_sha256,
        timing_head.last_seen_evidence_available_at
      );
      receipt := private.put_pit_daily_candle_timing_receipt_v1(
        p_request_idempotency_key,
        request_sha_value,
        'quarantined',
        timing_key_value,
        timing_candidate_sha_value,
        calendar_revision_row.revision,
        timing_head.latest_revision,
        calendar_inserted_value,
        false,
        expected_available_at_value,
        quarantine_id_value,
        quarantine_reason_value,
        true
      );
      return query select
        receipt.status,
        receipt.request_idempotency_key,
        receipt.timing_idempotency_key,
        receipt.canonical_timing_evidence_sha256,
        receipt.calendar_revision,
        receipt.timing_revision,
        receipt.calendar_inserted,
        receipt.timing_inserted,
        receipt.evidence_available_at,
        receipt.quarantine_id,
        receipt.reason_code;
      return;
    end if;
  end if;

  receipt := private.put_pit_daily_candle_timing_receipt_v1(
    p_request_idempotency_key,
    request_sha_value,
    case when timing_inserted_value then 'stored' else 'replayed' end,
    timing_key_value,
    timing_candidate_sha_value,
    calendar_revision_row.revision,
    next_revision_value,
    calendar_inserted_value,
    timing_inserted_value,
    expected_available_at_value,
    null,
    null,
    true
  );

  return query select
    receipt.status,
    receipt.request_idempotency_key,
    receipt.timing_idempotency_key,
    receipt.canonical_timing_evidence_sha256,
    receipt.calendar_revision,
    receipt.timing_revision,
    receipt.calendar_inserted,
    receipt.timing_inserted,
    receipt.evidence_available_at,
    receipt.quarantine_id,
    receipt.reason_code;
end;
$$;

revoke all on function
  private.put_pit_candle_observation_occurrence_v1(
    text,text,uuid,timestamptz,jsonb
  ),
  private.put_pit_calendar_observation_occurrence_v1(
    text,text,uuid,timestamptz,jsonb
  ),
  private.append_pit_candle_observation_v1_impl(jsonb),
  private.append_pit_daily_candle_timing_evidence_v1_impl(text,jsonb,jsonb),
  worker_api.append_pit_candle_observation_v1(jsonb),
  worker_api.append_pit_daily_candle_timing_evidence_v1(text,jsonb,jsonb)
from public, anon, authenticated, service_role;

grant execute on function
  private.append_pit_candle_observation_v1_impl(jsonb),
  private.append_pit_daily_candle_timing_evidence_v1_impl(text,jsonb,jsonb),
  worker_api.append_pit_candle_observation_v1(jsonb),
  worker_api.append_pit_daily_candle_timing_evidence_v1(text,jsonb,jsonb)
to service_role;

do $$
declare
  occurrence_rls_count bigint;
  occurrence_trigger_count bigint;
  occurrence_policy_count bigint;
  private_function_count bigint;
  wrapper_function_count bigint;
  timing_occurrence_not_null_count bigint;
  timing_occurrence_pair_count bigint;
  forbidden_table_acl_count bigint;
begin
  select count(*) into occurrence_rls_count
  from pg_catalog.pg_class as relation
  join pg_catalog.pg_namespace as namespace
    on namespace.oid = relation.relnamespace
  where namespace.nspname = 'private'
    and relation.relname in (
      'pit_candle_observation_occurrences',
      'pit_calendar_observation_occurrences'
    )
    and relation.relrowsecurity;

  select count(*) into occurrence_trigger_count
  from pg_catalog.pg_trigger as trigger_row
  where trigger_row.tgrelid in (
      'private.pit_candle_observation_occurrences'::regclass,
      'private.pit_calendar_observation_occurrences'::regclass,
      'private.pit_daily_candle_timing_revisions'::regclass
    )
    and trigger_row.tgname in (
      'reject_pit_candle_occurrence_mutation',
      'reject_pit_calendar_occurrence_mutation',
      'reject_pit_daily_candle_timing_revision_mutation'
    )
    and not trigger_row.tgisinternal;

  select count(*) into occurrence_policy_count
  from pg_catalog.pg_policy as policy
  where policy.polrelid in (
    'private.pit_candle_observation_occurrences'::regclass,
    'private.pit_calendar_observation_occurrences'::regclass
  );

  select count(*) into private_function_count
  from pg_catalog.pg_proc as procedure
  where procedure.oid in (
      'private.put_pit_candle_observation_occurrence_v1(text,text,uuid,timestamptz,jsonb)'::regprocedure,
      'private.put_pit_calendar_observation_occurrence_v1(text,text,uuid,timestamptz,jsonb)'::regprocedure,
      'private.append_pit_candle_observation_v1_impl(jsonb)'::regprocedure,
      'private.append_pit_daily_candle_timing_evidence_v1_impl(text,jsonb,jsonb)'::regprocedure
    )
    and procedure.prosecdef
    and procedure.proconfig = array['search_path=""']::text[];

  select count(*) into wrapper_function_count
  from pg_catalog.pg_proc as procedure
  where procedure.oid in (
      'worker_api.append_pit_candle_observation_v1(jsonb)'::regprocedure,
      'worker_api.append_pit_daily_candle_timing_evidence_v1(text,jsonb,jsonb)'::regprocedure
    )
    and not procedure.prosecdef
    and procedure.proconfig = array['search_path=""']::text[];

  select count(*) into timing_occurrence_not_null_count
  from pg_catalog.pg_attribute as attribute
  where attribute.attrelid =
      'private.pit_daily_candle_timing_revisions'::regclass
    and attribute.attname in (
      'candle_occurrence_id',
      'calendar_occurrence_id'
    )
    and attribute.attnotnull
    and not attribute.attisdropped;

  select count(*) into timing_occurrence_pair_count
  from pg_catalog.pg_constraint as constraint_row
  where constraint_row.conrelid =
      'private.pit_daily_candle_timing_revisions'::regclass
    and constraint_row.conname = 'pit_timing_occurrence_pair_key'
    and constraint_row.contype = 'u';

  select count(*) into forbidden_table_acl_count
  from pg_catalog.pg_class as relation
  join pg_catalog.pg_namespace as namespace
    on namespace.oid = relation.relnamespace
  cross join lateral pg_catalog.aclexplode(
    coalesce(
      relation.relacl,
      pg_catalog.acldefault('r', relation.relowner)
    )
  ) as acl
  left join pg_catalog.pg_roles as grantee
    on grantee.oid = acl.grantee
  where namespace.nspname = 'private'
    and relation.relname in (
      'pit_candle_observation_occurrences',
      'pit_calendar_observation_occurrences'
    )
    and (
      acl.grantee = 0
      or grantee.rolname in (
        'anon',
        'authenticated',
        'service_role'
      )
    );

  if occurrence_rls_count <> 2
     or occurrence_trigger_count <> 3
     or occurrence_policy_count <> 0
     or private_function_count <> 4
     or wrapper_function_count <> 2
     or timing_occurrence_not_null_count <> 2
     or timing_occurrence_pair_count <> 1
     or forbidden_table_acl_count <> 0
     or not pg_catalog.has_function_privilege(
       'service_role',
       'private.append_pit_candle_observation_v1_impl(jsonb)',
       'EXECUTE'
     )
     or not pg_catalog.has_function_privilege(
       'service_role',
       'private.append_pit_daily_candle_timing_evidence_v1_impl(text,jsonb,jsonb)',
       'EXECUTE'
     )
     or not pg_catalog.has_function_privilege(
       'service_role',
       'worker_api.append_pit_candle_observation_v1(jsonb)',
       'EXECUTE'
     )
     or not pg_catalog.has_function_privilege(
       'service_role',
       'worker_api.append_pit_daily_candle_timing_evidence_v1(text,jsonb,jsonb)',
       'EXECUTE'
     )
     or pg_catalog.has_function_privilege(
       'service_role',
       'private.put_pit_candle_observation_occurrence_v1(text,text,uuid,timestamptz,jsonb)',
       'EXECUTE'
     )
     or pg_catalog.has_function_privilege(
       'service_role',
       'private.put_pit_calendar_observation_occurrence_v1(text,text,uuid,timestamptz,jsonb)',
       'EXECUTE'
     ) then
    raise exception 'pit_source_occurrence_store_catalog_invalid'
      using errcode = '55000';
  end if;
end;
$$;

commit;
