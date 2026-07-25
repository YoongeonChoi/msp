begin;

set local lock_timeout = '5s';
set local statement_timeout = '90s';

-- Durable scheduler contract (all timestamps are database-clock observations):
--   ensure -> {definition_id,account_id,job_key,definition_sha256,revision,
--              next_due_at,observed_at}
--   converge -> {status,definition,claim,active_run_id,next_eligible_at,
--                reason_code,observed_at}; status is exactly one of
--                converged, claimed, wait, or manual_resolution
--              converged means the requested digest is installed and no
--              active run remains. Convergence may lease an existing safe
--              recovery run, but never creates a cadence run. Effectful
--              execution/settlement recovery is never auto-claimed.
-- Claims require at least ten database-clock seconds on both the definition
-- lease TTL and the current outer lease so the bounded transport can preserve
-- a five-second minimum handler-start window.
--   claim  -> {claimed,claim,observed_at}; claim is null or
--              {definition,run,lease}
--   complete/fail -> {run_id,state,run_revision,attempt_count,
--                     next_attempt_at,failure_reason_code,result_sha256,
--                     observed_at}
--   inspect -> {found,dead_letter,eligible,ineligibility_reason,observed_at}
--   replay  -> {source_run_id,new_run_id,replay_request_id,job_key,
--               definition_sha256,source_revision,failure_reason_code,
--               replay_generation,state,created_at,observed_at,idempotent}
--
-- The worker lease in private.worker_leases is the outer authority. Every RPC
-- binds account, holder, fencing token, and release SHA to that live lease.
do $$
begin
  if to_regprocedure('private.require_service_role()') is null
     or to_regprocedure('private.reject_append_only_mutation()') is null
     or to_regprocedure('private.pit_sha256_text_v1(text)') is null
     or to_regclass('private.trading_accounts') is null
     or to_regclass('private.worker_leases') is null
     or to_regnamespace('worker_api') is null then
    raise exception 'durable_operations_scheduler_dependency_missing'
      using errcode = '55000';
  end if;
end;
$$;

create table private.scheduler_job_definitions (
  definition_id uuid primary key default pg_catalog.gen_random_uuid(),
  account_id text not null references private.trading_accounts(account_id),
  job_key text not null check (job_key in (
    'operations.commands',
    'operations.execution',
    'operations.settlement',
    'operations.reconciliation',
    'operations.outbox'
  )),
  schema_version text not null
    check (schema_version = 'durable_scheduler_job_definition.v1'),
  definition_sha256 text not null
    check (definition_sha256 ~ '^[0-9a-f]{64}$'),
  interval_seconds integer not null check (interval_seconds between 1 and 86400),
  lease_ttl_seconds integer not null check (lease_ttl_seconds between 10 and 3600),
  max_attempts integer not null check (max_attempts between 1 and 100),
  retry_base_seconds integer not null
    check (retry_base_seconds between 1 and 86400),
  retry_max_seconds integer not null
    check (retry_max_seconds between retry_base_seconds and 604800),
  max_manual_replays integer not null check (max_manual_replays between 0 and 100),
  enabled boolean not null,
  scheduler_state text not null default 'ready'
    check (scheduler_state in ('ready', 'blocked')),
  revision bigint not null default 1 check (revision > 0),
  next_due_at timestamptz not null,
  latest_run_id uuid,
  blocked_by_run_id uuid,
  created_at timestamptz not null,
  updated_at timestamptz not null,
  unique (account_id, job_key),
  unique (definition_id, account_id, job_key),
  check (created_at <= updated_at),
  check (
    (scheduler_state = 'ready' and blocked_by_run_id is null)
    or
    (scheduler_state = 'blocked' and blocked_by_run_id is not null
     and blocked_by_run_id = latest_run_id)
  )
);

create table private.scheduler_job_runs (
  run_id uuid primary key default pg_catalog.gen_random_uuid(),
  definition_id uuid not null,
  account_id text not null,
  job_key text not null,
  definition_sha256 text not null
    check (definition_sha256 ~ '^[0-9a-f]{64}$'),
  state text not null check (state in (
    'pending', 'leased', 'retry_wait', 'succeeded', 'dead_letter'
  )),
  revision bigint not null default 1 check (revision > 0),
  attempt_count integer not null default 0 check (attempt_count between 0 and 100),
  lease_generation bigint not null default 0 check (lease_generation >= 0),
  replay_generation integer not null default 0
    check (replay_generation between 0 and 100),
  replay_of_run_id uuid references private.scheduler_job_runs(run_id),
  scheduled_for timestamptz not null,
  available_at timestamptz,
  lease_token uuid,
  lease_holder_id text,
  lease_release_sha text check (
    lease_release_sha is null
    or lease_release_sha ~ '^[0-9a-f]{40}([0-9a-f]{24})?$'
  ),
  outer_fencing_token bigint check (
    outer_fencing_token is null or outer_fencing_token > 0
  ),
  leased_at timestamptz,
  lease_expires_at timestamptz,
  failure_reason_code text check (
    failure_reason_code is null
    or failure_reason_code ~ '^[a-z][a-z0-9_]{0,127}$'
  ),
  failure_sha256 text check (
    failure_sha256 is null or failure_sha256 ~ '^[0-9a-f]{64}$'
  ),
  failure_retryable boolean,
  result_sha256 text check (
    result_sha256 is null or result_sha256 ~ '^[0-9a-f]{64}$'
  ),
  completed_at timestamptz,
  dead_lettered_at timestamptz,
  created_at timestamptz not null,
  updated_at timestamptz not null,
  foreign key (definition_id, account_id, job_key)
    references private.scheduler_job_definitions(definition_id, account_id, job_key),
  unique (run_id, definition_id),
  unique (definition_id, scheduled_for, replay_generation),
  check (created_at <= updated_at),
  check (
    (replay_generation = 0 and replay_of_run_id is null)
    or
    (replay_generation > 0 and replay_of_run_id is not null
     and replay_of_run_id <> run_id)
  ),
  check (
    (state = 'pending'
     and attempt_count = 0 and lease_generation = 0
     and available_at is not null and available_at >= scheduled_for
     and lease_token is null and lease_holder_id is null
     and lease_release_sha is null and outer_fencing_token is null
     and leased_at is null and lease_expires_at is null
     and failure_reason_code is null and failure_sha256 is null
     and failure_retryable is null and result_sha256 is null
     and completed_at is null and dead_lettered_at is null)
    or
    (state = 'leased'
     and attempt_count > 0 and lease_generation > 0
     and available_at is not null and available_at >= scheduled_for
     and lease_token is not null and lease_holder_id is not null
     and lease_release_sha is not null and outer_fencing_token is not null
     and leased_at is not null and lease_expires_at > leased_at
     and failure_reason_code is null and failure_sha256 is null
     and failure_retryable is null and result_sha256 is null
     and completed_at is null and dead_lettered_at is null)
    or
    (state = 'retry_wait'
     and attempt_count > 0 and lease_generation > 0
     and available_at is not null and available_at > updated_at
     and lease_token is not null and lease_holder_id is not null
     and lease_release_sha is not null and outer_fencing_token is not null
     and leased_at is not null and lease_expires_at > leased_at
     and failure_reason_code is not null and failure_sha256 is not null
     and failure_retryable is true and result_sha256 is null
     and completed_at is null and dead_lettered_at is null)
    or
    (state = 'succeeded'
     and attempt_count > 0 and lease_generation > 0
     and lease_token is not null and lease_holder_id is not null
     and lease_release_sha is not null and outer_fencing_token is not null
     and leased_at is not null and lease_expires_at > leased_at
     and failure_reason_code is null and failure_sha256 is null
     and failure_retryable is null and result_sha256 is not null
     and completed_at is not null and dead_lettered_at is null)
    or
    (state = 'dead_letter'
     and attempt_count > 0 and lease_generation > 0
     and available_at is null
     and lease_token is not null and lease_holder_id is not null
     and lease_release_sha is not null and outer_fencing_token is not null
     and leased_at is not null and lease_expires_at > leased_at
     and failure_reason_code is not null and failure_sha256 is not null
     and failure_retryable is not null and result_sha256 is null
     and completed_at is null and dead_lettered_at is not null)
  )
);

alter table private.scheduler_job_definitions
  add constraint scheduler_job_definitions_latest_run_fk
  foreign key (latest_run_id, definition_id)
  references private.scheduler_job_runs(run_id, definition_id),
  add constraint scheduler_job_definitions_blocked_run_fk
  foreign key (blocked_by_run_id, definition_id)
  references private.scheduler_job_runs(run_id, definition_id);

create unique index scheduler_job_runs_one_active_per_definition_idx
  on private.scheduler_job_runs (definition_id)
  where state in ('pending', 'leased', 'retry_wait');

create index scheduler_job_definitions_due_idx
  on private.scheduler_job_definitions (
    account_id,
    scheduler_state,
    enabled,
    next_due_at,
    job_key
  );

create index scheduler_job_runs_due_idx
  on private.scheduler_job_runs (definition_id, state, available_at, lease_expires_at)
  where state in ('pending', 'leased', 'retry_wait');

create table private.scheduler_job_leases (
  lease_token uuid primary key,
  run_id uuid not null,
  definition_id uuid not null,
  account_id text not null,
  holder_id text not null,
  release_sha text not null
    check (release_sha ~ '^[0-9a-f]{40}([0-9a-f]{24})?$'),
  outer_fencing_token bigint not null check (outer_fencing_token > 0),
  attempt_number integer not null check (attempt_number between 1 and 100),
  lease_generation bigint not null check (lease_generation > 0),
  run_revision bigint not null check (run_revision > 1),
  leased_at timestamptz not null,
  lease_expires_at timestamptz not null,
  foreign key (run_id, definition_id)
    references private.scheduler_job_runs(run_id, definition_id),
  unique (run_id, attempt_number),
  unique (run_id, lease_generation),
  check (lease_expires_at > leased_at)
);

create table private.scheduler_replay_requests (
  replay_request_id uuid primary key,
  source_run_id uuid not null unique references private.scheduler_job_runs(run_id),
  new_run_id uuid not null unique references private.scheduler_job_runs(run_id),
  definition_id uuid not null references private.scheduler_job_definitions(definition_id),
  account_id text not null,
  expected_source_revision bigint not null check (expected_source_revision > 0),
  expected_definition_sha256 text not null
    check (expected_definition_sha256 ~ '^[0-9a-f]{64}$'),
  expected_failure_reason_code text not null
    check (expected_failure_reason_code ~ '^[a-z][a-z0-9_]{0,127}$'),
  expected_failure_sha256 text not null
    check (expected_failure_sha256 ~ '^[0-9a-f]{64}$'),
  expected_replay_generation integer not null
    check (expected_replay_generation between 0 and 99),
  confirmed_reason_code text not null
    check (confirmed_reason_code ~ '^[a-z][a-z0-9_]{0,127}$'),
  holder_id text not null,
  outer_fencing_token bigint not null check (outer_fencing_token > 0),
  release_sha text not null
    check (release_sha ~ '^[0-9a-f]{40}([0-9a-f]{24})?$'),
  replay_generation integer not null check (replay_generation between 1 and 100),
  request_sha256 text not null check (request_sha256 ~ '^[0-9a-f]{64}$'),
  requested_at timestamptz not null,
  check (source_run_id <> new_run_id),
  check (replay_generation = expected_replay_generation + 1),
  check (confirmed_reason_code = expected_failure_reason_code)
);

alter table private.scheduler_job_definitions enable row level security;
alter table private.scheduler_job_definitions force row level security;
alter table private.scheduler_job_runs enable row level security;
alter table private.scheduler_job_runs force row level security;
alter table private.scheduler_job_leases enable row level security;
alter table private.scheduler_job_leases force row level security;
alter table private.scheduler_replay_requests enable row level security;
alter table private.scheduler_replay_requests force row level security;

revoke all on table
  private.scheduler_job_definitions,
  private.scheduler_job_runs,
  private.scheduler_job_leases,
  private.scheduler_replay_requests
from public, anon, authenticated, authenticator, service_role;

create or replace function private.scheduler_definition_sha256_v1(
  p_job_key text,
  p_interval_seconds integer,
  p_lease_ttl_seconds integer,
  p_max_attempts integer,
  p_retry_base_seconds integer,
  p_retry_max_seconds integer,
  p_max_manual_replays integer,
  p_enabled boolean
)
returns text
language sql
immutable
strict
security definer
set search_path = ''
as $$
  select private.pit_sha256_text_v1(
    '{"enabled":' || case when p_enabled then 'true' else 'false' end ||
    ',"interval_seconds":' || p_interval_seconds::text ||
    ',"job_key":' || pg_catalog.to_jsonb(p_job_key)::text ||
    ',"lease_ttl_seconds":' || p_lease_ttl_seconds::text ||
    ',"max_attempts":' || p_max_attempts::text ||
    ',"max_manual_replays":' || p_max_manual_replays::text ||
    ',"retry_base_seconds":' || p_retry_base_seconds::text ||
    ',"retry_max_seconds":' || p_retry_max_seconds::text ||
    ',"schema_version":"durable_scheduler_job_definition.v1"}'
  );
$$;

create or replace function private.require_scheduler_outer_lease_v1(
  p_account_id text,
  p_holder_id text,
  p_outer_fencing_token bigint,
  p_release_sha text,
  p_observed_at timestamptz
)
returns void
language plpgsql
volatile
security definer
set search_path = ''
as $$
begin
  perform private.require_service_role();
  if nullif(pg_catalog.btrim(p_account_id), '') is null
     or p_holder_id !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
     or p_outer_fencing_token is null
     or p_outer_fencing_token <= 0
     or p_release_sha !~ '^[0-9a-f]{40}([0-9a-f]{24})?$' then
    raise exception 'scheduler_outer_lease_parameters_invalid'
      using errcode = '22023';
  end if;

  perform 1
  from private.worker_leases as outer_lease
  where outer_lease.account_id = p_account_id
    and outer_lease.holder_id = p_holder_id
    and outer_lease.fencing_token = p_outer_fencing_token
    and outer_lease.release_sha = p_release_sha
    and outer_lease.acquired_at <= p_observed_at
    and outer_lease.expires_at > p_observed_at
  for share;
  if not found then
    raise exception 'scheduler_outer_lease_stale_or_missing'
      using errcode = '40001';
  end if;
end;
$$;

create or replace function private.scheduler_command_barrier_satisfied_v1(
  p_account_id text,
  p_holder_id text,
  p_outer_fencing_token bigint,
  p_release_sha text,
  p_outer_acquired_at timestamptz,
  p_observed_at timestamptz
)
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select exists (
    select 1
    from private.scheduler_job_definitions as command_definition
    where command_definition.account_id = p_account_id
      and command_definition.job_key = 'operations.commands'
      and command_definition.enabled
      and command_definition.scheduler_state = 'ready'
      and command_definition.next_due_at > p_observed_at
      and not exists (
        select 1
        from private.scheduler_job_runs as active_command
        where active_command.definition_id = command_definition.definition_id
          and active_command.state in ('pending', 'leased', 'retry_wait')
      )
      and exists (
        select 1
        from private.scheduler_job_definitions as settlement_definition
        where settlement_definition.account_id = p_account_id
          and settlement_definition.job_key = 'operations.settlement'
          and settlement_definition.enabled
          and settlement_definition.scheduler_state = 'ready'
          and not exists (
            select 1
            from private.scheduler_job_runs as uncertain_settlement
            where uncertain_settlement.definition_id = settlement_definition.definition_id
              and uncertain_settlement.state = 'leased'
              and uncertain_settlement.lease_expires_at <= p_observed_at
          )
      )
      and exists (
        select 1
        from private.scheduler_job_definitions as reconciliation_definition
        where reconciliation_definition.account_id = p_account_id
          and reconciliation_definition.job_key = 'operations.reconciliation'
          and reconciliation_definition.enabled
          and reconciliation_definition.scheduler_state = 'ready'
          and not exists (
            select 1
            from private.scheduler_job_runs as uncertain_reconciliation
            where uncertain_reconciliation.definition_id = reconciliation_definition.definition_id
              and uncertain_reconciliation.state = 'leased'
              and uncertain_reconciliation.lease_expires_at <= p_observed_at
          )
      )
      and exists (
        select 1
        from private.scheduler_job_runs as completed_command
        where completed_command.run_id = command_definition.latest_run_id
          and completed_command.definition_id = command_definition.definition_id
          and completed_command.definition_sha256 = command_definition.definition_sha256
          and completed_command.state = 'succeeded'
          and completed_command.lease_holder_id = p_holder_id
          and completed_command.outer_fencing_token = p_outer_fencing_token
          and completed_command.lease_release_sha = p_release_sha
          and completed_command.completed_at >= p_outer_acquired_at
      )
  );
$$;

create or replace function private.scheduler_definition_document_v1(
  p_definition private.scheduler_job_definitions
)
returns jsonb
language sql
stable
strict
security definer
set search_path = ''
as $$
  select pg_catalog.jsonb_build_object(
    'schema_version', p_definition.schema_version,
    'job_key', p_definition.job_key,
    'interval_seconds', p_definition.interval_seconds,
    'lease_ttl_seconds', p_definition.lease_ttl_seconds,
    'max_attempts', p_definition.max_attempts,
    'retry_base_seconds', p_definition.retry_base_seconds,
    'retry_max_seconds', p_definition.retry_max_seconds,
    'max_manual_replays', p_definition.max_manual_replays,
    'enabled', p_definition.enabled,
    'definition_sha256', p_definition.definition_sha256
  );
$$;

create or replace function private.scheduler_run_document_v1(
  p_run private.scheduler_job_runs
)
returns jsonb
language sql
stable
strict
security definer
set search_path = ''
as $$
  select pg_catalog.jsonb_build_object(
    'run_id', p_run.run_id,
    'account_id', p_run.account_id,
    'job_key', p_run.job_key,
    'definition_sha256', p_run.definition_sha256,
    'state', p_run.state,
    'revision', p_run.revision,
    'attempt_count', p_run.attempt_count,
    'replay_generation', p_run.replay_generation,
    'replay_of_run_id', p_run.replay_of_run_id,
    'scheduled_for', p_run.scheduled_for,
    'available_at', p_run.available_at,
    'created_at', p_run.created_at,
    'updated_at', p_run.updated_at
  );
$$;

create or replace function private.guard_scheduler_job_definition_v1()
returns trigger
language plpgsql
volatile
security definer
set search_path = ''
as $$
begin
  if tg_op = 'DELETE' then
    raise exception 'scheduler_job_definition_delete_forbidden'
      using errcode = '55000';
  end if;
  if new.definition_id is distinct from old.definition_id
     or new.account_id is distinct from old.account_id
     or new.job_key is distinct from old.job_key
     or new.created_at is distinct from old.created_at then
    raise exception 'scheduler_job_definition_identity_is_immutable'
      using errcode = '23514';
  end if;
  if new.revision <> old.revision + 1 or new.updated_at < old.updated_at then
    raise exception 'scheduler_job_definition_revision_invalid'
      using errcode = '40001';
  end if;
  return new;
end;
$$;

create or replace function private.guard_scheduler_job_run_v1()
returns trigger
language plpgsql
volatile
security definer
set search_path = ''
as $$
begin
  if tg_op = 'DELETE' then
    raise exception 'scheduler_job_run_delete_forbidden' using errcode = '55000';
  end if;
  if old.state in ('succeeded', 'dead_letter') then
    raise exception 'scheduler_terminal_run_is_immutable' using errcode = '55000';
  end if;
  if new.run_id is distinct from old.run_id
     or new.definition_id is distinct from old.definition_id
     or new.account_id is distinct from old.account_id
     or new.job_key is distinct from old.job_key
     or new.definition_sha256 is distinct from old.definition_sha256
     or new.replay_generation is distinct from old.replay_generation
     or new.replay_of_run_id is distinct from old.replay_of_run_id
     or new.scheduled_for is distinct from old.scheduled_for
     or new.created_at is distinct from old.created_at then
    raise exception 'scheduler_job_run_identity_is_immutable'
      using errcode = '23514';
  end if;
  if new.revision <> old.revision + 1 or new.updated_at < old.updated_at then
    raise exception 'scheduler_job_run_revision_invalid' using errcode = '40001';
  end if;

  if old.state in ('pending', 'retry_wait') and new.state = 'leased' then
    if new.attempt_count <> old.attempt_count + 1
       or new.lease_generation <> old.lease_generation + 1
       or new.lease_token is null then
      raise exception 'scheduler_claim_transition_invalid' using errcode = '23514';
    end if;
  elsif old.state = 'leased'
        and new.state in ('retry_wait', 'succeeded', 'dead_letter') then
    if new.attempt_count <> old.attempt_count
       or new.lease_generation <> old.lease_generation
       or new.lease_token is distinct from old.lease_token
       or new.lease_holder_id is distinct from old.lease_holder_id
       or new.lease_release_sha is distinct from old.lease_release_sha
       or new.outer_fencing_token is distinct from old.outer_fencing_token
       or new.leased_at is distinct from old.leased_at
       or new.lease_expires_at is distinct from old.lease_expires_at then
      raise exception 'scheduler_settlement_transition_invalid'
        using errcode = '23514';
    end if;
  else
    raise exception 'scheduler_job_run_state_transition_invalid'
      using errcode = '23514';
  end if;
  return new;
end;
$$;

create trigger guard_scheduler_job_definition
before update or delete on private.scheduler_job_definitions
for each row execute function private.guard_scheduler_job_definition_v1();

create trigger guard_scheduler_job_run
before update or delete on private.scheduler_job_runs
for each row execute function private.guard_scheduler_job_run_v1();

create trigger reject_scheduler_job_lease_mutation
before update or delete on private.scheduler_job_leases
for each row execute function private.reject_append_only_mutation();

create trigger reject_scheduler_replay_request_mutation
before update or delete on private.scheduler_replay_requests
for each row execute function private.reject_append_only_mutation();

create or replace function private.ensure_scheduler_job_definition_impl(
  p_account_id text,
  p_holder_id text,
  p_outer_fencing_token bigint,
  p_release_sha text,
  p_job_key text,
  p_definition_sha256 text,
  p_interval_seconds integer,
  p_lease_ttl_seconds integer,
  p_max_attempts integer,
  p_retry_base_seconds integer,
  p_retry_max_seconds integer,
  p_max_manual_replays integer,
  p_enabled boolean
)
returns table (
  definition_id uuid,
  account_id text,
  job_key text,
  definition_sha256 text,
  revision bigint,
  next_due_at timestamptz,
  observed_at timestamptz
)
language plpgsql
volatile
security definer
set search_path = ''
as $$
declare
  authorization_time timestamptz := pg_catalog.clock_timestamp();
  definition_row private.scheduler_job_definitions%rowtype;
  calculated_sha256 text;
  definition_found boolean;
begin
  perform private.require_scheduler_outer_lease_v1(
    p_account_id,
    p_holder_id,
    p_outer_fencing_token,
    p_release_sha,
    authorization_time
  );

  if p_job_key not in (
       'operations.commands',
       'operations.execution',
       'operations.settlement',
       'operations.reconciliation',
       'operations.outbox'
     )
     or p_definition_sha256 !~ '^[0-9a-f]{64}$'
     or p_interval_seconds not between 1 and 86400
     or p_lease_ttl_seconds not between 10 and 3600
     or p_max_attempts not between 1 and 100
     or p_retry_base_seconds not between 1 and 86400
     or p_retry_max_seconds not between p_retry_base_seconds and 604800
     or p_max_manual_replays not between 0 and 100
     or p_enabled is null then
    raise exception 'scheduler_definition_parameters_invalid'
      using errcode = '22023';
  end if;

  calculated_sha256 := private.scheduler_definition_sha256_v1(
    p_job_key,
    p_interval_seconds,
    p_lease_ttl_seconds,
    p_max_attempts,
    p_retry_base_seconds,
    p_retry_max_seconds,
    p_max_manual_replays,
    p_enabled
  );
  if calculated_sha256 is distinct from p_definition_sha256 then
    raise exception 'scheduler_definition_digest_mismatch'
      using errcode = '22023';
  end if;

  select definition.* into definition_row
  from private.scheduler_job_definitions as definition
  where definition.account_id = p_account_id
    and definition.job_key = p_job_key
  for update;
  definition_found := found;

  authorization_time := pg_catalog.clock_timestamp();
  perform private.require_scheduler_outer_lease_v1(
    p_account_id,
    p_holder_id,
    p_outer_fencing_token,
    p_release_sha,
    authorization_time
  );

  if not definition_found then
    insert into private.scheduler_job_definitions (
      account_id,
      job_key,
      schema_version,
      definition_sha256,
      interval_seconds,
      lease_ttl_seconds,
      max_attempts,
      retry_base_seconds,
      retry_max_seconds,
      max_manual_replays,
      enabled,
      scheduler_state,
      revision,
      next_due_at,
      created_at,
      updated_at
    ) values (
      p_account_id,
      p_job_key,
      'durable_scheduler_job_definition.v1',
      p_definition_sha256,
      p_interval_seconds,
      p_lease_ttl_seconds,
      p_max_attempts,
      p_retry_base_seconds,
      p_retry_max_seconds,
      p_max_manual_replays,
      p_enabled,
      'ready',
      1,
      authorization_time,
      authorization_time,
      authorization_time
    )
    on conflict (account_id, job_key) do nothing
    returning * into definition_row;
    if not found then
      select candidate.* into definition_row
      from private.scheduler_job_definitions as candidate
      where candidate.account_id = p_account_id
        and candidate.job_key = p_job_key
      for update;
      if not found then
        raise exception 'scheduler_definition_compare_and_swap_failed'
          using errcode = '40001';
      end if;
    end if;
    definition_found := true;
    authorization_time := pg_catalog.clock_timestamp();
    perform private.require_scheduler_outer_lease_v1(
      p_account_id,
      p_holder_id,
      p_outer_fencing_token,
      p_release_sha,
      authorization_time
    );
  end if;

  if definition_row.definition_sha256 = p_definition_sha256 then
    if definition_row.schema_version <> 'durable_scheduler_job_definition.v1'
       or definition_row.interval_seconds <> p_interval_seconds
       or definition_row.lease_ttl_seconds <> p_lease_ttl_seconds
       or definition_row.max_attempts <> p_max_attempts
       or definition_row.retry_base_seconds <> p_retry_base_seconds
       or definition_row.retry_max_seconds <> p_retry_max_seconds
       or definition_row.max_manual_replays <> p_max_manual_replays
       or definition_row.enabled is distinct from p_enabled then
      raise exception 'scheduler_definition_digest_collision_or_corruption'
        using errcode = '55000';
    end if;
  else
    if definition_row.scheduler_state = 'blocked'
       or exists (
         select 1
         from private.scheduler_job_runs as active_run
         where active_run.definition_id = definition_row.definition_id
           and active_run.state in ('pending', 'leased', 'retry_wait')
       ) then
      raise exception 'scheduler_definition_change_requires_quiescence'
        using errcode = '55000';
    end if;

    update private.scheduler_job_definitions as definition
    set schema_version = 'durable_scheduler_job_definition.v1',
        definition_sha256 = p_definition_sha256,
        interval_seconds = p_interval_seconds,
        lease_ttl_seconds = p_lease_ttl_seconds,
        max_attempts = p_max_attempts,
        retry_base_seconds = p_retry_base_seconds,
        retry_max_seconds = p_retry_max_seconds,
        max_manual_replays = p_max_manual_replays,
        enabled = p_enabled,
        revision = definition.revision + 1,
        next_due_at = authorization_time,
        updated_at = authorization_time
    where definition.definition_id = definition_row.definition_id
      and definition.revision = definition_row.revision
      and exists (
        select 1
        from private.worker_leases as current_outer_lease
        where current_outer_lease.account_id = p_account_id
          and current_outer_lease.holder_id = p_holder_id
          and current_outer_lease.fencing_token = p_outer_fencing_token
          and current_outer_lease.release_sha = p_release_sha
          and current_outer_lease.expires_at > authorization_time
      )
    returning definition.* into definition_row;
    if not found then
      raise exception 'scheduler_definition_compare_and_swap_failed'
        using errcode = '40001';
    end if;
  end if;

  return query
  select
    definition_row.definition_id,
    definition_row.account_id,
    definition_row.job_key,
    definition_row.definition_sha256,
    definition_row.revision,
    definition_row.next_due_at,
    authorization_time;
end;
$$;

create or replace function worker_api.ensure_scheduler_job_definition(
  p_account_id text,
  p_holder_id text,
  p_outer_fencing_token bigint,
  p_release_sha text,
  p_job_key text,
  p_definition_sha256 text,
  p_interval_seconds integer,
  p_lease_ttl_seconds integer,
  p_max_attempts integer,
  p_retry_base_seconds integer,
  p_retry_max_seconds integer,
  p_max_manual_replays integer,
  p_enabled boolean
)
returns table (
  definition_id uuid,
  account_id text,
  job_key text,
  definition_sha256 text,
  revision bigint,
  next_due_at timestamptz,
  observed_at timestamptz
)
language sql
volatile
security invoker
set search_path = ''
as $$
  select *
  from private.ensure_scheduler_job_definition_impl(
    p_account_id,
    p_holder_id,
    p_outer_fencing_token,
    p_release_sha,
    p_job_key,
    p_definition_sha256,
    p_interval_seconds,
    p_lease_ttl_seconds,
    p_max_attempts,
    p_retry_base_seconds,
    p_retry_max_seconds,
    p_max_manual_replays,
    p_enabled
  );
$$;

create or replace function private.converge_scheduler_job_definition_impl(
  p_account_id text,
  p_holder_id text,
  p_outer_fencing_token bigint,
  p_release_sha text,
  p_job_key text,
  p_definition_sha256 text,
  p_interval_seconds integer,
  p_lease_ttl_seconds integer,
  p_max_attempts integer,
  p_retry_base_seconds integer,
  p_retry_max_seconds integer,
  p_max_manual_replays integer,
  p_enabled boolean
)
returns table (
  status text,
  definition jsonb,
  claim jsonb,
  active_run_id uuid,
  next_eligible_at timestamptz,
  reason_code text,
  observed_at timestamptz
)
language plpgsql
volatile
security definer
set search_path = ''
as $$
declare
  authorization_time timestamptz := pg_catalog.clock_timestamp();
  definition_row private.scheduler_job_definitions%rowtype;
  run_row private.scheduler_job_runs%rowtype;
  lease_row private.scheduler_job_leases%rowtype;
  outer_lease_expires_at timestamptz;
  calculated_sha256 text;
  definition_found boolean;
  definition_inserted boolean;
  definition_matches boolean := false;
  active_run_found boolean;
  retry_delay_seconds integer;
  definition_document jsonb;
begin
  perform private.require_scheduler_outer_lease_v1(
    p_account_id,
    p_holder_id,
    p_outer_fencing_token,
    p_release_sha,
    authorization_time
  );
  if p_job_key not in (
       'operations.commands',
       'operations.execution',
       'operations.settlement',
       'operations.reconciliation',
       'operations.outbox'
     )
     or p_definition_sha256 !~ '^[0-9a-f]{64}$'
     or p_interval_seconds not between 1 and 86400
     or p_lease_ttl_seconds not between 10 and 3600
     or p_max_attempts not between 1 and 100
     or p_retry_base_seconds not between 1 and 86400
     or p_retry_max_seconds not between p_retry_base_seconds and 604800
     or p_max_manual_replays not between 0 and 100
     or p_enabled is null then
    raise exception 'scheduler_definition_parameters_invalid'
      using errcode = '22023';
  end if;

  calculated_sha256 := private.scheduler_definition_sha256_v1(
    p_job_key,
    p_interval_seconds,
    p_lease_ttl_seconds,
    p_max_attempts,
    p_retry_base_seconds,
    p_retry_max_seconds,
    p_max_manual_replays,
    p_enabled
  );
  if calculated_sha256 is distinct from p_definition_sha256 then
    raise exception 'scheduler_definition_digest_mismatch'
      using errcode = '22023';
  end if;

  select candidate.* into definition_row
  from private.scheduler_job_definitions as candidate
  where candidate.account_id = p_account_id
    and candidate.job_key = p_job_key
  for update;
  definition_found := found;

  authorization_time := pg_catalog.clock_timestamp();
  perform private.require_scheduler_outer_lease_v1(
    p_account_id,
    p_holder_id,
    p_outer_fencing_token,
    p_release_sha,
    authorization_time
  );
  select outer_lease.expires_at into outer_lease_expires_at
  from private.worker_leases as outer_lease
  where outer_lease.account_id = p_account_id
    and outer_lease.holder_id = p_holder_id
    and outer_lease.fencing_token = p_outer_fencing_token
    and outer_lease.release_sha = p_release_sha
    and outer_lease.expires_at > authorization_time
  for share;
  if not found then
    raise exception 'scheduler_outer_lease_stale_or_missing'
      using errcode = '40001';
  end if;

  if not definition_found then
    insert into private.scheduler_job_definitions (
      account_id,
      job_key,
      schema_version,
      definition_sha256,
      interval_seconds,
      lease_ttl_seconds,
      max_attempts,
      retry_base_seconds,
      retry_max_seconds,
      max_manual_replays,
      enabled,
      scheduler_state,
      revision,
      next_due_at,
      created_at,
      updated_at
    ) values (
      p_account_id,
      p_job_key,
      'durable_scheduler_job_definition.v1',
      p_definition_sha256,
      p_interval_seconds,
      p_lease_ttl_seconds,
      p_max_attempts,
      p_retry_base_seconds,
      p_retry_max_seconds,
      p_max_manual_replays,
      p_enabled,
      'ready',
      1,
      authorization_time,
      authorization_time,
      authorization_time
    )
    on conflict (account_id, job_key) do nothing
    returning * into definition_row;
    definition_inserted := found;
    authorization_time := pg_catalog.clock_timestamp();
    perform private.require_scheduler_outer_lease_v1(
      p_account_id,
      p_holder_id,
      p_outer_fencing_token,
      p_release_sha,
      authorization_time
    );
    select outer_lease.expires_at into outer_lease_expires_at
    from private.worker_leases as outer_lease
    where outer_lease.account_id = p_account_id
      and outer_lease.holder_id = p_holder_id
      and outer_lease.fencing_token = p_outer_fencing_token
      and outer_lease.release_sha = p_release_sha
      and outer_lease.expires_at > authorization_time
    for share;
    if not found then
      raise exception 'scheduler_outer_lease_stale_or_missing'
        using errcode = '40001';
    end if;
    if definition_inserted then
      definition_document := private.scheduler_definition_document_v1(definition_row)
        || pg_catalog.jsonb_build_object(
          'definition_id', definition_row.definition_id,
          'account_id', definition_row.account_id,
          'revision', definition_row.revision,
          'next_due_at', definition_row.next_due_at,
          'scheduler_state', definition_row.scheduler_state
        );
      return query
      select
        'converged'::text,
        definition_document,
        null::jsonb,
        null::uuid,
        null::timestamptz,
        null::text,
        authorization_time;
      return;
    end if;

    select candidate.* into definition_row
    from private.scheduler_job_definitions as candidate
    where candidate.account_id = p_account_id
      and candidate.job_key = p_job_key
    for update;
    if not found then
      raise exception 'scheduler_definition_compare_and_swap_failed'
        using errcode = '40001';
    end if;
    definition_found := true;
    authorization_time := pg_catalog.clock_timestamp();
    perform private.require_scheduler_outer_lease_v1(
      p_account_id,
      p_holder_id,
      p_outer_fencing_token,
      p_release_sha,
      authorization_time
    );
  end if;

  if definition_row.definition_sha256 = p_definition_sha256 then
    if definition_row.schema_version <> 'durable_scheduler_job_definition.v1'
       or definition_row.interval_seconds <> p_interval_seconds
       or definition_row.lease_ttl_seconds <> p_lease_ttl_seconds
       or definition_row.max_attempts <> p_max_attempts
       or definition_row.retry_base_seconds <> p_retry_base_seconds
       or definition_row.retry_max_seconds <> p_retry_max_seconds
       or definition_row.max_manual_replays <> p_max_manual_replays
       or definition_row.enabled is distinct from p_enabled then
      raise exception 'scheduler_definition_digest_collision_or_corruption'
        using errcode = '55000';
    end if;
    definition_matches := true;
  end if;

  if definition_found and definition_row.scheduler_state = 'blocked' then
    definition_document := private.scheduler_definition_document_v1(definition_row)
      || pg_catalog.jsonb_build_object(
        'definition_id', definition_row.definition_id,
        'account_id', definition_row.account_id,
        'revision', definition_row.revision,
        'next_due_at', definition_row.next_due_at,
        'scheduler_state', definition_row.scheduler_state
      );
    return query
    select
      'manual_resolution'::text,
      definition_document,
      null::jsonb,
      definition_row.blocked_by_run_id,
      null::timestamptz,
      case
        when definition_row.job_key in (
          'operations.execution', 'operations.settlement'
        ) then 'effectful_job_requires_resolution_evidence'::text
        else 'scheduler_dead_letter_requires_manual_replay'::text
      end,
      authorization_time;
    return;
  end if;

  if definition_found then
    select active_run.* into run_row
    from private.scheduler_job_runs as active_run
    where active_run.definition_id = definition_row.definition_id
      and active_run.state in ('pending', 'leased', 'retry_wait')
    for update;
    active_run_found := found;
  else
    active_run_found := false;
  end if;

  authorization_time := pg_catalog.clock_timestamp();
  perform private.require_scheduler_outer_lease_v1(
    p_account_id,
    p_holder_id,
    p_outer_fencing_token,
    p_release_sha,
    authorization_time
  );
  select outer_lease.expires_at into outer_lease_expires_at
  from private.worker_leases as outer_lease
  where outer_lease.account_id = p_account_id
    and outer_lease.holder_id = p_holder_id
    and outer_lease.fencing_token = p_outer_fencing_token
    and outer_lease.release_sha = p_release_sha
    and outer_lease.expires_at > authorization_time
  for share;
  if not found then
    raise exception 'scheduler_outer_lease_stale_or_missing'
      using errcode = '40001';
  end if;

  if definition_found and not active_run_found and definition_matches then
    definition_document := private.scheduler_definition_document_v1(definition_row)
      || pg_catalog.jsonb_build_object(
        'definition_id', definition_row.definition_id,
        'account_id', definition_row.account_id,
        'revision', definition_row.revision,
        'next_due_at', definition_row.next_due_at,
        'scheduler_state', definition_row.scheduler_state
      );
    return query
    select
      'converged'::text,
      definition_document,
      null::jsonb,
      null::uuid,
      null::timestamptz,
      null::text,
      authorization_time;
    return;
  end if;

  if definition_found and not active_run_found then
    update private.scheduler_job_definitions as candidate
    set schema_version = 'durable_scheduler_job_definition.v1',
        definition_sha256 = p_definition_sha256,
        interval_seconds = p_interval_seconds,
        lease_ttl_seconds = p_lease_ttl_seconds,
        max_attempts = p_max_attempts,
        retry_base_seconds = p_retry_base_seconds,
        retry_max_seconds = p_retry_max_seconds,
        max_manual_replays = p_max_manual_replays,
        enabled = p_enabled,
        revision = candidate.revision + 1,
        next_due_at = authorization_time,
        updated_at = authorization_time
    where candidate.definition_id = definition_row.definition_id
      and candidate.revision = definition_row.revision
      and candidate.scheduler_state = 'ready'
      and not exists (
        select 1
        from private.scheduler_job_runs as active_run
        where active_run.definition_id = candidate.definition_id
          and active_run.state in ('pending', 'leased', 'retry_wait')
      )
      and exists (
        select 1
        from private.worker_leases as current_outer_lease
        where current_outer_lease.account_id = p_account_id
          and current_outer_lease.holder_id = p_holder_id
          and current_outer_lease.fencing_token = p_outer_fencing_token
          and current_outer_lease.release_sha = p_release_sha
          and current_outer_lease.expires_at > authorization_time
      )
    returning candidate.* into definition_row;
    if not found then
      raise exception 'scheduler_definition_compare_and_swap_failed'
        using errcode = '40001';
    end if;

    definition_document := private.scheduler_definition_document_v1(definition_row)
      || pg_catalog.jsonb_build_object(
        'definition_id', definition_row.definition_id,
        'account_id', definition_row.account_id,
        'revision', definition_row.revision,
        'next_due_at', definition_row.next_due_at,
        'scheduler_state', definition_row.scheduler_state
      );
    return query
    select
      'converged'::text,
      definition_document,
      null::jsonb,
      null::uuid,
      null::timestamptz,
      null::text,
      authorization_time;
    return;
  end if;

  definition_document := private.scheduler_definition_document_v1(definition_row)
    || pg_catalog.jsonb_build_object(
      'definition_id', definition_row.definition_id,
      'account_id', definition_row.account_id,
      'revision', definition_row.revision,
      'next_due_at', definition_row.next_due_at,
      'scheduler_state', definition_row.scheduler_state
    );

  if definition_row.job_key in (
       'operations.execution', 'operations.settlement'
     ) then
    if run_row.state = 'leased'
       and run_row.lease_expires_at > authorization_time then
      return query
      select
        'wait'::text,
        definition_document,
        null::jsonb,
        run_row.run_id,
        run_row.lease_expires_at,
        'scheduler_effectful_lease_active'::text,
        authorization_time;
      return;
    end if;

    if run_row.state = 'leased'
       and run_row.lease_expires_at <= authorization_time then
      update private.scheduler_job_runs as expired_run
      set state = 'dead_letter',
          revision = expired_run.revision + 1,
          available_at = null,
          failure_reason_code = 'scheduler_lease_expired',
          failure_sha256 = private.pit_sha256_text_v1(
            'scheduler_lease_expired:' || expired_run.run_id::text || ':' ||
            expired_run.lease_token::text
          ),
          failure_retryable = false,
          dead_lettered_at = authorization_time,
          updated_at = authorization_time
      where expired_run.run_id = run_row.run_id
        and expired_run.revision = run_row.revision
        and expired_run.state = 'leased'
        and expired_run.lease_expires_at <= authorization_time
        and exists (
          select 1
          from private.worker_leases as current_outer_lease
          where current_outer_lease.account_id = p_account_id
            and current_outer_lease.holder_id = p_holder_id
            and current_outer_lease.fencing_token = p_outer_fencing_token
            and current_outer_lease.release_sha = p_release_sha
            and current_outer_lease.expires_at > authorization_time
        )
      returning expired_run.* into run_row;
      if not found then
        raise exception 'scheduler_lease_expiry_compare_and_swap_failed'
          using errcode = '40001';
      end if;

      update private.scheduler_job_definitions as blocked_definition
      set scheduler_state = 'blocked',
          blocked_by_run_id = run_row.run_id,
          latest_run_id = run_row.run_id,
          revision = blocked_definition.revision + 1,
          updated_at = authorization_time
      where blocked_definition.definition_id = definition_row.definition_id
        and blocked_definition.revision = definition_row.revision
      returning blocked_definition.* into definition_row;
      if not found then
        raise exception 'scheduler_definition_compare_and_swap_failed'
          using errcode = '40001';
      end if;
      definition_document := private.scheduler_definition_document_v1(definition_row)
        || pg_catalog.jsonb_build_object(
          'definition_id', definition_row.definition_id,
          'account_id', definition_row.account_id,
          'revision', definition_row.revision,
          'next_due_at', definition_row.next_due_at,
          'scheduler_state', definition_row.scheduler_state
        );
    end if;

    return query
    select
      'manual_resolution'::text,
      definition_document,
      null::jsonb,
      run_row.run_id,
      null::timestamptz,
      'effectful_job_requires_resolution_evidence'::text,
      authorization_time;
    return;
  end if;

  if run_row.state = 'leased' then
    if run_row.lease_expires_at > authorization_time then
      return query
      select
        'wait'::text,
        definition_document,
        null::jsonb,
        run_row.run_id,
        run_row.lease_expires_at,
        'scheduler_lease_active'::text,
        authorization_time;
      return;
    end if;

    if run_row.attempt_count >= definition_row.max_attempts then
      update private.scheduler_job_runs as expired_run
      set state = 'dead_letter',
          revision = expired_run.revision + 1,
          available_at = null,
          failure_reason_code = 'scheduler_lease_expired',
          failure_sha256 = private.pit_sha256_text_v1(
            'scheduler_lease_expired:' || expired_run.run_id::text || ':' ||
            expired_run.lease_token::text
          ),
          failure_retryable = true,
          dead_lettered_at = authorization_time,
          updated_at = authorization_time
      where expired_run.run_id = run_row.run_id
        and expired_run.revision = run_row.revision
        and expired_run.state = 'leased'
        and expired_run.lease_expires_at <= authorization_time
        and exists (
          select 1
          from private.worker_leases as current_outer_lease
          where current_outer_lease.account_id = p_account_id
            and current_outer_lease.holder_id = p_holder_id
            and current_outer_lease.fencing_token = p_outer_fencing_token
            and current_outer_lease.release_sha = p_release_sha
            and current_outer_lease.expires_at > authorization_time
        )
      returning expired_run.* into run_row;
      if not found then
        raise exception 'scheduler_lease_expiry_compare_and_swap_failed'
          using errcode = '40001';
      end if;

      update private.scheduler_job_definitions as blocked_definition
      set scheduler_state = 'blocked',
          blocked_by_run_id = run_row.run_id,
          latest_run_id = run_row.run_id,
          revision = blocked_definition.revision + 1,
          updated_at = authorization_time
      where blocked_definition.definition_id = definition_row.definition_id
        and blocked_definition.revision = definition_row.revision
      returning blocked_definition.* into definition_row;
      if not found then
        raise exception 'scheduler_definition_compare_and_swap_failed'
          using errcode = '40001';
      end if;
      definition_document := private.scheduler_definition_document_v1(definition_row)
        || pg_catalog.jsonb_build_object(
          'definition_id', definition_row.definition_id,
          'account_id', definition_row.account_id,
          'revision', definition_row.revision,
          'next_due_at', definition_row.next_due_at,
          'scheduler_state', definition_row.scheduler_state
        );

      return query
      select
        'manual_resolution'::text,
        definition_document,
        null::jsonb,
        run_row.run_id,
        null::timestamptz,
        'scheduler_attempts_exhausted'::text,
        authorization_time;
      return;
    end if;

    retry_delay_seconds := least(
      definition_row.retry_max_seconds::numeric,
      definition_row.retry_base_seconds::numeric * pg_catalog.power(
        2::numeric,
        least(run_row.attempt_count - 1, 30)::numeric
      )
    )::integer;
    update private.scheduler_job_runs as expired_run
    set state = 'retry_wait',
        revision = expired_run.revision + 1,
        available_at = authorization_time
          + pg_catalog.make_interval(secs => retry_delay_seconds),
        failure_reason_code = 'scheduler_lease_expired',
        failure_sha256 = private.pit_sha256_text_v1(
          'scheduler_lease_expired:' || expired_run.run_id::text || ':' ||
          expired_run.lease_token::text
        ),
        failure_retryable = true,
        updated_at = authorization_time
    where expired_run.run_id = run_row.run_id
      and expired_run.revision = run_row.revision
      and expired_run.state = 'leased'
      and expired_run.lease_expires_at <= authorization_time
      and exists (
        select 1
        from private.worker_leases as current_outer_lease
        where current_outer_lease.account_id = p_account_id
          and current_outer_lease.holder_id = p_holder_id
          and current_outer_lease.fencing_token = p_outer_fencing_token
          and current_outer_lease.release_sha = p_release_sha
          and current_outer_lease.expires_at > authorization_time
      )
    returning expired_run.* into run_row;
    if not found then
      raise exception 'scheduler_lease_expiry_compare_and_swap_failed'
        using errcode = '40001';
    end if;

    return query
    select
      'wait'::text,
      definition_document,
      null::jsonb,
      run_row.run_id,
      run_row.available_at,
      'scheduler_lease_expired_retry_scheduled'::text,
      authorization_time;
    return;
  end if;

  if run_row.attempt_count >= definition_row.max_attempts then
    return query
    select
      'manual_resolution'::text,
      definition_document,
      null::jsonb,
      run_row.run_id,
      null::timestamptz,
      'scheduler_attempts_exhausted'::text,
      authorization_time;
    return;
  end if;
  if run_row.replay_generation > definition_row.max_manual_replays then
    return query
    select
      'manual_resolution'::text,
      definition_document,
      null::jsonb,
      run_row.run_id,
      null::timestamptz,
      'scheduler_replay_budget_exhausted'::text,
      authorization_time;
    return;
  end if;
  if run_row.available_at > authorization_time then
    return query
    select
      'wait'::text,
      definition_document,
      null::jsonb,
      run_row.run_id,
      run_row.available_at,
      case
        when run_row.state = 'retry_wait'
          then 'scheduler_retry_wait'::text
        else 'scheduler_pending_not_available'::text
      end,
      authorization_time;
    return;
  end if;

  if outer_lease_expires_at < authorization_time
       + pg_catalog.make_interval(secs => 10) then
    return query
    select
      'wait'::text,
      definition_document,
      null::jsonb,
      run_row.run_id,
      outer_lease_expires_at,
      'scheduler_outer_lease_renewal_required'::text,
      authorization_time;
    return;
  end if;

  update private.scheduler_job_runs as due_run
  set state = 'leased',
      revision = due_run.revision + 1,
      attempt_count = due_run.attempt_count + 1,
      lease_generation = due_run.lease_generation + 1,
      lease_token = pg_catalog.gen_random_uuid(),
      lease_holder_id = p_holder_id,
      lease_release_sha = p_release_sha,
      outer_fencing_token = p_outer_fencing_token,
      leased_at = authorization_time,
      lease_expires_at = least(
        authorization_time
          + pg_catalog.make_interval(secs => definition_row.lease_ttl_seconds),
        outer_lease_expires_at
      ),
      failure_reason_code = null,
      failure_sha256 = null,
      failure_retryable = null,
      updated_at = authorization_time
  where due_run.run_id = run_row.run_id
    and due_run.revision = run_row.revision
    and due_run.state in ('pending', 'retry_wait')
    and due_run.available_at <= authorization_time
    and due_run.attempt_count < definition_row.max_attempts
    and due_run.replay_generation <= definition_row.max_manual_replays
    and exists (
      select 1
      from private.worker_leases as current_outer_lease
      where current_outer_lease.account_id = p_account_id
        and current_outer_lease.holder_id = p_holder_id
        and current_outer_lease.fencing_token = p_outer_fencing_token
        and current_outer_lease.release_sha = p_release_sha
        and current_outer_lease.expires_at > authorization_time
    )
  returning due_run.* into run_row;
  if not found then
    raise exception 'scheduler_convergence_claim_compare_and_swap_failed'
      using errcode = '40001';
  end if;

  insert into private.scheduler_job_leases (
    lease_token,
    run_id,
    definition_id,
    account_id,
    holder_id,
    release_sha,
    outer_fencing_token,
    attempt_number,
    lease_generation,
    run_revision,
    leased_at,
    lease_expires_at
  ) values (
    run_row.lease_token,
    run_row.run_id,
    run_row.definition_id,
    run_row.account_id,
    run_row.lease_holder_id,
    run_row.lease_release_sha,
    run_row.outer_fencing_token,
    run_row.attempt_count,
    run_row.lease_generation,
    run_row.revision,
    run_row.leased_at,
    run_row.lease_expires_at
  )
  returning * into lease_row;

  return query
  select
    'claimed'::text,
    definition_document,
    pg_catalog.jsonb_build_object(
      'definition', private.scheduler_definition_document_v1(definition_row),
      'run', private.scheduler_run_document_v1(run_row),
      'lease', pg_catalog.jsonb_build_object(
        'lease_token', lease_row.lease_token,
        'run_id', lease_row.run_id,
        'account_id', lease_row.account_id,
        'holder_id', lease_row.holder_id,
        'release_sha', lease_row.release_sha,
        'outer_fencing_token', lease_row.outer_fencing_token,
        'attempt_number', lease_row.attempt_number,
        'run_revision', lease_row.run_revision,
        'leased_at', lease_row.leased_at,
        'lease_expires_at', lease_row.lease_expires_at
      )
    ),
    run_row.run_id,
    null::timestamptz,
    null::text,
    authorization_time;
end;
$$;

create or replace function worker_api.converge_scheduler_job_definition(
  p_account_id text,
  p_holder_id text,
  p_outer_fencing_token bigint,
  p_release_sha text,
  p_job_key text,
  p_definition_sha256 text,
  p_interval_seconds integer,
  p_lease_ttl_seconds integer,
  p_max_attempts integer,
  p_retry_base_seconds integer,
  p_retry_max_seconds integer,
  p_max_manual_replays integer,
  p_enabled boolean
)
returns table (
  status text,
  definition jsonb,
  claim jsonb,
  active_run_id uuid,
  next_eligible_at timestamptz,
  reason_code text,
  observed_at timestamptz
)
language sql
volatile
security invoker
set search_path = ''
as $$
  select *
  from private.converge_scheduler_job_definition_impl(
    p_account_id,
    p_holder_id,
    p_outer_fencing_token,
    p_release_sha,
    p_job_key,
    p_definition_sha256,
    p_interval_seconds,
    p_lease_ttl_seconds,
    p_max_attempts,
    p_retry_base_seconds,
    p_retry_max_seconds,
    p_max_manual_replays,
    p_enabled
  );
$$;

create or replace function private.claim_due_scheduler_job_impl(
  p_account_id text,
  p_holder_id text,
  p_outer_fencing_token bigint,
  p_release_sha text
)
returns table (
  claimed boolean,
  claim jsonb,
  observed_at timestamptz
)
language plpgsql
volatile
security definer
set search_path = ''
as $$
declare
  authorization_time timestamptz := pg_catalog.clock_timestamp();
  definition_row private.scheduler_job_definitions%rowtype;
  run_row private.scheduler_job_runs%rowtype;
  lease_row private.scheduler_job_leases%rowtype;
  outer_lease_acquired_at timestamptz;
  outer_lease_expires_at timestamptz;
  retry_delay_seconds integer;
  lease_expiry_retryable boolean;
  active_run_found boolean;
  candidate_iteration integer;
begin
  perform private.require_scheduler_outer_lease_v1(
    p_account_id,
    p_holder_id,
    p_outer_fencing_token,
    p_release_sha,
    authorization_time
  );
  select outer_lease.acquired_at, outer_lease.expires_at
  into outer_lease_acquired_at, outer_lease_expires_at
  from private.worker_leases as outer_lease
  where outer_lease.account_id = p_account_id
    and outer_lease.holder_id = p_holder_id
    and outer_lease.fencing_token = p_outer_fencing_token
    and outer_lease.release_sha = p_release_sha
    and outer_lease.expires_at > authorization_time
  for share;
  if not found then
    raise exception 'scheduler_outer_lease_stale_or_missing'
      using errcode = '40001';
  end if;
  if outer_lease_expires_at < authorization_time
       + pg_catalog.make_interval(secs => 10) then
    return query select false, null::jsonb, authorization_time;
    return;
  end if;

  for candidate_iteration in 1..5 loop
    select definition.* into definition_row
    from private.scheduler_job_definitions as definition
    where definition.account_id = p_account_id
      and definition.enabled
      and definition.scheduler_state = 'ready'
      and (
        definition.job_key <> 'operations.execution'
        or private.scheduler_command_barrier_satisfied_v1(
          p_account_id,
          p_holder_id,
          p_outer_fencing_token,
          p_release_sha,
          outer_lease_acquired_at,
          authorization_time
        )
        or exists (
          select 1
          from private.scheduler_job_runs as expired_execution
          where expired_execution.definition_id = definition.definition_id
            and expired_execution.state = 'leased'
            and expired_execution.lease_expires_at <= authorization_time
        )
      )
      and (
        (
          not exists (
            select 1
            from private.scheduler_job_runs as active_run
            where active_run.definition_id = definition.definition_id
              and active_run.state in ('pending', 'leased', 'retry_wait')
          )
          and (
            definition.next_due_at <= authorization_time
            or (
              definition.job_key = 'operations.commands'
              and not exists (
                select 1
                from private.scheduler_job_runs as startup_command
                where startup_command.run_id = definition.latest_run_id
                  and startup_command.definition_id = definition.definition_id
                  and startup_command.definition_sha256 = definition.definition_sha256
                  and startup_command.state = 'succeeded'
                  and startup_command.lease_holder_id = p_holder_id
                  and startup_command.outer_fencing_token = p_outer_fencing_token
                  and startup_command.lease_release_sha = p_release_sha
                  and startup_command.completed_at >= outer_lease_acquired_at
              )
            )
          )
        )
        or exists (
          select 1
          from private.scheduler_job_runs as due_run
          where due_run.definition_id = definition.definition_id
            and (
              (due_run.state in ('pending', 'retry_wait')
               and due_run.available_at <= authorization_time)
              or
              (due_run.state = 'leased'
               and due_run.lease_expires_at <= authorization_time)
            )
        )
      )
    order by
      case definition.job_key
        when 'operations.commands' then 0
        when 'operations.execution' then 1
        when 'operations.settlement' then 2
        when 'operations.reconciliation' then 3
        when 'operations.outbox' then 4
      end,
      definition.next_due_at,
      definition.definition_id
    limit 1
    for update skip locked;

    if not found then
      authorization_time := pg_catalog.clock_timestamp();
      perform private.require_scheduler_outer_lease_v1(
        p_account_id,
        p_holder_id,
        p_outer_fencing_token,
        p_release_sha,
        authorization_time
      );
      return query select false, null::jsonb, authorization_time;
      return;
    end if;

    select active_run.* into run_row
    from private.scheduler_job_runs as active_run
    where active_run.definition_id = definition_row.definition_id
      and active_run.state in ('pending', 'leased', 'retry_wait')
    for update;
    active_run_found := found;

    if definition_row.job_key = 'operations.execution' then
      perform 1
      from private.scheduler_job_definitions as command_definition
      where command_definition.account_id = p_account_id
        and command_definition.job_key = 'operations.commands'
      for share;
      perform 1
      from private.scheduler_job_definitions as settlement_definition
      where settlement_definition.account_id = p_account_id
        and settlement_definition.job_key = 'operations.settlement'
      for share;
      perform 1
      from private.scheduler_job_definitions as reconciliation_definition
      where reconciliation_definition.account_id = p_account_id
        and reconciliation_definition.job_key = 'operations.reconciliation'
      for share;
    end if;

    authorization_time := pg_catalog.clock_timestamp();
    perform private.require_scheduler_outer_lease_v1(
      p_account_id,
      p_holder_id,
      p_outer_fencing_token,
      p_release_sha,
      authorization_time
    );
    select outer_lease.expires_at into outer_lease_expires_at
    from private.worker_leases as outer_lease
    where outer_lease.account_id = p_account_id
      and outer_lease.holder_id = p_holder_id
      and outer_lease.fencing_token = p_outer_fencing_token
      and outer_lease.release_sha = p_release_sha
      and outer_lease.expires_at > authorization_time
    for share;
    if not found then
      raise exception 'scheduler_outer_lease_stale_or_missing'
        using errcode = '40001';
    end if;
    if outer_lease_expires_at < authorization_time
         + pg_catalog.make_interval(secs => 10) then
      return query select false, null::jsonb, authorization_time;
      return;
    end if;
    if definition_row.job_key = 'operations.execution'
       and not private.scheduler_command_barrier_satisfied_v1(
         p_account_id,
         p_holder_id,
         p_outer_fencing_token,
         p_release_sha,
         outer_lease_acquired_at,
         authorization_time
       )
       and not (
         active_run_found
         and run_row.state = 'leased'
         and run_row.lease_expires_at <= authorization_time
       ) then
      continue;
    end if;

    if active_run_found and run_row.state = 'leased' then
      if run_row.lease_expires_at > authorization_time then
        continue;
      end if;

      lease_expiry_retryable := definition_row.job_key in (
        'operations.commands',
        'operations.reconciliation',
        'operations.outbox'
      );
      if not lease_expiry_retryable
         or run_row.attempt_count >= definition_row.max_attempts then
        update private.scheduler_job_runs as expired_run
        set state = 'dead_letter',
            revision = expired_run.revision + 1,
            available_at = null,
            failure_reason_code = 'scheduler_lease_expired',
            failure_sha256 = private.pit_sha256_text_v1(
              'scheduler_lease_expired:' || expired_run.run_id::text || ':' ||
              expired_run.lease_token::text
            ),
            failure_retryable = lease_expiry_retryable,
            dead_lettered_at = authorization_time,
            updated_at = authorization_time
        where expired_run.run_id = run_row.run_id
          and expired_run.revision = run_row.revision
          and expired_run.state = 'leased'
          and expired_run.lease_expires_at <= authorization_time
          and exists (
            select 1
            from private.worker_leases as current_outer_lease
            where current_outer_lease.account_id = p_account_id
              and current_outer_lease.holder_id = p_holder_id
              and current_outer_lease.fencing_token = p_outer_fencing_token
              and current_outer_lease.release_sha = p_release_sha
              and current_outer_lease.expires_at > authorization_time
          )
        returning expired_run.* into run_row;
        if not found then
          raise exception 'scheduler_lease_expiry_compare_and_swap_failed'
            using errcode = '40001';
        end if;

        update private.scheduler_job_definitions as blocked_definition
        set scheduler_state = 'blocked',
            blocked_by_run_id = run_row.run_id,
            latest_run_id = run_row.run_id,
            revision = blocked_definition.revision + 1,
            updated_at = authorization_time
        where blocked_definition.definition_id = definition_row.definition_id
          and blocked_definition.revision = definition_row.revision;
        if not found then
          raise exception 'scheduler_definition_compare_and_swap_failed'
            using errcode = '40001';
        end if;
        continue;
      end if;

      retry_delay_seconds := least(
        definition_row.retry_max_seconds::numeric,
        definition_row.retry_base_seconds::numeric * pg_catalog.power(
          2::numeric,
          least(run_row.attempt_count - 1, 30)::numeric
        )
      )::integer;

      update private.scheduler_job_runs as expired_run
      set state = 'retry_wait',
          revision = expired_run.revision + 1,
          available_at = authorization_time
            + pg_catalog.make_interval(secs => retry_delay_seconds),
          failure_reason_code = 'scheduler_lease_expired',
          failure_sha256 = private.pit_sha256_text_v1(
            'scheduler_lease_expired:' || expired_run.run_id::text || ':' ||
            expired_run.lease_token::text
          ),
          failure_retryable = true,
          updated_at = authorization_time
      where expired_run.run_id = run_row.run_id
        and expired_run.revision = run_row.revision
        and expired_run.state = 'leased'
        and expired_run.lease_expires_at <= authorization_time
        and exists (
          select 1
          from private.worker_leases as current_outer_lease
          where current_outer_lease.account_id = p_account_id
            and current_outer_lease.holder_id = p_holder_id
            and current_outer_lease.fencing_token = p_outer_fencing_token
            and current_outer_lease.release_sha = p_release_sha
            and current_outer_lease.expires_at > authorization_time
        );
      if not found then
        raise exception 'scheduler_lease_expiry_compare_and_swap_failed'
          using errcode = '40001';
      end if;
      continue;
    end if;

    if not active_run_found then
      insert into private.scheduler_job_runs (
        definition_id,
        account_id,
        job_key,
        definition_sha256,
        state,
        revision,
        attempt_count,
        lease_generation,
        replay_generation,
        scheduled_for,
        available_at,
        created_at,
        updated_at
      ) values (
        definition_row.definition_id,
        definition_row.account_id,
        definition_row.job_key,
        definition_row.definition_sha256,
        'pending',
        1,
        0,
        0,
        0,
        least(definition_row.next_due_at, authorization_time),
        authorization_time,
        authorization_time,
        authorization_time
      )
      returning * into run_row;

      update private.scheduler_job_definitions as scheduled_definition
      set latest_run_id = run_row.run_id,
          next_due_at = authorization_time
            + pg_catalog.make_interval(secs => scheduled_definition.interval_seconds),
          revision = scheduled_definition.revision + 1,
          updated_at = authorization_time
      where scheduled_definition.definition_id = definition_row.definition_id
        and scheduled_definition.revision = definition_row.revision
      returning scheduled_definition.* into definition_row;
      if not found then
        raise exception 'scheduler_definition_compare_and_swap_failed'
          using errcode = '40001';
      end if;
    end if;

    if run_row.state not in ('pending', 'retry_wait')
       or run_row.available_at > authorization_time
       or run_row.attempt_count >= definition_row.max_attempts
       or run_row.replay_generation > definition_row.max_manual_replays then
      continue;
    end if;

    update private.scheduler_job_runs as due_run
    set state = 'leased',
        revision = due_run.revision + 1,
        attempt_count = due_run.attempt_count + 1,
        lease_generation = due_run.lease_generation + 1,
        lease_token = pg_catalog.gen_random_uuid(),
        lease_holder_id = p_holder_id,
        lease_release_sha = p_release_sha,
        outer_fencing_token = p_outer_fencing_token,
        leased_at = authorization_time,
        lease_expires_at = least(
          authorization_time
            + pg_catalog.make_interval(secs => definition_row.lease_ttl_seconds),
          outer_lease_expires_at
        ),
        failure_reason_code = null,
        failure_sha256 = null,
        failure_retryable = null,
        updated_at = authorization_time
    where due_run.run_id = run_row.run_id
      and due_run.revision = run_row.revision
      and due_run.state in ('pending', 'retry_wait')
      and due_run.available_at <= authorization_time
      and due_run.attempt_count < definition_row.max_attempts
      and due_run.replay_generation <= definition_row.max_manual_replays
      and exists (
        select 1
        from private.worker_leases as current_outer_lease
        where current_outer_lease.account_id = p_account_id
          and current_outer_lease.holder_id = p_holder_id
          and current_outer_lease.fencing_token = p_outer_fencing_token
          and current_outer_lease.release_sha = p_release_sha
          and current_outer_lease.expires_at > authorization_time
      )
    returning due_run.* into run_row;
    if not found then
      raise exception 'scheduler_claim_compare_and_swap_failed'
        using errcode = '40001';
    end if;

    insert into private.scheduler_job_leases (
      lease_token,
      run_id,
      definition_id,
      account_id,
      holder_id,
      release_sha,
      outer_fencing_token,
      attempt_number,
      lease_generation,
      run_revision,
      leased_at,
      lease_expires_at
    ) values (
      run_row.lease_token,
      run_row.run_id,
      run_row.definition_id,
      run_row.account_id,
      run_row.lease_holder_id,
      run_row.lease_release_sha,
      run_row.outer_fencing_token,
      run_row.attempt_count,
      run_row.lease_generation,
      run_row.revision,
      run_row.leased_at,
      run_row.lease_expires_at
    )
    returning * into lease_row;

    return query
    select
      true,
      pg_catalog.jsonb_build_object(
        'definition', private.scheduler_definition_document_v1(definition_row),
        'run', private.scheduler_run_document_v1(run_row),
        'lease', pg_catalog.jsonb_build_object(
          'lease_token', lease_row.lease_token,
          'run_id', lease_row.run_id,
          'account_id', lease_row.account_id,
          'holder_id', lease_row.holder_id,
          'release_sha', lease_row.release_sha,
          'outer_fencing_token', lease_row.outer_fencing_token,
          'attempt_number', lease_row.attempt_number,
          'run_revision', lease_row.run_revision,
          'leased_at', lease_row.leased_at,
          'lease_expires_at', lease_row.lease_expires_at
        )
      ),
      authorization_time;
    return;
  end loop;

  return query select false, null::jsonb, authorization_time;
end;
$$;

create or replace function worker_api.claim_due_scheduler_job(
  p_account_id text,
  p_holder_id text,
  p_outer_fencing_token bigint,
  p_release_sha text
)
returns table (
  claimed boolean,
  claim jsonb,
  observed_at timestamptz
)
language sql
volatile
security invoker
set search_path = ''
as $$
  select *
  from private.claim_due_scheduler_job_impl(
    p_account_id,
    p_holder_id,
    p_outer_fencing_token,
    p_release_sha
  );
$$;

create or replace function private.complete_scheduler_job_run_impl(
  p_account_id text,
  p_holder_id text,
  p_outer_fencing_token bigint,
  p_release_sha text,
  p_run_id uuid,
  p_expected_run_revision bigint,
  p_definition_sha256 text,
  p_lease_token uuid,
  p_result_sha256 text
)
returns table (
  run_id uuid,
  state text,
  run_revision bigint,
  attempt_count integer,
  next_attempt_at timestamptz,
  failure_reason_code text,
  result_sha256 text,
  observed_at timestamptz
)
language plpgsql
volatile
security definer
set search_path = ''
as $$
declare
  authorization_time timestamptz := pg_catalog.clock_timestamp();
  target_definition_id uuid;
  definition_row private.scheduler_job_definitions%rowtype;
  run_row private.scheduler_job_runs%rowtype;
begin
  perform private.require_scheduler_outer_lease_v1(
    p_account_id,
    p_holder_id,
    p_outer_fencing_token,
    p_release_sha,
    authorization_time
  );
  if p_run_id is null
     or p_expected_run_revision is null
     or p_expected_run_revision <= 0
     or p_definition_sha256 !~ '^[0-9a-f]{64}$'
     or p_lease_token is null
     or p_result_sha256 !~ '^[0-9a-f]{64}$' then
    raise exception 'scheduler_completion_parameters_invalid'
      using errcode = '22023';
  end if;

  select candidate.definition_id into target_definition_id
  from private.scheduler_job_runs as candidate
  where candidate.run_id = p_run_id
    and candidate.account_id = p_account_id;
  if not found then
    raise exception 'scheduler_run_not_found' using errcode = 'P0002';
  end if;

  select definition.* into definition_row
  from private.scheduler_job_definitions as definition
  where definition.definition_id = target_definition_id
  for update;

  select candidate.* into run_row
  from private.scheduler_job_runs as candidate
  where candidate.run_id = p_run_id
    and candidate.definition_id = definition_row.definition_id
    and candidate.account_id = p_account_id
  for update;
  if not found then
    raise exception 'scheduler_run_binding_changed' using errcode = '40001';
  end if;

  authorization_time := pg_catalog.clock_timestamp();
  perform private.require_scheduler_outer_lease_v1(
    p_account_id,
    p_holder_id,
    p_outer_fencing_token,
    p_release_sha,
    authorization_time
  );

  if run_row.state = 'succeeded'
     and run_row.revision = p_expected_run_revision + 1
     and run_row.definition_sha256 = p_definition_sha256
     and run_row.lease_token = p_lease_token
     and run_row.lease_holder_id = p_holder_id
     and run_row.outer_fencing_token = p_outer_fencing_token
     and run_row.lease_release_sha = p_release_sha
     and run_row.result_sha256 = p_result_sha256 then
    return query
    select run_row.run_id, run_row.state, run_row.revision,
           run_row.attempt_count, null::timestamptz, null::text,
           run_row.result_sha256, authorization_time;
    return;
  end if;

  if run_row.state <> 'leased'
     or run_row.revision <> p_expected_run_revision
     or run_row.definition_sha256 <> p_definition_sha256
     or run_row.lease_token <> p_lease_token
     or run_row.lease_holder_id <> p_holder_id
     or run_row.outer_fencing_token <> p_outer_fencing_token
     or run_row.lease_release_sha <> p_release_sha
     or run_row.lease_expires_at <= authorization_time
     or not exists (
       select 1
       from private.scheduler_job_leases as lease
       where lease.lease_token = p_lease_token
         and lease.run_id = run_row.run_id
         and lease.run_revision = run_row.revision
         and lease.holder_id = p_holder_id
         and lease.outer_fencing_token = p_outer_fencing_token
         and lease.release_sha = p_release_sha
     ) then
    raise exception 'scheduler_completion_compare_and_swap_failed'
      using errcode = '40001';
  end if;

  update private.scheduler_job_runs as completed_run
  set state = 'succeeded',
      revision = completed_run.revision + 1,
      result_sha256 = p_result_sha256,
      completed_at = authorization_time,
      updated_at = authorization_time
  where completed_run.run_id = run_row.run_id
    and completed_run.revision = p_expected_run_revision
    and completed_run.state = 'leased'
    and completed_run.definition_sha256 = p_definition_sha256
    and completed_run.lease_token = p_lease_token
    and completed_run.lease_holder_id = p_holder_id
    and completed_run.outer_fencing_token = p_outer_fencing_token
    and completed_run.lease_release_sha = p_release_sha
    and completed_run.lease_expires_at > authorization_time
    and exists (
      select 1
      from private.worker_leases as current_outer_lease
      where current_outer_lease.account_id = p_account_id
        and current_outer_lease.holder_id = p_holder_id
        and current_outer_lease.fencing_token = p_outer_fencing_token
        and current_outer_lease.release_sha = p_release_sha
        and current_outer_lease.expires_at > authorization_time
    )
  returning completed_run.* into run_row;
  if not found then
    raise exception 'scheduler_completion_compare_and_swap_failed'
      using errcode = '40001';
  end if;

  return query
  select run_row.run_id, run_row.state, run_row.revision,
         run_row.attempt_count, null::timestamptz, null::text,
         run_row.result_sha256, authorization_time;
end;
$$;

create or replace function worker_api.complete_scheduler_job_run(
  p_account_id text,
  p_holder_id text,
  p_outer_fencing_token bigint,
  p_release_sha text,
  p_run_id uuid,
  p_expected_run_revision bigint,
  p_definition_sha256 text,
  p_lease_token uuid,
  p_result_sha256 text
)
returns table (
  run_id uuid,
  state text,
  run_revision bigint,
  attempt_count integer,
  next_attempt_at timestamptz,
  failure_reason_code text,
  result_sha256 text,
  observed_at timestamptz
)
language sql
volatile
security invoker
set search_path = ''
as $$
  select *
  from private.complete_scheduler_job_run_impl(
    p_account_id,
    p_holder_id,
    p_outer_fencing_token,
    p_release_sha,
    p_run_id,
    p_expected_run_revision,
    p_definition_sha256,
    p_lease_token,
    p_result_sha256
  );
$$;

create or replace function private.fail_scheduler_job_run_impl(
  p_account_id text,
  p_holder_id text,
  p_outer_fencing_token bigint,
  p_release_sha text,
  p_run_id uuid,
  p_expected_run_revision bigint,
  p_definition_sha256 text,
  p_lease_token uuid,
  p_failure_reason_code text,
  p_failure_sha256 text,
  p_retryable boolean
)
returns table (
  run_id uuid,
  state text,
  run_revision bigint,
  attempt_count integer,
  next_attempt_at timestamptz,
  failure_reason_code text,
  result_sha256 text,
  observed_at timestamptz
)
language plpgsql
volatile
security definer
set search_path = ''
as $$
declare
  authorization_time timestamptz := pg_catalog.clock_timestamp();
  target_definition_id uuid;
  definition_row private.scheduler_job_definitions%rowtype;
  run_row private.scheduler_job_runs%rowtype;
  retry_delay_seconds integer;
  effective_retryable boolean;
begin
  perform private.require_scheduler_outer_lease_v1(
    p_account_id,
    p_holder_id,
    p_outer_fencing_token,
    p_release_sha,
    authorization_time
  );
  if p_run_id is null
     or p_expected_run_revision is null
     or p_expected_run_revision <= 0
     or p_definition_sha256 !~ '^[0-9a-f]{64}$'
     or p_lease_token is null
     or p_failure_reason_code !~ '^[a-z][a-z0-9_]{0,127}$'
     or p_failure_sha256 !~ '^[0-9a-f]{64}$'
     or p_retryable is null then
    raise exception 'scheduler_failure_parameters_invalid'
      using errcode = '22023';
  end if;

  select candidate.definition_id into target_definition_id
  from private.scheduler_job_runs as candidate
  where candidate.run_id = p_run_id
    and candidate.account_id = p_account_id;
  if not found then
    raise exception 'scheduler_run_not_found' using errcode = 'P0002';
  end if;

  select definition.* into definition_row
  from private.scheduler_job_definitions as definition
  where definition.definition_id = target_definition_id
  for update;

  select candidate.* into run_row
  from private.scheduler_job_runs as candidate
  where candidate.run_id = p_run_id
    and candidate.definition_id = definition_row.definition_id
    and candidate.account_id = p_account_id
  for update;
  if not found then
    raise exception 'scheduler_run_binding_changed' using errcode = '40001';
  end if;

  authorization_time := pg_catalog.clock_timestamp();
  perform private.require_scheduler_outer_lease_v1(
    p_account_id,
    p_holder_id,
    p_outer_fencing_token,
    p_release_sha,
    authorization_time
  );

  if run_row.state in ('retry_wait', 'dead_letter')
     and run_row.revision = p_expected_run_revision + 1
     and run_row.definition_sha256 = p_definition_sha256
     and run_row.lease_token = p_lease_token
     and run_row.lease_holder_id = p_holder_id
     and run_row.outer_fencing_token = p_outer_fencing_token
     and run_row.lease_release_sha = p_release_sha
     and run_row.failure_reason_code = p_failure_reason_code
     and run_row.failure_sha256 = p_failure_sha256
     and run_row.failure_retryable is not distinct from (
       p_retryable and (
         (run_row.job_key = 'operations.commands'
          and p_failure_reason_code = 'command_poll_retryable')
         or
         (run_row.job_key = 'operations.reconciliation'
          and p_failure_reason_code = 'reconciliation_poll_retryable')
         or
         (run_row.job_key = 'operations.outbox'
          and p_failure_reason_code = 'outbox_poll_retryable')
       )
     ) then
    return query
    select run_row.run_id, run_row.state, run_row.revision,
           run_row.attempt_count,
           case when run_row.state = 'retry_wait' then run_row.available_at end,
           run_row.failure_reason_code, run_row.failure_sha256,
           authorization_time;
    return;
  end if;

  if run_row.state <> 'leased'
     or run_row.revision <> p_expected_run_revision
     or run_row.definition_sha256 <> p_definition_sha256
     or run_row.lease_token <> p_lease_token
     or run_row.lease_holder_id <> p_holder_id
     or run_row.outer_fencing_token <> p_outer_fencing_token
     or run_row.lease_release_sha <> p_release_sha
     or run_row.lease_expires_at <= authorization_time
     or not exists (
       select 1
       from private.scheduler_job_leases as lease
       where lease.lease_token = p_lease_token
         and lease.run_id = run_row.run_id
         and lease.run_revision = run_row.revision
         and lease.holder_id = p_holder_id
         and lease.outer_fencing_token = p_outer_fencing_token
         and lease.release_sha = p_release_sha
     ) then
    raise exception 'scheduler_failure_compare_and_swap_failed'
      using errcode = '40001';
  end if;

  effective_retryable := p_retryable and (
    (run_row.job_key = 'operations.commands'
     and p_failure_reason_code = 'command_poll_retryable')
    or
    (run_row.job_key = 'operations.reconciliation'
     and p_failure_reason_code = 'reconciliation_poll_retryable')
    or
    (run_row.job_key = 'operations.outbox'
     and p_failure_reason_code = 'outbox_poll_retryable')
  );

  if effective_retryable and run_row.attempt_count < definition_row.max_attempts then
    retry_delay_seconds := least(
      definition_row.retry_max_seconds::numeric,
      definition_row.retry_base_seconds::numeric * pg_catalog.power(
        2::numeric,
        least(run_row.attempt_count - 1, 30)::numeric
      )
    )::integer;

    update private.scheduler_job_runs as failed_run
    set state = 'retry_wait',
        revision = failed_run.revision + 1,
        available_at = authorization_time
          + pg_catalog.make_interval(secs => retry_delay_seconds),
        failure_reason_code = p_failure_reason_code,
        failure_sha256 = p_failure_sha256,
        failure_retryable = true,
        updated_at = authorization_time
    where failed_run.run_id = run_row.run_id
      and failed_run.revision = p_expected_run_revision
      and failed_run.state = 'leased'
      and failed_run.definition_sha256 = p_definition_sha256
      and failed_run.lease_token = p_lease_token
      and failed_run.lease_holder_id = p_holder_id
      and failed_run.outer_fencing_token = p_outer_fencing_token
      and failed_run.lease_release_sha = p_release_sha
      and failed_run.lease_expires_at > authorization_time
      and exists (
        select 1
        from private.worker_leases as current_outer_lease
        where current_outer_lease.account_id = p_account_id
          and current_outer_lease.holder_id = p_holder_id
          and current_outer_lease.fencing_token = p_outer_fencing_token
          and current_outer_lease.release_sha = p_release_sha
          and current_outer_lease.expires_at > authorization_time
      )
    returning failed_run.* into run_row;
  else
    update private.scheduler_job_runs as failed_run
    set state = 'dead_letter',
        revision = failed_run.revision + 1,
        available_at = null,
        failure_reason_code = p_failure_reason_code,
        failure_sha256 = p_failure_sha256,
        failure_retryable = effective_retryable,
        dead_lettered_at = authorization_time,
        updated_at = authorization_time
    where failed_run.run_id = run_row.run_id
      and failed_run.revision = p_expected_run_revision
      and failed_run.state = 'leased'
      and failed_run.definition_sha256 = p_definition_sha256
      and failed_run.lease_token = p_lease_token
      and failed_run.lease_holder_id = p_holder_id
      and failed_run.outer_fencing_token = p_outer_fencing_token
      and failed_run.lease_release_sha = p_release_sha
      and failed_run.lease_expires_at > authorization_time
      and exists (
        select 1
        from private.worker_leases as current_outer_lease
        where current_outer_lease.account_id = p_account_id
          and current_outer_lease.holder_id = p_holder_id
          and current_outer_lease.fencing_token = p_outer_fencing_token
          and current_outer_lease.release_sha = p_release_sha
          and current_outer_lease.expires_at > authorization_time
      )
    returning failed_run.* into run_row;

    update private.scheduler_job_definitions as blocked_definition
    set scheduler_state = 'blocked',
        blocked_by_run_id = run_row.run_id,
        latest_run_id = run_row.run_id,
        revision = blocked_definition.revision + 1,
        updated_at = authorization_time
    where blocked_definition.definition_id = definition_row.definition_id
      and blocked_definition.revision = definition_row.revision;
    if not found then
      raise exception 'scheduler_definition_compare_and_swap_failed'
        using errcode = '40001';
    end if;
  end if;

  if run_row.run_id is null then
    raise exception 'scheduler_failure_compare_and_swap_failed'
      using errcode = '40001';
  end if;

  return query
  select run_row.run_id, run_row.state, run_row.revision,
         run_row.attempt_count,
         case when run_row.state = 'retry_wait' then run_row.available_at end,
         run_row.failure_reason_code, run_row.failure_sha256,
         authorization_time;
end;
$$;

create or replace function worker_api.fail_scheduler_job_run(
  p_account_id text,
  p_holder_id text,
  p_outer_fencing_token bigint,
  p_release_sha text,
  p_run_id uuid,
  p_expected_run_revision bigint,
  p_definition_sha256 text,
  p_lease_token uuid,
  p_failure_reason_code text,
  p_failure_sha256 text,
  p_retryable boolean
)
returns table (
  run_id uuid,
  state text,
  run_revision bigint,
  attempt_count integer,
  next_attempt_at timestamptz,
  failure_reason_code text,
  result_sha256 text,
  observed_at timestamptz
)
language sql
volatile
security invoker
set search_path = ''
as $$
  select *
  from private.fail_scheduler_job_run_impl(
    p_account_id,
    p_holder_id,
    p_outer_fencing_token,
    p_release_sha,
    p_run_id,
    p_expected_run_revision,
    p_definition_sha256,
    p_lease_token,
    p_failure_reason_code,
    p_failure_sha256,
    p_retryable
  );
$$;

create or replace function private.inspect_scheduler_dead_letter_impl(
  p_account_id text,
  p_holder_id text,
  p_outer_fencing_token bigint,
  p_release_sha text,
  p_source_run_id uuid
)
returns table (
  found boolean,
  dead_letter jsonb,
  eligible boolean,
  ineligibility_reason text,
  observed_at timestamptz
)
language plpgsql
volatile
security definer
set search_path = ''
as $$
declare
  authorization_time timestamptz := pg_catalog.clock_timestamp();
  run_row private.scheduler_job_runs%rowtype;
  definition_row private.scheduler_job_definitions%rowtype;
  replay_eligible boolean;
  replay_ineligibility_reason text;
begin
  perform private.require_scheduler_outer_lease_v1(
    p_account_id,
    p_holder_id,
    p_outer_fencing_token,
    p_release_sha,
    authorization_time
  );
  if p_source_run_id is null then
    raise exception 'scheduler_dead_letter_parameters_invalid'
      using errcode = '22023';
  end if;

  select source_run.* into run_row
  from private.scheduler_job_runs as source_run
  where source_run.run_id = p_source_run_id
    and source_run.account_id = p_account_id
    and source_run.state = 'dead_letter';
  if not found then
    authorization_time := pg_catalog.clock_timestamp();
    perform private.require_scheduler_outer_lease_v1(
      p_account_id,
      p_holder_id,
      p_outer_fencing_token,
      p_release_sha,
      authorization_time
    );
    return query
    select false, null::jsonb, null::boolean, null::text, authorization_time;
    return;
  end if;

  select definition.* into definition_row
  from private.scheduler_job_definitions as definition
  where definition.definition_id = run_row.definition_id;
  if not found then
    raise exception 'scheduler_dead_letter_definition_missing'
      using errcode = '55000';
  end if;

  authorization_time := pg_catalog.clock_timestamp();
  perform private.require_scheduler_outer_lease_v1(
    p_account_id,
    p_holder_id,
    p_outer_fencing_token,
    p_release_sha,
    authorization_time
  );

  replay_eligible := true;
  replay_ineligibility_reason := null;
  if run_row.job_key in ('operations.execution', 'operations.settlement') then
    replay_eligible := false;
    replay_ineligibility_reason := 'effectful_job_requires_resolution_evidence';
  elsif not definition_row.enabled then
    replay_eligible := false;
    replay_ineligibility_reason := 'definition_disabled';
  elsif run_row.replay_generation >= definition_row.max_manual_replays then
    replay_eligible := false;
    replay_ineligibility_reason := 'manual_replay_budget_exhausted';
  elsif exists (
    select 1
    from private.scheduler_replay_requests as prior_replay
    where prior_replay.source_run_id = run_row.run_id
  ) then
    replay_eligible := false;
    replay_ineligibility_reason := 'source_already_replayed';
  elsif definition_row.scheduler_state <> 'blocked'
        or definition_row.latest_run_id is distinct from run_row.run_id
        or definition_row.blocked_by_run_id is distinct from run_row.run_id then
    replay_eligible := false;
    replay_ineligibility_reason := 'source_is_not_latest_dead_letter';
  end if;

  return query
  select
    true,
    pg_catalog.jsonb_build_object(
      'source_run_id', run_row.run_id,
      'account_id', run_row.account_id,
      'job_key', run_row.job_key,
      'definition_sha256', run_row.definition_sha256,
      'source_revision', run_row.revision,
      'attempt_count', run_row.attempt_count,
      'failure_reason_code', run_row.failure_reason_code,
      'failure_sha256', run_row.failure_sha256,
      'replay_generation', run_row.replay_generation,
      'max_manual_replays', definition_row.max_manual_replays,
      'dead_lettered_at', run_row.dead_lettered_at,
      'state', run_row.state
    ),
    replay_eligible,
    replay_ineligibility_reason,
    authorization_time;
end;
$$;

create or replace function worker_api.inspect_scheduler_dead_letter(
  p_account_id text,
  p_holder_id text,
  p_outer_fencing_token bigint,
  p_release_sha text,
  p_source_run_id uuid
)
returns table (
  found boolean,
  dead_letter jsonb,
  eligible boolean,
  ineligibility_reason text,
  observed_at timestamptz
)
language sql
volatile
security invoker
set search_path = ''
as $$
  select *
  from private.inspect_scheduler_dead_letter_impl(
    p_account_id,
    p_holder_id,
    p_outer_fencing_token,
    p_release_sha,
    p_source_run_id
  );
$$;

create or replace function private.replay_scheduler_dead_letter_impl(
  p_account_id text,
  p_holder_id text,
  p_outer_fencing_token bigint,
  p_release_sha text,
  p_source_run_id uuid,
  p_expected_source_revision bigint,
  p_expected_definition_sha256 text,
  p_expected_failure_reason_code text,
  p_expected_failure_sha256 text,
  p_expected_replay_generation integer,
  p_replay_request_id uuid,
  p_confirmed_reason_code text,
  p_explicit_confirmation boolean
)
returns table (
  source_run_id uuid,
  new_run_id uuid,
  replay_request_id uuid,
  job_key text,
  definition_sha256 text,
  source_revision bigint,
  failure_reason_code text,
  replay_generation integer,
  state text,
  created_at timestamptz,
  observed_at timestamptz,
  idempotent boolean
)
language plpgsql
volatile
security definer
set search_path = ''
as $$
declare
  authorization_time timestamptz := pg_catalog.clock_timestamp();
  target_definition_id uuid;
  definition_row private.scheduler_job_definitions%rowtype;
  source_row private.scheduler_job_runs%rowtype;
  new_run_row private.scheduler_job_runs%rowtype;
  request_row private.scheduler_replay_requests%rowtype;
  calculated_request_sha256 text;
begin
  perform private.require_scheduler_outer_lease_v1(
    p_account_id,
    p_holder_id,
    p_outer_fencing_token,
    p_release_sha,
    authorization_time
  );
  if p_source_run_id is null
     or p_replay_request_id is null
     or p_expected_source_revision is null
     or p_expected_source_revision <= 0
     or p_expected_definition_sha256 !~ '^[0-9a-f]{64}$'
     or p_expected_failure_reason_code !~ '^[a-z][a-z0-9_]{0,127}$'
     or p_expected_failure_sha256 !~ '^[0-9a-f]{64}$'
     or p_expected_replay_generation not between 0 and 99
     or p_confirmed_reason_code !~ '^[a-z][a-z0-9_]{0,127}$'
     or p_confirmed_reason_code <> p_expected_failure_reason_code
     or p_explicit_confirmation is distinct from true then
    raise exception 'scheduler_replay_parameters_invalid'
      using errcode = '22023';
  end if;

  select replay.* into request_row
  from private.scheduler_replay_requests as replay
  where replay.replay_request_id = p_replay_request_id
  for update;
  if found then
    if request_row.source_run_id <> p_source_run_id
       or request_row.account_id <> p_account_id
       or request_row.expected_source_revision <> p_expected_source_revision
       or request_row.expected_definition_sha256 <> p_expected_definition_sha256
       or request_row.expected_failure_reason_code <> p_expected_failure_reason_code
       or request_row.expected_failure_sha256 <> p_expected_failure_sha256
       or request_row.expected_replay_generation <> p_expected_replay_generation
       or request_row.confirmed_reason_code <> p_confirmed_reason_code then
      raise exception 'scheduler_replay_idempotency_conflict'
        using errcode = '23505';
    end if;

    -- The original actor tuple remains immutable audit evidence. A retry under
    -- any current valid outer lease for the same account may recover the exact
    -- semantic creation receipt after response loss or worker takeover.

    select prior_source.* into source_row
    from private.scheduler_job_runs as prior_source
    where prior_source.run_id = request_row.source_run_id;
    select prior_child.* into new_run_row
    from private.scheduler_job_runs as prior_child
    where prior_child.run_id = request_row.new_run_id;
    if source_row.run_id is null or new_run_row.run_id is null then
      raise exception 'scheduler_replay_receipt_corrupted'
        using errcode = '55000';
    end if;

    authorization_time := pg_catalog.clock_timestamp();
    perform private.require_scheduler_outer_lease_v1(
      p_account_id,
      p_holder_id,
      p_outer_fencing_token,
      p_release_sha,
      authorization_time
    );

    -- Replay returns an immutable creation receipt. Idempotent reads therefore
    -- retain state='pending' even if the child run has since advanced.
    return query
    select
      source_row.run_id,
      new_run_row.run_id,
      request_row.replay_request_id,
      source_row.job_key,
      source_row.definition_sha256,
      request_row.expected_source_revision,
      request_row.expected_failure_reason_code,
      request_row.replay_generation,
      'pending'::text,
      request_row.requested_at,
      authorization_time,
      true;
    return;
  end if;

  select candidate.definition_id into target_definition_id
  from private.scheduler_job_runs as candidate
  where candidate.run_id = p_source_run_id
    and candidate.account_id = p_account_id;
  if not found then
    raise exception 'scheduler_dead_letter_not_found' using errcode = 'P0002';
  end if;

  select definition.* into definition_row
  from private.scheduler_job_definitions as definition
  where definition.definition_id = target_definition_id
  for update;

  select candidate.* into source_row
  from private.scheduler_job_runs as candidate
  where candidate.run_id = p_source_run_id
    and candidate.definition_id = definition_row.definition_id
    and candidate.account_id = p_account_id
  for update;
  if not found then
    raise exception 'scheduler_dead_letter_binding_changed'
      using errcode = '40001';
  end if;

  authorization_time := pg_catalog.clock_timestamp();
  perform private.require_scheduler_outer_lease_v1(
    p_account_id,
    p_holder_id,
    p_outer_fencing_token,
    p_release_sha,
    authorization_time
  );

  if source_row.state <> 'dead_letter'
     or source_row.revision <> p_expected_source_revision
     or source_row.definition_sha256 <> p_expected_definition_sha256
     or source_row.failure_reason_code <> p_expected_failure_reason_code
     or source_row.failure_sha256 <> p_expected_failure_sha256
     or source_row.replay_generation <> p_expected_replay_generation
     or source_row.job_key in ('operations.execution', 'operations.settlement')
     or not definition_row.enabled
     or definition_row.scheduler_state <> 'blocked'
     or definition_row.latest_run_id is distinct from source_row.run_id
     or definition_row.blocked_by_run_id is distinct from source_row.run_id
     or source_row.replay_generation >= definition_row.max_manual_replays
     or exists (
       select 1
       from private.scheduler_replay_requests as existing_replay
       where existing_replay.source_run_id = source_row.run_id
     ) then
    raise exception 'scheduler_replay_compare_and_swap_failed'
      using errcode = '40001';
  end if;

  insert into private.scheduler_job_runs (
    definition_id,
    account_id,
    job_key,
    definition_sha256,
    state,
    revision,
    attempt_count,
    lease_generation,
    replay_generation,
    replay_of_run_id,
    scheduled_for,
    available_at,
    created_at,
    updated_at
  ) values (
    source_row.definition_id,
    source_row.account_id,
    source_row.job_key,
    source_row.definition_sha256,
    'pending',
    1,
    0,
    0,
    source_row.replay_generation + 1,
    source_row.run_id,
    source_row.scheduled_for,
    authorization_time,
    authorization_time,
    authorization_time
  )
  returning * into new_run_row;

  calculated_request_sha256 := private.pit_sha256_text_v1(
    p_replay_request_id::text || ':' || p_source_run_id::text || ':' ||
    new_run_row.run_id::text || ':' || p_expected_source_revision::text || ':' ||
    p_expected_definition_sha256 || ':' || p_expected_failure_reason_code || ':' ||
    p_expected_failure_sha256 || ':' || p_expected_replay_generation::text || ':' ||
    p_confirmed_reason_code || ':' || p_account_id || ':' || p_holder_id || ':' ||
    p_outer_fencing_token::text || ':' || p_release_sha
  );

  insert into private.scheduler_replay_requests (
    replay_request_id,
    source_run_id,
    new_run_id,
    definition_id,
    account_id,
    expected_source_revision,
    expected_definition_sha256,
    expected_failure_reason_code,
    expected_failure_sha256,
    expected_replay_generation,
    confirmed_reason_code,
    holder_id,
    outer_fencing_token,
    release_sha,
    replay_generation,
    request_sha256,
    requested_at
  ) values (
    p_replay_request_id,
    source_row.run_id,
    new_run_row.run_id,
    source_row.definition_id,
    source_row.account_id,
    p_expected_source_revision,
    p_expected_definition_sha256,
    p_expected_failure_reason_code,
    p_expected_failure_sha256,
    p_expected_replay_generation,
    p_confirmed_reason_code,
    p_holder_id,
    p_outer_fencing_token,
    p_release_sha,
    new_run_row.replay_generation,
    calculated_request_sha256,
    authorization_time
  )
  returning * into request_row;

  update private.scheduler_job_definitions as replayed_definition
  set scheduler_state = 'ready',
      blocked_by_run_id = null,
      latest_run_id = new_run_row.run_id,
      revision = replayed_definition.revision + 1,
      updated_at = authorization_time
  where replayed_definition.definition_id = definition_row.definition_id
    and replayed_definition.revision = definition_row.revision
    and replayed_definition.scheduler_state = 'blocked'
    and replayed_definition.blocked_by_run_id = source_row.run_id
    and replayed_definition.latest_run_id = source_row.run_id
    and exists (
      select 1
      from private.worker_leases as current_outer_lease
      where current_outer_lease.account_id = p_account_id
        and current_outer_lease.holder_id = p_holder_id
        and current_outer_lease.fencing_token = p_outer_fencing_token
        and current_outer_lease.release_sha = p_release_sha
        and current_outer_lease.expires_at > authorization_time
    );
  if not found then
    raise exception 'scheduler_replay_compare_and_swap_failed'
      using errcode = '40001';
  end if;

  return query
  select
    source_row.run_id,
    new_run_row.run_id,
    request_row.replay_request_id,
    source_row.job_key,
    source_row.definition_sha256,
    source_row.revision,
    source_row.failure_reason_code,
    new_run_row.replay_generation,
    new_run_row.state,
    new_run_row.created_at,
    authorization_time,
    false;
end;
$$;

create or replace function worker_api.replay_scheduler_dead_letter(
  p_account_id text,
  p_holder_id text,
  p_outer_fencing_token bigint,
  p_release_sha text,
  p_source_run_id uuid,
  p_expected_source_revision bigint,
  p_expected_definition_sha256 text,
  p_expected_failure_reason_code text,
  p_expected_failure_sha256 text,
  p_expected_replay_generation integer,
  p_replay_request_id uuid,
  p_confirmed_reason_code text,
  p_explicit_confirmation boolean
)
returns table (
  source_run_id uuid,
  new_run_id uuid,
  replay_request_id uuid,
  job_key text,
  definition_sha256 text,
  source_revision bigint,
  failure_reason_code text,
  replay_generation integer,
  state text,
  created_at timestamptz,
  observed_at timestamptz,
  idempotent boolean
)
language sql
volatile
security invoker
set search_path = ''
as $$
  select *
  from private.replay_scheduler_dead_letter_impl(
    p_account_id,
    p_holder_id,
    p_outer_fencing_token,
    p_release_sha,
    p_source_run_id,
    p_expected_source_revision,
    p_expected_definition_sha256,
    p_expected_failure_reason_code,
    p_expected_failure_sha256,
    p_expected_replay_generation,
    p_replay_request_id,
    p_confirmed_reason_code,
    p_explicit_confirmation
  );
$$;

revoke all on function
  private.scheduler_definition_sha256_v1(
    text,integer,integer,integer,integer,integer,integer,boolean
  ),
  private.require_scheduler_outer_lease_v1(text,text,bigint,text,timestamptz),
  private.scheduler_command_barrier_satisfied_v1(
    text,text,bigint,text,timestamptz,timestamptz
  ),
  private.scheduler_definition_document_v1(private.scheduler_job_definitions),
  private.scheduler_run_document_v1(private.scheduler_job_runs),
  private.guard_scheduler_job_definition_v1(),
  private.guard_scheduler_job_run_v1(),
  private.ensure_scheduler_job_definition_impl(
    text,text,bigint,text,text,text,integer,integer,integer,integer,integer,integer,boolean
  ),
  private.converge_scheduler_job_definition_impl(
    text,text,bigint,text,text,text,integer,integer,integer,integer,integer,integer,boolean
  ),
  private.claim_due_scheduler_job_impl(text,text,bigint,text),
  private.complete_scheduler_job_run_impl(
    text,text,bigint,text,uuid,bigint,text,uuid,text
  ),
  private.fail_scheduler_job_run_impl(
    text,text,bigint,text,uuid,bigint,text,uuid,text,text,boolean
  ),
  private.inspect_scheduler_dead_letter_impl(text,text,bigint,text,uuid),
  private.replay_scheduler_dead_letter_impl(
    text,text,bigint,text,uuid,bigint,text,text,text,integer,uuid,text,boolean
  ),
  worker_api.ensure_scheduler_job_definition(
    text,text,bigint,text,text,text,integer,integer,integer,integer,integer,integer,boolean
  ),
  worker_api.converge_scheduler_job_definition(
    text,text,bigint,text,text,text,integer,integer,integer,integer,integer,integer,boolean
  ),
  worker_api.claim_due_scheduler_job(text,text,bigint,text),
  worker_api.complete_scheduler_job_run(
    text,text,bigint,text,uuid,bigint,text,uuid,text
  ),
  worker_api.fail_scheduler_job_run(
    text,text,bigint,text,uuid,bigint,text,uuid,text,text,boolean
  ),
  worker_api.inspect_scheduler_dead_letter(text,text,bigint,text,uuid),
  worker_api.replay_scheduler_dead_letter(
    text,text,bigint,text,uuid,bigint,text,text,text,integer,uuid,text,boolean
  )
from public, anon, authenticated, authenticator, service_role;

grant execute on function
  private.ensure_scheduler_job_definition_impl(
    text,text,bigint,text,text,text,integer,integer,integer,integer,integer,integer,boolean
  ),
  private.converge_scheduler_job_definition_impl(
    text,text,bigint,text,text,text,integer,integer,integer,integer,integer,integer,boolean
  ),
  private.claim_due_scheduler_job_impl(text,text,bigint,text),
  private.complete_scheduler_job_run_impl(
    text,text,bigint,text,uuid,bigint,text,uuid,text
  ),
  private.fail_scheduler_job_run_impl(
    text,text,bigint,text,uuid,bigint,text,uuid,text,text,boolean
  ),
  private.inspect_scheduler_dead_letter_impl(text,text,bigint,text,uuid),
  private.replay_scheduler_dead_letter_impl(
    text,text,bigint,text,uuid,bigint,text,text,text,integer,uuid,text,boolean
  ),
  worker_api.ensure_scheduler_job_definition(
    text,text,bigint,text,text,text,integer,integer,integer,integer,integer,integer,boolean
  ),
  worker_api.converge_scheduler_job_definition(
    text,text,bigint,text,text,text,integer,integer,integer,integer,integer,integer,boolean
  ),
  worker_api.claim_due_scheduler_job(text,text,bigint,text),
  worker_api.complete_scheduler_job_run(
    text,text,bigint,text,uuid,bigint,text,uuid,text
  ),
  worker_api.fail_scheduler_job_run(
    text,text,bigint,text,uuid,bigint,text,uuid,text,text,boolean
  ),
  worker_api.inspect_scheduler_dead_letter(text,text,bigint,text,uuid),
  worker_api.replay_scheduler_dead_letter(
    text,text,bigint,text,uuid,bigint,text,text,text,integer,uuid,text,boolean
  )
to service_role;

do $$
declare
  protected_table_count bigint;
  policy_count bigint;
  forbidden_table_acl_count bigint;
  implementation_contract_count bigint;
  wrapper_contract_count bigint;
  forbidden_function_acl_count bigint;
  missing_service_execute_count bigint;
  exposed_helper_count bigint;
  contract_owner_count bigint;
  contract_object_count bigint;
  contract_owner_oid oid;
  contract_owner_trusted boolean;
begin
  select pg_catalog.count(*) into protected_table_count
  from pg_catalog.pg_class as relation
  where relation.oid in (
      'private.scheduler_job_definitions'::regclass,
      'private.scheduler_job_runs'::regclass,
      'private.scheduler_job_leases'::regclass,
      'private.scheduler_replay_requests'::regclass
    )
    and relation.relrowsecurity
    and relation.relforcerowsecurity;

  select pg_catalog.count(*) into policy_count
  from pg_catalog.pg_policy as policy
  where policy.polrelid in (
    'private.scheduler_job_definitions'::regclass,
    'private.scheduler_job_runs'::regclass,
    'private.scheduler_job_leases'::regclass,
    'private.scheduler_replay_requests'::regclass
  );

  select pg_catalog.count(*) into forbidden_table_acl_count
  from pg_catalog.pg_class as relation
  cross join lateral pg_catalog.aclexplode(
    coalesce(
      relation.relacl,
      pg_catalog.acldefault('r', relation.relowner)
    )
  ) as acl
  left join pg_catalog.pg_roles as grantee on grantee.oid = acl.grantee
  where relation.oid in (
      'private.scheduler_job_definitions'::regclass,
      'private.scheduler_job_runs'::regclass,
      'private.scheduler_job_leases'::regclass,
      'private.scheduler_replay_requests'::regclass
    )
    and (
      acl.grantee = 0
      or grantee.rolname in ('anon','authenticated','authenticator','service_role')
    );

  select pg_catalog.count(*) into implementation_contract_count
  from pg_catalog.pg_proc as procedure
  where procedure.oid in (
      'private.ensure_scheduler_job_definition_impl(text,text,bigint,text,text,text,integer,integer,integer,integer,integer,integer,boolean)'::regprocedure,
      'private.converge_scheduler_job_definition_impl(text,text,bigint,text,text,text,integer,integer,integer,integer,integer,integer,boolean)'::regprocedure,
      'private.claim_due_scheduler_job_impl(text,text,bigint,text)'::regprocedure,
      'private.complete_scheduler_job_run_impl(text,text,bigint,text,uuid,bigint,text,uuid,text)'::regprocedure,
      'private.fail_scheduler_job_run_impl(text,text,bigint,text,uuid,bigint,text,uuid,text,text,boolean)'::regprocedure,
      'private.inspect_scheduler_dead_letter_impl(text,text,bigint,text,uuid)'::regprocedure,
      'private.replay_scheduler_dead_letter_impl(text,text,bigint,text,uuid,bigint,text,text,text,integer,uuid,text,boolean)'::regprocedure
    )
    and procedure.prosecdef
    and procedure.provolatile = 'v'
    and procedure.proconfig = array['search_path=""']::text[];

  select pg_catalog.count(*) into wrapper_contract_count
  from pg_catalog.pg_proc as procedure
  where procedure.oid in (
      'worker_api.ensure_scheduler_job_definition(text,text,bigint,text,text,text,integer,integer,integer,integer,integer,integer,boolean)'::regprocedure,
      'worker_api.converge_scheduler_job_definition(text,text,bigint,text,text,text,integer,integer,integer,integer,integer,integer,boolean)'::regprocedure,
      'worker_api.claim_due_scheduler_job(text,text,bigint,text)'::regprocedure,
      'worker_api.complete_scheduler_job_run(text,text,bigint,text,uuid,bigint,text,uuid,text)'::regprocedure,
      'worker_api.fail_scheduler_job_run(text,text,bigint,text,uuid,bigint,text,uuid,text,text,boolean)'::regprocedure,
      'worker_api.inspect_scheduler_dead_letter(text,text,bigint,text,uuid)'::regprocedure,
      'worker_api.replay_scheduler_dead_letter(text,text,bigint,text,uuid,bigint,text,text,text,integer,uuid,text,boolean)'::regprocedure
    )
    and not procedure.prosecdef
    and procedure.provolatile = 'v'
    and procedure.proconfig = array['search_path=""']::text[];

  select pg_catalog.count(*) into forbidden_function_acl_count
  from pg_catalog.pg_proc as procedure
  cross join lateral pg_catalog.aclexplode(
    coalesce(
      procedure.proacl,
      pg_catalog.acldefault('f', procedure.proowner)
    )
  ) as acl
  left join pg_catalog.pg_roles as grantee on grantee.oid = acl.grantee
  where procedure.oid in (
      'private.scheduler_definition_sha256_v1(text,integer,integer,integer,integer,integer,integer,boolean)'::regprocedure,
      'private.require_scheduler_outer_lease_v1(text,text,bigint,text,timestamptz)'::regprocedure,
      'private.scheduler_command_barrier_satisfied_v1(text,text,bigint,text,timestamptz,timestamptz)'::regprocedure,
      'private.scheduler_definition_document_v1(private.scheduler_job_definitions)'::regprocedure,
      'private.scheduler_run_document_v1(private.scheduler_job_runs)'::regprocedure,
      'private.guard_scheduler_job_definition_v1()'::regprocedure,
      'private.guard_scheduler_job_run_v1()'::regprocedure,
      'private.ensure_scheduler_job_definition_impl(text,text,bigint,text,text,text,integer,integer,integer,integer,integer,integer,boolean)'::regprocedure,
      'private.converge_scheduler_job_definition_impl(text,text,bigint,text,text,text,integer,integer,integer,integer,integer,integer,boolean)'::regprocedure,
      'private.claim_due_scheduler_job_impl(text,text,bigint,text)'::regprocedure,
      'private.complete_scheduler_job_run_impl(text,text,bigint,text,uuid,bigint,text,uuid,text)'::regprocedure,
      'private.fail_scheduler_job_run_impl(text,text,bigint,text,uuid,bigint,text,uuid,text,text,boolean)'::regprocedure,
      'private.inspect_scheduler_dead_letter_impl(text,text,bigint,text,uuid)'::regprocedure,
      'private.replay_scheduler_dead_letter_impl(text,text,bigint,text,uuid,bigint,text,text,text,integer,uuid,text,boolean)'::regprocedure,
      'worker_api.ensure_scheduler_job_definition(text,text,bigint,text,text,text,integer,integer,integer,integer,integer,integer,boolean)'::regprocedure,
      'worker_api.converge_scheduler_job_definition(text,text,bigint,text,text,text,integer,integer,integer,integer,integer,integer,boolean)'::regprocedure,
      'worker_api.claim_due_scheduler_job(text,text,bigint,text)'::regprocedure,
      'worker_api.complete_scheduler_job_run(text,text,bigint,text,uuid,bigint,text,uuid,text)'::regprocedure,
      'worker_api.fail_scheduler_job_run(text,text,bigint,text,uuid,bigint,text,uuid,text,text,boolean)'::regprocedure,
      'worker_api.inspect_scheduler_dead_letter(text,text,bigint,text,uuid)'::regprocedure,
      'worker_api.replay_scheduler_dead_letter(text,text,bigint,text,uuid,bigint,text,text,text,integer,uuid,text,boolean)'::regprocedure
    )
    and acl.privilege_type = 'EXECUTE'
    and (
      acl.grantee = 0
      or grantee.rolname in ('anon','authenticated','authenticator')
    );

  select pg_catalog.count(*) into missing_service_execute_count
  from pg_catalog.unnest(array[
    'private.ensure_scheduler_job_definition_impl(text,text,bigint,text,text,text,integer,integer,integer,integer,integer,integer,boolean)'::regprocedure::oid,
    'private.converge_scheduler_job_definition_impl(text,text,bigint,text,text,text,integer,integer,integer,integer,integer,integer,boolean)'::regprocedure::oid,
    'private.claim_due_scheduler_job_impl(text,text,bigint,text)'::regprocedure::oid,
    'private.complete_scheduler_job_run_impl(text,text,bigint,text,uuid,bigint,text,uuid,text)'::regprocedure::oid,
    'private.fail_scheduler_job_run_impl(text,text,bigint,text,uuid,bigint,text,uuid,text,text,boolean)'::regprocedure::oid,
    'private.inspect_scheduler_dead_letter_impl(text,text,bigint,text,uuid)'::regprocedure::oid,
    'private.replay_scheduler_dead_letter_impl(text,text,bigint,text,uuid,bigint,text,text,text,integer,uuid,text,boolean)'::regprocedure::oid,
    'worker_api.ensure_scheduler_job_definition(text,text,bigint,text,text,text,integer,integer,integer,integer,integer,integer,boolean)'::regprocedure::oid,
    'worker_api.converge_scheduler_job_definition(text,text,bigint,text,text,text,integer,integer,integer,integer,integer,integer,boolean)'::regprocedure::oid,
    'worker_api.claim_due_scheduler_job(text,text,bigint,text)'::regprocedure::oid,
    'worker_api.complete_scheduler_job_run(text,text,bigint,text,uuid,bigint,text,uuid,text)'::regprocedure::oid,
    'worker_api.fail_scheduler_job_run(text,text,bigint,text,uuid,bigint,text,uuid,text,text,boolean)'::regprocedure::oid,
    'worker_api.inspect_scheduler_dead_letter(text,text,bigint,text,uuid)'::regprocedure::oid,
    'worker_api.replay_scheduler_dead_letter(text,text,bigint,text,uuid,bigint,text,text,text,integer,uuid,text,boolean)'::regprocedure::oid
  ]) as expected(procedure_oid)
  where not pg_catalog.has_function_privilege(
    'service_role',
    expected.procedure_oid,
    'EXECUTE'
  );

  select pg_catalog.count(*) into exposed_helper_count
  from pg_catalog.unnest(array[
    'private.scheduler_definition_sha256_v1(text,integer,integer,integer,integer,integer,integer,boolean)'::regprocedure::oid,
    'private.require_scheduler_outer_lease_v1(text,text,bigint,text,timestamptz)'::regprocedure::oid,
    'private.scheduler_command_barrier_satisfied_v1(text,text,bigint,text,timestamptz,timestamptz)'::regprocedure::oid,
    'private.scheduler_definition_document_v1(private.scheduler_job_definitions)'::regprocedure::oid,
    'private.scheduler_run_document_v1(private.scheduler_job_runs)'::regprocedure::oid,
    'private.guard_scheduler_job_definition_v1()'::regprocedure::oid,
    'private.guard_scheduler_job_run_v1()'::regprocedure::oid
  ]) as helper(procedure_oid)
  where pg_catalog.has_function_privilege(
    'service_role',
    helper.procedure_oid,
    'EXECUTE'
  );

  select
    pg_catalog.count(distinct contract_object.owner_oid),
    pg_catalog.count(*)
  into contract_owner_count, contract_object_count
  from (
    select relation.relowner as owner_oid
    from pg_catalog.pg_class as relation
    where relation.oid in (
      'private.scheduler_job_definitions'::regclass,
      'private.scheduler_job_runs'::regclass,
      'private.scheduler_job_leases'::regclass,
      'private.scheduler_replay_requests'::regclass
    )
    union all
    select procedure.proowner as owner_oid
    from pg_catalog.pg_proc as procedure
    where procedure.oid in (
      'private.scheduler_definition_sha256_v1(text,integer,integer,integer,integer,integer,integer,boolean)'::regprocedure,
      'private.require_scheduler_outer_lease_v1(text,text,bigint,text,timestamptz)'::regprocedure,
      'private.scheduler_command_barrier_satisfied_v1(text,text,bigint,text,timestamptz,timestamptz)'::regprocedure,
      'private.scheduler_definition_document_v1(private.scheduler_job_definitions)'::regprocedure,
      'private.scheduler_run_document_v1(private.scheduler_job_runs)'::regprocedure,
      'private.guard_scheduler_job_definition_v1()'::regprocedure,
      'private.guard_scheduler_job_run_v1()'::regprocedure,
      'private.ensure_scheduler_job_definition_impl(text,text,bigint,text,text,text,integer,integer,integer,integer,integer,integer,boolean)'::regprocedure,
      'private.converge_scheduler_job_definition_impl(text,text,bigint,text,text,text,integer,integer,integer,integer,integer,integer,boolean)'::regprocedure,
      'private.claim_due_scheduler_job_impl(text,text,bigint,text)'::regprocedure,
      'private.complete_scheduler_job_run_impl(text,text,bigint,text,uuid,bigint,text,uuid,text)'::regprocedure,
      'private.fail_scheduler_job_run_impl(text,text,bigint,text,uuid,bigint,text,uuid,text,text,boolean)'::regprocedure,
      'private.inspect_scheduler_dead_letter_impl(text,text,bigint,text,uuid)'::regprocedure,
      'private.replay_scheduler_dead_letter_impl(text,text,bigint,text,uuid,bigint,text,text,text,integer,uuid,text,boolean)'::regprocedure,
      'worker_api.ensure_scheduler_job_definition(text,text,bigint,text,text,text,integer,integer,integer,integer,integer,integer,boolean)'::regprocedure,
      'worker_api.converge_scheduler_job_definition(text,text,bigint,text,text,text,integer,integer,integer,integer,integer,integer,boolean)'::regprocedure,
      'worker_api.claim_due_scheduler_job(text,text,bigint,text)'::regprocedure,
      'worker_api.complete_scheduler_job_run(text,text,bigint,text,uuid,bigint,text,uuid,text)'::regprocedure,
      'worker_api.fail_scheduler_job_run(text,text,bigint,text,uuid,bigint,text,uuid,text,text,boolean)'::regprocedure,
      'worker_api.inspect_scheduler_dead_letter(text,text,bigint,text,uuid)'::regprocedure,
      'worker_api.replay_scheduler_dead_letter(text,text,bigint,text,uuid,bigint,text,text,text,integer,uuid,text,boolean)'::regprocedure
    )
  ) as contract_object;

  select relation.relowner into contract_owner_oid
  from pg_catalog.pg_class as relation
  where relation.oid = 'private.scheduler_job_definitions'::regclass;

  select
    role.rolname not in ('anon','authenticated','authenticator','service_role')
    and (role.rolsuper or role.rolbypassrls)
  into contract_owner_trusted
  from pg_catalog.pg_roles as role
  where role.oid = contract_owner_oid;

  if protected_table_count <> 4
     or policy_count <> 0
     or forbidden_table_acl_count <> 0
     or implementation_contract_count <> 7
     or wrapper_contract_count <> 7
     or forbidden_function_acl_count <> 0
     or missing_service_execute_count <> 0
     or exposed_helper_count <> 0
     or contract_owner_count <> 1
     or contract_object_count <> 25
     or contract_owner_trusted is distinct from true then
    raise exception 'durable_operations_scheduler_security_contract_failed'
      using errcode = '55000';
  end if;
end;
$$;

commit;
