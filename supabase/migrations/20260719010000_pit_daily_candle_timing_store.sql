begin;

set local lock_timeout = '5s';
set local statement_timeout = '90s';

-- Forward-only dependency: 20260719001947_pit_candle_revision_store.sql.
do $$
begin
  if to_regprocedure('private.require_service_role()') is null
     or to_regprocedure('private.jsonb_exact_keys_v1(jsonb,text[])') is null
     or to_regprocedure('private.reject_append_only_mutation()') is null
     or to_regprocedure('private.pit_canonical_timestamp_v1(timestamptz)') is null
     or to_regprocedure('private.pit_sha256_text_v1(text)') is null
     or to_regclass('private.pit_candle_observation_revisions') is null then
    raise exception 'pit_daily_candle_timing_store_dependency_missing'
      using errcode = '55000';
  end if;
end;
$$;

create table private.pit_calendar_stream_heads (
  calendar_idempotency_key text primary key
    check (calendar_idempotency_key ~ '^[0-9a-f]{64}$'),
  provider text not null
    check (provider ~ '^[a-z][a-z0-9._-]{0,63}$'),
  market text not null check (market = 'KR'),
  session_date date not null,
  latest_revision bigint not null check (latest_revision > 0),
  latest_canonical_evidence_sha256 text not null
    check (latest_canonical_evidence_sha256 ~ '^[0-9a-f]{64}$'),
  latest_observed_at timestamptz not null,
  last_seen_observed_at timestamptz not null,
  created_at timestamptz not null default clock_timestamp(),
  updated_at timestamptz not null default clock_timestamp(),
  unique (provider, market, session_date),
  check (latest_observed_at <= last_seen_observed_at),
  check (created_at <= updated_at)
);

create table private.pit_calendar_content_revisions (
  id uuid primary key default pg_catalog.gen_random_uuid(),
  calendar_idempotency_key text not null
    references private.pit_calendar_stream_heads(calendar_idempotency_key),
  revision bigint not null check (revision > 0),
  canonical_evidence_sha256 text not null
    check (canonical_evidence_sha256 ~ '^[0-9a-f]{64}$'),
  observed_at timestamptz not null,
  calendar_payload jsonb not null
    check (jsonb_typeof(calendar_payload) = 'object'),
  received_at timestamptz not null default clock_timestamp(),
  unique (calendar_idempotency_key, revision),
  unique (calendar_idempotency_key, canonical_evidence_sha256),
  unique (
    calendar_idempotency_key,
    canonical_evidence_sha256,
    observed_at
  )
);

create index pit_calendar_content_revisions_stream_idx
  on private.pit_calendar_content_revisions (
    calendar_idempotency_key,
    observed_at desc,
    revision desc
  );

create table private.pit_calendar_observation_quarantine (
  id uuid primary key default pg_catalog.gen_random_uuid(),
  quarantine_fingerprint_sha256 text not null unique
    check (quarantine_fingerprint_sha256 ~ '^[0-9a-f]{64}$'),
  request_idempotency_key text not null
    check (request_idempotency_key ~ '^[0-9a-f]{64}$'),
  request_sha256 text not null check (request_sha256 ~ '^[0-9a-f]{64}$'),
  calendar_idempotency_key text not null
    references private.pit_calendar_stream_heads(calendar_idempotency_key),
  reason_code text not null check (
    reason_code in (
      'pit_calendar_observation_time_regressed',
      'pit_calendar_historical_hash_recurrence_ambiguous',
      'pit_calendar_revision_time_not_increasing'
    )
  ),
  candidate_canonical_evidence_sha256 text not null
    check (candidate_canonical_evidence_sha256 ~ '^[0-9a-f]{64}$'),
  candidate_observed_at timestamptz not null,
  candidate_payload jsonb not null
    check (jsonb_typeof(candidate_payload) = 'object'),
  latest_revision bigint not null check (latest_revision > 0),
  latest_canonical_evidence_sha256 text not null
    check (latest_canonical_evidence_sha256 ~ '^[0-9a-f]{64}$'),
  last_seen_observed_at timestamptz not null,
  quarantined_at timestamptz not null default clock_timestamp()
);

create index pit_calendar_observation_quarantine_stream_idx
  on private.pit_calendar_observation_quarantine (
    calendar_idempotency_key,
    quarantined_at desc
  );

create table private.pit_daily_candle_timing_heads (
  timing_idempotency_key text primary key
    check (timing_idempotency_key ~ '^[0-9a-f]{64}$'),
  candle_idempotency_key text not null
    references private.pit_candle_stream_heads(idempotency_key),
  calendar_idempotency_key text not null
    references private.pit_calendar_stream_heads(calendar_idempotency_key),
  latest_revision bigint not null check (latest_revision > 0),
  latest_canonical_timing_evidence_sha256 text not null
    check (latest_canonical_timing_evidence_sha256 ~ '^[0-9a-f]{64}$'),
  latest_evidence_available_at timestamptz not null,
  last_seen_evidence_available_at timestamptz not null,
  created_at timestamptz not null default clock_timestamp(),
  updated_at timestamptz not null default clock_timestamp(),
  unique (candle_idempotency_key, calendar_idempotency_key),
  check (latest_evidence_available_at <= last_seen_evidence_available_at),
  check (created_at <= updated_at)
);

create index pit_daily_candle_timing_heads_calendar_idx
  on private.pit_daily_candle_timing_heads (calendar_idempotency_key);

create table private.pit_daily_candle_timing_revisions (
  id uuid primary key default pg_catalog.gen_random_uuid(),
  timing_idempotency_key text not null
    references private.pit_daily_candle_timing_heads(timing_idempotency_key),
  revision bigint not null check (revision > 0),
  canonical_timing_evidence_sha256 text not null
    check (canonical_timing_evidence_sha256 ~ '^[0-9a-f]{64}$'),
  candle_revision_id uuid not null
    references private.pit_candle_observation_revisions(id),
  calendar_revision_id uuid not null
    references private.pit_calendar_content_revisions(id),
  evidence_available_at timestamptz not null,
  timing_payload jsonb not null
    check (jsonb_typeof(timing_payload) = 'object'),
  received_at timestamptz not null default clock_timestamp(),
  unique (timing_idempotency_key, revision),
  unique (timing_idempotency_key, canonical_timing_evidence_sha256),
  unique (candle_revision_id, calendar_revision_id)
);

create index pit_daily_candle_timing_revisions_candle_idx
  on private.pit_daily_candle_timing_revisions (candle_revision_id);

create index pit_daily_candle_timing_revisions_calendar_idx
  on private.pit_daily_candle_timing_revisions (calendar_revision_id);

create index pit_daily_candle_timing_revisions_as_of_idx
  on private.pit_daily_candle_timing_revisions (
    timing_idempotency_key,
    evidence_available_at desc,
    revision desc
  );

create table private.pit_daily_candle_timing_quarantine (
  id uuid primary key default pg_catalog.gen_random_uuid(),
  quarantine_fingerprint_sha256 text not null unique
    check (quarantine_fingerprint_sha256 ~ '^[0-9a-f]{64}$'),
  request_idempotency_key text not null
    check (request_idempotency_key ~ '^[0-9a-f]{64}$'),
  request_sha256 text not null check (request_sha256 ~ '^[0-9a-f]{64}$'),
  timing_idempotency_key text
    check (
      timing_idempotency_key is null
      or timing_idempotency_key ~ '^[0-9a-f]{64}$'
    ),
  reason_code text not null check (
    reason_code in (
      'pit_timing_request_idempotency_conflict',
      'pit_timing_candle_revision_missing',
      'pit_timing_calendar_revision_missing',
      'pit_timing_source_binding_mismatch',
      'pit_timing_available_at_mismatch',
      'pit_timing_canonical_sha256_mismatch',
      'pit_timing_observation_time_regressed',
      'pit_timing_historical_hash_recurrence_ambiguous',
      'pit_timing_revision_time_not_increasing'
    )
  ),
  candidate_canonical_timing_evidence_sha256 text
    check (
      candidate_canonical_timing_evidence_sha256 is null
      or candidate_canonical_timing_evidence_sha256 ~ '^[0-9a-f]{64}$'
    ),
  candidate_payload jsonb not null
    check (jsonb_typeof(candidate_payload) = 'object'),
  latest_revision bigint check (latest_revision is null or latest_revision > 0),
  latest_canonical_timing_evidence_sha256 text
    check (
      latest_canonical_timing_evidence_sha256 is null
      or latest_canonical_timing_evidence_sha256 ~ '^[0-9a-f]{64}$'
    ),
  last_seen_evidence_available_at timestamptz,
  quarantined_at timestamptz not null default clock_timestamp()
);

create index pit_daily_candle_timing_quarantine_stream_idx
  on private.pit_daily_candle_timing_quarantine (
    timing_idempotency_key,
    quarantined_at desc
  );

create table private.pit_daily_candle_timing_request_receipts (
  id uuid primary key default pg_catalog.gen_random_uuid(),
  request_idempotency_key text not null
    check (request_idempotency_key ~ '^[0-9a-f]{64}$'),
  request_sha256 text not null check (request_sha256 ~ '^[0-9a-f]{64}$'),
  status text not null check (status in ('stored', 'replayed', 'quarantined')),
  timing_idempotency_key text
    check (
      timing_idempotency_key is null
      or timing_idempotency_key ~ '^[0-9a-f]{64}$'
    ),
  canonical_timing_evidence_sha256 text
    check (
      canonical_timing_evidence_sha256 is null
      or canonical_timing_evidence_sha256 ~ '^[0-9a-f]{64}$'
    ),
  calendar_revision bigint
    check (calendar_revision is null or calendar_revision > 0),
  timing_revision bigint check (timing_revision is null or timing_revision > 0),
  calendar_inserted boolean not null,
  timing_inserted boolean not null,
  evidence_available_at timestamptz,
  quarantine_id uuid,
  reason_code text,
  created_at timestamptz not null default clock_timestamp(),
  unique (request_idempotency_key, request_sha256),
  check (
    (
      status in ('stored', 'replayed')
      and timing_idempotency_key is not null
      and canonical_timing_evidence_sha256 is not null
      and calendar_revision is not null
      and timing_revision is not null
      and evidence_available_at is not null
      and quarantine_id is null
      and reason_code is null
    )
    or (
      status = 'quarantined'
      and not timing_inserted
      and quarantine_id is not null
      and reason_code is not null
    )
  )
);

create table private.pit_daily_candle_timing_request_ledger (
  request_idempotency_key text primary key
    check (request_idempotency_key ~ '^[0-9a-f]{64}$'),
  original_request_sha256 text not null
    check (original_request_sha256 ~ '^[0-9a-f]{64}$'),
  original_receipt_id uuid not null unique
    references private.pit_daily_candle_timing_request_receipts(id),
  created_at timestamptz not null default clock_timestamp()
);

create trigger reject_pit_calendar_revision_mutation
  before update or delete on private.pit_calendar_content_revisions
  for each row execute function private.reject_append_only_mutation();

create trigger reject_pit_calendar_quarantine_mutation
  before update or delete on private.pit_calendar_observation_quarantine
  for each row execute function private.reject_append_only_mutation();

create trigger reject_pit_daily_candle_timing_revision_mutation
  before update or delete on private.pit_daily_candle_timing_revisions
  for each row execute function private.reject_append_only_mutation();

create trigger reject_pit_daily_candle_timing_quarantine_mutation
  before update or delete on private.pit_daily_candle_timing_quarantine
  for each row execute function private.reject_append_only_mutation();

create trigger reject_pit_daily_candle_timing_request_receipt_mutation
  before update or delete on private.pit_daily_candle_timing_request_receipts
  for each row execute function private.reject_append_only_mutation();

create trigger reject_pit_daily_candle_timing_request_ledger_mutation
  before update or delete on private.pit_daily_candle_timing_request_ledger
  for each row execute function private.reject_append_only_mutation();

alter table private.pit_calendar_stream_heads enable row level security;
alter table private.pit_calendar_content_revisions enable row level security;
alter table private.pit_calendar_observation_quarantine enable row level security;
alter table private.pit_daily_candle_timing_heads enable row level security;
alter table private.pit_daily_candle_timing_revisions enable row level security;
alter table private.pit_daily_candle_timing_quarantine enable row level security;
alter table private.pit_daily_candle_timing_request_receipts enable row level security;
alter table private.pit_daily_candle_timing_request_ledger enable row level security;

revoke all on table
  private.pit_calendar_stream_heads,
  private.pit_calendar_content_revisions,
  private.pit_calendar_observation_quarantine,
  private.pit_daily_candle_timing_heads,
  private.pit_daily_candle_timing_revisions,
  private.pit_daily_candle_timing_quarantine,
  private.pit_daily_candle_timing_request_receipts,
  private.pit_daily_candle_timing_request_ledger
from public, anon, authenticated, service_role;

create or replace function private.pit_calendar_identity_sha256_v1(
  p_provider text,
  p_market text,
  p_session_date date
)
returns text
language sql
immutable
security definer
set search_path = ''
as $$
  select private.pit_sha256_text_v1(
    '{"market":"' || p_market || '"' ||
    ',"provider":"' || p_provider || '"' ||
    ',"schema_version":1' ||
    ',"session_date":"' || p_session_date::text || '"}'
  );
$$;

create or replace function private.pit_calendar_canonical_evidence_sha256_v1(
  p_provider text,
  p_market text,
  p_session_date date,
  p_is_open boolean,
  p_regular_start_at timestamptz,
  p_regular_end_at timestamptz,
  p_next_business_date date,
  p_next_regular_start_at timestamptz,
  p_next_regular_end_at timestamptz,
  p_provider_contract_sha256 text
)
returns text
language sql
immutable
security definer
set search_path = ''
as $$
  select private.pit_sha256_text_v1(
    '{"is_open":' || p_is_open::text ||
    ',"market":"' || p_market || '"' ||
    ',"next_business_date":"' || p_next_business_date::text || '"' ||
    ',"next_regular_end_at":"' ||
      private.pit_canonical_timestamp_v1(p_next_regular_end_at) || '"' ||
    ',"next_regular_start_at":"' ||
      private.pit_canonical_timestamp_v1(p_next_regular_start_at) || '"' ||
    ',"provider":"' || p_provider || '"' ||
    ',"provider_contract_sha256":"' || p_provider_contract_sha256 || '"' ||
    ',"regular_end_at":' || case
      when p_regular_end_at is null then 'null'
      else '"' || private.pit_canonical_timestamp_v1(p_regular_end_at) || '"'
    end ||
    ',"regular_start_at":' || case
      when p_regular_start_at is null then 'null'
      else '"' || private.pit_canonical_timestamp_v1(p_regular_start_at) || '"'
    end ||
    ',"schema_version":1' ||
    ',"session_date":"' || p_session_date::text || '"}'
  );
$$;

create or replace function private.pit_daily_candle_timing_identity_sha256_v1(
  p_candle_idempotency_key text,
  p_calendar_idempotency_key text
)
returns text
language sql
immutable
security definer
set search_path = ''
as $$
  select private.pit_sha256_text_v1(
    '{"calendar_idempotency_key":"' || p_calendar_idempotency_key || '"' ||
    ',"candle_idempotency_key":"' || p_candle_idempotency_key || '"' ||
    ',"check_kind":"kr_daily_candle_observed_at_not_before_next_regular_start"' ||
    ',"schema_version":1}'
  );
$$;

create or replace function private.pit_daily_candle_timing_evidence_sha256_v1(
  p_provider text,
  p_market text,
  p_symbol text,
  p_interval text,
  p_adjusted boolean,
  p_session_date date,
  p_candle_provider_event_at timestamptz,
  p_regular_start_at timestamptz,
  p_regular_end_at timestamptz,
  p_next_business_date date,
  p_cutoff_at timestamptz,
  p_candle_observed_at timestamptz,
  p_calendar_observed_at timestamptz,
  p_evidence_available_at timestamptz,
  p_candle_idempotency_key text,
  p_calendar_idempotency_key text,
  p_candle_provider_contract_sha256 text,
  p_calendar_provider_contract_sha256 text,
  p_candle_canonical_observation_sha256 text,
  p_calendar_canonical_evidence_sha256 text
)
returns text
language sql
immutable
security definer
set search_path = ''
as $$
  select private.pit_sha256_text_v1(
    '{"adjusted":' || p_adjusted::text ||
    ',"calendar_canonical_evidence_sha256":"' ||
      p_calendar_canonical_evidence_sha256 || '"' ||
    ',"calendar_idempotency_key":"' || p_calendar_idempotency_key || '"' ||
    ',"calendar_observed_at":"' ||
      private.pit_canonical_timestamp_v1(p_calendar_observed_at) || '"' ||
    ',"calendar_provider_contract_sha256":"' ||
      p_calendar_provider_contract_sha256 || '"' ||
    ',"candle_canonical_observation_sha256":"' ||
      p_candle_canonical_observation_sha256 || '"' ||
    ',"candle_idempotency_key":"' || p_candle_idempotency_key || '"' ||
    ',"candle_observed_at":"' ||
      private.pit_canonical_timestamp_v1(p_candle_observed_at) || '"' ||
    ',"candle_provider_contract_sha256":"' ||
      p_candle_provider_contract_sha256 || '"' ||
    ',"candle_provider_event_at":"' ||
      private.pit_canonical_timestamp_v1(p_candle_provider_event_at) || '"' ||
    ',"check_kind":"kr_daily_candle_observed_at_not_before_next_regular_start"' ||
    ',"cutoff_at":"' || private.pit_canonical_timestamp_v1(p_cutoff_at) || '"' ||
    ',"evidence_available_at":"' ||
      private.pit_canonical_timestamp_v1(p_evidence_available_at) || '"' ||
    ',"interval":"' || p_interval || '"' ||
    ',"market":"' || p_market || '"' ||
    ',"next_business_date":"' || p_next_business_date::text || '"' ||
    ',"provider":"' || p_provider || '"' ||
    ',"regular_end_at":"' ||
      private.pit_canonical_timestamp_v1(p_regular_end_at) || '"' ||
    ',"regular_start_at":"' ||
      private.pit_canonical_timestamp_v1(p_regular_start_at) || '"' ||
    ',"schema_version":1' ||
    ',"session_date":"' || p_session_date::text || '"' ||
    ',"symbol":"' || p_symbol || '"}'
  );
$$;

create or replace function private.pit_daily_candle_timing_request_sha256_v1(
  p_calendar jsonb,
  p_timing jsonb
)
returns text
language sql
immutable
security definer
set search_path = ''
as $$
  select private.pit_sha256_text_v1(
    p_calendar::text || '|' || p_timing::text
  );
$$;

create or replace function private.quarantine_pit_calendar_observation_v1(
  p_request_idempotency_key text,
  p_request_sha256 text,
  p_calendar_idempotency_key text,
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
    p_request_idempotency_key || '|' || p_request_sha256 || '|' ||
    p_calendar_idempotency_key || '|' || p_reason_code || '|' ||
    p_candidate_sha256 || '|' ||
    private.pit_canonical_timestamp_v1(p_candidate_observed_at) || '|' ||
    p_candidate_payload::text || '|' || p_latest_revision::text || '|' ||
    p_latest_sha256 || '|' ||
    private.pit_canonical_timestamp_v1(p_last_seen_observed_at)
  );

  insert into private.pit_calendar_observation_quarantine (
    quarantine_fingerprint_sha256,
    request_idempotency_key,
    request_sha256,
    calendar_idempotency_key,
    reason_code,
    candidate_canonical_evidence_sha256,
    candidate_observed_at,
    candidate_payload,
    latest_revision,
    latest_canonical_evidence_sha256,
    last_seen_observed_at
  ) values (
    fingerprint_value,
    p_request_idempotency_key,
    p_request_sha256,
    p_calendar_idempotency_key,
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
    from private.pit_calendar_observation_quarantine as quarantine
    where quarantine.quarantine_fingerprint_sha256 = fingerprint_value;
  end if;

  return quarantine_id_value;
end;
$$;

create or replace function private.quarantine_pit_daily_candle_timing_v1(
  p_request_idempotency_key text,
  p_request_sha256 text,
  p_timing_idempotency_key text,
  p_reason_code text,
  p_candidate_sha256 text,
  p_candidate_payload jsonb,
  p_latest_revision bigint,
  p_latest_sha256 text,
  p_last_seen_evidence_available_at timestamptz
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
    p_request_idempotency_key || '|' || p_request_sha256 || '|' ||
    coalesce(p_timing_idempotency_key, '') || '|' || p_reason_code || '|' ||
    coalesce(p_candidate_sha256, '') || '|' || p_candidate_payload::text || '|' ||
    coalesce(p_latest_revision::text, '') || '|' ||
    coalesce(p_latest_sha256, '') || '|' ||
    coalesce(
      private.pit_canonical_timestamp_v1(
        p_last_seen_evidence_available_at
      ),
      ''
    )
  );

  insert into private.pit_daily_candle_timing_quarantine (
    quarantine_fingerprint_sha256,
    request_idempotency_key,
    request_sha256,
    timing_idempotency_key,
    reason_code,
    candidate_canonical_timing_evidence_sha256,
    candidate_payload,
    latest_revision,
    latest_canonical_timing_evidence_sha256,
    last_seen_evidence_available_at
  ) values (
    fingerprint_value,
    p_request_idempotency_key,
    p_request_sha256,
    p_timing_idempotency_key,
    p_reason_code,
    p_candidate_sha256,
    p_candidate_payload,
    p_latest_revision,
    p_latest_sha256,
    p_last_seen_evidence_available_at
  )
  on conflict (quarantine_fingerprint_sha256) do nothing
  returning id into quarantine_id_value;

  if quarantine_id_value is null then
    select quarantine.id into strict quarantine_id_value
    from private.pit_daily_candle_timing_quarantine as quarantine
    where quarantine.quarantine_fingerprint_sha256 = fingerprint_value;
  end if;

  return quarantine_id_value;
end;
$$;

create or replace function private.put_pit_daily_candle_timing_receipt_v1(
  p_request_idempotency_key text,
  p_request_sha256 text,
  p_status text,
  p_timing_idempotency_key text,
  p_canonical_timing_evidence_sha256 text,
  p_calendar_revision bigint,
  p_timing_revision bigint,
  p_calendar_inserted boolean,
  p_timing_inserted boolean,
  p_evidence_available_at timestamptz,
  p_quarantine_id uuid,
  p_reason_code text,
  p_register_original boolean
)
returns private.pit_daily_candle_timing_request_receipts
language plpgsql
volatile
security definer
set search_path = ''
as $$
declare
  receipt private.pit_daily_candle_timing_request_receipts%rowtype;
begin
  insert into private.pit_daily_candle_timing_request_receipts (
    request_idempotency_key,
    request_sha256,
    status,
    timing_idempotency_key,
    canonical_timing_evidence_sha256,
    calendar_revision,
    timing_revision,
    calendar_inserted,
    timing_inserted,
    evidence_available_at,
    quarantine_id,
    reason_code
  ) values (
    p_request_idempotency_key,
    p_request_sha256,
    p_status,
    p_timing_idempotency_key,
    p_canonical_timing_evidence_sha256,
    p_calendar_revision,
    p_timing_revision,
    p_calendar_inserted,
    p_timing_inserted,
    p_evidence_available_at,
    p_quarantine_id,
    p_reason_code
  )
  on conflict (request_idempotency_key, request_sha256) do nothing
  returning * into receipt;

  if receipt.id is null then
    select stored.* into strict receipt
    from private.pit_daily_candle_timing_request_receipts as stored
    where stored.request_idempotency_key = p_request_idempotency_key
      and stored.request_sha256 = p_request_sha256;
  end if;

  if p_register_original then
    insert into private.pit_daily_candle_timing_request_ledger (
      request_idempotency_key,
      original_request_sha256,
      original_receipt_id
    ) values (
      p_request_idempotency_key,
      p_request_sha256,
      receipt.id
    );
  end if;

  return receipt;
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
  calendar_head private.pit_calendar_stream_heads%rowtype;
  calendar_revision_row private.pit_calendar_content_revisions%rowtype;
  timing_head private.pit_daily_candle_timing_heads%rowtype;
  next_revision_value bigint;
  calendar_inserted_value boolean := false;
  timing_inserted_value boolean := false;
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

  select source.* into candle_revision
  from private.pit_candle_observation_revisions as source
  where source.idempotency_key = timing_candle_key_value
    and source.canonical_observation_sha256 =
      p_timing->>'candle_canonical_observation_sha256'
    and source.observed_at = timing_candle_observed_at_value;

  if not found then
    quarantine_reason_value := coalesce(
      quarantine_reason_value,
      'pit_timing_candle_revision_missing'
    );
  elsif quarantine_reason_value is null and (
    candle_revision.candle_payload->>'provider' <> p_timing->>'provider'
    or candle_revision.candle_payload->>'market' <> p_timing->>'market'
    or candle_revision.candle_payload->>'symbol' <> p_timing->>'symbol'
    or candle_revision.candle_payload->>'interval' <> p_timing->>'interval'
    or candle_revision.candle_payload->>'adjusted' <> p_timing->>'adjusted'
    or candle_revision.candle_payload->>'provider_event_at'
      <> p_timing->>'candle_provider_event_at'
    or candle_revision.candle_payload->>'provider_contract_sha256'
      <> p_timing->>'candle_provider_contract_sha256'
  ) then
    quarantine_reason_value := 'pit_timing_source_binding_mismatch';
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
      (candle_revision.candle_payload->>'provider_event_at')::timestamptz
      at time zone 'Asia/Seoul'
    )::date <> calendar_session_date_value
    or timing_candle_observed_at_value < calendar_next_start_value
    or timing_calendar_observed_at_value < calendar_next_start_value
  ) then
    quarantine_reason_value := 'pit_timing_source_binding_mismatch';
  end if;

  if quarantine_reason_value is null then
    expected_available_at_value := greatest(
      candle_revision.observed_at,
      calendar_observed_at_value
    );

    if timing_available_at_value <> expected_available_at_value then
      quarantine_reason_value := 'pit_timing_available_at_mismatch';
    else
      expected_timing_sha_value :=
        private.pit_daily_candle_timing_evidence_sha256_v1(
          candle_revision.candle_payload->>'provider',
          candle_revision.candle_payload->>'market',
          candle_revision.candle_payload->>'symbol',
          candle_revision.candle_payload->>'interval',
          (candle_revision.candle_payload->>'adjusted')::boolean,
          calendar_session_date_value,
          (candle_revision.candle_payload->>'provider_event_at')::timestamptz,
          calendar_regular_start_value,
          calendar_regular_end_value,
          calendar_next_date_value,
          calendar_next_start_value,
          candle_revision.observed_at,
          calendar_observed_at_value,
          expected_available_at_value,
          candle_revision.idempotency_key,
          calendar_key_value,
          candle_revision.candle_payload->>'provider_contract_sha256',
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
      -- A later re-observation has no immutable occurrence row to bind. Keep
      -- the head unchanged so the later exact lookup fails without poisoning
      -- the monotonic stream clock.
      select revision_row.* into strict calendar_revision_row
      from private.pit_calendar_content_revisions as revision_row
      where revision_row.calendar_idempotency_key = calendar_key_value
        and revision_row.revision = calendar_head.latest_revision;
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

  select source.* into calendar_revision_row
  from private.pit_calendar_content_revisions as source
  where source.calendar_idempotency_key = calendar_key_value
    and source.canonical_evidence_sha256 = calendar_candidate_sha_value
    and source.observed_at = calendar_observed_at_value;

  if not found then
    quarantine_reason_value := 'pit_timing_calendar_revision_missing';
  else
    expected_available_at_value := greatest(
      candle_revision.observed_at,
      calendar_revision_row.observed_at
    );

    if p_timing->>'candle_observed_at'
         <> private.pit_canonical_timestamp_v1(candle_revision.observed_at)
       or p_timing->>'calendar_observed_at'
         <> private.pit_canonical_timestamp_v1(calendar_revision_row.observed_at)
       or p_timing->>'candle_canonical_observation_sha256'
         <> candle_revision.canonical_observation_sha256
       or p_timing->>'calendar_canonical_evidence_sha256'
         <> calendar_revision_row.canonical_evidence_sha256 then
      quarantine_reason_value := 'pit_timing_source_binding_mismatch';
    elsif timing_available_at_value <> expected_available_at_value then
      quarantine_reason_value := 'pit_timing_available_at_mismatch';
    else
      expected_timing_sha_value :=
        private.pit_daily_candle_timing_evidence_sha256_v1(
          candle_revision.candle_payload->>'provider',
          candle_revision.candle_payload->>'market',
          candle_revision.candle_payload->>'symbol',
          candle_revision.candle_payload->>'interval',
          (candle_revision.candle_payload->>'adjusted')::boolean,
          calendar_session_date_value,
          (candle_revision.candle_payload->>'provider_event_at')::timestamptz,
          calendar_regular_start_value,
          calendar_regular_end_value,
          calendar_next_date_value,
          calendar_next_start_value,
          candle_revision.observed_at,
          calendar_revision_row.observed_at,
          expected_available_at_value,
          candle_revision.idempotency_key,
          calendar_revision_row.calendar_idempotency_key,
          candle_revision.candle_payload->>'provider_contract_sha256',
          calendar_contract_sha_value,
          candle_revision.canonical_observation_sha256,
          calendar_revision_row.canonical_evidence_sha256
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
      evidence_available_at,
      timing_payload
    ) values (
      timing_key_value,
      1,
      timing_candidate_sha_value,
      candle_revision.id,
      calendar_revision_row.id,
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

    quarantine_reason_value := null;
    if timing_candidate_sha_value =
      timing_head.latest_canonical_timing_evidence_sha256 then
      if expected_available_at_value >
        timing_head.last_seen_evidence_available_at then
        update private.pit_daily_candle_timing_heads as stream
        set last_seen_evidence_available_at = expected_available_at_value,
            updated_at = clock_timestamp()
        where stream.timing_idempotency_key = timing_key_value;
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
    elsif expected_available_at_value <
      timing_head.last_seen_evidence_available_at then
      quarantine_reason_value := 'pit_timing_observation_time_regressed';
    elsif expected_available_at_value <=
      timing_head.last_seen_evidence_available_at then
      quarantine_reason_value := 'pit_timing_revision_time_not_increasing';
    else
      next_revision_value := timing_head.latest_revision + 1;
      insert into private.pit_daily_candle_timing_revisions (
        timing_idempotency_key,
        revision,
        canonical_timing_evidence_sha256,
        candle_revision_id,
        calendar_revision_id,
        evidence_available_at,
        timing_payload
      ) values (
        timing_key_value,
        next_revision_value,
        timing_candidate_sha_value,
        candle_revision.id,
        calendar_revision_row.id,
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

create or replace function worker_api.append_pit_daily_candle_timing_evidence_v1(
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
language sql
volatile
security invoker
set search_path = ''
as $$
  select *
  from private.append_pit_daily_candle_timing_evidence_v1_impl(
    p_request_idempotency_key,
    p_calendar,
    p_timing
  );
$$;

revoke all on function
  private.pit_calendar_identity_sha256_v1(text,text,date),
  private.pit_calendar_canonical_evidence_sha256_v1(
    text,text,date,boolean,timestamptz,timestamptz,date,timestamptz,
    timestamptz,text
  ),
  private.pit_daily_candle_timing_identity_sha256_v1(text,text),
  private.pit_daily_candle_timing_evidence_sha256_v1(
    text,text,text,text,boolean,date,timestamptz,timestamptz,timestamptz,
    date,timestamptz,timestamptz,timestamptz,timestamptz,text,text,text,
    text,text,text
  ),
  private.pit_daily_candle_timing_request_sha256_v1(jsonb,jsonb),
  private.quarantine_pit_calendar_observation_v1(
    text,text,text,text,text,timestamptz,jsonb,bigint,text,timestamptz
  ),
  private.quarantine_pit_daily_candle_timing_v1(
    text,text,text,text,text,jsonb,bigint,text,timestamptz
  ),
  private.put_pit_daily_candle_timing_receipt_v1(
    text,text,text,text,text,bigint,bigint,boolean,boolean,timestamptz,uuid,
    text,boolean
  ),
  private.append_pit_daily_candle_timing_evidence_v1_impl(text,jsonb,jsonb),
  worker_api.append_pit_daily_candle_timing_evidence_v1(text,jsonb,jsonb)
from public, anon, authenticated, service_role;

grant execute on function
  private.append_pit_daily_candle_timing_evidence_v1_impl(text,jsonb,jsonb),
  worker_api.append_pit_daily_candle_timing_evidence_v1(text,jsonb,jsonb)
to service_role;

do $$
declare
  private_definer boolean;
  private_search_path text[];
  wrapper_definer boolean;
  wrapper_search_path text[];
  rls_table_count bigint;
begin
  select count(*) into rls_table_count
  from pg_catalog.pg_class as relation
  join pg_catalog.pg_namespace as namespace
    on namespace.oid = relation.relnamespace
  where namespace.nspname = 'private'
    and relation.relname in (
      'pit_calendar_stream_heads',
      'pit_calendar_content_revisions',
      'pit_calendar_observation_quarantine',
      'pit_daily_candle_timing_heads',
      'pit_daily_candle_timing_revisions',
      'pit_daily_candle_timing_quarantine',
      'pit_daily_candle_timing_request_receipts',
      'pit_daily_candle_timing_request_ledger'
    )
    and relation.relrowsecurity;

  select procedure.prosecdef, procedure.proconfig
  into private_definer, private_search_path
  from pg_catalog.pg_proc as procedure
  where procedure.oid = to_regprocedure(
    'private.append_pit_daily_candle_timing_evidence_v1_impl(text,jsonb,jsonb)'
  );

  select procedure.prosecdef, procedure.proconfig
  into wrapper_definer, wrapper_search_path
  from pg_catalog.pg_proc as procedure
  where procedure.oid = to_regprocedure(
    'worker_api.append_pit_daily_candle_timing_evidence_v1(text,jsonb,jsonb)'
  );

  if rls_table_count <> 8
     or private_definer is distinct from true
     or private_search_path is distinct from array['search_path=""']::text[]
     or wrapper_definer is distinct from false
     or wrapper_search_path is distinct from array['search_path=""']::text[] then
    raise exception 'pit_daily_candle_timing_store_catalog_invalid'
      using errcode = '55000';
  end if;
end;
$$;

commit;
