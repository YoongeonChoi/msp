begin;

set local lock_timeout = '5s';
set local statement_timeout = '90s';

do $$
begin
  if to_regprocedure('private.require_service_role()') is null
     or to_regprocedure('private.jsonb_exact_keys_v1(jsonb,text[])') is null
     or to_regprocedure('private.reject_append_only_mutation()') is null
     or to_regprocedure('extensions.digest(bytea,text)') is null then
    raise exception 'pit_candle_revision_store_dependency_missing'
      using errcode = '55000';
  end if;
end;
$$;

create table private.pit_candle_stream_heads (
  idempotency_key text primary key
    check (idempotency_key ~ '^[0-9a-f]{64}$'),
  provider text not null
    check (provider ~ '^[a-z][a-z0-9._-]{0,63}$'),
  symbol text not null check (symbol ~ '^[0-9]{6}$'),
  market text not null check (market = 'KR'),
  interval text not null check (interval = '1d'),
  adjusted boolean not null,
  provider_event_at timestamptz not null,
  latest_revision bigint not null check (latest_revision > 0),
  latest_canonical_observation_sha256 text not null
    check (latest_canonical_observation_sha256 ~ '^[0-9a-f]{64}$'),
  latest_observed_at timestamptz not null,
  last_seen_observed_at timestamptz not null,
  created_at timestamptz not null default clock_timestamp(),
  updated_at timestamptz not null default clock_timestamp(),
  unique (
    provider,
    symbol,
    market,
    interval,
    adjusted,
    provider_event_at
  ),
  check (provider_event_at <= latest_observed_at),
  check (latest_observed_at <= last_seen_observed_at),
  check (created_at <= updated_at)
);

create table private.pit_candle_observation_revisions (
  id uuid primary key default pg_catalog.gen_random_uuid(),
  idempotency_key text not null
    references private.pit_candle_stream_heads(idempotency_key),
  revision bigint not null check (revision > 0),
  canonical_observation_sha256 text not null
    check (canonical_observation_sha256 ~ '^[0-9a-f]{64}$'),
  observed_at timestamptz not null,
  candle_payload jsonb not null
    check (jsonb_typeof(candle_payload) = 'object'),
  received_at timestamptz not null default clock_timestamp(),
  unique (idempotency_key, revision),
  unique (idempotency_key, canonical_observation_sha256)
);

create index pit_candle_observation_revisions_as_of_idx
  on private.pit_candle_observation_revisions (
    idempotency_key,
    observed_at desc,
    revision desc
  );

create table private.pit_candle_observation_quarantine (
  id uuid primary key default pg_catalog.gen_random_uuid(),
  quarantine_fingerprint_sha256 text not null unique
    check (quarantine_fingerprint_sha256 ~ '^[0-9a-f]{64}$'),
  idempotency_key text not null
    references private.pit_candle_stream_heads(idempotency_key),
  reason_code text not null check (
    reason_code in (
      'candle_observation_store_observation_time_regressed',
      'candle_observation_store_historical_hash_recurrence_ambiguous',
      'candle_observation_store_revision_time_not_increasing'
    )
  ),
  candidate_canonical_observation_sha256 text not null
    check (candidate_canonical_observation_sha256 ~ '^[0-9a-f]{64}$'),
  candidate_observed_at timestamptz not null,
  candidate_payload jsonb not null
    check (jsonb_typeof(candidate_payload) = 'object'),
  latest_revision bigint not null check (latest_revision > 0),
  latest_canonical_observation_sha256 text not null
    check (latest_canonical_observation_sha256 ~ '^[0-9a-f]{64}$'),
  last_seen_observed_at timestamptz not null,
  quarantined_at timestamptz not null default clock_timestamp()
);

create index pit_candle_observation_quarantine_stream_idx
  on private.pit_candle_observation_quarantine (
    idempotency_key,
    quarantined_at desc
  );

create trigger reject_pit_candle_revision_mutation
  before update or delete on private.pit_candle_observation_revisions
  for each row execute function private.reject_append_only_mutation();

create trigger reject_pit_candle_quarantine_mutation
  before update or delete on private.pit_candle_observation_quarantine
  for each row execute function private.reject_append_only_mutation();

alter table private.pit_candle_stream_heads enable row level security;
alter table private.pit_candle_observation_revisions enable row level security;
alter table private.pit_candle_observation_quarantine enable row level security;

revoke all on table
  private.pit_candle_stream_heads,
  private.pit_candle_observation_revisions,
  private.pit_candle_observation_quarantine
from public, anon, authenticated, service_role;

create or replace function private.pit_canonical_timestamp_v1(
  p_value timestamptz
)
returns text
language sql
immutable
security definer
set search_path = ''
as $$
  select case
    when extract(microseconds from p_value at time zone 'UTC')::bigint
      % 1000000 = 0
      then to_char(
        p_value at time zone 'UTC',
        'YYYY-MM-DD"T"HH24:MI:SS'
      ) || 'Z'
    else to_char(
      p_value at time zone 'UTC',
      'YYYY-MM-DD"T"HH24:MI:SS.US'
    ) || 'Z'
  end;
$$;

create or replace function private.pit_sha256_text_v1(p_value text)
returns text
language sql
immutable
security definer
set search_path = ''
as $$
  select pg_catalog.encode(
    extensions.digest(pg_catalog.convert_to(p_value, 'UTF8'), 'sha256'),
    'hex'
  );
$$;

create or replace function private.pit_candle_identity_sha256_v1(
  p_provider text,
  p_symbol text,
  p_market text,
  p_interval text,
  p_adjusted boolean,
  p_provider_event_at timestamptz
)
returns text
language sql
immutable
security definer
set search_path = ''
as $$
  select private.pit_sha256_text_v1(
    '{"adjusted":' || p_adjusted::text ||
    ',"interval":"' || p_interval || '"' ||
    ',"market":"' || p_market || '"' ||
    ',"provider":"' || p_provider || '"' ||
    ',"provider_event_at":"' ||
      private.pit_canonical_timestamp_v1(p_provider_event_at) || '"' ||
    ',"schema_version":1' ||
    ',"symbol":"' || p_symbol || '"}'
  );
$$;

create or replace function private.pit_candle_observation_sha256_v1(
  p_provider text,
  p_symbol text,
  p_market text,
  p_interval text,
  p_adjusted boolean,
  p_provider_event_at timestamptz,
  p_currency text,
  p_open_krw numeric,
  p_high_krw numeric,
  p_low_krw numeric,
  p_close_krw numeric,
  p_volume numeric,
  p_provider_contract_sha256 text
)
returns text
language sql
immutable
security definer
set search_path = ''
as $$
  select private.pit_sha256_text_v1(
    '{"adjusted":' || p_adjusted::text ||
    ',"close_krw":' || p_close_krw::text ||
    ',"currency":"' || p_currency || '"' ||
    ',"high_krw":' || p_high_krw::text ||
    ',"interval":"' || p_interval || '"' ||
    ',"low_krw":' || p_low_krw::text ||
    ',"market":"' || p_market || '"' ||
    ',"open_krw":' || p_open_krw::text ||
    ',"provider":"' || p_provider || '"' ||
    ',"provider_contract_sha256":"' || p_provider_contract_sha256 || '"' ||
    ',"provider_event_at":"' ||
      private.pit_canonical_timestamp_v1(p_provider_event_at) || '"' ||
    ',"schema_version":1' ||
    ',"symbol":"' || p_symbol || '"' ||
    ',"volume":' || p_volume::text || '}'
  );
$$;

create or replace function private.quarantine_pit_candle_observation_v1(
  p_idempotency_key text,
  p_reason_code text,
  p_candidate_sha256 text,
  p_candidate_observed_at timestamptz,
  p_candidate_payload jsonb,
  p_latest_revision bigint,
  p_latest_sha256 text,
  p_last_seen_observed_at timestamptz
)
returns uuid
language plpgsql
volatile
security definer
set search_path = ''
as $$
declare
  fingerprint_value text;
  quarantine_id_value uuid;
begin
  fingerprint_value := private.pit_sha256_text_v1(
    p_idempotency_key || '|' || p_reason_code || '|' ||
    p_candidate_sha256 || '|' ||
    private.pit_canonical_timestamp_v1(p_candidate_observed_at) || '|' ||
    p_candidate_payload::text || '|' || p_latest_revision::text || '|' ||
    p_latest_sha256 || '|' ||
    private.pit_canonical_timestamp_v1(p_last_seen_observed_at)
  );

  insert into private.pit_candle_observation_quarantine (
    quarantine_fingerprint_sha256,
    idempotency_key,
    reason_code,
    candidate_canonical_observation_sha256,
    candidate_observed_at,
    candidate_payload,
    latest_revision,
    latest_canonical_observation_sha256,
    last_seen_observed_at
  ) values (
    fingerprint_value,
    p_idempotency_key,
    p_reason_code,
    p_candidate_sha256,
    p_candidate_observed_at,
    p_candidate_payload,
    p_latest_revision,
    p_latest_sha256,
    p_last_seen_observed_at
  )
  on conflict (quarantine_fingerprint_sha256) do nothing
  returning id into quarantine_id_value;

  if quarantine_id_value is null then
    select quarantine.id into strict quarantine_id_value
    from private.pit_candle_observation_quarantine as quarantine
    where quarantine.quarantine_fingerprint_sha256 = fingerprint_value;
  end if;

  return quarantine_id_value;
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
    );

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
    if observed_at_value > head.last_seen_observed_at then
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
  );

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

create or replace function worker_api.append_pit_candle_observation_v1(
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
language sql
volatile
security invoker
set search_path = ''
as $$
  select *
  from private.append_pit_candle_observation_v1_impl(p_candle);
$$;

revoke all on function
  private.pit_canonical_timestamp_v1(timestamptz),
  private.pit_sha256_text_v1(text),
  private.pit_candle_identity_sha256_v1(text,text,text,text,boolean,timestamptz),
  private.pit_candle_observation_sha256_v1(
    text,text,text,text,boolean,timestamptz,text,numeric,numeric,numeric,numeric,
    numeric,text
  ),
  private.quarantine_pit_candle_observation_v1(
    text,text,text,timestamptz,jsonb,bigint,text,timestamptz
  ),
  private.append_pit_candle_observation_v1_impl(jsonb),
  worker_api.append_pit_candle_observation_v1(jsonb)
from public, anon, authenticated, service_role;

grant execute on function
  private.append_pit_candle_observation_v1_impl(jsonb),
  worker_api.append_pit_candle_observation_v1(jsonb)
to service_role;

do $$
declare
  private_definer boolean;
  private_search_path text[];
  wrapper_definer boolean;
  wrapper_search_path text[];
begin
  if to_regclass('private.pit_candle_stream_heads') is null
     or to_regclass('private.pit_candle_observation_revisions') is null
     or to_regclass('private.pit_candle_observation_quarantine') is null then
    raise exception 'pit_candle_revision_store_catalog_missing'
      using errcode = '55000';
  end if;

  select p.prosecdef, p.proconfig
  into private_definer, private_search_path
  from pg_proc as p
  where p.oid = to_regprocedure(
    'private.append_pit_candle_observation_v1_impl(jsonb)'
  );

  select p.prosecdef, p.proconfig
  into wrapper_definer, wrapper_search_path
  from pg_proc as p
  where p.oid = to_regprocedure(
    'worker_api.append_pit_candle_observation_v1(jsonb)'
  );

  if private_definer is distinct from true
     or private_search_path is distinct from array['search_path=""']::text[]
     or wrapper_definer is distinct from false
     or wrapper_search_path is distinct from array['search_path=""']::text[] then
    raise exception 'pit_candle_revision_store_function_boundary_invalid'
      using errcode = '55000';
  end if;
end;
$$;

commit;
