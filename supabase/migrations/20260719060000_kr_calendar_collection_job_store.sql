begin;

set local lock_timeout = '5s';
set local statement_timeout = '90s';

-- Durable, manually driven KR calendar range collection. This state machine
-- deliberately has no lease expiry, takeover, scheduler, or automatic retry.
do $$
begin
  if to_regprocedure('private.require_service_role()') is null
     or to_regprocedure('private.jsonb_exact_keys_v1(jsonb,text[])') is null
     or to_regprocedure('private.reject_append_only_mutation()') is null
     or to_regprocedure('private.pit_canonical_timestamp_v1(timestamptz)') is null
     or to_regprocedure('private.pit_sha256_text_v1(text)') is null
     or to_regclass('private.pit_calendar_content_revisions') is null
     or to_regclass('private.pit_calendar_observation_occurrences') is null then
    raise exception 'kr_calendar_collection_job_store_dependency_missing'
      using errcode = '55000';
  end if;
end;
$$;

create table private.kr_calendar_collection_jobs (
  job_id uuid primary key,
  spec_sha256 text not null check (spec_sha256 ~ '^[0-9a-f]{64}$'),
  spec jsonb not null check (pg_catalog.jsonb_typeof(spec) = 'object'),
  provider text not null check (provider ~ '^[a-z][a-z0-9._-]{0,63}$'),
  market text not null check (market = 'KR'),
  start_date date not null,
  end_date date not null,
  total_days integer not null check (total_days between 1 and 366),
  revision bigint not null check (revision > 0),
  state text not null check (state in (
    'ready', 'collecting', 'paused_retryable', 'blocked_unknown', 'completed'
  )),
  active_attempt_id uuid,
  active_holder_id uuid,
  active_target_date date,
  active_fencing_revision bigint,
  active_begun_at timestamptz,
  state_reason text check (
    state_reason is null or state_reason ~ '^[a-z][a-z0-9_]{0,127}$'
  ),
  terminal_manifest_sha256 text check (
    terminal_manifest_sha256 is null
    or terminal_manifest_sha256 ~ '^[0-9a-f]{64}$'
  ),
  created_at timestamptz not null,
  updated_at timestamptz not null,
  check (end_date >= start_date and end_date - start_date <= 365),
  check (total_days = end_date - start_date + 1),
  check (created_at <= updated_at),
  check (
    (state = 'collecting' and revision % 2 = 0)
    or (state <> 'collecting' and revision % 2 = 1)
  ),
  check (
    (state in ('collecting', 'blocked_unknown')
     and active_attempt_id is not null and active_holder_id is not null
     and active_target_date is not null and active_fencing_revision > 1
     and active_begun_at is not null
     and active_fencing_revision = case
       when state = 'collecting' then revision else revision - 1
     end)
    or
    (state not in ('collecting', 'blocked_unknown')
     and active_attempt_id is null and active_holder_id is null
     and active_target_date is null and active_fencing_revision is null
     and active_begun_at is null)
  ),
  check ((state = 'completed') = (terminal_manifest_sha256 is not null)),
  check ((state in ('paused_retryable', 'blocked_unknown')) = (state_reason is not null))
);

create table private.kr_calendar_collection_attempt_ledger (
  event_id uuid primary key default pg_catalog.gen_random_uuid(),
  job_id uuid not null references private.kr_calendar_collection_jobs(job_id),
  job_revision bigint not null check (job_revision > 1),
  attempt_id uuid not null,
  holder_id uuid not null,
  target_date date not null,
  fencing_revision bigint not null check (fencing_revision > 1),
  begun_at timestamptz not null,
  event_kind text not null check (event_kind in (
    'begun', 'paused_retryable', 'blocked_unknown', 'confirmed'
  )),
  reason_code text check (
    reason_code is null or reason_code ~ '^[a-z][a-z0-9_]{0,127}$'
  ),
  session_payload jsonb,
  receipt_payload jsonb,
  occurred_at timestamptz not null,
  event_sha256 text not null check (event_sha256 ~ '^[0-9a-f]{64}$'),
  unique (job_id, job_revision),
  unique (attempt_id, event_kind),
  check (begun_at <= occurred_at),
  check (
    (event_kind = 'begun' and job_revision = fencing_revision)
    or
    (event_kind <> 'begun' and job_revision = fencing_revision + 1)
  ),
  check (fencing_revision % 2 = 0),
  check (
    (event_kind = 'begun' and reason_code is null
     and session_payload is null and receipt_payload is null)
    or
    (event_kind in ('paused_retryable', 'blocked_unknown')
     and reason_code is not null
     and session_payload is null and receipt_payload is null)
    or
    (event_kind = 'confirmed' and reason_code is null
     and session_payload is not null and receipt_payload is not null)
  )
);

create unique index kr_calendar_collection_attempt_begin_once_idx
  on private.kr_calendar_collection_attempt_ledger (attempt_id)
  where event_kind = 'begun';

create index kr_calendar_collection_attempt_job_timeline_idx
  on private.kr_calendar_collection_attempt_ledger (
    job_id, target_date, job_revision, event_id
  );

create trigger reject_kr_calendar_collection_attempt_mutation
before update or delete on private.kr_calendar_collection_attempt_ledger
for each row execute function private.reject_append_only_mutation();

alter table private.kr_calendar_collection_jobs enable row level security;
alter table private.kr_calendar_collection_jobs force row level security;
alter table private.kr_calendar_collection_attempt_ledger enable row level security;
alter table private.kr_calendar_collection_attempt_ledger force row level security;

revoke all on table
  private.kr_calendar_collection_jobs,
  private.kr_calendar_collection_attempt_ledger
from public, anon, authenticated, authenticator, service_role;

create or replace function private.kr_calendar_collection_uuid4_v1(p_value text)
returns boolean
language sql
immutable
strict
security definer
set search_path = ''
as $$
  select p_value ~
    '^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
    and (p_value::uuid)::text = p_value;
$$;

-- PostgreSQL jsonb uses a deterministic tree but its text form contains
-- spaces. This renderer matches Python json.dumps(sort_keys=True,
-- separators=(",", ":"), ensure_ascii=False) for the validated contract
-- values used by job specs, checkpoints, events, and terminal manifests.
create or replace function private.kr_calendar_collection_canonical_json_v1(
  p_value jsonb
)
returns text
language plpgsql
immutable
strict
security definer
set search_path = ''
as $$
declare
  rendered text;
begin
  case pg_catalog.jsonb_typeof(p_value)
    when 'object' then
      select '{' || coalesce(
        pg_catalog.string_agg(
          pg_catalog.to_jsonb(item.key)::text || ':' ||
          private.kr_calendar_collection_canonical_json_v1(item.value),
          ',' order by item.key collate "C"
        ),
        ''
      ) || '}'
      into rendered
      from pg_catalog.jsonb_each(p_value) as item;
    when 'array' then
      select '[' || coalesce(
        pg_catalog.string_agg(
          private.kr_calendar_collection_canonical_json_v1(item.value),
          ',' order by item.ordinality
        ),
        ''
      ) || ']'
      into rendered
      from pg_catalog.jsonb_array_elements(p_value)
        with ordinality as item(value, ordinality);
    else
      rendered := p_value::text;
  end case;
  return rendered;
end;
$$;

create or replace function private.kr_calendar_collection_spec_document_v1(
  p_spec jsonb
)
returns jsonb
language sql
immutable
strict
security definer
set search_path = ''
as $$
  select pg_catalog.jsonb_build_object(
    'schema_version', p_spec->>'schema_version',
    'job_id', p_spec->>'job_id',
    'provider', p_spec->>'provider',
    'market', p_spec->>'market',
    'start_date', p_spec->>'start_date',
    'end_date', p_spec->>'end_date',
    'trigger', p_spec->>'trigger',
    'maximum_inclusive_days', 366,
    'automatic_retry_allowed', false
  );
$$;

create or replace function private.kr_calendar_collection_spec_sha256_v1(
  p_spec jsonb
)
returns text
language sql
immutable
strict
security definer
set search_path = ''
as $$
  select private.pit_sha256_text_v1(
    private.kr_calendar_collection_canonical_json_v1(
      private.kr_calendar_collection_spec_document_v1(p_spec)
    )
  );
$$;

create or replace function private.kr_calendar_collection_checkpoints_v1(
  p_job_id uuid
)
returns jsonb
language sql
stable
strict
security definer
set search_path = ''
as $$
  select coalesce(
    pg_catalog.jsonb_agg(
      pg_catalog.jsonb_build_object(
        'attempt_id', event.attempt_id::text,
        'holder_id', event.holder_id::text,
        'target_date', pg_catalog.to_char(event.target_date, 'YYYY-MM-DD'),
        'fencing_revision', event.fencing_revision,
        'begun_at', private.pit_canonical_timestamp_v1(event.begun_at),
        'session', event.session_payload,
        'receipt', event.receipt_payload,
        'confirmed_at', private.pit_canonical_timestamp_v1(event.occurred_at)
      ) order by event.target_date, event.fencing_revision, event.event_id
    ),
    '[]'::jsonb
  )
  from private.kr_calendar_collection_attempt_ledger as event
  where event.job_id = p_job_id
    and event.event_kind = 'confirmed';
$$;

create or replace function private.kr_calendar_collection_snapshot_v1(
  p_job_id uuid
)
returns jsonb
language plpgsql
stable
strict
security definer
set search_path = ''
as $$
declare
  job private.kr_calendar_collection_jobs%rowtype;
  checkpoints jsonb;
  active_attempt jsonb;
begin
  select * into strict job
  from private.kr_calendar_collection_jobs as stored
  where stored.job_id = p_job_id;

  checkpoints := private.kr_calendar_collection_checkpoints_v1(p_job_id);
  active_attempt := case
    when job.active_attempt_id is null then null::jsonb
    else pg_catalog.jsonb_build_object(
      'attempt_id', job.active_attempt_id::text,
      'holder_id', job.active_holder_id::text,
      'target_date', pg_catalog.to_char(job.active_target_date, 'YYYY-MM-DD'),
      'fencing_revision', job.active_fencing_revision,
      'begun_at', private.pit_canonical_timestamp_v1(job.active_begun_at)
    )
  end;

  return pg_catalog.jsonb_build_object(
    'schema_version', 'kr_calendar_collection_job_snapshot.v1',
    'spec_sha256', job.spec_sha256,
    'spec', job.spec,
    'revision', job.revision,
    'state', job.state,
    'checkpoints', checkpoints,
    'active_attempt', active_attempt,
    'state_reason', job.state_reason,
    'terminal_manifest_sha256', job.terminal_manifest_sha256,
    'created_at', private.pit_canonical_timestamp_v1(job.created_at),
    'updated_at', private.pit_canonical_timestamp_v1(job.updated_at),
    'automatic_retry_allowed', false
  );
exception
  when no_data_found then
    raise exception 'kr_calendar_collection_job_not_found'
      using errcode = 'P0002';
end;
$$;

create or replace function private.kr_calendar_collection_manifest_sha256_v1(
  p_job_id uuid
)
returns text
language plpgsql
stable
strict
security definer
set search_path = ''
as $$
declare
  job private.kr_calendar_collection_jobs%rowtype;
  checkpoints jsonb;
begin
  select * into strict job
  from private.kr_calendar_collection_jobs as stored
  where stored.job_id = p_job_id;
  checkpoints := private.kr_calendar_collection_checkpoints_v1(p_job_id);
  if pg_catalog.jsonb_array_length(checkpoints) <> job.total_days then
    raise exception 'kr_calendar_collection_job_manifest_incomplete'
      using errcode = '55000';
  end if;
  return private.pit_sha256_text_v1(
    private.kr_calendar_collection_canonical_json_v1(
      pg_catalog.jsonb_build_object(
        'schema_version', 'kr_calendar_collection_job_manifest.v1',
        'spec', private.kr_calendar_collection_spec_document_v1(job.spec),
        'spec_sha256', job.spec_sha256,
        'confirmed_count', job.total_days,
        'checkpoints', checkpoints
      )
    )
  );
end;
$$;

create or replace function private.kr_calendar_collection_event_sha256_v1(
  p_job_id uuid,
  p_job_revision bigint,
  p_attempt_id uuid,
  p_holder_id uuid,
  p_target_date date,
  p_fencing_revision bigint,
  p_begun_at timestamptz,
  p_event_kind text,
  p_reason_code text,
  p_session jsonb,
  p_receipt jsonb,
  p_occurred_at timestamptz
)
returns text
language sql
immutable
security definer
set search_path = ''
as $$
  select private.pit_sha256_text_v1(
    private.kr_calendar_collection_canonical_json_v1(
      pg_catalog.jsonb_build_object(
        'job_id', p_job_id::text,
        'job_revision', p_job_revision,
        'attempt_id', p_attempt_id::text,
        'holder_id', p_holder_id::text,
        'target_date', pg_catalog.to_char(p_target_date, 'YYYY-MM-DD'),
        'fencing_revision', p_fencing_revision,
        'begun_at', private.pit_canonical_timestamp_v1(p_begun_at),
        'event_kind', p_event_kind,
        'reason_code', p_reason_code,
        'session', p_session,
        'receipt', p_receipt,
        'occurred_at', private.pit_canonical_timestamp_v1(p_occurred_at)
      )
    )
  );
$$;

create or replace function private.load_or_create_kr_calendar_collection_job_v1_impl(
  p_spec jsonb,
  p_now timestamptz
)
returns table(snapshot jsonb)
language plpgsql
volatile
security definer
set search_path = ''
as $$
declare
  normalized_spec jsonb;
  job_id_value uuid;
  start_date_value date;
  end_date_value date;
  spec_sha256_value text;
  existing private.kr_calendar_collection_jobs%rowtype;
begin
  perform private.require_service_role();
  if p_spec is null
     or pg_catalog.jsonb_typeof(p_spec) <> 'object'
     or pg_catalog.octet_length(p_spec::text) > 2048
     or private.jsonb_exact_keys_v1(
       p_spec,
       array[
         'schema_version', 'job_id', 'provider', 'market', 'start_date',
         'end_date', 'trigger'
       ]
     ) is distinct from true
     or p_spec->>'schema_version' <> 'kr_calendar_collection_job.v1'
     or pg_catalog.jsonb_typeof(p_spec->'job_id') <> 'string'
     or not private.kr_calendar_collection_uuid4_v1(p_spec->>'job_id')
     or pg_catalog.jsonb_typeof(p_spec->'provider') <> 'string'
     or (p_spec->>'provider') !~ '^[a-z][a-z0-9._-]{0,63}$'
     or p_spec->>'market' <> 'KR'
     or p_spec->>'trigger' <> 'manual'
     or pg_catalog.jsonb_typeof(p_spec->'start_date') <> 'string'
     or (p_spec->>'start_date') !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
     or pg_catalog.jsonb_typeof(p_spec->'end_date') <> 'string'
     or (p_spec->>'end_date') !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
     or p_now is null
     or not pg_catalog.isfinite(p_now) then
    raise exception 'kr_calendar_collection_job_argument_invalid'
      using errcode = '22023';
  end if;

  begin
    job_id_value := (p_spec->>'job_id')::uuid;
    start_date_value := (p_spec->>'start_date')::date;
    end_date_value := (p_spec->>'end_date')::date;
  exception when others then
    raise exception 'kr_calendar_collection_job_argument_invalid'
      using errcode = '22023';
  end;
  if pg_catalog.to_char(start_date_value, 'YYYY-MM-DD') <>
       p_spec->>'start_date'
     or pg_catalog.to_char(end_date_value, 'YYYY-MM-DD') <>
        p_spec->>'end_date'
     or start_date_value > end_date_value
     or end_date_value - start_date_value > 365 then
    raise exception 'kr_calendar_collection_job_argument_invalid'
      using errcode = '22023';
  end if;

  normalized_spec := pg_catalog.jsonb_build_object(
    'schema_version', 'kr_calendar_collection_job.v1',
    'job_id', job_id_value::text,
    'provider', p_spec->>'provider',
    'market', 'KR',
    'start_date', pg_catalog.to_char(start_date_value, 'YYYY-MM-DD'),
    'end_date', pg_catalog.to_char(end_date_value, 'YYYY-MM-DD'),
    'trigger', 'manual'
  );
  spec_sha256_value :=
    private.kr_calendar_collection_spec_sha256_v1(normalized_spec);

  -- Serialize first creation as well as later spec comparisons. Row locking
  -- cannot protect a job that does not exist yet.
  perform pg_catalog.pg_advisory_xact_lock(
    pg_catalog.hashtextextended(job_id_value::text, 19060000)
  );

  select * into existing
  from private.kr_calendar_collection_jobs as job
  where job.job_id = job_id_value
  for update;

  if found then
    if existing.spec_sha256 <> spec_sha256_value
       or existing.spec <> normalized_spec then
      raise exception 'kr_calendar_collection_job_spec_conflict'
        using errcode = '23505';
    end if;
  else
    insert into private.kr_calendar_collection_jobs (
      job_id, spec_sha256, spec, provider, market, start_date, end_date,
      total_days, revision, state, created_at, updated_at
    ) values (
      job_id_value, spec_sha256_value, normalized_spec, p_spec->>'provider',
      'KR', start_date_value, end_date_value,
      end_date_value - start_date_value + 1, 1, 'ready', p_now, p_now
    );
  end if;

  return query
  select private.kr_calendar_collection_snapshot_v1(job_id_value);
end;
$$;

create or replace function private.begin_kr_calendar_collection_date_attempt_v1_impl(
  p_job_id text,
  p_spec_sha256 text,
  p_expected_revision bigint,
  p_attempt_id text,
  p_holder_id text,
  p_target_date date,
  p_now timestamptz
)
returns table(snapshot jsonb)
language plpgsql
volatile
security definer
set search_path = ''
as $$
declare
  job private.kr_calendar_collection_jobs%rowtype;
  job_id_value uuid;
  attempt_id_value uuid;
  holder_id_value uuid;
  confirmed_count integer;
  next_date_value date;
  new_revision bigint;
begin
  perform private.require_service_role();
  if not coalesce(private.kr_calendar_collection_uuid4_v1(p_job_id), false)
     or p_spec_sha256 !~ '^[0-9a-f]{64}$'
     or p_expected_revision is null or p_expected_revision <= 0
     or p_expected_revision >= 9223372036854775807
     or not coalesce(private.kr_calendar_collection_uuid4_v1(p_attempt_id), false)
     or not coalesce(private.kr_calendar_collection_uuid4_v1(p_holder_id), false)
     or p_target_date is null or p_now is null or not pg_catalog.isfinite(p_now) then
    raise exception 'kr_calendar_collection_job_argument_invalid'
      using errcode = '22023';
  end if;
  job_id_value := p_job_id::uuid;
  attempt_id_value := p_attempt_id::uuid;
  holder_id_value := p_holder_id::uuid;

  select * into job
  from private.kr_calendar_collection_jobs as stored
  where stored.job_id = job_id_value
  for update;
  if not found then
    raise exception 'kr_calendar_collection_job_not_found' using errcode = 'P0002';
  end if;
  if job.spec_sha256 <> p_spec_sha256 then
    raise exception 'kr_calendar_collection_job_spec_hash_mismatch' using errcode = '40001';
  end if;
  if job.revision <> p_expected_revision then
    raise exception 'kr_calendar_collection_job_revision_conflict' using errcode = '40001';
  end if;
  if p_now < job.updated_at then
    raise exception 'kr_calendar_collection_job_clock_regressed' using errcode = '40001';
  end if;
  if job.state not in ('ready', 'paused_retryable') then
    raise exception 'kr_calendar_collection_job_begin_state_invalid' using errcode = '55000';
  end if;
  select count(*)::integer into confirmed_count
  from private.kr_calendar_collection_attempt_ledger as event
  where event.job_id = job_id_value and event.event_kind = 'confirmed';
  next_date_value := job.start_date + confirmed_count;
  if confirmed_count >= job.total_days or p_target_date <> next_date_value then
    raise exception 'kr_calendar_collection_job_begin_target_invalid' using errcode = '22023';
  end if;
  if exists (
    select 1 from private.kr_calendar_collection_attempt_ledger as event
    where event.attempt_id = attempt_id_value
  ) then
    raise exception 'kr_calendar_collection_job_attempt_reused' using errcode = '23505';
  end if;
  new_revision := p_expected_revision + 1;

  insert into private.kr_calendar_collection_attempt_ledger (
    job_id, job_revision, attempt_id, holder_id, target_date,
    fencing_revision, begun_at, event_kind, occurred_at, event_sha256
  ) values (
    job_id_value, new_revision, attempt_id_value, holder_id_value,
    p_target_date, new_revision, p_now, 'begun', p_now,
    private.kr_calendar_collection_event_sha256_v1(
      job_id_value, new_revision, attempt_id_value, holder_id_value,
      p_target_date, new_revision, p_now, 'begun', null, null, null, p_now
    )
  );
  update private.kr_calendar_collection_jobs
  set revision = new_revision,
      state = 'collecting',
      active_attempt_id = attempt_id_value,
      active_holder_id = holder_id_value,
      active_target_date = p_target_date,
      active_fencing_revision = new_revision,
      active_begun_at = p_now,
      state_reason = null,
      updated_at = p_now
  where job_id = job_id_value;

  return query select private.kr_calendar_collection_snapshot_v1(job_id_value);
end;
$$;

create or replace function private.pause_kr_calendar_collection_date_attempt_v1_impl(
  p_job_id text,
  p_spec_sha256 text,
  p_expected_revision bigint,
  p_attempt_id text,
  p_holder_id text,
  p_target_date date,
  p_reason_code text,
  p_now timestamptz
)
returns table(snapshot jsonb)
language plpgsql
volatile
security definer
set search_path = ''
as $$
declare
  job private.kr_calendar_collection_jobs%rowtype;
  job_id_value uuid;
  attempt_id_value uuid;
  holder_id_value uuid;
  new_revision bigint;
begin
  perform private.require_service_role();
  if not coalesce(private.kr_calendar_collection_uuid4_v1(p_job_id), false)
     or p_spec_sha256 !~ '^[0-9a-f]{64}$'
     or p_expected_revision is null or p_expected_revision <= 0
     or p_expected_revision >= 9223372036854775807
     or not coalesce(private.kr_calendar_collection_uuid4_v1(p_attempt_id), false)
     or not coalesce(private.kr_calendar_collection_uuid4_v1(p_holder_id), false)
     or p_target_date is null
     or p_reason_code !~ '^[a-z][a-z0-9_]{0,127}$'
     or p_now is null or not pg_catalog.isfinite(p_now) then
    raise exception 'kr_calendar_collection_job_argument_invalid' using errcode = '22023';
  end if;
  job_id_value := p_job_id::uuid;
  attempt_id_value := p_attempt_id::uuid;
  holder_id_value := p_holder_id::uuid;
  select * into job
  from private.kr_calendar_collection_jobs as stored
  where stored.job_id = job_id_value
  for update;
  if not found then
    raise exception 'kr_calendar_collection_job_not_found' using errcode = 'P0002';
  end if;
  if job.spec_sha256 <> p_spec_sha256 then
    raise exception 'kr_calendar_collection_job_spec_hash_mismatch' using errcode = '40001';
  end if;
  if job.revision <> p_expected_revision then
    raise exception 'kr_calendar_collection_job_revision_conflict' using errcode = '40001';
  end if;
  if p_now < job.updated_at then
    raise exception 'kr_calendar_collection_job_clock_regressed' using errcode = '40001';
  end if;
  if job.state <> 'collecting' or job.active_attempt_id is null then
    raise exception 'kr_calendar_collection_job_active_state_invalid' using errcode = '55000';
  end if;
  if job.active_attempt_id <> attempt_id_value
     or job.active_holder_id <> holder_id_value
     or job.active_target_date <> p_target_date
     or job.active_fencing_revision <> p_expected_revision then
    raise exception 'kr_calendar_collection_job_attempt_fence_mismatch' using errcode = '40001';
  end if;
  new_revision := p_expected_revision + 1;
  insert into private.kr_calendar_collection_attempt_ledger (
    job_id, job_revision, attempt_id, holder_id, target_date,
    fencing_revision, begun_at, event_kind, reason_code, occurred_at,
    event_sha256
  ) values (
    job_id_value, new_revision, attempt_id_value, holder_id_value,
    p_target_date, p_expected_revision, job.active_begun_at,
    'paused_retryable', p_reason_code, p_now,
    private.kr_calendar_collection_event_sha256_v1(
      job_id_value, new_revision, attempt_id_value, holder_id_value,
      p_target_date, p_expected_revision, job.active_begun_at,
      'paused_retryable', p_reason_code, null, null, p_now
    )
  );
  update private.kr_calendar_collection_jobs
  set revision = new_revision,
      state = 'paused_retryable',
      active_attempt_id = null,
      active_holder_id = null,
      active_target_date = null,
      active_fencing_revision = null,
      active_begun_at = null,
      state_reason = p_reason_code,
      updated_at = p_now
  where job_id = job_id_value;
  return query select private.kr_calendar_collection_snapshot_v1(job_id_value);
end;
$$;

create or replace function private.block_kr_calendar_collection_date_attempt_v1_impl(
  p_job_id text,
  p_spec_sha256 text,
  p_expected_revision bigint,
  p_attempt_id text,
  p_holder_id text,
  p_target_date date,
  p_reason_code text,
  p_now timestamptz
)
returns table(snapshot jsonb)
language plpgsql
volatile
security definer
set search_path = ''
as $$
declare
  job private.kr_calendar_collection_jobs%rowtype;
  job_id_value uuid;
  attempt_id_value uuid;
  holder_id_value uuid;
  new_revision bigint;
begin
  perform private.require_service_role();
  if not coalesce(private.kr_calendar_collection_uuid4_v1(p_job_id), false)
     or p_spec_sha256 !~ '^[0-9a-f]{64}$'
     or p_expected_revision is null or p_expected_revision <= 0
     or p_expected_revision >= 9223372036854775807
     or not coalesce(private.kr_calendar_collection_uuid4_v1(p_attempt_id), false)
     or not coalesce(private.kr_calendar_collection_uuid4_v1(p_holder_id), false)
     or p_target_date is null
     or p_reason_code !~ '^[a-z][a-z0-9_]{0,127}$'
     or p_now is null or not pg_catalog.isfinite(p_now) then
    raise exception 'kr_calendar_collection_job_argument_invalid' using errcode = '22023';
  end if;
  job_id_value := p_job_id::uuid;
  attempt_id_value := p_attempt_id::uuid;
  holder_id_value := p_holder_id::uuid;
  select * into job
  from private.kr_calendar_collection_jobs as stored
  where stored.job_id = job_id_value
  for update;
  if not found then
    raise exception 'kr_calendar_collection_job_not_found' using errcode = 'P0002';
  end if;
  if job.spec_sha256 <> p_spec_sha256 then
    raise exception 'kr_calendar_collection_job_spec_hash_mismatch' using errcode = '40001';
  end if;
  if job.revision <> p_expected_revision then
    raise exception 'kr_calendar_collection_job_revision_conflict' using errcode = '40001';
  end if;
  if p_now < job.updated_at then
    raise exception 'kr_calendar_collection_job_clock_regressed' using errcode = '40001';
  end if;
  if job.state <> 'collecting' or job.active_attempt_id is null then
    raise exception 'kr_calendar_collection_job_active_state_invalid' using errcode = '55000';
  end if;
  if job.active_attempt_id <> attempt_id_value
     or job.active_holder_id <> holder_id_value
     or job.active_target_date <> p_target_date
     or job.active_fencing_revision <> p_expected_revision then
    raise exception 'kr_calendar_collection_job_attempt_fence_mismatch' using errcode = '40001';
  end if;
  new_revision := p_expected_revision + 1;
  insert into private.kr_calendar_collection_attempt_ledger (
    job_id, job_revision, attempt_id, holder_id, target_date,
    fencing_revision, begun_at, event_kind, reason_code, occurred_at,
    event_sha256
  ) values (
    job_id_value, new_revision, attempt_id_value, holder_id_value,
    p_target_date, p_expected_revision, job.active_begun_at,
    'blocked_unknown', p_reason_code, p_now,
    private.kr_calendar_collection_event_sha256_v1(
      job_id_value, new_revision, attempt_id_value, holder_id_value,
      p_target_date, p_expected_revision, job.active_begun_at,
      'blocked_unknown', p_reason_code, null, null, p_now
    )
  );
  update private.kr_calendar_collection_jobs
  set revision = new_revision,
      state = 'blocked_unknown',
      state_reason = p_reason_code,
      updated_at = p_now
  where job_id = job_id_value;
  return query select private.kr_calendar_collection_snapshot_v1(job_id_value);
end;
$$;

create or replace function private.confirm_kr_calendar_collection_date_v1_impl(
  p_job_id text,
  p_spec_sha256 text,
  p_expected_revision bigint,
  p_attempt_id text,
  p_holder_id text,
  p_target_date date,
  p_session jsonb,
  p_receipt jsonb,
  p_now timestamptz
)
returns table(snapshot jsonb)
language plpgsql
volatile
security definer
set search_path = ''
as $$
declare
  job private.kr_calendar_collection_jobs%rowtype;
  job_id_value uuid;
  attempt_id_value uuid;
  holder_id_value uuid;
  occurrence_id_value uuid;
  observed_at_value timestamptz;
  confirmed_count integer;
  new_revision bigint;
  completed_value boolean;
  manifest_value text;
begin
  perform private.require_service_role();
  if not coalesce(private.kr_calendar_collection_uuid4_v1(p_job_id), false)
     or p_spec_sha256 !~ '^[0-9a-f]{64}$'
     or p_expected_revision is null or p_expected_revision <= 0
     or p_expected_revision >= 9223372036854775807
     or not coalesce(private.kr_calendar_collection_uuid4_v1(p_attempt_id), false)
     or not coalesce(private.kr_calendar_collection_uuid4_v1(p_holder_id), false)
     or p_target_date is null
     or p_now is null or not pg_catalog.isfinite(p_now)
     or p_session is null or pg_catalog.jsonb_typeof(p_session) <> 'object'
     or pg_catalog.octet_length(p_session::text) > 8192
     or p_receipt is null or pg_catalog.jsonb_typeof(p_receipt) <> 'object'
     or pg_catalog.octet_length(p_receipt::text) > 4096
     or private.jsonb_exact_keys_v1(
       p_session,
       array[
         'schema_version', 'provider', 'market', 'session_date', 'is_open',
         'regular_start_at', 'regular_end_at', 'next_business_date',
         'next_regular_start_at', 'next_regular_end_at', 'observed_at',
         'provider_contract_sha256', 'canonical_evidence_sha256'
       ]
     ) is distinct from true
     or private.jsonb_exact_keys_v1(
       p_receipt,
       array[
         'status', 'calendar_idempotency_key',
         'canonical_evidence_sha256', 'revision', 'revision_inserted',
         'occurrence_id', 'occurrence_inserted', 'observed_at'
       ]
     ) is distinct from true
     or p_session->>'schema_version' <> '1'
     or pg_catalog.jsonb_typeof(p_session->'is_open') <> 'boolean'
     or (p_session->>'session_date') !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
     or (p_session->>'provider_contract_sha256') !~ '^[0-9a-f]{64}$'
     or (p_session->>'canonical_evidence_sha256') !~ '^[0-9a-f]{64}$'
     or p_receipt->>'status' not in ('stored', 'replayed')
     or (p_receipt->>'calendar_idempotency_key') !~ '^[0-9a-f]{64}$'
     or (p_receipt->>'canonical_evidence_sha256') !~ '^[0-9a-f]{64}$'
     or pg_catalog.jsonb_typeof(p_receipt->'status') <> 'string'
     or pg_catalog.jsonb_typeof(p_receipt->'revision') <> 'number'
     or (p_receipt->>'revision') !~ '^[1-9][0-9]*$'
     or pg_catalog.jsonb_typeof(p_receipt->'revision_inserted') <> 'boolean'
     or pg_catalog.jsonb_typeof(p_receipt->'occurrence_inserted') <> 'boolean'
     or not coalesce(
       private.kr_calendar_collection_uuid4_v1(p_receipt->>'occurrence_id'),
       false
     )
     or pg_catalog.jsonb_typeof(p_receipt->'observed_at') <> 'string'
     or p_receipt->>'canonical_evidence_sha256' <>
        p_session->>'canonical_evidence_sha256'
     or p_receipt->>'observed_at' <> p_session->>'observed_at'
     or (p_receipt->>'status' = 'stored' and not (
       (p_receipt->>'revision_inserted')::boolean
       and (p_receipt->>'occurrence_inserted')::boolean
     ))
     or (p_receipt->>'status' = 'replayed' and
       (p_receipt->>'revision_inserted')::boolean) then
    raise exception 'kr_calendar_collection_job_argument_invalid'
      using errcode = '22023';
  end if;
  begin
    job_id_value := p_job_id::uuid;
    attempt_id_value := p_attempt_id::uuid;
    holder_id_value := p_holder_id::uuid;
    occurrence_id_value := (p_receipt->>'occurrence_id')::uuid;
    observed_at_value := (p_session->>'observed_at')::timestamptz;
    perform (p_session->>'session_date')::date;
  exception when others then
    raise exception 'kr_calendar_collection_job_argument_invalid'
      using errcode = '22023';
  end;
  if private.pit_canonical_timestamp_v1(observed_at_value) <>
       p_session->>'observed_at'
     or pg_catalog.to_char(
       (p_session->>'session_date')::date,
       'YYYY-MM-DD'
     ) <> p_session->>'session_date' then
    raise exception 'kr_calendar_collection_job_argument_invalid'
      using errcode = '22023';
  end if;

  select * into job
  from private.kr_calendar_collection_jobs as stored
  where stored.job_id = job_id_value
  for update;
  if not found then
    raise exception 'kr_calendar_collection_job_not_found' using errcode = 'P0002';
  end if;
  if job.spec_sha256 <> p_spec_sha256 then
    raise exception 'kr_calendar_collection_job_spec_hash_mismatch' using errcode = '40001';
  end if;
  if job.revision <> p_expected_revision then
    raise exception 'kr_calendar_collection_job_revision_conflict' using errcode = '40001';
  end if;
  if p_now < job.updated_at then
    raise exception 'kr_calendar_collection_job_clock_regressed' using errcode = '40001';
  end if;
  if job.state <> 'collecting' or job.active_attempt_id is null then
    raise exception 'kr_calendar_collection_job_active_state_invalid' using errcode = '55000';
  end if;
  if job.active_attempt_id <> attempt_id_value
     or job.active_holder_id <> holder_id_value
     or job.active_target_date <> p_target_date
     or job.active_fencing_revision <> p_expected_revision then
    raise exception 'kr_calendar_collection_job_attempt_fence_mismatch' using errcode = '40001';
  end if;
  if p_session->>'provider' <> job.provider
     or p_session->>'market' <> job.market
     or p_session->>'session_date' <>
        pg_catalog.to_char(p_target_date, 'YYYY-MM-DD')
     or observed_at_value < job.active_begun_at
     or observed_at_value > p_now then
    raise exception 'kr_calendar_collection_job_collection_scope_mismatch'
      using errcode = '22023';
  end if;

  if not exists (
    select 1
    from private.pit_calendar_observation_occurrences as occurrence
    join private.pit_calendar_content_revisions as revision
      on revision.id = occurrence.content_revision_id
     and revision.calendar_idempotency_key = occurrence.calendar_idempotency_key
     and revision.canonical_evidence_sha256 =
         occurrence.canonical_evidence_sha256
    where occurrence.id = occurrence_id_value
      and occurrence.calendar_idempotency_key =
          p_receipt->>'calendar_idempotency_key'
      and occurrence.canonical_evidence_sha256 =
          p_receipt->>'canonical_evidence_sha256'
      and occurrence.observed_at = observed_at_value
      and occurrence.observation_payload = p_session
      and revision.revision = (p_receipt->>'revision')::bigint
  ) then
    raise exception 'kr_calendar_collection_job_collection_scope_mismatch'
      using errcode = '22023';
  end if;
  if exists (
    select 1
    from private.kr_calendar_collection_attempt_ledger as event
    where event.job_id = job_id_value
      and event.event_kind = 'confirmed'
      and (
        event.receipt_payload->>'occurrence_id' = occurrence_id_value::text
        or event.receipt_payload->>'calendar_idempotency_key' =
           p_receipt->>'calendar_idempotency_key'
      )
  ) then
    raise exception 'kr_calendar_collection_job_collection_scope_mismatch'
      using errcode = '23505';
  end if;

  select count(*)::integer into confirmed_count
  from private.kr_calendar_collection_attempt_ledger as event
  where event.job_id = job_id_value and event.event_kind = 'confirmed';
  if confirmed_count >= job.total_days
     or p_target_date <> job.start_date + confirmed_count then
    raise exception 'kr_calendar_collection_job_collection_scope_mismatch'
      using errcode = '22023';
  end if;
  new_revision := p_expected_revision + 1;
  insert into private.kr_calendar_collection_attempt_ledger (
    job_id, job_revision, attempt_id, holder_id, target_date,
    fencing_revision, begun_at, event_kind, session_payload,
    receipt_payload, occurred_at, event_sha256
  ) values (
    job_id_value, new_revision, attempt_id_value, holder_id_value,
    p_target_date, p_expected_revision, job.active_begun_at, 'confirmed',
    p_session, p_receipt, p_now,
    private.kr_calendar_collection_event_sha256_v1(
      job_id_value, new_revision, attempt_id_value, holder_id_value,
      p_target_date, p_expected_revision, job.active_begun_at,
      'confirmed', null, p_session, p_receipt, p_now
    )
  );
  completed_value := confirmed_count + 1 = job.total_days;
  if completed_value then
    manifest_value :=
      private.kr_calendar_collection_manifest_sha256_v1(job_id_value);
  end if;
  update private.kr_calendar_collection_jobs
  set revision = new_revision,
      state = case when completed_value then 'completed' else 'ready' end,
      active_attempt_id = null,
      active_holder_id = null,
      active_target_date = null,
      active_fencing_revision = null,
      active_begun_at = null,
      state_reason = null,
      terminal_manifest_sha256 = manifest_value,
      updated_at = p_now
  where job_id = job_id_value;
  return query select private.kr_calendar_collection_snapshot_v1(job_id_value);
end;
$$;

create or replace function worker_api.load_or_create_kr_calendar_collection_job_v1(
  p_spec jsonb,
  p_now timestamptz
)
returns table(snapshot jsonb)
language sql
volatile
security invoker
set search_path = ''
as $$
  select *
  from private.load_or_create_kr_calendar_collection_job_v1_impl(p_spec, p_now);
$$;

create or replace function worker_api.begin_kr_calendar_collection_date_attempt_v1(
  p_job_id text,
  p_spec_sha256 text,
  p_expected_revision bigint,
  p_attempt_id text,
  p_holder_id text,
  p_target_date date,
  p_now timestamptz
)
returns table(snapshot jsonb)
language sql
volatile
security invoker
set search_path = ''
as $$
  select *
  from private.begin_kr_calendar_collection_date_attempt_v1_impl(
    p_job_id, p_spec_sha256, p_expected_revision, p_attempt_id, p_holder_id,
    p_target_date, p_now
  );
$$;

create or replace function worker_api.pause_kr_calendar_collection_date_attempt_v1(
  p_job_id text,
  p_spec_sha256 text,
  p_expected_revision bigint,
  p_attempt_id text,
  p_holder_id text,
  p_target_date date,
  p_reason_code text,
  p_now timestamptz
)
returns table(snapshot jsonb)
language sql
volatile
security invoker
set search_path = ''
as $$
  select *
  from private.pause_kr_calendar_collection_date_attempt_v1_impl(
    p_job_id, p_spec_sha256, p_expected_revision, p_attempt_id, p_holder_id,
    p_target_date, p_reason_code, p_now
  );
$$;

create or replace function worker_api.block_kr_calendar_collection_date_attempt_v1(
  p_job_id text,
  p_spec_sha256 text,
  p_expected_revision bigint,
  p_attempt_id text,
  p_holder_id text,
  p_target_date date,
  p_reason_code text,
  p_now timestamptz
)
returns table(snapshot jsonb)
language sql
volatile
security invoker
set search_path = ''
as $$
  select *
  from private.block_kr_calendar_collection_date_attempt_v1_impl(
    p_job_id, p_spec_sha256, p_expected_revision, p_attempt_id, p_holder_id,
    p_target_date, p_reason_code, p_now
  );
$$;

create or replace function worker_api.confirm_kr_calendar_collection_date_v1(
  p_job_id text,
  p_spec_sha256 text,
  p_expected_revision bigint,
  p_attempt_id text,
  p_holder_id text,
  p_target_date date,
  p_session jsonb,
  p_receipt jsonb,
  p_now timestamptz
)
returns table(snapshot jsonb)
language sql
volatile
security invoker
set search_path = ''
as $$
  select *
  from private.confirm_kr_calendar_collection_date_v1_impl(
    p_job_id, p_spec_sha256, p_expected_revision, p_attempt_id, p_holder_id,
    p_target_date, p_session, p_receipt, p_now
  );
$$;

revoke all on function
  private.kr_calendar_collection_uuid4_v1(text),
  private.kr_calendar_collection_canonical_json_v1(jsonb),
  private.kr_calendar_collection_spec_document_v1(jsonb),
  private.kr_calendar_collection_spec_sha256_v1(jsonb),
  private.kr_calendar_collection_checkpoints_v1(uuid),
  private.kr_calendar_collection_snapshot_v1(uuid),
  private.kr_calendar_collection_manifest_sha256_v1(uuid),
  private.kr_calendar_collection_event_sha256_v1(
    uuid,bigint,uuid,uuid,date,bigint,timestamptz,text,text,jsonb,jsonb,timestamptz
  ),
  private.load_or_create_kr_calendar_collection_job_v1_impl(jsonb,timestamptz),
  private.begin_kr_calendar_collection_date_attempt_v1_impl(
    text,text,bigint,text,text,date,timestamptz
  ),
  private.pause_kr_calendar_collection_date_attempt_v1_impl(
    text,text,bigint,text,text,date,text,timestamptz
  ),
  private.block_kr_calendar_collection_date_attempt_v1_impl(
    text,text,bigint,text,text,date,text,timestamptz
  ),
  private.confirm_kr_calendar_collection_date_v1_impl(
    text,text,bigint,text,text,date,jsonb,jsonb,timestamptz
  ),
  worker_api.load_or_create_kr_calendar_collection_job_v1(jsonb,timestamptz),
  worker_api.begin_kr_calendar_collection_date_attempt_v1(
    text,text,bigint,text,text,date,timestamptz
  ),
  worker_api.pause_kr_calendar_collection_date_attempt_v1(
    text,text,bigint,text,text,date,text,timestamptz
  ),
  worker_api.block_kr_calendar_collection_date_attempt_v1(
    text,text,bigint,text,text,date,text,timestamptz
  ),
  worker_api.confirm_kr_calendar_collection_date_v1(
    text,text,bigint,text,text,date,jsonb,jsonb,timestamptz
  )
from public, anon, authenticated, authenticator, service_role;

grant execute on function
  private.load_or_create_kr_calendar_collection_job_v1_impl(jsonb,timestamptz),
  private.begin_kr_calendar_collection_date_attempt_v1_impl(
    text,text,bigint,text,text,date,timestamptz
  ),
  private.pause_kr_calendar_collection_date_attempt_v1_impl(
    text,text,bigint,text,text,date,text,timestamptz
  ),
  private.block_kr_calendar_collection_date_attempt_v1_impl(
    text,text,bigint,text,text,date,text,timestamptz
  ),
  private.confirm_kr_calendar_collection_date_v1_impl(
    text,text,bigint,text,text,date,jsonb,jsonb,timestamptz
  ),
  worker_api.load_or_create_kr_calendar_collection_job_v1(jsonb,timestamptz),
  worker_api.begin_kr_calendar_collection_date_attempt_v1(
    text,text,bigint,text,text,date,timestamptz
  ),
  worker_api.pause_kr_calendar_collection_date_attempt_v1(
    text,text,bigint,text,text,date,text,timestamptz
  ),
  worker_api.block_kr_calendar_collection_date_attempt_v1(
    text,text,bigint,text,text,date,text,timestamptz
  ),
  worker_api.confirm_kr_calendar_collection_date_v1(
    text,text,bigint,text,text,date,jsonb,jsonb,timestamptz
  )
to service_role;

do $$
declare
  table_contract_count bigint;
  policy_count bigint;
  forbidden_table_acl_count bigint;
  impl_contract_count bigint;
  wrapper_contract_count bigint;
  helper_service_acl_count bigint;
  forbidden_function_acl_count bigint;
  function_owner_count bigint;
  function_count bigint;
begin
  select count(*) into table_contract_count
  from pg_catalog.pg_class as relation
  where relation.oid in (
      'private.kr_calendar_collection_jobs'::regclass,
      'private.kr_calendar_collection_attempt_ledger'::regclass
    )
    and relation.relrowsecurity and relation.relforcerowsecurity;
  select count(*) into policy_count
  from pg_catalog.pg_policy as policy
  where policy.polrelid in (
    'private.kr_calendar_collection_jobs'::regclass,
    'private.kr_calendar_collection_attempt_ledger'::regclass
  );
  select count(*) into forbidden_table_acl_count
  from pg_catalog.pg_class as relation
  cross join lateral pg_catalog.aclexplode(
    coalesce(relation.relacl, pg_catalog.acldefault('r', relation.relowner))
  ) as acl
  left join pg_catalog.pg_roles as grantee on grantee.oid = acl.grantee
  where relation.oid in (
      'private.kr_calendar_collection_jobs'::regclass,
      'private.kr_calendar_collection_attempt_ledger'::regclass
    )
    and (
      acl.grantee = 0
      or grantee.rolname in ('anon','authenticated','authenticator','service_role')
    );
  select count(*) into impl_contract_count
  from pg_catalog.pg_proc as procedure
  where procedure.oid in (
      'private.load_or_create_kr_calendar_collection_job_v1_impl(jsonb,timestamptz)'::regprocedure,
      'private.begin_kr_calendar_collection_date_attempt_v1_impl(text,text,bigint,text,text,date,timestamptz)'::regprocedure,
      'private.pause_kr_calendar_collection_date_attempt_v1_impl(text,text,bigint,text,text,date,text,timestamptz)'::regprocedure,
      'private.block_kr_calendar_collection_date_attempt_v1_impl(text,text,bigint,text,text,date,text,timestamptz)'::regprocedure,
      'private.confirm_kr_calendar_collection_date_v1_impl(text,text,bigint,text,text,date,jsonb,jsonb,timestamptz)'::regprocedure
    )
    and procedure.prosecdef
    and procedure.provolatile = 'v'
    and procedure.proconfig = array['search_path=""']::text[];
  select count(*) into wrapper_contract_count
  from pg_catalog.pg_proc as procedure
  where procedure.oid in (
      'worker_api.load_or_create_kr_calendar_collection_job_v1(jsonb,timestamptz)'::regprocedure,
      'worker_api.begin_kr_calendar_collection_date_attempt_v1(text,text,bigint,text,text,date,timestamptz)'::regprocedure,
      'worker_api.pause_kr_calendar_collection_date_attempt_v1(text,text,bigint,text,text,date,text,timestamptz)'::regprocedure,
      'worker_api.block_kr_calendar_collection_date_attempt_v1(text,text,bigint,text,text,date,text,timestamptz)'::regprocedure,
      'worker_api.confirm_kr_calendar_collection_date_v1(text,text,bigint,text,text,date,jsonb,jsonb,timestamptz)'::regprocedure
    )
    and not procedure.prosecdef
    and procedure.provolatile = 'v'
    and procedure.proconfig = array['search_path=""']::text[];
  select count(distinct procedure.proowner), count(*)
  into function_owner_count, function_count
  from pg_catalog.pg_proc as procedure
  where procedure.oid in (
      'private.load_or_create_kr_calendar_collection_job_v1_impl(jsonb,timestamptz)'::regprocedure,
      'private.begin_kr_calendar_collection_date_attempt_v1_impl(text,text,bigint,text,text,date,timestamptz)'::regprocedure,
      'private.pause_kr_calendar_collection_date_attempt_v1_impl(text,text,bigint,text,text,date,text,timestamptz)'::regprocedure,
      'private.block_kr_calendar_collection_date_attempt_v1_impl(text,text,bigint,text,text,date,text,timestamptz)'::regprocedure,
      'private.confirm_kr_calendar_collection_date_v1_impl(text,text,bigint,text,text,date,jsonb,jsonb,timestamptz)'::regprocedure,
      'worker_api.load_or_create_kr_calendar_collection_job_v1(jsonb,timestamptz)'::regprocedure,
      'worker_api.begin_kr_calendar_collection_date_attempt_v1(text,text,bigint,text,text,date,timestamptz)'::regprocedure,
      'worker_api.pause_kr_calendar_collection_date_attempt_v1(text,text,bigint,text,text,date,text,timestamptz)'::regprocedure,
      'worker_api.block_kr_calendar_collection_date_attempt_v1(text,text,bigint,text,text,date,text,timestamptz)'::regprocedure,
      'worker_api.confirm_kr_calendar_collection_date_v1(text,text,bigint,text,text,date,jsonb,jsonb,timestamptz)'::regprocedure
    );
  select count(*) into helper_service_acl_count
  from pg_catalog.pg_proc as procedure
  where procedure.oid in (
      'private.kr_calendar_collection_uuid4_v1(text)'::regprocedure,
      'private.kr_calendar_collection_canonical_json_v1(jsonb)'::regprocedure,
      'private.kr_calendar_collection_spec_document_v1(jsonb)'::regprocedure,
      'private.kr_calendar_collection_spec_sha256_v1(jsonb)'::regprocedure,
      'private.kr_calendar_collection_checkpoints_v1(uuid)'::regprocedure,
      'private.kr_calendar_collection_snapshot_v1(uuid)'::regprocedure,
      'private.kr_calendar_collection_manifest_sha256_v1(uuid)'::regprocedure
    )
    and pg_catalog.has_function_privilege('service_role', procedure.oid, 'EXECUTE');
  select count(*) into forbidden_function_acl_count
  from pg_catalog.pg_proc as procedure
  cross join lateral pg_catalog.aclexplode(
    coalesce(procedure.proacl, pg_catalog.acldefault('f', procedure.proowner))
  ) as acl
  left join pg_catalog.pg_roles as grantee on grantee.oid = acl.grantee
  where procedure.oid in (
      'private.load_or_create_kr_calendar_collection_job_v1_impl(jsonb,timestamptz)'::regprocedure,
      'private.begin_kr_calendar_collection_date_attempt_v1_impl(text,text,bigint,text,text,date,timestamptz)'::regprocedure,
      'private.pause_kr_calendar_collection_date_attempt_v1_impl(text,text,bigint,text,text,date,text,timestamptz)'::regprocedure,
      'private.block_kr_calendar_collection_date_attempt_v1_impl(text,text,bigint,text,text,date,text,timestamptz)'::regprocedure,
      'private.confirm_kr_calendar_collection_date_v1_impl(text,text,bigint,text,text,date,jsonb,jsonb,timestamptz)'::regprocedure,
      'worker_api.load_or_create_kr_calendar_collection_job_v1(jsonb,timestamptz)'::regprocedure,
      'worker_api.begin_kr_calendar_collection_date_attempt_v1(text,text,bigint,text,text,date,timestamptz)'::regprocedure,
      'worker_api.pause_kr_calendar_collection_date_attempt_v1(text,text,bigint,text,text,date,text,timestamptz)'::regprocedure,
      'worker_api.block_kr_calendar_collection_date_attempt_v1(text,text,bigint,text,text,date,text,timestamptz)'::regprocedure,
      'worker_api.confirm_kr_calendar_collection_date_v1(text,text,bigint,text,text,date,jsonb,jsonb,timestamptz)'::regprocedure
    )
    and acl.privilege_type = 'EXECUTE'
    and (
      acl.grantee = 0
      or grantee.rolname in ('anon','authenticated','authenticator')
    );
  if table_contract_count <> 2
     or policy_count <> 0
     or forbidden_table_acl_count <> 0
     or impl_contract_count <> 5
     or wrapper_contract_count <> 5
     or function_owner_count <> 1
     or function_count <> 10
     or helper_service_acl_count <> 0
     or forbidden_function_acl_count <> 0
     or exists (
       select 1
       from pg_catalog.pg_proc as procedure
       join pg_catalog.pg_roles as role on role.oid = procedure.proowner
       where procedure.oid =
         'private.load_or_create_kr_calendar_collection_job_v1_impl(jsonb,timestamptz)'::regprocedure
         and role.rolname in ('anon','authenticated','authenticator','service_role')
     )
     or not pg_catalog.has_function_privilege(
       'service_role',
       'worker_api.load_or_create_kr_calendar_collection_job_v1(jsonb,timestamptz)',
       'EXECUTE'
     ) then
    raise exception 'kr_calendar_collection_job_store_security_contract_failed'
      using errcode = '55000';
  end if;
end;
$$;

commit;
