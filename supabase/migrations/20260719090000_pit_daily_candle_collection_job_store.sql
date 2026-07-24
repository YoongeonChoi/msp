begin;

set local lock_timeout = '5s';
set local statement_timeout = '90s';

-- Durable, manually driven collection of at most one PIT daily candle.
-- A candidate is fenced before an append may start. Once fenced there is
-- deliberately no lease expiry, takeover, reset, or automatic retry path.
do $$
begin
  if to_regprocedure('private.require_service_role()') is null
     or to_regprocedure('private.jsonb_exact_keys_v1(jsonb,text[])') is null
     or to_regprocedure('private.reject_append_only_mutation()') is null
     or to_regprocedure('private.pit_canonical_timestamp_v1(timestamptz)') is null
     or to_regprocedure('private.pit_sha256_text_v1(text)') is null
     or to_regprocedure(
       'private.pit_candle_identity_sha256_v1(text,text,text,text,boolean,timestamptz)'
     ) is null
     or to_regprocedure(
       'private.pit_candle_observation_sha256_v1(text,text,text,text,boolean,timestamptz,text,numeric,numeric,numeric,numeric,numeric,text)'
     ) is null
     or to_regclass('private.pit_candle_observation_revisions') is null
     or to_regclass('private.pit_candle_observation_occurrences') is null then
    raise exception 'pit_daily_candle_collection_job_store_dependency_missing'
      using errcode = '55000';
  end if;
end;
$$;

create table private.pit_daily_candle_collection_jobs (
  job_id uuid primary key,
  spec_sha256 text not null check (spec_sha256 ~ '^[0-9a-f]{64}$'),
  spec jsonb not null check (pg_catalog.jsonb_typeof(spec) = 'object'),
  provider text not null check (provider ~ '^[a-z][a-z0-9._-]{0,63}$'),
  symbol text not null check (symbol ~ '^[0-9]{6}$'),
  market text not null check (market = 'KR'),
  interval text not null check (interval = '1d'),
  adjusted boolean not null,
  before_at timestamptz not null,
  requested_count integer not null check (requested_count = 1),
  provider_contract_sha256 text not null
    check (provider_contract_sha256 ~ '^[0-9a-f]{64}$'),
  revision bigint not null check (
    revision > 0 and revision <= 9223372036854775807
  ),
  state text not null check (state in (
    'ready', 'collecting', 'candidate_fenced', 'paused_retryable',
    'blocked_unknown', 'completed'
  )),
  active_attempt_id uuid,
  active_holder_id uuid,
  active_fencing_revision bigint,
  active_begun_at timestamptz,
  candidate_attempt_id uuid,
  candidate_holder_id uuid,
  candidate_fencing_revision bigint,
  candidate_begun_at timestamptz,
  candidate_idempotency_key text check (
    candidate_idempotency_key is null
    or candidate_idempotency_key ~ '^[0-9a-f]{64}$'
  ),
  candidate_canonical_observation_sha256 text check (
    candidate_canonical_observation_sha256 is null
    or candidate_canonical_observation_sha256 ~ '^[0-9a-f]{64}$'
  ),
  candidate_provider_event_at timestamptz,
  candidate_observed_at timestamptz,
  candidate_payload jsonb check (
    candidate_payload is null
    or pg_catalog.jsonb_typeof(candidate_payload) = 'object'
  ),
  candidate_fenced_at timestamptz,
  confirmed_occurrence_id uuid,
  confirmed_content_revision_id uuid,
  confirmed_content_revision bigint check (
    confirmed_content_revision is null or confirmed_content_revision > 0
  ),
  confirmed_at timestamptz,
  confirmation_receipt jsonb check (
    confirmation_receipt is null
    or pg_catalog.jsonb_typeof(confirmation_receipt) = 'object'
  ),
  state_reason text check (
    state_reason is null
    or state_reason in (
      'provider_read_failed_before_candidate',
      'provider_read_outcome_unknown_before_candidate',
      'unexpected_failure_before_candidate',
      'cancelled_before_candidate',
      'append_outcome_unknown',
      'confirm_outcome_unknown',
      'unexpected_failure_after_candidate',
      'cancelled_after_candidate'
    )
  ),
  created_at timestamptz not null,
  updated_at timestamptz not null,
  constraint pit_daily_candle_collection_completion_occurrence_fk
    foreign key (confirmed_occurrence_id, confirmed_content_revision_id)
    references private.pit_candle_observation_occurrences (
      id, content_revision_id
    ),
  constraint pit_daily_candle_collection_completion_revision_fk
    foreign key (
      confirmed_content_revision_id,
      candidate_idempotency_key,
      candidate_canonical_observation_sha256
    )
    references private.pit_candle_observation_revisions (
      id, idempotency_key, canonical_observation_sha256
    ),
  check (created_at <= updated_at),
  constraint pit_daily_candle_collection_revision_headroom_check check (
    (
      active_fencing_revision is null
      or (
        active_fencing_revision > 1
        and active_fencing_revision % 2 = 0
        and active_fencing_revision <= 9223372036854775804
      )
    )
    and (
      candidate_fencing_revision is null
      or (
        candidate_fencing_revision > 1
        and candidate_fencing_revision % 2 = 0
        and candidate_fencing_revision <= 9223372036854775804
      )
    )
    and (state <> 'collecting' or revision <= 9223372036854775804)
    and (state <> 'candidate_fenced' or revision <= 9223372036854775805)
    and (
      state <> 'paused_retryable'
      or (revision % 2 = 1 and revision <= 9223372036854775803)
    )
    and (state <> 'blocked_unknown' or revision <= 9223372036854775806)
    and (state <> 'completed' or revision <= 9223372036854775806)
  ),
  check (
    (state = 'ready' and revision = 1)
    or (state = 'collecting' and revision = active_fencing_revision)
    or (state = 'candidate_fenced'
        and revision = candidate_fencing_revision + 1)
    or (state = 'paused_retryable' and revision > 1)
    or (state = 'blocked_unknown'
        and candidate_fencing_revision is null
        and revision = active_fencing_revision + 1)
    or (state in ('blocked_unknown', 'completed')
        and candidate_fencing_revision is not null
        and revision = candidate_fencing_revision + 2)
  ),
  check (
    (state in ('collecting', 'candidate_fenced', 'blocked_unknown')
     and active_attempt_id is not null
     and active_holder_id is not null
     and active_fencing_revision is not null
     and active_begun_at is not null)
    or
    (state in ('ready', 'paused_retryable', 'completed')
     and active_attempt_id is null
     and active_holder_id is null
     and active_fencing_revision is null
     and active_begun_at is null)
  ),
  check (
    ((state in ('ready', 'collecting', 'paused_retryable')
      or (state = 'blocked_unknown'
          and candidate_fencing_revision is null))
     and candidate_attempt_id is null
     and candidate_holder_id is null
     and candidate_fencing_revision is null
     and candidate_begun_at is null
     and candidate_idempotency_key is null
     and candidate_canonical_observation_sha256 is null
     and candidate_provider_event_at is null
     and candidate_observed_at is null
     and candidate_payload is null
     and candidate_fenced_at is null)
    or
    (state in ('candidate_fenced', 'blocked_unknown',
               'completed')
     and candidate_attempt_id is not null
     and candidate_holder_id is not null
     and candidate_fencing_revision is not null
     and candidate_begun_at is not null
     and candidate_idempotency_key is not null
     and candidate_canonical_observation_sha256 is not null
     and candidate_provider_event_at is not null
     and candidate_observed_at is not null
     and candidate_payload is not null
     and candidate_fenced_at is not null
     and candidate_provider_event_at <= before_at
     and candidate_begun_at <= candidate_observed_at
     and candidate_observed_at <= candidate_fenced_at)
  ),
  check (
    candidate_fencing_revision is null
    or state not in ('candidate_fenced', 'blocked_unknown')
    or (
      active_attempt_id = candidate_attempt_id
      and active_holder_id = candidate_holder_id
      and active_fencing_revision = candidate_fencing_revision
      and active_begun_at = candidate_begun_at
    )
  ),
  check (
    (state = 'completed'
     and confirmed_occurrence_id is not null
     and confirmed_content_revision_id is not null
     and confirmed_content_revision is not null
     and confirmed_at is not null
     and confirmation_receipt is not null
     and candidate_fenced_at <= confirmed_at)
    or
    (state <> 'completed'
     and confirmed_occurrence_id is null
     and confirmed_content_revision_id is null
     and confirmed_content_revision is null
     and confirmed_at is null
     and confirmation_receipt is null)
  ),
  check (
    (state = 'paused_retryable'
     and state_reason = 'provider_read_failed_before_candidate')
    or (state = 'blocked_unknown'
        and (
          (candidate_fencing_revision is null
           and state_reason in (
             'provider_read_outcome_unknown_before_candidate',
             'unexpected_failure_before_candidate',
             'cancelled_before_candidate'
           ))
          or
          (candidate_fencing_revision is not null
           and state_reason in (
             'append_outcome_unknown',
             'confirm_outcome_unknown',
             'unexpected_failure_after_candidate',
             'cancelled_after_candidate'
           ))
        ))
    or (state not in ('paused_retryable', 'blocked_unknown')
        and state_reason is null)
  )
);

create table private.pit_daily_candle_collection_attempt_ledger (
  event_id bigint generated always as identity primary key,
  job_id uuid not null
    references private.pit_daily_candle_collection_jobs(job_id),
  job_revision bigint not null check (
    job_revision > 1 and job_revision <= 9223372036854775806
  ),
  attempt_id uuid not null,
  holder_id uuid not null,
  fencing_revision bigint not null check (
    fencing_revision > 1
    and fencing_revision % 2 = 0
    and fencing_revision <= 9223372036854775804
  ),
  begun_at timestamptz not null,
  event_kind text not null check (event_kind in (
    'begun', 'candidate_fenced', 'paused_retryable',
    'blocked_unknown', 'confirmed'
  )),
  candidate_idempotency_key text check (
    candidate_idempotency_key is null
    or candidate_idempotency_key ~ '^[0-9a-f]{64}$'
  ),
  candidate_canonical_observation_sha256 text check (
    candidate_canonical_observation_sha256 is null
    or candidate_canonical_observation_sha256 ~ '^[0-9a-f]{64}$'
  ),
  candidate_observed_at timestamptz,
  candidate_payload jsonb,
  receipt_payload jsonb,
  occurrence_id uuid,
  content_revision_id uuid,
  content_revision bigint check (
    content_revision is null or content_revision > 0
  ),
  reason_code text check (
    reason_code is null
    or reason_code in (
      'provider_read_failed_before_candidate',
      'provider_read_outcome_unknown_before_candidate',
      'unexpected_failure_before_candidate',
      'cancelled_before_candidate',
      'append_outcome_unknown',
      'confirm_outcome_unknown',
      'unexpected_failure_after_candidate',
      'cancelled_after_candidate'
    )
  ),
  occurred_at timestamptz not null,
  event_sha256 text not null check (event_sha256 ~ '^[0-9a-f]{64}$'),
  unique (job_id, job_revision),
  unique (attempt_id, event_kind),
  check (begun_at <= occurred_at),
  check (
    (event_kind = 'begun' and job_revision = fencing_revision)
    or (event_kind in ('candidate_fenced', 'paused_retryable')
        and job_revision = fencing_revision + 1)
    or (event_kind = 'blocked_unknown'
        and candidate_payload is null
        and job_revision = fencing_revision + 1)
    or (event_kind in ('blocked_unknown', 'confirmed')
        and candidate_payload is not null
        and job_revision = fencing_revision + 2)
  ),
  check (
    ((event_kind in ('begun', 'paused_retryable')
      or (event_kind = 'blocked_unknown' and candidate_payload is null))
     and candidate_idempotency_key is null
     and candidate_canonical_observation_sha256 is null
     and candidate_observed_at is null
     and candidate_payload is null)
    or
    ((event_kind in ('candidate_fenced', 'confirmed')
      or (event_kind = 'blocked_unknown' and candidate_payload is not null))
     and candidate_idempotency_key is not null
     and candidate_canonical_observation_sha256 is not null
     and candidate_observed_at is not null
     and candidate_payload is not null)
  ),
  check (
    (event_kind = 'confirmed'
     and receipt_payload is not null
     and occurrence_id is not null
     and content_revision_id is not null
     and content_revision is not null)
    or
    (event_kind <> 'confirmed'
     and receipt_payload is null
     and occurrence_id is null
     and content_revision_id is null
     and content_revision is null)
  ),
  check (
    (event_kind = 'paused_retryable'
     and reason_code = 'provider_read_failed_before_candidate')
    or (event_kind = 'blocked_unknown'
        and (
          (candidate_payload is null
           and reason_code in (
             'provider_read_outcome_unknown_before_candidate',
             'unexpected_failure_before_candidate',
             'cancelled_before_candidate'
           ))
          or
          (candidate_payload is not null
           and reason_code in (
             'append_outcome_unknown',
             'confirm_outcome_unknown',
             'unexpected_failure_after_candidate',
             'cancelled_after_candidate'
           ))
        ))
    or (event_kind not in ('paused_retryable', 'blocked_unknown')
        and reason_code is null)
  )
);

create unique index pit_daily_candle_collection_attempt_begin_once_idx
  on private.pit_daily_candle_collection_attempt_ledger (attempt_id)
  where event_kind = 'begun';

create index pit_daily_candle_collection_attempt_timeline_idx
  on private.pit_daily_candle_collection_attempt_ledger (
    job_id, job_revision, event_id
  );

create trigger reject_pit_daily_candle_collection_attempt_mutation
before update or delete on private.pit_daily_candle_collection_attempt_ledger
for each row execute function private.reject_append_only_mutation();

alter table private.pit_daily_candle_collection_jobs enable row level security;
alter table private.pit_daily_candle_collection_jobs force row level security;
alter table private.pit_daily_candle_collection_attempt_ledger
  enable row level security;
alter table private.pit_daily_candle_collection_attempt_ledger
  force row level security;

revoke all on table
  private.pit_daily_candle_collection_jobs,
  private.pit_daily_candle_collection_attempt_ledger
from public, anon, authenticated, authenticator, service_role;

revoke all on sequence
  private.pit_daily_candle_collection_attempt_ledger_event_id_seq
from public, anon, authenticated, authenticator, service_role;

create or replace function private.pit_daily_candle_collection_uuid4_v1(
  p_value text
)
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

create or replace function private.pit_daily_candle_collection_canonical_json_v1(
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
          private.pit_daily_candle_collection_canonical_json_v1(item.value),
          ',' order by item.key collate "C"
        ),
        ''
      ) || '}'
      into rendered
      from pg_catalog.jsonb_each(p_value) as item;
    when 'array' then
      select '[' || coalesce(
        pg_catalog.string_agg(
          private.pit_daily_candle_collection_canonical_json_v1(item.value),
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

create or replace function private.pit_daily_candle_collection_spec_document_v1(
  p_spec jsonb
)
returns jsonb
language sql
immutable
strict
security definer
set search_path = ''
as $$
  select p_spec;
$$;

create or replace function private.pit_daily_candle_collection_spec_sha256_v1(
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
    private.pit_daily_candle_collection_canonical_json_v1(
      private.pit_daily_candle_collection_spec_document_v1(p_spec)
    )
  );
$$;

create or replace function private.pit_daily_candle_collection_snapshot_v1(
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
  job private.pit_daily_candle_collection_jobs%rowtype;
  active_attempt jsonb;
  candidate jsonb;
  completion jsonb;
begin
  select * into strict job
  from private.pit_daily_candle_collection_jobs as stored
  where stored.job_id = p_job_id;

  active_attempt := case
    when job.active_attempt_id is null then null::jsonb
    else pg_catalog.jsonb_build_object(
      'attempt_id', job.active_attempt_id::text,
      'holder_id', job.active_holder_id::text,
      'fencing_revision', job.active_fencing_revision,
      'begun_at', private.pit_canonical_timestamp_v1(job.active_begun_at)
    )
  end;
  candidate := case
    when job.candidate_attempt_id is null then null::jsonb
    else pg_catalog.jsonb_build_object(
      'attempt_id', job.candidate_attempt_id::text,
      'holder_id', job.candidate_holder_id::text,
      'fencing_revision', job.candidate_fencing_revision,
      'begun_at', private.pit_canonical_timestamp_v1(job.candidate_begun_at),
      'idempotency_key', job.candidate_idempotency_key,
      'canonical_observation_sha256',
        job.candidate_canonical_observation_sha256,
      'candle', job.candidate_payload,
      'fenced_at',
        private.pit_canonical_timestamp_v1(job.candidate_fenced_at)
    )
  end;
  completion := case
    when job.confirmed_occurrence_id is null then null::jsonb
    else pg_catalog.jsonb_build_object(
      'persistence_kind', 'durable',
      'occurrence_id', job.confirmed_occurrence_id::text,
      'content_revision_id', job.confirmed_content_revision_id::text,
      'occurrence_observed_at',
        private.pit_canonical_timestamp_v1(job.candidate_observed_at),
      'content_revision', job.confirmed_content_revision,
      'content_revision_observed_at',
        job.confirmation_receipt->>'stored_observed_at',
      'idempotency_key', job.candidate_idempotency_key,
      'canonical_observation_sha256',
        job.candidate_canonical_observation_sha256,
      'receipt', job.confirmation_receipt,
      'confirmed_at', private.pit_canonical_timestamp_v1(job.confirmed_at)
    )
  end;

  return pg_catalog.jsonb_build_object(
    'schema_version', 'daily_candle_collection_job_snapshot.v1',
    'spec_sha256', job.spec_sha256,
    'spec', job.spec,
    'revision', job.revision,
    'state', job.state,
    'active_attempt', active_attempt,
    'candidate', candidate,
    'completion', completion,
    'state_reason', job.state_reason,
    'created_at', private.pit_canonical_timestamp_v1(job.created_at),
    'updated_at', private.pit_canonical_timestamp_v1(job.updated_at),
    'automatic_retry_allowed', false
  );
exception
  when no_data_found then
    raise exception 'pit_daily_candle_collection_job_not_found'
      using errcode = 'P0002';
end;
$$;

create or replace function private.pit_daily_candle_collection_event_sha256_v1(
  p_job_id uuid,
  p_job_revision bigint,
  p_attempt_id uuid,
  p_holder_id uuid,
  p_fencing_revision bigint,
  p_event_kind text,
  p_candidate_payload jsonb,
  p_receipt_payload jsonb,
  p_reason_code text,
  p_occurred_at timestamptz
)
returns text
language sql
immutable
security definer
set search_path = ''
as $$
  select private.pit_sha256_text_v1(
    private.pit_daily_candle_collection_canonical_json_v1(
      pg_catalog.jsonb_build_object(
        'job_id', p_job_id::text,
        'job_revision', p_job_revision,
        'attempt_id', p_attempt_id::text,
        'holder_id', p_holder_id::text,
        'fencing_revision', p_fencing_revision,
        'event_kind', p_event_kind,
        'candidate', p_candidate_payload,
        'receipt', p_receipt_payload,
        'reason_code', p_reason_code,
        'occurred_at', private.pit_canonical_timestamp_v1(p_occurred_at)
      )
    )
  );
$$;

create or replace function private.load_or_create_pit_daily_candle_collection_job_v1_impl(
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
  before_value timestamptz;
  adjusted_value boolean;
  spec_sha256_value text;
  existing private.pit_daily_candle_collection_jobs%rowtype;
begin
  perform private.require_service_role();
  if p_spec is null
     or pg_catalog.jsonb_typeof(p_spec) <> 'object'
     or pg_catalog.octet_length(p_spec::text) > 2048
     or private.jsonb_exact_keys_v1(
       p_spec,
       array[
         'schema_version', 'job_id', 'provider', 'symbol', 'market',
         'interval', 'adjusted', 'before', 'count', 'trigger',
         'provider_contract_sha256', 'pagination_allowed',
         'automatic_retry_allowed'
       ]
     ) is distinct from true
     or pg_catalog.jsonb_typeof(p_spec->'schema_version') <> 'string'
     or p_spec->>'schema_version'
       <> 'daily_candle_collection_job.v1'
     or pg_catalog.jsonb_typeof(p_spec->'job_id') <> 'string'
     or not private.pit_daily_candle_collection_uuid4_v1(
       p_spec->>'job_id'
     )
     or pg_catalog.jsonb_typeof(p_spec->'provider') <> 'string'
     or (p_spec->>'provider') !~ '^[a-z][a-z0-9._-]{0,63}$'
     or pg_catalog.jsonb_typeof(p_spec->'symbol') <> 'string'
     or (p_spec->>'symbol') !~ '^[0-9]{6}$'
     or pg_catalog.jsonb_typeof(p_spec->'market') <> 'string'
     or p_spec->>'market' <> 'KR'
     or pg_catalog.jsonb_typeof(p_spec->'interval') <> 'string'
     or p_spec->>'interval' <> '1d'
     or pg_catalog.jsonb_typeof(p_spec->'adjusted') <> 'boolean'
     or pg_catalog.jsonb_typeof(p_spec->'before') <> 'string'
     or pg_catalog.jsonb_typeof(p_spec->'count') <> 'number'
     or p_spec->>'count' <> '1'
     or pg_catalog.jsonb_typeof(p_spec->'pagination_allowed') <> 'boolean'
     or p_spec->>'pagination_allowed' <> 'false'
     or pg_catalog.jsonb_typeof(p_spec->'automatic_retry_allowed')
       <> 'boolean'
     or p_spec->>'automatic_retry_allowed' <> 'false'
     or pg_catalog.jsonb_typeof(p_spec->'trigger') <> 'string'
     or p_spec->>'trigger' <> 'manual'
     or pg_catalog.jsonb_typeof(p_spec->'provider_contract_sha256')
       <> 'string'
     or (p_spec->>'provider_contract_sha256') !~ '^[0-9a-f]{64}$'
     or p_now is null
     or not pg_catalog.isfinite(p_now) then
    raise exception 'pit_daily_candle_collection_job_argument_invalid'
      using errcode = '22023';
  end if;

  begin
    job_id_value := (p_spec->>'job_id')::uuid;
    adjusted_value := (p_spec->>'adjusted')::boolean;
    before_value := (p_spec->>'before')::timestamptz;
  exception
    when others then
      raise exception 'pit_daily_candle_collection_job_argument_invalid'
        using errcode = '22023';
  end;
  if not pg_catalog.isfinite(before_value)
     or private.pit_canonical_timestamp_v1(before_value)
       <> p_spec->>'before' then
    raise exception 'pit_daily_candle_collection_job_argument_invalid'
      using errcode = '22023';
  end if;

  normalized_spec := pg_catalog.jsonb_build_object(
    'schema_version', 'daily_candle_collection_job.v1',
    'job_id', job_id_value::text,
    'provider', p_spec->>'provider',
    'symbol', p_spec->>'symbol',
    'market', 'KR',
    'interval', '1d',
    'adjusted', adjusted_value,
    'before', private.pit_canonical_timestamp_v1(before_value),
    'count', 1,
    'pagination_allowed', false,
    'automatic_retry_allowed', false,
    'trigger', 'manual',
    'provider_contract_sha256', p_spec->>'provider_contract_sha256'
  );
  spec_sha256_value :=
    private.pit_daily_candle_collection_spec_sha256_v1(normalized_spec);

  perform pg_catalog.pg_advisory_xact_lock(
    pg_catalog.hashtextextended(job_id_value::text, 19090000)
  );
  select * into existing
  from private.pit_daily_candle_collection_jobs as job
  where job.job_id = job_id_value
  for update;

  if found then
    if existing.spec_sha256 <> spec_sha256_value
       or existing.spec <> normalized_spec then
      raise exception 'pit_daily_candle_collection_job_spec_conflict'
        using errcode = '23505';
    end if;
  else
    insert into private.pit_daily_candle_collection_jobs (
      job_id, spec_sha256, spec, provider, symbol, market, interval,
      adjusted, before_at, requested_count, provider_contract_sha256,
      revision, state, created_at, updated_at
    ) values (
      job_id_value, spec_sha256_value, normalized_spec,
      p_spec->>'provider', p_spec->>'symbol', 'KR', '1d', adjusted_value,
      before_value, 1, p_spec->>'provider_contract_sha256',
      1, 'ready', p_now, p_now
    );
  end if;

  return query
  select private.pit_daily_candle_collection_snapshot_v1(job_id_value);
end;
$$;

create or replace function private.inspect_pit_daily_candle_collection_job_v1_impl(
  p_job_id text
)
returns table(found boolean, snapshot jsonb)
language plpgsql
stable
security definer
set search_path = ''
as $$
declare
  job_id_value uuid;
begin
  perform private.require_service_role();
  if not coalesce(
       private.pit_daily_candle_collection_uuid4_v1(p_job_id), false
     ) then
    raise exception 'pit_daily_candle_collection_job_argument_invalid'
      using errcode = '22023';
  end if;
  job_id_value := p_job_id::uuid;
  if exists (
    select 1
    from private.pit_daily_candle_collection_jobs as job
    where job.job_id = job_id_value
  ) then
    return query select
      true,
      private.pit_daily_candle_collection_snapshot_v1(job_id_value);
  else
    return query select false, null::jsonb;
  end if;
end;
$$;

create or replace function private.begin_pit_daily_candle_collection_attempt_v1_impl(
  p_job_id text,
  p_spec_sha256 text,
  p_expected_revision bigint,
  p_attempt_id text,
  p_holder_id text,
  p_now timestamptz
)
returns table(snapshot jsonb)
language plpgsql
volatile
security definer
set search_path = ''
as $$
declare
  job private.pit_daily_candle_collection_jobs%rowtype;
  job_id_value uuid;
  attempt_id_value uuid;
  holder_id_value uuid;
  new_revision bigint;
begin
  perform private.require_service_role();
  if not coalesce(
       private.pit_daily_candle_collection_uuid4_v1(p_job_id), false
     )
     or p_spec_sha256 is null
     or p_spec_sha256 !~ '^[0-9a-f]{64}$'
     or p_expected_revision is null
     or p_expected_revision <= 0
     or p_expected_revision >= 9223372036854775807
     or not coalesce(
       private.pit_daily_candle_collection_uuid4_v1(p_attempt_id), false
     )
     or not coalesce(
       private.pit_daily_candle_collection_uuid4_v1(p_holder_id), false
     )
     or p_now is null
     or not pg_catalog.isfinite(p_now) then
    raise exception 'pit_daily_candle_collection_job_argument_invalid'
      using errcode = '22023';
  end if;
  if p_expected_revision > 9223372036854775803 then
    raise exception 'pit_daily_candle_collection_job_revision_exhausted'
      using errcode = '54000';
  end if;
  job_id_value := p_job_id::uuid;
  attempt_id_value := p_attempt_id::uuid;
  holder_id_value := p_holder_id::uuid;

  select * into job
  from private.pit_daily_candle_collection_jobs as stored
  where stored.job_id = job_id_value
  for update;
  if not found then
    raise exception 'pit_daily_candle_collection_job_not_found'
      using errcode = 'P0002';
  end if;
  if job.spec_sha256 <> p_spec_sha256 then
    raise exception 'pit_daily_candle_collection_job_spec_hash_mismatch'
      using errcode = '40001';
  end if;
  if job.revision <> p_expected_revision then
    raise exception 'pit_daily_candle_collection_job_revision_conflict'
      using errcode = '40001';
  end if;
  if p_now < job.updated_at then
    raise exception 'pit_daily_candle_collection_job_clock_regressed'
      using errcode = '40001';
  end if;
  if job.state not in ('ready', 'paused_retryable') then
    raise exception 'pit_daily_candle_collection_job_begin_state_invalid'
      using errcode = '55000';
  end if;
  if exists (
    select 1
    from private.pit_daily_candle_collection_attempt_ledger as event
    where event.attempt_id = attempt_id_value
  ) then
    raise exception 'pit_daily_candle_collection_job_attempt_reused'
      using errcode = '23505';
  end if;

  new_revision := p_expected_revision + 1;
  insert into private.pit_daily_candle_collection_attempt_ledger (
    job_id, job_revision, attempt_id, holder_id, fencing_revision,
    begun_at, event_kind, occurred_at, event_sha256
  ) values (
    job_id_value, new_revision, attempt_id_value, holder_id_value,
    new_revision, p_now, 'begun', p_now,
    private.pit_daily_candle_collection_event_sha256_v1(
      job_id_value, new_revision, attempt_id_value, holder_id_value,
      new_revision, 'begun', null, null, null, p_now
    )
  );
  update private.pit_daily_candle_collection_jobs
  set revision = new_revision,
      state = 'collecting',
      active_attempt_id = attempt_id_value,
      active_holder_id = holder_id_value,
      active_fencing_revision = new_revision,
      active_begun_at = p_now,
      state_reason = null,
      updated_at = p_now
  where job_id = job_id_value
    and spec_sha256 = p_spec_sha256
    and revision = p_expected_revision
    and state in ('ready', 'paused_retryable');
  if not found then
    raise exception 'pit_daily_candle_collection_job_revision_conflict'
      using errcode = '40001';
  end if;

  return query
  select private.pit_daily_candle_collection_snapshot_v1(job_id_value);
end;
$$;

create or replace function private.fence_pit_daily_candle_collection_candidate_v1_impl(
  p_job_id text,
  p_spec_sha256 text,
  p_expected_revision bigint,
  p_attempt_id text,
  p_holder_id text,
  p_fencing_revision bigint,
  p_candidate jsonb,
  p_now timestamptz
)
returns table(snapshot jsonb)
language plpgsql
volatile
security definer
set search_path = ''
as $$
declare
  job private.pit_daily_candle_collection_jobs%rowtype;
  job_id_value uuid;
  attempt_id_value uuid;
  holder_id_value uuid;
  adjusted_value boolean;
  provider_event_at_value timestamptz;
  observed_at_value timestamptz;
  open_value numeric;
  high_value numeric;
  low_value numeric;
  close_value numeric;
  volume_value numeric;
  expected_idempotency_key text;
  expected_candidate_sha256 text;
  new_revision bigint;
begin
  perform private.require_service_role();
  if not coalesce(
       private.pit_daily_candle_collection_uuid4_v1(p_job_id), false
     )
     or p_spec_sha256 is null
     or p_spec_sha256 !~ '^[0-9a-f]{64}$'
     or p_expected_revision is null
     or p_expected_revision <= 0
     or p_expected_revision >= 9223372036854775807
     or not coalesce(
       private.pit_daily_candle_collection_uuid4_v1(p_attempt_id), false
     )
     or not coalesce(
       private.pit_daily_candle_collection_uuid4_v1(p_holder_id), false
     )
     or p_fencing_revision is null
     or p_fencing_revision <= 1
     or p_fencing_revision % 2 <> 0
     or p_fencing_revision > 9223372036854775804
     or p_candidate is null
     or pg_catalog.octet_length(p_candidate::text) > 4096
     or private.jsonb_exact_keys_v1(
       p_candidate,
       array[
         'adjusted', 'canonical_observation_sha256', 'close_krw',
         'currency', 'high_krw', 'interval', 'low_krw', 'market',
         'observed_at', 'open_krw', 'provider',
         'provider_contract_sha256', 'provider_event_at',
         'schema_version', 'symbol', 'volume'
       ]
     ) is distinct from true
     or p_now is null
     or not pg_catalog.isfinite(p_now) then
    raise exception 'pit_daily_candle_collection_job_argument_invalid'
      using errcode = '22023';
  end if;
  if p_expected_revision > 9223372036854775804 then
    raise exception 'pit_daily_candle_collection_job_revision_exhausted'
      using errcode = '54000';
  end if;
  if pg_catalog.jsonb_typeof(p_candidate->'schema_version') <> 'number'
     or p_candidate->>'schema_version' <> '1'
     or pg_catalog.jsonb_typeof(p_candidate->'provider') <> 'string'
     or (p_candidate->>'provider') !~ '^[a-z][a-z0-9._-]{0,63}$'
     or pg_catalog.jsonb_typeof(p_candidate->'symbol') <> 'string'
     or (p_candidate->>'symbol') !~ '^[0-9]{6}$'
     or pg_catalog.jsonb_typeof(p_candidate->'market') <> 'string'
     or p_candidate->>'market' <> 'KR'
     or pg_catalog.jsonb_typeof(p_candidate->'interval') <> 'string'
     or p_candidate->>'interval' <> '1d'
     or pg_catalog.jsonb_typeof(p_candidate->'adjusted') <> 'boolean'
     or pg_catalog.jsonb_typeof(p_candidate->'provider_event_at') <> 'string'
     or pg_catalog.jsonb_typeof(p_candidate->'observed_at') <> 'string'
     or pg_catalog.jsonb_typeof(p_candidate->'currency') <> 'string'
     or p_candidate->>'currency' <> 'KRW'
     or pg_catalog.jsonb_typeof(
       p_candidate->'provider_contract_sha256'
     ) <> 'string'
     or (p_candidate->>'provider_contract_sha256')
       !~ '^[0-9a-f]{64}$'
     or pg_catalog.jsonb_typeof(
       p_candidate->'canonical_observation_sha256'
     ) <> 'string'
     or (p_candidate->>'canonical_observation_sha256')
       !~ '^[0-9a-f]{64}$'
     or pg_catalog.jsonb_typeof(p_candidate->'open_krw') <> 'number'
     or (p_candidate->>'open_krw') !~ '^[1-9][0-9]*$'
     or pg_catalog.jsonb_typeof(p_candidate->'high_krw') <> 'number'
     or (p_candidate->>'high_krw') !~ '^[1-9][0-9]*$'
     or pg_catalog.jsonb_typeof(p_candidate->'low_krw') <> 'number'
     or (p_candidate->>'low_krw') !~ '^[1-9][0-9]*$'
     or pg_catalog.jsonb_typeof(p_candidate->'close_krw') <> 'number'
     or (p_candidate->>'close_krw') !~ '^[1-9][0-9]*$'
     or pg_catalog.jsonb_typeof(p_candidate->'volume') <> 'number'
     or (p_candidate->>'volume') !~ '^(0|[1-9][0-9]*)$' then
    raise exception 'pit_daily_candle_collection_job_candidate_invalid'
      using errcode = '22023';
  end if;

  begin
    job_id_value := p_job_id::uuid;
    attempt_id_value := p_attempt_id::uuid;
    holder_id_value := p_holder_id::uuid;
    adjusted_value := (p_candidate->>'adjusted')::boolean;
    provider_event_at_value :=
      (p_candidate->>'provider_event_at')::timestamptz;
    observed_at_value := (p_candidate->>'observed_at')::timestamptz;
    open_value := (p_candidate->>'open_krw')::numeric;
    high_value := (p_candidate->>'high_krw')::numeric;
    low_value := (p_candidate->>'low_krw')::numeric;
    close_value := (p_candidate->>'close_krw')::numeric;
    volume_value := (p_candidate->>'volume')::numeric;
  exception
    when others then
      raise exception 'pit_daily_candle_collection_job_candidate_invalid'
        using errcode = '22023';
  end;
  if not pg_catalog.isfinite(provider_event_at_value)
     or not pg_catalog.isfinite(observed_at_value)
     or private.pit_canonical_timestamp_v1(provider_event_at_value)
       <> p_candidate->>'provider_event_at'
     or private.pit_canonical_timestamp_v1(observed_at_value)
       <> p_candidate->>'observed_at'
     or observed_at_value < provider_event_at_value
     or high_value < greatest(open_value, close_value)
     or low_value > least(open_value, close_value) then
    raise exception 'pit_daily_candle_collection_job_candidate_invalid'
      using errcode = '22023';
  end if;

  select * into job
  from private.pit_daily_candle_collection_jobs as stored
  where stored.job_id = job_id_value
  for update;
  if not found then
    raise exception 'pit_daily_candle_collection_job_not_found'
      using errcode = 'P0002';
  end if;
  if job.spec_sha256 <> p_spec_sha256 then
    raise exception 'pit_daily_candle_collection_job_spec_hash_mismatch'
      using errcode = '40001';
  end if;
  if job.revision <> p_expected_revision then
    raise exception 'pit_daily_candle_collection_job_revision_conflict'
      using errcode = '40001';
  end if;
  if job.state <> 'collecting' then
    raise exception 'pit_daily_candle_collection_job_fence_state_invalid'
      using errcode = '55000';
  end if;
  if job.active_attempt_id <> attempt_id_value
     or job.active_holder_id <> holder_id_value
     or job.active_fencing_revision <> p_fencing_revision
     or p_fencing_revision <> p_expected_revision then
    raise exception 'pit_daily_candle_collection_job_attempt_fence_mismatch'
      using errcode = '40001';
  end if;
  if p_now < job.updated_at
     or observed_at_value < job.active_begun_at
     or observed_at_value > p_now then
    raise exception 'pit_daily_candle_collection_job_clock_regressed'
      using errcode = '40001';
  end if;
  if p_candidate->>'provider' <> job.provider
     or p_candidate->>'symbol' <> job.symbol
     or p_candidate->>'market' <> job.market
     or p_candidate->>'interval' <> job.interval
     or adjusted_value <> job.adjusted
     or provider_event_at_value > job.before_at
     or p_candidate->>'provider_contract_sha256'
       <> job.provider_contract_sha256 then
    raise exception 'pit_daily_candle_collection_job_scope_mismatch'
      using errcode = '40001';
  end if;

  expected_idempotency_key := private.pit_candle_identity_sha256_v1(
    job.provider, job.symbol, job.market, job.interval, job.adjusted,
    provider_event_at_value
  );
  expected_candidate_sha256 := private.pit_candle_observation_sha256_v1(
    job.provider, job.symbol, job.market, job.interval, job.adjusted,
    provider_event_at_value, 'KRW', open_value, high_value, low_value,
    close_value, volume_value, job.provider_contract_sha256
  );
  if p_candidate->>'canonical_observation_sha256'
       <> expected_candidate_sha256 then
    raise exception 'pit_daily_candle_collection_job_candidate_hash_mismatch'
      using errcode = '40001';
  end if;

  new_revision := p_expected_revision + 1;
  insert into private.pit_daily_candle_collection_attempt_ledger (
    job_id, job_revision, attempt_id, holder_id, fencing_revision,
    begun_at, event_kind, candidate_idempotency_key,
    candidate_canonical_observation_sha256, candidate_observed_at,
    candidate_payload, occurred_at, event_sha256
  ) values (
    job_id_value, new_revision, attempt_id_value, holder_id_value,
    p_fencing_revision, job.active_begun_at, 'candidate_fenced',
    expected_idempotency_key, expected_candidate_sha256,
    observed_at_value, p_candidate, p_now,
    private.pit_daily_candle_collection_event_sha256_v1(
      job_id_value, new_revision, attempt_id_value, holder_id_value,
      p_fencing_revision, 'candidate_fenced', p_candidate, null, null, p_now
    )
  );
  update private.pit_daily_candle_collection_jobs
  set revision = new_revision,
      state = 'candidate_fenced',
      candidate_attempt_id = attempt_id_value,
      candidate_holder_id = holder_id_value,
      candidate_fencing_revision = p_fencing_revision,
      candidate_begun_at = job.active_begun_at,
      candidate_idempotency_key = expected_idempotency_key,
      candidate_canonical_observation_sha256 = expected_candidate_sha256,
      candidate_provider_event_at = provider_event_at_value,
      candidate_observed_at = observed_at_value,
      candidate_payload = p_candidate,
      candidate_fenced_at = p_now,
      updated_at = p_now
  where job_id = job_id_value
    and spec_sha256 = p_spec_sha256
    and revision = p_expected_revision
    and state = 'collecting'
    and active_attempt_id = attempt_id_value
    and active_holder_id = holder_id_value
    and active_fencing_revision = p_fencing_revision;
  if not found then
    raise exception 'pit_daily_candle_collection_job_revision_conflict'
      using errcode = '40001';
  end if;

  return query
  select private.pit_daily_candle_collection_snapshot_v1(job_id_value);
end;
$$;

create or replace function private.pause_pit_daily_candle_collection_attempt_v1_impl(
  p_job_id text,
  p_spec_sha256 text,
  p_expected_revision bigint,
  p_attempt_id text,
  p_holder_id text,
  p_fencing_revision bigint,
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
  job private.pit_daily_candle_collection_jobs%rowtype;
  job_id_value uuid;
  attempt_id_value uuid;
  holder_id_value uuid;
  new_revision bigint;
begin
  perform private.require_service_role();
  if not coalesce(
       private.pit_daily_candle_collection_uuid4_v1(p_job_id), false
     )
     or p_spec_sha256 is null
     or p_spec_sha256 !~ '^[0-9a-f]{64}$'
     or p_expected_revision is null
     or p_expected_revision <= 0
     or p_expected_revision >= 9223372036854775807
     or not coalesce(
       private.pit_daily_candle_collection_uuid4_v1(p_attempt_id), false
     )
     or not coalesce(
       private.pit_daily_candle_collection_uuid4_v1(p_holder_id), false
     )
     or p_fencing_revision is null
     or p_fencing_revision <= 1
     or p_fencing_revision % 2 <> 0
     or p_fencing_revision > 9223372036854775804
     or p_reason_code is distinct from
       'provider_read_failed_before_candidate'
     or p_now is null
     or not pg_catalog.isfinite(p_now) then
    raise exception 'pit_daily_candle_collection_job_argument_invalid'
      using errcode = '22023';
  end if;
  if p_expected_revision > 9223372036854775802 then
    raise exception 'pit_daily_candle_collection_job_revision_exhausted'
      using errcode = '54000';
  end if;
  job_id_value := p_job_id::uuid;
  attempt_id_value := p_attempt_id::uuid;
  holder_id_value := p_holder_id::uuid;

  select * into job
  from private.pit_daily_candle_collection_jobs as stored
  where stored.job_id = job_id_value
  for update;
  if not found then
    raise exception 'pit_daily_candle_collection_job_not_found'
      using errcode = 'P0002';
  end if;
  if job.spec_sha256 <> p_spec_sha256 then
    raise exception 'pit_daily_candle_collection_job_spec_hash_mismatch'
      using errcode = '40001';
  end if;
  if job.revision <> p_expected_revision then
    raise exception 'pit_daily_candle_collection_job_revision_conflict'
      using errcode = '40001';
  end if;
  if job.state <> 'collecting' then
    raise exception 'pit_daily_candle_collection_job_pause_state_invalid'
      using errcode = '55000';
  end if;
  if job.active_attempt_id <> attempt_id_value
     or job.active_holder_id <> holder_id_value
     or job.active_fencing_revision <> p_fencing_revision
     or p_fencing_revision <> p_expected_revision then
    raise exception 'pit_daily_candle_collection_job_attempt_fence_mismatch'
      using errcode = '40001';
  end if;
  if p_now < job.updated_at then
    raise exception 'pit_daily_candle_collection_job_clock_regressed'
      using errcode = '40001';
  end if;

  new_revision := p_expected_revision + 1;
  insert into private.pit_daily_candle_collection_attempt_ledger (
    job_id, job_revision, attempt_id, holder_id, fencing_revision,
    begun_at, event_kind, reason_code, occurred_at, event_sha256
  ) values (
    job_id_value, new_revision, attempt_id_value, holder_id_value,
    p_fencing_revision, job.active_begun_at, 'paused_retryable',
    p_reason_code, p_now,
    private.pit_daily_candle_collection_event_sha256_v1(
      job_id_value, new_revision, attempt_id_value, holder_id_value,
      p_fencing_revision, 'paused_retryable', null, null, p_reason_code, p_now
    )
  );
  update private.pit_daily_candle_collection_jobs
  set revision = new_revision,
      state = 'paused_retryable',
      active_attempt_id = null,
      active_holder_id = null,
      active_fencing_revision = null,
      active_begun_at = null,
      state_reason = p_reason_code,
      updated_at = p_now
  where job_id = job_id_value
    and spec_sha256 = p_spec_sha256
    and revision = p_expected_revision
    and state = 'collecting'
    and active_attempt_id = attempt_id_value
    and active_holder_id = holder_id_value
    and active_fencing_revision = p_fencing_revision;
  if not found then
    raise exception 'pit_daily_candle_collection_job_revision_conflict'
      using errcode = '40001';
  end if;

  return query
  select private.pit_daily_candle_collection_snapshot_v1(job_id_value);
end;
$$;

create or replace function private.block_pit_daily_candle_collection_attempt_v1_impl(
  p_job_id text,
  p_spec_sha256 text,
  p_expected_revision bigint,
  p_attempt_id text,
  p_holder_id text,
  p_fencing_revision bigint,
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
  job private.pit_daily_candle_collection_jobs%rowtype;
  job_id_value uuid;
  attempt_id_value uuid;
  holder_id_value uuid;
  new_revision bigint;
begin
  perform private.require_service_role();
  if not coalesce(
       private.pit_daily_candle_collection_uuid4_v1(p_job_id), false
     )
     or p_spec_sha256 is null
     or p_spec_sha256 !~ '^[0-9a-f]{64}$'
     or p_expected_revision is null
     or p_expected_revision <= 0
     or p_expected_revision >= 9223372036854775807
     or not coalesce(
       private.pit_daily_candle_collection_uuid4_v1(p_attempt_id), false
     )
     or not coalesce(
       private.pit_daily_candle_collection_uuid4_v1(p_holder_id), false
     )
     or p_fencing_revision is null
     or p_fencing_revision <= 1
     or p_fencing_revision % 2 <> 0
     or p_fencing_revision > 9223372036854775804
     or p_reason_code is null
     or p_reason_code not in (
       'provider_read_outcome_unknown_before_candidate',
       'unexpected_failure_before_candidate',
       'cancelled_before_candidate',
       'append_outcome_unknown',
       'confirm_outcome_unknown',
       'unexpected_failure_after_candidate',
       'cancelled_after_candidate'
     )
     or p_now is null
     or not pg_catalog.isfinite(p_now) then
    raise exception 'pit_daily_candle_collection_job_argument_invalid'
      using errcode = '22023';
  end if;
  if p_expected_revision > 9223372036854775805 then
    raise exception 'pit_daily_candle_collection_job_revision_exhausted'
      using errcode = '54000';
  end if;
  job_id_value := p_job_id::uuid;
  attempt_id_value := p_attempt_id::uuid;
  holder_id_value := p_holder_id::uuid;

  select * into job
  from private.pit_daily_candle_collection_jobs as stored
  where stored.job_id = job_id_value
  for update;
  if not found then
    raise exception 'pit_daily_candle_collection_job_not_found'
      using errcode = 'P0002';
  end if;
  if job.spec_sha256 <> p_spec_sha256 then
    raise exception 'pit_daily_candle_collection_job_spec_hash_mismatch'
      using errcode = '40001';
  end if;
  if job.revision <> p_expected_revision then
    raise exception 'pit_daily_candle_collection_job_revision_conflict'
      using errcode = '40001';
  end if;
  if job.state not in ('collecting', 'candidate_fenced') then
    raise exception 'pit_daily_candle_collection_job_block_state_invalid'
      using errcode = '55000';
  end if;
  if job.active_attempt_id <> attempt_id_value
     or job.active_holder_id <> holder_id_value
     or job.active_fencing_revision <> p_fencing_revision
     or (job.state = 'collecting'
         and p_expected_revision <> p_fencing_revision)
     or (job.state = 'candidate_fenced'
         and (
           job.candidate_attempt_id <> attempt_id_value
           or job.candidate_holder_id <> holder_id_value
           or job.candidate_fencing_revision <> p_fencing_revision
           or p_expected_revision <> p_fencing_revision + 1
         )) then
    raise exception 'pit_daily_candle_collection_job_attempt_fence_mismatch'
      using errcode = '40001';
  end if;
  if (job.state = 'collecting'
      and p_reason_code not in (
        'provider_read_outcome_unknown_before_candidate',
        'unexpected_failure_before_candidate',
        'cancelled_before_candidate'
      ))
     or (job.state = 'candidate_fenced'
         and p_reason_code not in (
           'append_outcome_unknown',
           'confirm_outcome_unknown',
           'unexpected_failure_after_candidate',
           'cancelled_after_candidate'
         )) then
    raise exception 'pit_daily_candle_collection_job_reason_scope_invalid'
      using errcode = '22023';
  end if;
  if p_now < job.updated_at then
    raise exception 'pit_daily_candle_collection_job_clock_regressed'
      using errcode = '40001';
  end if;

  new_revision := p_expected_revision + 1;
  insert into private.pit_daily_candle_collection_attempt_ledger (
    job_id, job_revision, attempt_id, holder_id, fencing_revision,
    begun_at, event_kind, candidate_idempotency_key,
    candidate_canonical_observation_sha256, candidate_observed_at,
    candidate_payload, reason_code, occurred_at, event_sha256
  ) values (
    job_id_value, new_revision, attempt_id_value, holder_id_value,
    p_fencing_revision, job.active_begun_at, 'blocked_unknown',
    job.candidate_idempotency_key,
    job.candidate_canonical_observation_sha256,
    job.candidate_observed_at, job.candidate_payload,
    p_reason_code, p_now,
    private.pit_daily_candle_collection_event_sha256_v1(
      job_id_value, new_revision, attempt_id_value, holder_id_value,
      p_fencing_revision, 'blocked_unknown', job.candidate_payload,
      null, p_reason_code, p_now
    )
  );
  update private.pit_daily_candle_collection_jobs as stored
  set revision = new_revision,
      state = 'blocked_unknown',
      state_reason = p_reason_code,
      updated_at = p_now
  where stored.job_id = job_id_value
    and stored.spec_sha256 = p_spec_sha256
    and stored.revision = p_expected_revision
    and stored.state = job.state
    and stored.active_attempt_id = attempt_id_value
    and stored.active_holder_id = holder_id_value
    and stored.active_fencing_revision = p_fencing_revision
    and (
      (job.state = 'collecting'
       and stored.candidate_fencing_revision is null)
      or
      (job.state = 'candidate_fenced'
       and stored.candidate_attempt_id = attempt_id_value
       and stored.candidate_holder_id = holder_id_value
       and stored.candidate_fencing_revision = p_fencing_revision)
    );
  if not found then
    raise exception 'pit_daily_candle_collection_job_revision_conflict'
      using errcode = '40001';
  end if;

  return query
  select private.pit_daily_candle_collection_snapshot_v1(job_id_value);
end;
$$;

create or replace function private.confirm_pit_daily_candle_collection_attempt_v1_impl(
  p_job_id text,
  p_spec_sha256 text,
  p_expected_revision bigint,
  p_attempt_id text,
  p_holder_id text,
  p_fencing_revision bigint,
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
  job private.pit_daily_candle_collection_jobs%rowtype;
  job_id_value uuid;
  attempt_id_value uuid;
  holder_id_value uuid;
  receipt_revision_value bigint;
  receipt_stored_observed_at_value timestamptz;
  occurrence_id_value uuid;
  content_revision_id_value uuid;
  content_revision_value bigint;
  new_revision bigint;
begin
  perform private.require_service_role();
  if not coalesce(
       private.pit_daily_candle_collection_uuid4_v1(p_job_id), false
     )
     or p_spec_sha256 is null
     or p_spec_sha256 !~ '^[0-9a-f]{64}$'
     or p_expected_revision is null
     or p_expected_revision <= 0
     or p_expected_revision >= 9223372036854775807
     or not coalesce(
       private.pit_daily_candle_collection_uuid4_v1(p_attempt_id), false
     )
     or not coalesce(
       private.pit_daily_candle_collection_uuid4_v1(p_holder_id), false
     )
     or p_fencing_revision is null
     or p_fencing_revision <= 1
     or p_fencing_revision % 2 <> 0
     or p_fencing_revision > 9223372036854775804
     or p_now is null
     or not pg_catalog.isfinite(p_now) then
    raise exception 'pit_daily_candle_collection_job_argument_invalid'
      using errcode = '22023';
  end if;
  if p_receipt is null
     or pg_catalog.octet_length(p_receipt::text) > 2048
     or private.jsonb_exact_keys_v1(
       p_receipt,
       array[
         'idempotency_key', 'canonical_observation_sha256', 'revision',
         'inserted', 'stored_observed_at'
       ]
     ) is distinct from true
     or pg_catalog.jsonb_typeof(p_receipt->'idempotency_key') <> 'string'
     or (p_receipt->>'idempotency_key') !~ '^[0-9a-f]{64}$'
     or pg_catalog.jsonb_typeof(
       p_receipt->'canonical_observation_sha256'
     ) <> 'string'
     or (p_receipt->>'canonical_observation_sha256') !~ '^[0-9a-f]{64}$'
     or pg_catalog.jsonb_typeof(p_receipt->'revision') <> 'number'
     or (p_receipt->>'revision') !~ '^[1-9][0-9]*$'
     or pg_catalog.jsonb_typeof(p_receipt->'inserted') <> 'boolean'
     or pg_catalog.jsonb_typeof(p_receipt->'stored_observed_at') <> 'string' then
    raise exception 'pit_daily_candle_collection_job_receipt_invalid'
      using errcode = '22023';
  end if;
  if p_expected_revision > 9223372036854775805 then
    raise exception 'pit_daily_candle_collection_job_revision_exhausted'
      using errcode = '54000';
  end if;
  begin
    job_id_value := p_job_id::uuid;
    attempt_id_value := p_attempt_id::uuid;
    holder_id_value := p_holder_id::uuid;
    receipt_revision_value := (p_receipt->>'revision')::bigint;
    receipt_stored_observed_at_value :=
      (p_receipt->>'stored_observed_at')::timestamptz;
  exception
    when others then
      raise exception 'pit_daily_candle_collection_job_receipt_invalid'
        using errcode = '22023';
  end;
  if not pg_catalog.isfinite(receipt_stored_observed_at_value)
     or private.pit_canonical_timestamp_v1(receipt_stored_observed_at_value)
       <> p_receipt->>'stored_observed_at' then
    raise exception 'pit_daily_candle_collection_job_receipt_invalid'
      using errcode = '22023';
  end if;

  select * into job
  from private.pit_daily_candle_collection_jobs as stored
  where stored.job_id = job_id_value
  for update;
  if not found then
    raise exception 'pit_daily_candle_collection_job_not_found'
      using errcode = 'P0002';
  end if;
  if job.spec_sha256 <> p_spec_sha256 then
    raise exception 'pit_daily_candle_collection_job_spec_hash_mismatch'
      using errcode = '40001';
  end if;
  if job.revision <> p_expected_revision then
    raise exception 'pit_daily_candle_collection_job_revision_conflict'
      using errcode = '40001';
  end if;
  if job.state <> 'candidate_fenced' then
    raise exception 'pit_daily_candle_collection_job_confirm_state_invalid'
      using errcode = '55000';
  end if;
  if job.active_attempt_id <> attempt_id_value
     or job.active_holder_id <> holder_id_value
     or job.active_fencing_revision <> p_fencing_revision
     or job.candidate_attempt_id <> attempt_id_value
     or job.candidate_holder_id <> holder_id_value
     or job.candidate_fencing_revision <> p_fencing_revision
     or p_expected_revision <> p_fencing_revision + 1 then
    raise exception 'pit_daily_candle_collection_job_attempt_fence_mismatch'
      using errcode = '40001';
  end if;
  if p_now < job.updated_at then
    raise exception 'pit_daily_candle_collection_job_clock_regressed'
      using errcode = '40001';
  end if;
  if p_receipt->>'idempotency_key' <> job.candidate_idempotency_key
     or p_receipt->>'canonical_observation_sha256'
       <> job.candidate_canonical_observation_sha256 then
    raise exception 'pit_daily_candle_collection_job_receipt_mismatch'
      using errcode = '40001';
  end if;

  -- `inserted` is telemetry only. Durable authority is the exact occurrence
  -- plus the content revision identified by key/hash/revision/stored time.
  select occurrence.id, content.id, content.revision
  into occurrence_id_value, content_revision_id_value,
       content_revision_value
  from private.pit_candle_observation_occurrences as occurrence
  join private.pit_candle_observation_revisions as content
    on content.id = occurrence.content_revision_id
   and content.idempotency_key = occurrence.idempotency_key
   and content.canonical_observation_sha256 =
       occurrence.canonical_observation_sha256
  where occurrence.idempotency_key = job.candidate_idempotency_key
    and occurrence.observed_at = job.candidate_observed_at
    and occurrence.canonical_observation_sha256 =
        job.candidate_canonical_observation_sha256
    and occurrence.observation_payload = job.candidate_payload
    and content.idempotency_key = p_receipt->>'idempotency_key'
    and content.canonical_observation_sha256 =
        p_receipt->>'canonical_observation_sha256'
    and content.revision = receipt_revision_value
    and content.observed_at = receipt_stored_observed_at_value;
  if not found then
    raise exception 'pit_daily_candle_collection_job_receipt_mismatch'
      using errcode = '40001';
  end if;

  new_revision := p_expected_revision + 1;
  insert into private.pit_daily_candle_collection_attempt_ledger (
    job_id, job_revision, attempt_id, holder_id, fencing_revision,
    begun_at, event_kind, candidate_idempotency_key,
    candidate_canonical_observation_sha256, candidate_observed_at,
    candidate_payload, receipt_payload, occurrence_id,
    content_revision_id, content_revision, occurred_at, event_sha256
  ) values (
    job_id_value, new_revision, attempt_id_value, holder_id_value,
    p_fencing_revision, job.active_begun_at, 'confirmed',
    job.candidate_idempotency_key,
    job.candidate_canonical_observation_sha256,
    job.candidate_observed_at, job.candidate_payload, p_receipt,
    occurrence_id_value, content_revision_id_value,
    content_revision_value, p_now,
    private.pit_daily_candle_collection_event_sha256_v1(
      job_id_value, new_revision, attempt_id_value, holder_id_value,
      p_fencing_revision, 'confirmed', job.candidate_payload,
      p_receipt, null, p_now
    )
  );
  update private.pit_daily_candle_collection_jobs
  set revision = new_revision,
      state = 'completed',
      active_attempt_id = null,
      active_holder_id = null,
      active_fencing_revision = null,
      active_begun_at = null,
      confirmed_occurrence_id = occurrence_id_value,
      confirmed_content_revision_id = content_revision_id_value,
      confirmed_content_revision = content_revision_value,
      confirmed_at = p_now,
      confirmation_receipt = p_receipt,
      state_reason = null,
      updated_at = p_now
  where job_id = job_id_value
    and spec_sha256 = p_spec_sha256
    and revision = p_expected_revision
    and state = 'candidate_fenced'
    and active_attempt_id = attempt_id_value
    and active_holder_id = holder_id_value
    and active_fencing_revision = p_fencing_revision
    and candidate_attempt_id = attempt_id_value
    and candidate_holder_id = holder_id_value
    and candidate_fencing_revision = p_fencing_revision;
  if not found then
    raise exception 'pit_daily_candle_collection_job_revision_conflict'
      using errcode = '40001';
  end if;

  return query
  select private.pit_daily_candle_collection_snapshot_v1(job_id_value);
end;
$$;

create or replace function worker_api.load_or_create_pit_daily_candle_collection_job_v1(
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
  from private.load_or_create_pit_daily_candle_collection_job_v1_impl(
    p_spec, p_now
  );
$$;

create or replace function worker_api.inspect_pit_daily_candle_collection_job_v1(
  p_job_id text
)
returns table(found boolean, snapshot jsonb)
language sql
stable
security invoker
set search_path = ''
as $$
  select *
  from private.inspect_pit_daily_candle_collection_job_v1_impl(p_job_id);
$$;

create or replace function worker_api.begin_pit_daily_candle_collection_attempt_v1(
  p_job_id text,
  p_spec_sha256 text,
  p_expected_revision bigint,
  p_attempt_id text,
  p_holder_id text,
  p_now timestamptz
)
returns table(snapshot jsonb)
language plpgsql
volatile
security invoker
set search_path = ''
as $$
begin
  return query
  select *
  from private.begin_pit_daily_candle_collection_attempt_v1_impl(
    p_job_id, p_spec_sha256, p_expected_revision, p_attempt_id,
    p_holder_id, p_now
  );
exception
  when sqlstate '40001' then
    if sqlerrm in (
      'pit_daily_candle_collection_job_spec_hash_mismatch',
      'pit_daily_candle_collection_job_revision_conflict',
      'pit_daily_candle_collection_job_clock_regressed',
      'pit_daily_candle_collection_job_attempt_fence_mismatch'
    ) then
      raise sqlstate 'PT409' using message = sqlerrm;
    end if;
    raise;
end;
$$;

create or replace function worker_api.fence_pit_daily_candle_collection_candidate_v1(
  p_job_id text,
  p_spec_sha256 text,
  p_expected_revision bigint,
  p_attempt_id text,
  p_holder_id text,
  p_fencing_revision bigint,
  p_candidate jsonb,
  p_now timestamptz
)
returns table(snapshot jsonb)
language plpgsql
volatile
security invoker
set search_path = ''
as $$
begin
  return query
  select *
  from private.fence_pit_daily_candle_collection_candidate_v1_impl(
    p_job_id, p_spec_sha256, p_expected_revision, p_attempt_id,
    p_holder_id, p_fencing_revision, p_candidate, p_now
  );
exception
  when sqlstate '40001' then
    if sqlerrm in (
      'pit_daily_candle_collection_job_spec_hash_mismatch',
      'pit_daily_candle_collection_job_revision_conflict',
      'pit_daily_candle_collection_job_clock_regressed',
      'pit_daily_candle_collection_job_attempt_fence_mismatch',
      'pit_daily_candle_collection_job_scope_mismatch',
      'pit_daily_candle_collection_job_candidate_hash_mismatch'
    ) then
      raise sqlstate 'PT409' using message = sqlerrm;
    end if;
    raise;
end;
$$;

create or replace function worker_api.pause_pit_daily_candle_collection_attempt_v1(
  p_job_id text,
  p_spec_sha256 text,
  p_expected_revision bigint,
  p_attempt_id text,
  p_holder_id text,
  p_fencing_revision bigint,
  p_reason_code text,
  p_now timestamptz
)
returns table(snapshot jsonb)
language plpgsql
volatile
security invoker
set search_path = ''
as $$
begin
  return query
  select *
  from private.pause_pit_daily_candle_collection_attempt_v1_impl(
    p_job_id, p_spec_sha256, p_expected_revision, p_attempt_id,
    p_holder_id, p_fencing_revision, p_reason_code, p_now
  );
exception
  when sqlstate '40001' then
    if sqlerrm in (
      'pit_daily_candle_collection_job_spec_hash_mismatch',
      'pit_daily_candle_collection_job_revision_conflict',
      'pit_daily_candle_collection_job_clock_regressed',
      'pit_daily_candle_collection_job_attempt_fence_mismatch'
    ) then
      raise sqlstate 'PT409' using message = sqlerrm;
    end if;
    raise;
end;
$$;

create or replace function worker_api.block_pit_daily_candle_collection_attempt_v1(
  p_job_id text,
  p_spec_sha256 text,
  p_expected_revision bigint,
  p_attempt_id text,
  p_holder_id text,
  p_fencing_revision bigint,
  p_reason_code text,
  p_now timestamptz
)
returns table(snapshot jsonb)
language plpgsql
volatile
security invoker
set search_path = ''
as $$
begin
  return query
  select *
  from private.block_pit_daily_candle_collection_attempt_v1_impl(
    p_job_id, p_spec_sha256, p_expected_revision, p_attempt_id,
    p_holder_id, p_fencing_revision, p_reason_code, p_now
  );
exception
  when sqlstate '40001' then
    if sqlerrm in (
      'pit_daily_candle_collection_job_spec_hash_mismatch',
      'pit_daily_candle_collection_job_revision_conflict',
      'pit_daily_candle_collection_job_clock_regressed',
      'pit_daily_candle_collection_job_attempt_fence_mismatch'
    ) then
      raise sqlstate 'PT409' using message = sqlerrm;
    end if;
    raise;
end;
$$;

create or replace function worker_api.confirm_pit_daily_candle_collection_attempt_v1(
  p_job_id text,
  p_spec_sha256 text,
  p_expected_revision bigint,
  p_attempt_id text,
  p_holder_id text,
  p_fencing_revision bigint,
  p_receipt jsonb,
  p_now timestamptz
)
returns table(snapshot jsonb)
language plpgsql
volatile
security invoker
set search_path = ''
as $$
begin
  return query
  select *
  from private.confirm_pit_daily_candle_collection_attempt_v1_impl(
    p_job_id, p_spec_sha256, p_expected_revision, p_attempt_id,
    p_holder_id, p_fencing_revision, p_receipt, p_now
  );
exception
  when sqlstate '40001' then
    if sqlerrm in (
      'pit_daily_candle_collection_job_spec_hash_mismatch',
      'pit_daily_candle_collection_job_revision_conflict',
      'pit_daily_candle_collection_job_clock_regressed',
      'pit_daily_candle_collection_job_attempt_fence_mismatch',
      'pit_daily_candle_collection_job_receipt_mismatch'
    ) then
      raise sqlstate 'PT409' using message = sqlerrm;
    end if;
    raise;
end;
$$;

revoke all on function
  private.pit_daily_candle_collection_uuid4_v1(text),
  private.pit_daily_candle_collection_canonical_json_v1(jsonb),
  private.pit_daily_candle_collection_spec_document_v1(jsonb),
  private.pit_daily_candle_collection_spec_sha256_v1(jsonb),
  private.pit_daily_candle_collection_snapshot_v1(uuid),
  private.pit_daily_candle_collection_event_sha256_v1(
    uuid,bigint,uuid,uuid,bigint,text,jsonb,jsonb,text,timestamptz
  ),
  private.load_or_create_pit_daily_candle_collection_job_v1_impl(
    jsonb,timestamptz
  ),
  private.inspect_pit_daily_candle_collection_job_v1_impl(text),
  private.begin_pit_daily_candle_collection_attempt_v1_impl(
    text,text,bigint,text,text,timestamptz
  ),
  private.fence_pit_daily_candle_collection_candidate_v1_impl(
    text,text,bigint,text,text,bigint,jsonb,timestamptz
  ),
  private.pause_pit_daily_candle_collection_attempt_v1_impl(
    text,text,bigint,text,text,bigint,text,timestamptz
  ),
  private.block_pit_daily_candle_collection_attempt_v1_impl(
    text,text,bigint,text,text,bigint,text,timestamptz
  ),
  private.confirm_pit_daily_candle_collection_attempt_v1_impl(
    text,text,bigint,text,text,bigint,jsonb,timestamptz
  ),
  worker_api.load_or_create_pit_daily_candle_collection_job_v1(
    jsonb,timestamptz
  ),
  worker_api.inspect_pit_daily_candle_collection_job_v1(text),
  worker_api.begin_pit_daily_candle_collection_attempt_v1(
    text,text,bigint,text,text,timestamptz
  ),
  worker_api.fence_pit_daily_candle_collection_candidate_v1(
    text,text,bigint,text,text,bigint,jsonb,timestamptz
  ),
  worker_api.pause_pit_daily_candle_collection_attempt_v1(
    text,text,bigint,text,text,bigint,text,timestamptz
  ),
  worker_api.block_pit_daily_candle_collection_attempt_v1(
    text,text,bigint,text,text,bigint,text,timestamptz
  ),
  worker_api.confirm_pit_daily_candle_collection_attempt_v1(
    text,text,bigint,text,text,bigint,jsonb,timestamptz
  )
from public, anon, authenticated, authenticator, service_role;

grant execute on function
  private.load_or_create_pit_daily_candle_collection_job_v1_impl(
    jsonb,timestamptz
  ),
  private.inspect_pit_daily_candle_collection_job_v1_impl(text),
  private.begin_pit_daily_candle_collection_attempt_v1_impl(
    text,text,bigint,text,text,timestamptz
  ),
  private.fence_pit_daily_candle_collection_candidate_v1_impl(
    text,text,bigint,text,text,bigint,jsonb,timestamptz
  ),
  private.pause_pit_daily_candle_collection_attempt_v1_impl(
    text,text,bigint,text,text,bigint,text,timestamptz
  ),
  private.block_pit_daily_candle_collection_attempt_v1_impl(
    text,text,bigint,text,text,bigint,text,timestamptz
  ),
  private.confirm_pit_daily_candle_collection_attempt_v1_impl(
    text,text,bigint,text,text,bigint,jsonb,timestamptz
  ),
  worker_api.load_or_create_pit_daily_candle_collection_job_v1(
    jsonb,timestamptz
  ),
  worker_api.inspect_pit_daily_candle_collection_job_v1(text),
  worker_api.begin_pit_daily_candle_collection_attempt_v1(
    text,text,bigint,text,text,timestamptz
  ),
  worker_api.fence_pit_daily_candle_collection_candidate_v1(
    text,text,bigint,text,text,bigint,jsonb,timestamptz
  ),
  worker_api.pause_pit_daily_candle_collection_attempt_v1(
    text,text,bigint,text,text,bigint,text,timestamptz
  ),
  worker_api.block_pit_daily_candle_collection_attempt_v1(
    text,text,bigint,text,text,bigint,text,timestamptz
  ),
  worker_api.confirm_pit_daily_candle_collection_attempt_v1(
    text,text,bigint,text,text,bigint,jsonb,timestamptz
  )
to service_role;

comment on table private.pit_daily_candle_collection_jobs is
  'Private durable single-candle collection state; no TTL, takeover, or automatic retry.';
comment on table private.pit_daily_candle_collection_attempt_ledger is
  'Private append-only attempt and fenced-candidate audit ledger.';

do $$
declare
  table_contract_count bigint;
  policy_count bigint;
  forbidden_table_acl_count bigint;
  forbidden_sequence_acl_count bigint;
  impl_contract_count bigint;
  wrapper_contract_count bigint;
  service_impl_acl_count bigint;
  service_wrapper_acl_count bigint;
  helper_service_acl_count bigint;
  forbidden_function_acl_count bigint;
  function_owner_count bigint;
  function_count bigint;
begin
  select count(*) into table_contract_count
  from pg_catalog.pg_class as relation
  where relation.oid in (
      'private.pit_daily_candle_collection_jobs'::regclass,
      'private.pit_daily_candle_collection_attempt_ledger'::regclass
    )
    and relation.relrowsecurity
    and relation.relforcerowsecurity;

  select count(*) into policy_count
  from pg_catalog.pg_policy as policy
  where policy.polrelid in (
    'private.pit_daily_candle_collection_jobs'::regclass,
    'private.pit_daily_candle_collection_attempt_ledger'::regclass
  );

  select count(*) into forbidden_table_acl_count
  from pg_catalog.pg_class as relation
  cross join lateral pg_catalog.aclexplode(
    coalesce(relation.relacl, pg_catalog.acldefault('r', relation.relowner))
  ) as acl
  left join pg_catalog.pg_roles as grantee on grantee.oid = acl.grantee
  where relation.oid in (
      'private.pit_daily_candle_collection_jobs'::regclass,
      'private.pit_daily_candle_collection_attempt_ledger'::regclass
    )
    and (
      acl.grantee = 0
      or grantee.rolname in (
        'anon', 'authenticated', 'authenticator', 'service_role'
      )
    );

  select count(*) into forbidden_sequence_acl_count
  from pg_catalog.pg_class as relation
  cross join lateral pg_catalog.aclexplode(
    coalesce(relation.relacl, pg_catalog.acldefault('S', relation.relowner))
  ) as acl
  left join pg_catalog.pg_roles as grantee on grantee.oid = acl.grantee
  where relation.oid =
      'private.pit_daily_candle_collection_attempt_ledger_event_id_seq'::regclass
    and (
      acl.grantee = 0
      or grantee.rolname in (
        'anon', 'authenticated', 'authenticator', 'service_role'
      )
    );

  select count(*) into impl_contract_count
  from pg_catalog.pg_proc as procedure
  where procedure.oid in (
      'private.load_or_create_pit_daily_candle_collection_job_v1_impl(jsonb,timestamptz)'::regprocedure,
      'private.inspect_pit_daily_candle_collection_job_v1_impl(text)'::regprocedure,
      'private.begin_pit_daily_candle_collection_attempt_v1_impl(text,text,bigint,text,text,timestamptz)'::regprocedure,
      'private.fence_pit_daily_candle_collection_candidate_v1_impl(text,text,bigint,text,text,bigint,jsonb,timestamptz)'::regprocedure,
      'private.pause_pit_daily_candle_collection_attempt_v1_impl(text,text,bigint,text,text,bigint,text,timestamptz)'::regprocedure,
      'private.block_pit_daily_candle_collection_attempt_v1_impl(text,text,bigint,text,text,bigint,text,timestamptz)'::regprocedure,
      'private.confirm_pit_daily_candle_collection_attempt_v1_impl(text,text,bigint,text,text,bigint,jsonb,timestamptz)'::regprocedure
    )
    and procedure.prosecdef
    and procedure.proconfig = array['search_path=""']::text[];

  select count(*) into wrapper_contract_count
  from pg_catalog.pg_proc as procedure
  where procedure.oid in (
      'worker_api.load_or_create_pit_daily_candle_collection_job_v1(jsonb,timestamptz)'::regprocedure,
      'worker_api.inspect_pit_daily_candle_collection_job_v1(text)'::regprocedure,
      'worker_api.begin_pit_daily_candle_collection_attempt_v1(text,text,bigint,text,text,timestamptz)'::regprocedure,
      'worker_api.fence_pit_daily_candle_collection_candidate_v1(text,text,bigint,text,text,bigint,jsonb,timestamptz)'::regprocedure,
      'worker_api.pause_pit_daily_candle_collection_attempt_v1(text,text,bigint,text,text,bigint,text,timestamptz)'::regprocedure,
      'worker_api.block_pit_daily_candle_collection_attempt_v1(text,text,bigint,text,text,bigint,text,timestamptz)'::regprocedure,
      'worker_api.confirm_pit_daily_candle_collection_attempt_v1(text,text,bigint,text,text,bigint,jsonb,timestamptz)'::regprocedure
    )
    and not procedure.prosecdef
    and procedure.proconfig = array['search_path=""']::text[];

  select count(*) into service_impl_acl_count
  from pg_catalog.pg_proc as procedure
  where procedure.oid in (
      'private.load_or_create_pit_daily_candle_collection_job_v1_impl(jsonb,timestamptz)'::regprocedure,
      'private.inspect_pit_daily_candle_collection_job_v1_impl(text)'::regprocedure,
      'private.begin_pit_daily_candle_collection_attempt_v1_impl(text,text,bigint,text,text,timestamptz)'::regprocedure,
      'private.fence_pit_daily_candle_collection_candidate_v1_impl(text,text,bigint,text,text,bigint,jsonb,timestamptz)'::regprocedure,
      'private.pause_pit_daily_candle_collection_attempt_v1_impl(text,text,bigint,text,text,bigint,text,timestamptz)'::regprocedure,
      'private.block_pit_daily_candle_collection_attempt_v1_impl(text,text,bigint,text,text,bigint,text,timestamptz)'::regprocedure,
      'private.confirm_pit_daily_candle_collection_attempt_v1_impl(text,text,bigint,text,text,bigint,jsonb,timestamptz)'::regprocedure
    )
    and pg_catalog.has_function_privilege(
      'service_role', procedure.oid, 'EXECUTE'
    );

  select count(*) into service_wrapper_acl_count
  from pg_catalog.pg_proc as procedure
  where procedure.oid in (
      'worker_api.load_or_create_pit_daily_candle_collection_job_v1(jsonb,timestamptz)'::regprocedure,
      'worker_api.inspect_pit_daily_candle_collection_job_v1(text)'::regprocedure,
      'worker_api.begin_pit_daily_candle_collection_attempt_v1(text,text,bigint,text,text,timestamptz)'::regprocedure,
      'worker_api.fence_pit_daily_candle_collection_candidate_v1(text,text,bigint,text,text,bigint,jsonb,timestamptz)'::regprocedure,
      'worker_api.pause_pit_daily_candle_collection_attempt_v1(text,text,bigint,text,text,bigint,text,timestamptz)'::regprocedure,
      'worker_api.block_pit_daily_candle_collection_attempt_v1(text,text,bigint,text,text,bigint,text,timestamptz)'::regprocedure,
      'worker_api.confirm_pit_daily_candle_collection_attempt_v1(text,text,bigint,text,text,bigint,jsonb,timestamptz)'::regprocedure
    )
    and pg_catalog.has_function_privilege(
      'service_role', procedure.oid, 'EXECUTE'
    );

  select count(*) into helper_service_acl_count
  from pg_catalog.pg_proc as procedure
  where procedure.oid in (
      'private.pit_daily_candle_collection_uuid4_v1(text)'::regprocedure,
      'private.pit_daily_candle_collection_canonical_json_v1(jsonb)'::regprocedure,
      'private.pit_daily_candle_collection_spec_document_v1(jsonb)'::regprocedure,
      'private.pit_daily_candle_collection_spec_sha256_v1(jsonb)'::regprocedure,
      'private.pit_daily_candle_collection_snapshot_v1(uuid)'::regprocedure,
      'private.pit_daily_candle_collection_event_sha256_v1(uuid,bigint,uuid,uuid,bigint,text,jsonb,jsonb,text,timestamptz)'::regprocedure
    )
    and pg_catalog.has_function_privilege(
      'service_role', procedure.oid, 'EXECUTE'
    );

  select count(*) into forbidden_function_acl_count
  from pg_catalog.pg_proc as procedure
  cross join lateral pg_catalog.aclexplode(
    coalesce(procedure.proacl, pg_catalog.acldefault('f', procedure.proowner))
  ) as acl
  left join pg_catalog.pg_roles as grantee on grantee.oid = acl.grantee
  where procedure.proname like '%pit_daily_candle_collection%'
    and procedure.pronamespace in (
      'private'::regnamespace, 'worker_api'::regnamespace
    )
    and acl.privilege_type = 'EXECUTE'
    and (
      acl.grantee = 0
      or grantee.rolname in ('anon', 'authenticated', 'authenticator')
    );

  select count(distinct procedure.proowner), count(*)
  into function_owner_count, function_count
  from pg_catalog.pg_proc as procedure
  where procedure.proname like '%pit_daily_candle_collection%'
    and procedure.pronamespace in (
      'private'::regnamespace, 'worker_api'::regnamespace
    );

  if table_contract_count <> 2
     or policy_count <> 0
     or forbidden_table_acl_count <> 0
     or forbidden_sequence_acl_count <> 0
     or impl_contract_count <> 7
     or wrapper_contract_count <> 7
     or service_impl_acl_count <> 7
     or service_wrapper_acl_count <> 7
     or helper_service_acl_count <> 0
     or forbidden_function_acl_count <> 0
     or function_owner_count <> 1
     or function_count <> 20 then
    raise exception 'pit_daily_candle_collection_job_store_security_contract_failed'
      using errcode = '55000';
  end if;
end;
$$;

commit;
