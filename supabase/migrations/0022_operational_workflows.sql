-- Operational workflow completion for the G1/G2 control plane.
--
-- This migration makes approved commands discoverable by the worker, adds a
-- strict maker-checker access workflow, and adds the one-time account-opening
-- workflow required before either non-live account can execute.  No function
-- in an exposed schema is SECURITY DEFINER.

-- Realtime carries only a monotonic invalidation signal.  Clients must fetch
-- the canonical snapshot after receiving it; no order, decision, audit, or
-- accounting payload is published.
create table api.control_plane_signal (
  id text primary key check (id = 'singleton'),
  signal_version bigint not null default 0 check (signal_version >= 0),
  signaled_at timestamptz not null default clock_timestamp(),
  signal_kind text not null default 'snapshot_invalidated'
    check (signal_kind = 'snapshot_invalidated')
);
insert into api.control_plane_signal (id) values ('singleton');
alter table api.control_plane_signal enable row level security;
create policy control_plane_signal_authenticated_read
  on api.control_plane_signal
  for select
  to authenticated
  using ((select auth.uid()) is not null);
revoke all on api.control_plane_signal from public, anon, service_role;
grant select on api.control_plane_signal to authenticated;
alter publication supabase_realtime add table api.control_plane_signal;
alter publication supabase_realtime drop table public.bot_settings;
alter publication supabase_realtime drop table public.worker_heartbeats;
alter publication supabase_realtime drop table public.api_health;
alter publication supabase_realtime drop table public.decision_snapshots;
alter publication supabase_realtime drop table public.ai_upgrade_candidates;
alter publication supabase_realtime drop table public.engine_events;

create or replace function private.bump_control_plane_signal()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  update api.control_plane_signal
  set signal_version = signal_version + 1,
      signaled_at = clock_timestamp()
  where id = 'singleton';
  return null;
end;
$$;

create trigger signal_operation_command_change
  after insert or update on private.operation_commands
  for each statement execute function private.bump_control_plane_signal();
create trigger signal_execution_control_change
  after insert or update on private.execution_controls
  for each statement execute function private.bump_control_plane_signal();
create trigger signal_incident_change
  after insert or update on private.incidents
  for each statement execute function private.bump_control_plane_signal();
create trigger signal_access_change
  after insert or update on private.access_change_requests
  for each statement execute function private.bump_control_plane_signal();
create trigger signal_reconciliation_change
  after insert or update on private.execution_reconciliation_state
  for each statement execute function private.bump_control_plane_signal();
create trigger signal_worker_heartbeat
  after insert on public.worker_heartbeats
  for each statement execute function private.bump_control_plane_signal();

alter table private.reconciliation_breaks
  add column revision bigint not null default 0 check (revision >= 0);
alter table private.operation_command_reviews
  add column evidence_sha256 text check (
    evidence_sha256 is null or evidence_sha256 ~ '^[0-9a-f]{64}$'
  ),
  add column request_digest_sha256 text check (
    request_digest_sha256 is null or request_digest_sha256 ~ '^[0-9a-f]{64}$'
  );

create table private.worker_lease_releases (
  account_id text not null references private.trading_accounts(account_id),
  fencing_token bigint not null check (fencing_token > 0),
  holder_id text not null check (nullif(btrim(holder_id), '') is not null),
  release_sha text not null check (release_sha ~ '^[0-9a-f]{40}([0-9a-f]{24})?$'),
  released_at timestamptz not null,
  primary key (account_id, fencing_token)
);
alter table private.worker_lease_releases enable row level security;

create or replace function private.release_worker_lease_impl(
  p_account_id text,
  p_holder_id text,
  p_fencing_token bigint,
  p_now timestamptz,
  p_release_sha text
)
returns table (
  account_id text,
  holder_id text,
  fencing_token bigint,
  released_at timestamptz,
  idempotent boolean
)
language plpgsql
security definer
set search_path = ''
as $$
declare
  lease_row private.worker_leases%rowtype;
  prior_release private.worker_lease_releases%rowtype;
  effective_expiry timestamptz;
begin
  perform private.require_service_role();
  if nullif(btrim(p_account_id), '') is null
     or p_holder_id !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
     or p_fencing_token <= 0
     or p_release_sha !~ '^[0-9a-f]{40}([0-9a-f]{24})?$'
     or p_now < clock_timestamp() - interval '5 minutes'
     or p_now > clock_timestamp() + interval '30 seconds' then
    raise exception 'worker_lease_release_parameters_invalid' using errcode = '22023';
  end if;
  select * into prior_release
  from private.worker_lease_releases
  where private.worker_lease_releases.account_id = p_account_id
    and private.worker_lease_releases.fencing_token = p_fencing_token;
  if found then
    if prior_release.holder_id <> p_holder_id
       or prior_release.release_sha <> p_release_sha then
      raise exception 'worker_lease_release_identity_conflict' using errcode = '40001';
    end if;
    return query select p_account_id, p_holder_id, p_fencing_token,
      prior_release.released_at, true;
    return;
  end if;
  select * into lease_row
  from private.worker_leases
  where private.worker_leases.account_id = p_account_id
  for update;
  if not found
     or lease_row.holder_id <> p_holder_id
     or lease_row.fencing_token <> p_fencing_token
     or lease_row.release_sha <> p_release_sha then
    raise exception 'worker_lease_not_owned_or_stale' using errcode = '40001';
  end if;
  effective_expiry := greatest(p_now, lease_row.renewed_at + interval '1 microsecond');
  update private.worker_leases
  set expires_at = effective_expiry
  where private.worker_leases.account_id = p_account_id
    and private.worker_leases.holder_id = p_holder_id
    and private.worker_leases.fencing_token = p_fencing_token
    and private.worker_leases.release_sha = p_release_sha;
  insert into private.worker_lease_releases (
    account_id, fencing_token, holder_id, release_sha, released_at
  ) values (
    p_account_id, p_fencing_token, p_holder_id, p_release_sha, p_now
  );
  return query select p_account_id, p_holder_id, p_fencing_token, p_now, false;
end;
$$;

create or replace function worker_api.release_worker_lease(
  p_account_id text,
  p_holder_id text,
  p_fencing_token bigint,
  p_now timestamptz,
  p_release_sha text
)
returns table (
  account_id text,
  holder_id text,
  fencing_token bigint,
  released_at timestamptz,
  idempotent boolean
)
language sql
security invoker
set search_path = ''
as $$
  select * from private.release_worker_lease_impl(
    p_account_id, p_holder_id, p_fencing_token, p_now, p_release_sha
  );
$$;

create or replace function private.record_worker_heartbeat_impl(
  p_worker_id text,
  p_status text,
  p_details jsonb,
  p_now timestamptz,
  p_release_sha text
)
returns table (heartbeat_id uuid, created_at timestamptz)
language plpgsql
security definer
set search_path = ''
as $$
declare
  new_id uuid;
  completed_time timestamptz;
begin
  perform private.require_service_role();
  if p_worker_id !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
     or p_status not in ('ok', 'warning', 'error', 'shutting_down')
     or jsonb_typeof(p_details) <> 'object'
     or p_release_sha !~ '^[0-9a-f]{40}([0-9a-f]{24})?$'
     or p_details->>'release_sha' is distinct from p_release_sha
     or p_now < clock_timestamp() - interval '5 minutes'
     or p_now > clock_timestamp() + interval '30 seconds' then
    raise exception 'worker_heartbeat_parameters_invalid' using errcode = '22023';
  end if;
  if p_status = 'ok' then
    if p_details->>'checkpoint' not in ('cycle_completed', 'operations_completed')
       or nullif(p_details->>'completed_at', '') is null
       or (
         p_details->>'checkpoint' = 'cycle_completed'
         and p_details->>'component' is distinct from 'trading_cycle'
       )
       or (
         p_details->>'checkpoint' = 'operations_completed'
         and p_details->>'component' is distinct from 'operations_v2'
       ) then
      raise exception 'completed_worker_heartbeat_evidence_required'
        using errcode = '22023';
    end if;
    completed_time := (p_details->>'completed_at')::timestamptz;
    if completed_time > p_now + interval '30 seconds'
       or completed_time < p_now - interval '1 hour' then
      raise exception 'worker_heartbeat_completion_time_invalid'
        using errcode = '22023';
    end if;
  end if;
  insert into public.worker_heartbeats (
    worker_name, status, details, created_at
  ) values (
    'trading-worker:' || p_worker_id, p_status, p_details, p_now
  ) returning id into new_id;
  return query select new_id, p_now;
exception
  when invalid_text_representation or datetime_field_overflow then
    raise exception 'worker_heartbeat_completion_time_invalid'
      using errcode = '22023';
end;
$$;

create or replace function worker_api.record_worker_heartbeat(
  p_worker_id text,
  p_status text,
  p_details jsonb,
  p_now timestamptz,
  p_release_sha text
)
returns table (heartbeat_id uuid, created_at timestamptz)
language sql
security invoker
set search_path = ''
as $$
  select * from private.record_worker_heartbeat_impl(
    p_worker_id, p_status, p_details, p_now, p_release_sha
  );
$$;

create or replace function private.fail_reserved_intent_pre_dispatch_impl(
  p_intent_id uuid,
  p_worker_id text,
  p_fencing_token bigint,
  p_control_epoch bigint,
  p_release_sha text,
  p_now timestamptz,
  p_reason_code text
)
returns table (
  intent_id uuid,
  observation_id uuid,
  state text,
  reason_code text,
  idempotent boolean
)
language plpgsql
security definer
set search_path = ''
as $$
declare
  authorization_time timestamptz := clock_timestamp();
  intent_row private.order_intents%rowtype;
  reservation_row private.order_reservations%rowtype;
  existing_observation private.execution_observations%rowtype;
  new_observation_id uuid;
  latest_reservation_sequence integer;
  remaining_cash bigint;
  remaining_quantity bigint;
  observation_hash text;
begin
  perform private.require_service_role();
  if p_worker_id !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
     or p_fencing_token is null
     or p_fencing_token <= 0
     or p_control_epoch is null
     or p_control_epoch <= 0
     or p_release_sha !~ '^[0-9a-f]{40}([0-9a-f]{24})?$'
     or p_now is null
     or p_now < authorization_time - interval '5 minutes'
     or p_now > authorization_time + interval '30 seconds'
     or nullif(btrim(p_reason_code), '') is null
     or p_reason_code !~ '^[a-z0-9_]{3,100}$' then
    raise exception 'pre_dispatch_failure_parameters_invalid' using errcode = '22023';
  end if;

  perform pg_catalog.pg_advisory_xact_lock(
    pg_catalog.hashtextextended(p_intent_id::text, 68491723011702::bigint)
  );
  select * into intent_row
  from private.order_intents
  where id = p_intent_id
  for update;
  if not found then
    raise exception 'execution_intent_not_found' using errcode = 'P0002';
  end if;
  select * into existing_observation
  from private.execution_observations
  where private.execution_observations.intent_id = p_intent_id
  order by sequence desc
  limit 1;
  if found then
    if existing_observation.sequence = 1
       and existing_observation.event_type = 'failed_pre_dispatch'
       and existing_observation.reason_code = p_reason_code
       and existing_observation.attempt_id is null
       and not exists (
         select 1 from private.order_attempts
         where private.order_attempts.intent_id = p_intent_id
       )
       and exists (
         select 1
         from private.execution_reconciliation_state as reconciliation
         where reconciliation.intent_id = p_intent_id
           and reconciliation.state = 'complete'
       )
       and exists (
         select 1
         from private.reservation_events as reservation_event
         join private.order_reservations as reservation
           on reservation.id = reservation_event.reservation_id
         where reservation.intent_id = p_intent_id
           and reservation_event.remaining_cash_krw = 0
           and reservation_event.remaining_quantity = 0
       ) then
      return query select
        p_intent_id, existing_observation.id, 'complete'::text,
        p_reason_code, true;
      return;
    end if;
    raise exception 'pre_dispatch_failure_observation_already_exists'
      using errcode = '40001';
  end if;
  if exists (
    select 1 from private.order_attempts
    where private.order_attempts.intent_id = p_intent_id
  ) then
    raise exception 'pre_dispatch_failure_dispatch_already_started'
      using errcode = '40001';
  end if;
  if not exists (
    select 1
    from private.worker_leases as lease
    where lease.account_id = intent_row.account_id
      and lease.holder_id = p_worker_id
      and lease.fencing_token = p_fencing_token
      and lease.release_sha = p_release_sha
      and lease.expires_at > authorization_time
  ) then
    raise exception 'worker_fencing_token_stale' using errcode = '40001';
  end if;
  if not exists (
    select 1
    from private.execution_controls as control
    where control.account_id = intent_row.account_id
      and control.environment = intent_row.environment
      and control.control_epoch = p_control_epoch
      and control.control_epoch >= intent_row.control_epoch
  ) then
    raise exception 'pre_dispatch_failure_control_epoch_stale'
      using errcode = '40001';
  end if;
  if not exists (
    select 1
    from private.execution_reconciliation_state as reconciliation
    where reconciliation.intent_id = p_intent_id
      and reconciliation.state = 'leased'
      and reconciliation.lease_owner = p_worker_id
      and reconciliation.lease_expires_at > authorization_time
  ) then
    raise exception 'reconciliation_lease_not_owned_or_expired'
      using errcode = '40001';
  end if;

  select * into reservation_row
  from private.order_reservations
  where private.order_reservations.intent_id = p_intent_id
  for update;
  if not found
     or reservation_row.fencing_token > p_fencing_token
     or reservation_row.control_epoch > p_control_epoch then
    raise exception 'pre_dispatch_failure_reservation_stale_or_missing'
      using errcode = '40001';
  end if;
  select
    reservation_event.event_sequence,
    reservation_event.remaining_cash_krw,
    reservation_event.remaining_quantity
  into latest_reservation_sequence, remaining_cash, remaining_quantity
  from private.reservation_events as reservation_event
  where reservation_event.reservation_id = reservation_row.id
  order by reservation_event.event_sequence desc
  limit 1
  for update;
  if not found or (remaining_cash = 0 and remaining_quantity = 0) then
    raise exception 'pre_dispatch_failure_reservation_not_releasable'
      using errcode = '40001';
  end if;

  observation_hash := pg_catalog.encode(
    public.digest(
      pg_catalog.convert_to(
        concat_ws(
          '|', 'pre_dispatch_failure_v1', p_intent_id::text, '1',
          private.utc_iso8601(p_now), p_reason_code, p_worker_id,
          p_fencing_token::text, p_control_epoch::text, p_release_sha
        ),
        'UTF8'
      ),
      'sha256'
    ),
    'hex'
  );
  insert into private.execution_observations (
    intent_id, attempt_id, sequence, event_type, observed_at,
    cumulative_quantity, cumulative_gross_krw, cumulative_commission_krw,
    cumulative_tax_krw, reason_code, observation_sha256,
    provider_order_id, provider_execution_id, provider_observation_sha256
  ) values (
    p_intent_id, null, 1, 'failed_pre_dispatch', p_now,
    0, 0, 0, 0, p_reason_code, observation_hash,
    null, null, observation_hash
  ) returning id into new_observation_id;
  insert into private.order_events (
    intent_id, attempt_id, observation_id, event_key, event_type,
    correlation_id, event_summary, occurred_at
  ) values (
    p_intent_id, null, new_observation_id, 'observation:1',
    'terminal_confirmed', intent_row.correlation_id,
    jsonb_build_object(
      'status', 'failed_pre_dispatch',
      'sequence', 1,
      'reason_code', p_reason_code
    ),
    p_now
  );

  if remaining_cash > 0 then
    update private.cash_balance_projection
    set reserved_cash_krw = reserved_cash_krw - remaining_cash,
        projection_version = projection_version + 1,
        projected_at = authorization_time
    where account_id = intent_row.account_id
      and reserved_cash_krw >= remaining_cash;
  else
    update private.position_projection
    set reserved_quantity = reserved_quantity - remaining_quantity,
        projection_version = projection_version + 1,
        projected_at = authorization_time
    where account_id = intent_row.account_id
      and symbol = intent_row.symbol
      and reserved_quantity >= remaining_quantity;
  end if;
  if not found then
    raise exception 'pre_dispatch_failure_projection_underflow'
      using errcode = '23514';
  end if;
  insert into private.reservation_events (
    reservation_id, intent_id, event_sequence, event_type,
    cash_delta_krw, quantity_delta, remaining_cash_krw,
    remaining_quantity, source_observation_id, occurred_at
  ) values (
    reservation_row.id, p_intent_id, latest_reservation_sequence + 1, 'released',
    -remaining_cash, -remaining_quantity, 0, 0, new_observation_id, p_now
  );
  update private.execution_reconciliation_state
  set state = 'complete',
      next_reconcile_at = p_now,
      lease_owner = null,
      lease_expires_at = null,
      last_reason_code = p_reason_code,
      updated_at = authorization_time
  where private.execution_reconciliation_state.intent_id = p_intent_id;

  insert into private.delivery_outbox (
    event_type, aggregate_type, aggregate_id, dedupe_key, payload,
    destination_type, available_at
  ) values (
    'reserved_intent_failed_pre_dispatch',
    'execution_observation', new_observation_id::text,
    'pre-dispatch-failure:' || p_intent_id::text,
    jsonb_build_object(
      'intent_id', p_intent_id,
      'observation_id', new_observation_id,
      'reason_code', p_reason_code,
      'severity', 'critical'
    ),
    'incident_alert', p_now
  );
  perform private.write_audit_event(
    'worker', null, null, 'trading_worker', p_worker_id, p_release_sha,
    'reserved_intent_failed_pre_dispatch', 'execution_observation',
    new_observation_id::text, intent_row.correlation_id, null, p_reason_code,
    null,
    array[
      'event_type', 'sequence', 'reservation_remaining',
      'reconciliation_state', 'release_sha'
    ],
    pg_catalog.encode(
      public.digest(
        pg_catalog.convert_to(intent_row.release_sha, 'UTF8'),
        'sha256'
      ),
      'hex'
    ),
    observation_hash, null
  );
  return query select
    p_intent_id, new_observation_id, 'complete'::text,
    p_reason_code, false;
end;
$$;

create or replace function worker_api.fail_reserved_intent_pre_dispatch(
  p_intent_id uuid,
  p_worker_id text,
  p_fencing_token bigint,
  p_control_epoch bigint,
  p_release_sha text,
  p_now timestamptz,
  p_reason_code text
)
returns table (
  intent_id uuid,
  observation_id uuid,
  state text,
  reason_code text,
  idempotent boolean
)
language sql
security invoker
set search_path = ''
as $$
  select * from private.fail_reserved_intent_pre_dispatch_impl(
    p_intent_id, p_worker_id, p_fencing_token, p_control_epoch,
    p_release_sha, p_now, p_reason_code
  );
$$;

create or replace function private.load_paper_execution_checkpoint_impl(
  p_intent_id uuid,
  p_account_id text,
  p_holder_id text,
  p_fencing_token bigint,
  p_control_epoch bigint,
  p_release_sha text,
  p_now timestamptz
)
returns table (
  intent_id uuid,
  attempt_id uuid,
  provider_order_id text,
  latest_sequence integer,
  latest_status text,
  latest_observed_at timestamptz,
  latest_cumulative_quantity bigint,
  latest_cumulative_gross_krw bigint,
  latest_cumulative_commission_krw bigint,
  latest_cumulative_tax_krw bigint,
  observation_history_sha256 text,
  expires_at timestamptz,
  intent_release_sha text,
  lease_release_sha text,
  position_cost_basis_method text,
  position_quantity_snapshot bigint,
  position_total_cost_krw bigint,
  position_cost_basis_sha256 text
)
language plpgsql
security definer
set search_path = ''
as $$
declare
  authorization_time timestamptz := clock_timestamp();
  intent_row private.order_intents%rowtype;
begin
  perform private.require_service_role();
  if nullif(btrim(p_account_id), '') is null
     or p_holder_id !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
     or p_fencing_token is null
     or p_fencing_token <= 0
     or p_control_epoch is null
     or p_control_epoch <= 0
     or p_release_sha !~ '^[0-9a-f]{40}([0-9a-f]{24})?$'
     or p_now is null
     or p_now < authorization_time - interval '5 minutes'
     or p_now > authorization_time + interval '30 seconds' then
    raise exception 'paper_checkpoint_parameters_invalid' using errcode = '22023';
  end if;
  perform pg_catalog.pg_advisory_xact_lock(
    pg_catalog.hashtextextended(p_intent_id::text, 68491723011702::bigint)
  );
  select * into intent_row
  from private.order_intents as intent
  where intent.id = p_intent_id
    and intent.account_id = p_account_id
  for update;
  if not found or intent_row.environment <> 'paper' then
    raise exception 'paper_checkpoint_intent_not_found' using errcode = 'P0002';
  end if;
  if intent_row.release_sha <> p_release_sha then
    raise exception 'paper_checkpoint_origin_release_mismatch' using errcode = '40001';
  end if;
  if not exists (
    select 1
    from private.worker_leases as lease
    where lease.account_id = p_account_id
      and lease.holder_id = p_holder_id
      and lease.fencing_token = p_fencing_token
      and lease.release_sha = p_release_sha
      and lease.expires_at > authorization_time
  ) then
    raise exception 'worker_fencing_token_stale' using errcode = '40001';
  end if;
  if not exists (
    select 1
    from private.order_reservations as reservation
    join private.execution_controls as control
      on control.account_id = reservation.account_id
     and control.environment = reservation.environment
    where reservation.intent_id = p_intent_id
      and reservation.account_id = p_account_id
      and reservation.control_epoch = p_control_epoch
      and control.execution_enabled is true
      and control.control_epoch = p_control_epoch
      and control.control_epoch = intent_row.control_epoch
      and control.effective_at <= authorization_time
      and control.expires_at > authorization_time
      and control.execution_policy_version = intent_row.execution_policy_version
      and control.execution_policy_sha256 = intent_row.execution_policy_sha256
      and control.risk_policy_sha256 = intent_row.risk_policy_sha256
      and control.provider_contract_version
        is not distinct from intent_row.provider_contract_version
      and control.provider_openapi_sha256
        is not distinct from intent_row.provider_openapi_sha256
  ) then
    raise exception 'paper_checkpoint_control_revalidation_failed'
      using errcode = '40001';
  end if;
  return query
  select
    intent.id,
    attempt.id,
    binding.provider_order_id,
    latest.sequence,
    latest.event_type,
    latest.observed_at,
    coalesce(latest.cumulative_quantity, 0),
    coalesce(latest.cumulative_gross_krw, 0),
    coalesce(latest.cumulative_commission_krw, 0),
    coalesce(latest.cumulative_tax_krw, 0),
    history.observation_history_sha256,
    intent.expires_at,
    intent.release_sha,
    lease.release_sha,
    intent.position_cost_basis_method,
    intent.position_quantity_snapshot,
    intent.position_total_cost_krw,
    intent.position_cost_basis_sha256
  from private.order_intents as intent
  join private.worker_leases as lease on lease.account_id = intent.account_id
  left join private.order_attempts as attempt on attempt.intent_id = intent.id
  left join private.provider_order_bindings as binding
    on binding.attempt_id = attempt.id
  left join lateral (
    select observation.*
    from private.execution_observations as observation
    where observation.intent_id = intent.id
    order by observation.sequence desc
    limit 1
  ) as latest on true
  left join lateral (
    select pg_catalog.encode(
      public.digest(
        pg_catalog.convert_to(
          string_agg(
            observation.sequence::text || ':'
              || observation.provider_observation_sha256,
            '|' order by observation.sequence
          ),
          'UTF8'
        ),
        'sha256'
      ),
      'hex'
    ) as observation_history_sha256
    from private.execution_observations as observation
    where observation.intent_id = intent.id
  ) as history on true
  where intent.id = p_intent_id
    and intent.account_id = p_account_id
    and lease.holder_id = p_holder_id
    and lease.fencing_token = p_fencing_token
    and lease.release_sha = p_release_sha
    and lease.expires_at > authorization_time;
end;
$$;

create or replace function worker_api.load_paper_execution_checkpoint(
  p_intent_id uuid,
  p_account_id text,
  p_holder_id text,
  p_fencing_token bigint,
  p_control_epoch bigint,
  p_release_sha text,
  p_now timestamptz
)
returns table (
  intent_id uuid,
  attempt_id uuid,
  provider_order_id text,
  latest_sequence integer,
  latest_status text,
  latest_observed_at timestamptz,
  latest_cumulative_quantity bigint,
  latest_cumulative_gross_krw bigint,
  latest_cumulative_commission_krw bigint,
  latest_cumulative_tax_krw bigint,
  observation_history_sha256 text,
  expires_at timestamptz,
  intent_release_sha text,
  lease_release_sha text,
  position_cost_basis_method text,
  position_quantity_snapshot bigint,
  position_total_cost_krw bigint,
  position_cost_basis_sha256 text
)
language sql
security invoker
set search_path = ''
as $$
  select * from private.load_paper_execution_checkpoint_impl(
    p_intent_id, p_account_id, p_holder_id, p_fencing_token, p_control_epoch,
    p_release_sha, p_now
  );
$$;

create or replace function private.expire_paper_intent_remainder_impl(
  p_intent_id uuid,
  p_worker_id text,
  p_fencing_token bigint,
  p_control_epoch bigint,
  p_release_sha text,
  p_now timestamptz,
  p_reason_code text
)
returns table (
  intent_id uuid,
  observation_id uuid,
  sequence integer,
  state text,
  reason_code text,
  idempotent boolean
)
language plpgsql
security definer
set search_path = ''
as $$
declare
  authorization_time timestamptz := clock_timestamp();
  intent_row private.order_intents%rowtype;
  attempt_row private.order_attempts%rowtype;
  binding_row private.provider_order_bindings%rowtype;
  reservation_row private.order_reservations%rowtype;
  latest_observation private.execution_observations%rowtype;
  new_observation_id uuid;
  next_sequence integer;
  latest_reservation_sequence integer;
  remaining_cash bigint;
  remaining_quantity bigint;
  cumulative_quantity bigint;
  cumulative_gross bigint;
  cumulative_commission bigint;
  cumulative_tax bigint;
  observation_hash text;
begin
  perform private.require_service_role();
  if p_worker_id !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
     or p_fencing_token is null or p_fencing_token <= 0
     or p_control_epoch is null or p_control_epoch <= 0
     or p_release_sha !~ '^[0-9a-f]{40}([0-9a-f]{24})?$'
     or p_now is null
     or p_now < authorization_time - interval '5 minutes'
     or p_now > authorization_time + interval '30 seconds'
     or nullif(btrim(p_reason_code), '') is null
     or p_reason_code !~ '^[a-z0-9_]{3,100}$' then
    raise exception 'paper_expiry_parameters_invalid' using errcode = '22023';
  end if;
  perform pg_catalog.pg_advisory_xact_lock(
    pg_catalog.hashtextextended(p_intent_id::text, 68491723011702::bigint)
  );
  select * into intent_row
  from private.order_intents as intent
  where intent.id = p_intent_id
  for update;
  if not found or intent_row.environment <> 'paper' then
    raise exception 'paper_expiry_intent_not_found' using errcode = 'P0002';
  end if;
  select * into latest_observation
  from private.execution_observations as observation
  where observation.intent_id = p_intent_id
  order by observation.sequence desc
  limit 1
  for update;
  if found and latest_observation.event_type = 'expired'
     and latest_observation.reason_code = p_reason_code
     and exists (
       select 1 from private.execution_reconciliation_state as reconciliation
       where reconciliation.intent_id = p_intent_id
         and reconciliation.state = 'complete'
     )
     and exists (
       select 1
       from private.reservation_events as reservation_event
       join private.order_reservations as reservation
         on reservation.id = reservation_event.reservation_id
       where reservation.intent_id = p_intent_id
         and reservation_event.remaining_cash_krw = 0
         and reservation_event.remaining_quantity = 0
     ) then
    return query select
      p_intent_id, latest_observation.id, latest_observation.sequence,
      'complete'::text, p_reason_code, true;
    return;
  end if;
  if found and latest_observation.event_type not in ('open', 'partial_filled') then
    raise exception 'paper_expiry_observation_not_resumable' using errcode = '40001';
  end if;
  if p_now < intent_row.expires_at then
    raise exception 'paper_intent_not_expired' using errcode = '40001';
  end if;
  if intent_row.release_sha <> p_release_sha then
    raise exception 'paper_expiry_origin_release_mismatch' using errcode = '40001';
  end if;
  if not exists (
    select 1 from private.worker_leases as lease
    where lease.account_id = intent_row.account_id
      and lease.holder_id = p_worker_id
      and lease.fencing_token = p_fencing_token
      and lease.release_sha = p_release_sha
      and lease.expires_at > authorization_time
  ) then
    raise exception 'worker_fencing_token_stale' using errcode = '40001';
  end if;
  if not exists (
    select 1 from private.execution_controls as control
    where control.account_id = intent_row.account_id
      and control.environment = intent_row.environment
      and control.control_epoch = p_control_epoch
      and control.control_epoch >= intent_row.control_epoch
  ) then
    raise exception 'paper_expiry_control_epoch_stale' using errcode = '40001';
  end if;
  if not exists (
    select 1 from private.execution_reconciliation_state as reconciliation
    where reconciliation.intent_id = p_intent_id
      and reconciliation.state = 'leased'
      and reconciliation.lease_owner = p_worker_id
      and reconciliation.lease_expires_at > authorization_time
  ) then
    raise exception 'reconciliation_lease_not_owned_or_expired'
      using errcode = '40001';
  end if;
  select * into attempt_row
  from private.order_attempts as attempt
  where attempt.intent_id = p_intent_id;
  if not found or attempt_row.broker <> 'internal_paper' then
    raise exception 'paper_expiry_dispatch_attempt_required' using errcode = '40001';
  end if;
  select * into binding_row
  from private.provider_order_bindings as binding
  where binding.attempt_id = attempt_row.id;
  if not found then
    raise exception 'paper_expiry_provider_binding_required' using errcode = '40001';
  end if;
  select * into reservation_row
  from private.order_reservations as reservation
  where reservation.intent_id = p_intent_id
  for update;
  if not found
     or reservation_row.fencing_token > p_fencing_token
     or reservation_row.control_epoch > p_control_epoch then
    raise exception 'paper_expiry_reservation_stale_or_missing'
      using errcode = '40001';
  end if;
  select
    reservation_event.event_sequence,
    reservation_event.remaining_cash_krw,
    reservation_event.remaining_quantity
  into latest_reservation_sequence, remaining_cash, remaining_quantity
  from private.reservation_events as reservation_event
  where reservation_event.reservation_id = reservation_row.id
  order by reservation_event.event_sequence desc
  limit 1
  for update;
  if not found then
    raise exception 'paper_expiry_reservation_event_missing' using errcode = '40001';
  end if;
  next_sequence := coalesce(latest_observation.sequence, 0) + 1;
  cumulative_quantity := coalesce(latest_observation.cumulative_quantity, 0);
  cumulative_gross := coalesce(latest_observation.cumulative_gross_krw, 0);
  cumulative_commission := coalesce(
    latest_observation.cumulative_commission_krw, 0
  );
  cumulative_tax := coalesce(latest_observation.cumulative_tax_krw, 0);
  observation_hash := pg_catalog.encode(
    public.digest(
      pg_catalog.convert_to(
        concat_ws(
          '|', p_intent_id::text, next_sequence::text, 'expired',
          binding_row.provider_order_id, '', private.utc_iso8601(p_now),
          cumulative_quantity::text, cumulative_gross::text,
          cumulative_commission::text, cumulative_tax::text,
          '', '', '', p_reason_code
        ),
        'UTF8'
      ),
      'sha256'
    ),
    'hex'
  );
  insert into private.execution_observations (
    intent_id, attempt_id, sequence, event_type, observed_at,
    cumulative_quantity, cumulative_gross_krw, cumulative_commission_krw,
    cumulative_tax_krw, reason_code, observation_sha256,
    provider_order_id, provider_execution_id, provider_observation_sha256
  ) values (
    p_intent_id, attempt_row.id, next_sequence, 'expired', p_now,
    cumulative_quantity, cumulative_gross, cumulative_commission,
    cumulative_tax, p_reason_code, observation_hash,
    binding_row.provider_order_id, null, observation_hash
  ) returning id into new_observation_id;
  insert into private.order_events (
    intent_id, attempt_id, observation_id, event_key, event_type,
    correlation_id, event_summary, occurred_at
  ) values (
    p_intent_id, attempt_row.id, new_observation_id,
    'observation:' || next_sequence::text, 'terminal_confirmed',
    intent_row.correlation_id,
    jsonb_build_object(
      'status', 'expired', 'sequence', next_sequence,
      'reason_code', p_reason_code
    ),
    p_now
  );
  if remaining_cash > 0 then
    update private.cash_balance_projection as cash
    set reserved_cash_krw = cash.reserved_cash_krw - remaining_cash,
        projection_version = cash.projection_version + 1,
        projected_at = authorization_time
    where cash.account_id = intent_row.account_id
      and cash.reserved_cash_krw >= remaining_cash;
    if not found then
      raise exception 'paper_expiry_projection_underflow' using errcode = '23514';
    end if;
  elsif remaining_quantity > 0 then
    update private.position_projection as position
    set reserved_quantity = position.reserved_quantity - remaining_quantity,
        projection_version = position.projection_version + 1,
        projected_at = authorization_time
    where position.account_id = intent_row.account_id
      and position.symbol = intent_row.symbol
      and position.reserved_quantity >= remaining_quantity;
    if not found then
      raise exception 'paper_expiry_projection_underflow' using errcode = '23514';
    end if;
  end if;
  if remaining_cash > 0 or remaining_quantity > 0 then
    insert into private.reservation_events (
      reservation_id, intent_id, event_sequence, event_type,
      cash_delta_krw, quantity_delta, remaining_cash_krw,
      remaining_quantity, source_observation_id, occurred_at
    ) values (
      reservation_row.id, p_intent_id, latest_reservation_sequence + 1,
      'released', -remaining_cash, -remaining_quantity, 0, 0,
      new_observation_id, p_now
    );
  end if;
  update private.execution_reconciliation_state as reconciliation
  set state = 'complete', next_reconcile_at = p_now,
      lease_owner = null, lease_expires_at = null,
      last_reason_code = p_reason_code, updated_at = authorization_time
  where reconciliation.intent_id = p_intent_id;
  insert into private.delivery_outbox (
    event_type, aggregate_type, aggregate_id, dedupe_key, payload,
    destination_type, available_at
  ) values (
    'paper_intent_remainder_expired', 'execution_observation',
    new_observation_id::text, 'paper-expiry:' || p_intent_id::text,
    jsonb_build_object(
      'intent_id', p_intent_id,
      'observation_id', new_observation_id,
      'sequence', next_sequence,
      'reason_code', p_reason_code,
      'severity', 'critical'
    ),
    'incident_alert', p_now
  );
  perform private.write_audit_event(
    'worker', null, null, 'trading_worker', p_worker_id, p_release_sha,
    'paper_intent_remainder_expired', 'execution_observation',
    new_observation_id::text, intent_row.correlation_id, null,
    p_reason_code, null,
    array['event_type', 'sequence', 'reservation_remaining',
      'reconciliation_state'],
    null, observation_hash, null
  );
  return query select
    p_intent_id, new_observation_id, next_sequence, 'complete'::text,
    p_reason_code, false;
end;
$$;

create or replace function worker_api.expire_paper_intent_remainder(
  p_intent_id uuid,
  p_worker_id text,
  p_fencing_token bigint,
  p_control_epoch bigint,
  p_release_sha text,
  p_now timestamptz,
  p_reason_code text
)
returns table (
  intent_id uuid,
  observation_id uuid,
  sequence integer,
  state text,
  reason_code text,
  idempotent boolean
)
language sql
security invoker
set search_path = ''
as $$
  select * from private.expire_paper_intent_remainder_impl(
    p_intent_id, p_worker_id, p_fencing_token, p_control_epoch,
    p_release_sha, p_now, p_reason_code
  );
$$;

create index idx_operation_commands_worker_claim
  on private.operation_commands (requested_at, id)
  where state in ('approved', 'claimed');

create or replace function private.claim_operation_command_batch_impl(
  p_holder_id text,
  p_release_sha text,
  p_now timestamptz,
  p_limit integer
)
returns table (
  command_id uuid,
  command_type text,
  environment text,
  account_id text,
  requested_change jsonb,
  requested_at timestamptz,
  expires_at timestamptz,
  revision bigint,
  claimed_at timestamptz,
  claim_expires_at timestamptz
)
language plpgsql
security definer
set search_path = ''
as $$
declare
  candidate private.operation_commands%rowtype;
  claimed private.operation_commands%rowtype;
begin
  perform private.require_service_role();
  if p_holder_id !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
     or p_release_sha !~ '^[0-9a-f]{40}([0-9a-f]{24})?$'
     or p_limit not between 1 and 25
     or p_now < clock_timestamp() - interval '5 minutes'
     or p_now > clock_timestamp() + interval '30 seconds' then
    raise exception 'operation_command_claim_parameters_invalid' using errcode = '22023';
  end if;

  for candidate in
    select command.*
    from private.operation_commands as command
    where command.expires_at > p_now
      and command.command_type in (
        'emergency_stop', 'account_opening', 'pause_paper', 'paper_resume',
        'strategy_promotion', 'contract_test_enable', 'risk_policy_change'
      )
      and (command.target_release_sha is null or command.target_release_sha = p_release_sha)
      and (
        command.state = 'approved'
        or (
          command.state = 'claimed'
          and (
            command.claim_expires_at <= p_now
            or (
              command.claimed_by_service = p_holder_id
              and command.claim_expires_at > p_now
            )
          )
        )
      )
    order by
      case command.command_type
        when 'emergency_stop' then 0
        when 'account_opening' then 1
        else 2
      end,
      command.requested_at,
      command.id
    limit p_limit
    for update skip locked
  loop
    if candidate.state = 'claimed'
       and candidate.claimed_by_service = p_holder_id
       and candidate.claim_expires_at > p_now then
      claimed := candidate;
    else
      update private.operation_commands
      set state = 'claimed',
          claimed_by_service = p_holder_id,
          claimed_at = p_now,
          claim_expires_at = p_now + interval '30 seconds',
          revision = private.operation_commands.revision + 1
      where id = candidate.id
      returning * into claimed;

      insert into private.operation_command_events (
        command_id, event_type, actor_type, service_principal,
        event_summary, occurred_at
      ) values (
        claimed.id, 'claimed', 'worker', p_holder_id,
        jsonb_build_object(
          'release_sha', p_release_sha,
          'claim_expires_at', claimed.claim_expires_at,
          'reclaimed', candidate.state = 'claimed'
        ),
        p_now
      );
      perform private.write_audit_event(
        'worker', null, null, 'trading_worker', p_holder_id, p_release_sha,
        'operation_command_claimed', 'operation_command', claimed.id::text,
        gen_random_uuid(), claimed.id,
        case when candidate.state = 'claimed' then 'lease_reclaimed' else 'claimed' end,
        null, array['state', 'claimed_at', 'claim_expires_at', 'revision'],
        null, null, claimed.evidence_id
      );
    end if;

    return query select
      claimed.id,
      case claimed.command_type
        when 'emergency_stop' then 'emergency_stop'
        when 'account_opening' then 'account_opening'
        when 'pause_paper' then 'pause_paper'
        when 'paper_resume' then 'resume_paper'
        when 'strategy_promotion' then 'activate_paper_strategy'
        when 'contract_test_enable' then 'start_contract_test'
        when 'risk_policy_change' then 'apply_risk_policy_version'
        else claimed.command_type
      end,
      coalesce(
        nullif(claimed.requested_change->>'environment', ''),
        account.environment
      ),
      nullif(claimed.requested_change->>'account_id', ''),
      claimed.requested_change,
      claimed.requested_at,
      claimed.expires_at,
      claimed.revision,
      claimed.claimed_at,
      claimed.claim_expires_at
    from private.trading_accounts as account
    where account.account_id = nullif(claimed.requested_change->>'account_id', '');
  end loop;
end;
$$;

create or replace function worker_api.claim_operation_command_batch(
  p_holder_id text,
  p_release_sha text,
  p_now timestamptz,
  p_limit integer default 25
)
returns table (
  command_id uuid,
  command_type text,
  environment text,
  account_id text,
  requested_change jsonb,
  requested_at timestamptz,
  expires_at timestamptz,
  revision bigint,
  claimed_at timestamptz,
  claim_expires_at timestamptz
)
language sql
security invoker
set search_path = ''
as $$
  select * from private.claim_operation_command_batch_impl(
    p_holder_id, p_release_sha, p_now, p_limit
  );
$$;

-- Access-control requests are immutable drafts.  The step-up grant is bound
-- to the draft hash and current auth session, and its clock window is checked
-- at consumption time rather than against the client-authored action time.

create or replace function private.access_change_draft_v1(p_payload jsonb)
returns jsonb
language sql
immutable
security definer
set search_path = ''
as $$
  select p_payload - array[
    'step_up_grant_id', 'change_hash', 'step_up_grant_issued_at',
    'step_up_grant_expires_at', 'step_up_grant_one_time',
    'step_up_grant_consumed_at', 'bound_action'
  ];
$$;

create or replace function private.validate_access_change_draft_v1(
  p_bound_action text,
  p_payload jsonb
)
returns void
language plpgsql
immutable
security definer
set search_path = ''
as $$
begin
  if p_bound_action = 'request' then
    perform private.assert_exact_json_keys(p_payload, array[
      'schema_version', 'request_id', 'subject_user_id', 'requested_role',
      'change_type', 'evidence_id', 'reason_code', 'requested_at', 'expires_at'
    ]);
    if jsonb_typeof(p_payload->'schema_version') <> 'number'
       or coalesce((p_payload->>'schema_version')::integer, -1) <> 1
       or p_payload->>'requested_role' not in (
         'platform_admin', 'operator', 'risk_approver', 'strategy_reviewer',
         'auditor', 'release_manager', 'viewer'
       )
       or p_payload->>'change_type' not in ('grant', 'revoke')
       or p_payload->>'reason_code' not in (
         'role_required', 'duty_separation', 'access_removal'
       ) then
      raise exception 'access_change_request_draft_invalid' using errcode = '22023';
    end if;
  elsif p_bound_action = 'review' then
    perform private.assert_exact_json_keys(p_payload, array[
      'schema_version', 'review_id', 'request_id', 'decision',
      'reason_code', 'expected_state', 'reviewed_at'
    ]);
    if jsonb_typeof(p_payload->'schema_version') <> 'number'
       or coalesce((p_payload->>'schema_version')::integer, -1) <> 1
       or p_payload->>'decision' not in ('approve', 'reject')
       or p_payload->>'reason_code' not in (
         'policy_satisfied', 'evidence_incomplete', 'separation_of_duties'
       )
       or p_payload->>'expected_state' <> 'requested' then
      raise exception 'access_change_review_draft_invalid' using errcode = '22023';
    end if;
  else
    raise exception 'access_change_bound_action_invalid' using errcode = '22023';
  end if;
exception
  when invalid_text_representation or numeric_value_out_of_range then
    raise exception 'access_change_draft_invalid' using errcode = '22023';
end;
$$;

create or replace function private.issue_access_step_up_grant_v1_impl(
  p_request_payload jsonb
)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  action_name text;
  draft_payload jsonb;
  change_hash text;
  result_row record;
  issued_time timestamptz;
begin
  perform private.assert_exact_json_keys(p_request_payload, array[
    'schema_version', 'bound_action', 'access_change_payload'
  ]);
  if jsonb_typeof(p_request_payload->'schema_version') <> 'number'
     or coalesce((p_request_payload->>'schema_version')::integer, -1) <> 1 then
    raise exception 'access_step_up_request_schema_invalid' using errcode = '22023';
  end if;
  action_name := p_request_payload->>'bound_action';
  draft_payload := p_request_payload->'access_change_payload';
  perform private.validate_access_change_draft_v1(action_name, draft_payload);
  perform private.require_human_roles(array['platform_admin'], true);
  perform private.require_recent_aal2();
  change_hash := private.compute_command_sha256(
    'access_v1:' || action_name, draft_payload
  );
  select * into result_row from private.issue_step_up_grant(change_hash);
  update private.step_up_grants
  set bound_action = action_name,
      bound_command_type = 'access_change'
  where id = result_row.grant_id
  returning issued_at into issued_time;
  return jsonb_build_object(
    'schema_version', 1,
    'step_up_grant_id', result_row.grant_id,
    'change_hash', change_hash,
    'step_up_grant_issued_at', issued_time,
    'step_up_grant_expires_at', result_row.expires_at,
    'step_up_grant_one_time', true,
    'step_up_grant_consumed_at', null,
    'bound_action', action_name
  );
exception
  when invalid_text_representation or numeric_value_out_of_range then
    raise exception 'access_step_up_request_invalid' using errcode = '22023';
end;
$$;

create or replace function private.consume_access_step_up_grant_v1(
  p_payload jsonb,
  p_bound_action text,
  p_consumed_for text
)
returns uuid
language plpgsql
security definer
set search_path = ''
as $$
declare
  actor uuid := (select auth.uid());
  draft_payload jsonb;
  expected_hash text;
  grant_id uuid;
begin
  draft_payload := private.access_change_draft_v1(p_payload);
  expected_hash := private.compute_command_sha256(
    'access_v1:' || p_bound_action, draft_payload
  );
  grant_id := (p_payload->>'step_up_grant_id')::uuid;
  if p_payload->>'change_hash' is distinct from expected_hash
     or p_payload->>'bound_action' is distinct from p_bound_action
     or jsonb_typeof(p_payload->'step_up_grant_one_time') <> 'boolean'
     or (p_payload->'step_up_grant_one_time')::boolean is not true
     or jsonb_typeof(p_payload->'step_up_grant_consumed_at') <> 'null' then
    raise exception 'access_step_up_binding_invalid' using errcode = '42501';
  end if;
  update private.step_up_grants
  set consumed_at = clock_timestamp(), consumed_for = p_consumed_for
  where id = grant_id
    and user_id = actor
    and command_sha256 = expected_hash
    and bound_action = p_bound_action
    and bound_command_type = 'access_change'
    and issued_at = (p_payload->>'step_up_grant_issued_at')::timestamptz
    and expires_at = (p_payload->>'step_up_grant_expires_at')::timestamptz
    and expires_at > clock_timestamp()
    and session_binding_sha256 = private.current_session_binding_sha256()
    and consumed_at is null;
  if not found then
    raise exception 'access_step_up_grant_invalid_expired_or_consumed'
      using errcode = '42501';
  end if;
  return actor;
exception
  when invalid_text_representation or numeric_value_out_of_range then
    raise exception 'access_step_up_binding_invalid' using errcode = '42501';
end;
$$;

create or replace function private.access_change_receipt_v1(p_request_id uuid)
returns jsonb
language plpgsql
stable
security definer
set search_path = ''
as $$
declare
  request_row private.access_change_requests%rowtype;
begin
  select * into request_row
  from private.access_change_requests
  where id = p_request_id;
  if not found then
    raise exception 'access_change_request_not_found' using errcode = 'P0002';
  end if;
  return jsonb_build_object(
    'schema_version', 1,
    'request_id', request_row.id,
    'subject_user_id', request_row.subject_user_id,
    'requested_role', request_row.requested_role,
    'change_type', request_row.change_type,
    'state', case request_row.state
      when 'approved' then 'applied'
      else request_row.state
    end,
    'requested_by', private.actor_ref_v1(request_row.requester_user_id),
    'reviewed_by', private.actor_ref_v1(request_row.reviewer_user_id),
    'requested_at', request_row.requested_at,
    'reviewed_at', request_row.reviewed_at,
    'applied_at', request_row.applied_at,
    'expires_at', request_row.expires_at,
    'reason_code', request_row.reason_code
  );
end;
$$;

create or replace function private.request_access_change_v1_impl(
  p_request_payload jsonb
)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  actor uuid;
  draft_payload jsonb;
  request_id_value uuid;
  subject_id uuid;
  evidence_id_value uuid;
  role_value text;
  change_value text;
  reason_value text;
  requested_time timestamptz;
  expiry_time timestamptz;
  change_hash text;
begin
  perform private.assert_exact_json_keys(p_request_payload, array[
    'schema_version', 'request_id', 'subject_user_id', 'requested_role',
    'change_type', 'evidence_id', 'reason_code', 'requested_at', 'expires_at',
    'step_up_grant_id', 'change_hash', 'step_up_grant_issued_at',
    'step_up_grant_expires_at', 'step_up_grant_one_time',
    'step_up_grant_consumed_at', 'bound_action'
  ]);
  if jsonb_typeof(p_request_payload->'schema_version') <> 'number'
     or jsonb_typeof(p_request_payload->'step_up_grant_one_time') <> 'boolean'
     or jsonb_typeof(p_request_payload->'step_up_grant_consumed_at') <> 'null' then
    raise exception 'access_change_request_json_type_invalid' using errcode = '22023';
  end if;
  draft_payload := private.access_change_draft_v1(p_request_payload);
  perform private.validate_access_change_draft_v1('request', draft_payload);
  actor := private.require_human_roles(array['platform_admin'], true);
  perform private.require_recent_aal2();
  request_id_value := (p_request_payload->>'request_id')::uuid;
  subject_id := (p_request_payload->>'subject_user_id')::uuid;
  evidence_id_value := (p_request_payload->>'evidence_id')::uuid;
  role_value := p_request_payload->>'requested_role';
  change_value := p_request_payload->>'change_type';
  reason_value := p_request_payload->>'reason_code';
  requested_time := (p_request_payload->>'requested_at')::timestamptz;
  expiry_time := (p_request_payload->>'expires_at')::timestamptz;
  if actor = subject_id
     or requested_time < clock_timestamp() - interval '5 minutes'
     or requested_time > clock_timestamp() + interval '30 seconds'
     or expiry_time <= requested_time
     or expiry_time > requested_time + interval '24 hours'
     or (change_value = 'revoke' and reason_value <> 'access_removal')
     or (change_value = 'grant' and reason_value = 'access_removal') then
    raise exception 'access_change_request_values_invalid' using errcode = '22023';
  end if;
  if not exists (select 1 from auth.users where id = subject_id) then
    raise exception 'access_change_subject_not_found' using errcode = 'P0002';
  end if;
  if not exists (
    select 1 from private.control_evidence where id = evidence_id_value
  ) then
    raise exception 'access_change_evidence_not_found' using errcode = 'P0002';
  end if;
  if (change_value = 'grant' and private.has_active_role(subject_id, role_value))
     or (change_value = 'revoke' and not private.has_active_role(subject_id, role_value)) then
    raise exception 'access_change_requested_state_not_satisfied' using errcode = '40001';
  end if;
  change_hash := private.compute_command_sha256('access_v1:request', draft_payload);
  if p_request_payload->>'change_hash' is distinct from change_hash then
    raise exception 'access_change_request_hash_mismatch' using errcode = '42501';
  end if;
  perform private.consume_access_step_up_grant_v1(
    p_request_payload, 'request', 'request_access_change_v1'
  );

  insert into private.access_change_requests (
    id, subject_user_id, requested_role, change_type, state,
    requester_user_id, evidence_id, request_step_up_grant_id,
    requested_at, expires_at, reason_code
  ) values (
    request_id_value, subject_id, role_value, change_value, 'requested',
    actor, evidence_id_value, (p_request_payload->>'step_up_grant_id')::uuid,
    requested_time, expiry_time, reason_value
  );
  perform private.write_audit_event(
    'human', actor, 'platform_admin', null, null, null,
    'access_change_requested', 'access_change', request_id_value::text,
    request_id_value, null, reason_value, null,
    array['state', 'subject_user_id', 'requested_role', 'change_type'],
    null, change_hash, evidence_id_value
  );
  insert into private.delivery_outbox (
    event_type, aggregate_type, aggregate_id, dedupe_key, payload,
    destination_type, available_at
  ) values (
    'access_change_requested', 'access_change', request_id_value::text,
    'access-change-requested:' || request_id_value::text,
    jsonb_build_object(
      'request_id', request_id_value,
      'requested_role', role_value,
      'change_type', change_value
    ),
    'operations_metric', requested_time
  );
  return private.access_change_receipt_v1(request_id_value);
exception
  when invalid_text_representation or numeric_value_out_of_range then
    raise exception 'access_change_request_values_invalid' using errcode = '22023';
end;
$$;

create or replace function private.review_access_change_v1_impl(
  p_review_payload jsonb
)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  actor uuid;
  draft_payload jsonb;
  review_id_value uuid;
  request_id_value uuid;
  decision_value text;
  reason_value text;
  reviewed_time timestamptz;
  change_hash text;
  request_row private.access_change_requests%rowtype;
begin
  perform private.assert_exact_json_keys(p_review_payload, array[
    'schema_version', 'review_id', 'request_id', 'decision', 'reason_code',
    'expected_state', 'reviewed_at', 'step_up_grant_id', 'change_hash',
    'step_up_grant_issued_at', 'step_up_grant_expires_at',
    'step_up_grant_one_time', 'step_up_grant_consumed_at', 'bound_action'
  ]);
  if jsonb_typeof(p_review_payload->'schema_version') <> 'number'
     or jsonb_typeof(p_review_payload->'step_up_grant_one_time') <> 'boolean'
     or jsonb_typeof(p_review_payload->'step_up_grant_consumed_at') <> 'null' then
    raise exception 'access_change_review_json_type_invalid' using errcode = '22023';
  end if;
  draft_payload := private.access_change_draft_v1(p_review_payload);
  perform private.validate_access_change_draft_v1('review', draft_payload);
  actor := private.require_human_roles(array['platform_admin'], true);
  perform private.require_recent_aal2();
  review_id_value := (p_review_payload->>'review_id')::uuid;
  request_id_value := (p_review_payload->>'request_id')::uuid;
  decision_value := p_review_payload->>'decision';
  reason_value := p_review_payload->>'reason_code';
  reviewed_time := (p_review_payload->>'reviewed_at')::timestamptz;
  if reviewed_time < clock_timestamp() - interval '5 minutes'
     or reviewed_time > clock_timestamp() + interval '30 seconds'
     or (decision_value = 'approve' and reason_value <> 'policy_satisfied')
     or (decision_value = 'reject' and reason_value = 'policy_satisfied') then
    raise exception 'access_change_review_values_invalid' using errcode = '22023';
  end if;
  change_hash := private.compute_command_sha256('access_v1:review', draft_payload);
  if p_review_payload->>'change_hash' is distinct from change_hash then
    raise exception 'access_change_review_hash_mismatch' using errcode = '42501';
  end if;
  perform private.consume_access_step_up_grant_v1(
    p_review_payload, 'review', 'review_access_change_v1'
  );

  select * into request_row
  from private.access_change_requests
  where id = request_id_value
  for update;
  if not found then
    raise exception 'access_change_request_not_found' using errcode = 'P0002';
  end if;
  if request_row.state <> 'requested'
     or request_row.expires_at <= reviewed_time then
    raise exception 'access_change_request_not_reviewable_or_expired'
      using errcode = '40001';
  end if;
  if actor in (request_row.requester_user_id, request_row.subject_user_id) then
    raise exception 'access_change_separation_of_duties_required'
      using errcode = '42501';
  end if;

  if decision_value = 'approve' then
    if request_row.change_type = 'grant' then
      if private.has_active_role(request_row.subject_user_id, request_row.requested_role) then
        raise exception 'access_change_target_state_changed' using errcode = '40001';
      end if;
      insert into private.role_assignments (
        user_id, role, valid_from, granted_by, approved_by, reason, ticket_ref
      ) values (
        request_row.subject_user_id, request_row.requested_role, reviewed_time,
        request_row.requester_user_id, actor,
        'approved_access_change', request_id_value::text
      );
    else
      update private.role_assignments
      set revoked_at = reviewed_time,
          revoked_by = actor,
          reason = 'approved_access_removal',
          ticket_ref = request_id_value::text
      where user_id = request_row.subject_user_id
        and role = request_row.requested_role
        and revoked_at is null
        and valid_from <= reviewed_time
        and (valid_until is null or valid_until > reviewed_time);
      if not found then
        raise exception 'access_change_target_state_changed' using errcode = '40001';
      end if;
    end if;
    update private.access_change_requests
    set state = 'applied', reviewer_user_id = actor,
        review_step_up_grant_id = (p_review_payload->>'step_up_grant_id')::uuid,
        reviewed_at = reviewed_time, applied_at = reviewed_time,
        reason_code = reason_value
    where id = request_id_value;
  else
    update private.access_change_requests
    set state = 'rejected', reviewer_user_id = actor,
        review_step_up_grant_id = (p_review_payload->>'step_up_grant_id')::uuid,
        reviewed_at = reviewed_time, reason_code = reason_value
    where id = request_id_value;
  end if;

  perform private.write_audit_event(
    'human', actor, 'platform_admin', null, null, null,
    case when decision_value = 'approve'
      then 'access_change_applied' else 'access_change_rejected' end,
    'access_change', request_id_value::text, review_id_value, null,
    reason_value, null,
    array['state', 'reviewer_user_id', 'reviewed_at', 'applied_at'],
    null, change_hash, request_row.evidence_id
  );
  insert into private.delivery_outbox (
    event_type, aggregate_type, aggregate_id, dedupe_key, payload,
    destination_type, available_at
  ) values (
    case when decision_value = 'approve'
      then 'access_change_applied' else 'access_change_rejected' end,
    'access_change', request_id_value::text,
    'access-change-reviewed:' || review_id_value::text,
    jsonb_build_object(
      'request_id', request_id_value,
      'review_id', review_id_value,
      'decision', decision_value,
      'change_type', request_row.change_type,
      'requested_role', request_row.requested_role
    ),
    'operations_metric', reviewed_time
  );
  return private.access_change_receipt_v1(request_id_value);
exception
  when invalid_text_representation or numeric_value_out_of_range then
    raise exception 'access_change_review_values_invalid' using errcode = '22023';
end;
$$;

-- Account opening deliberately has a separate contract from trading-control
-- commands so it cannot accidentally enter the desktop command union.

create or replace function private.validate_account_opening_draft_v1(
  p_bound_action text,
  p_payload jsonb
)
returns void
language plpgsql
immutable
security definer
set search_path = ''
as $$
begin
  if p_bound_action = 'request' then
    perform private.assert_exact_json_keys(p_payload, array[
      'schema_version', 'request_id', 'environment', 'account_id',
      'opening_capital_krw', 'idempotency_key', 'requested_at', 'expires_at',
      'command_type', 'evidence_id', 'reason_code'
    ]);
    if jsonb_typeof(p_payload->'schema_version') <> 'number'
       or jsonb_typeof(p_payload->'opening_capital_krw') <> 'number'
       or coalesce((p_payload->>'schema_version')::integer, -1) <> 1
       or p_payload->>'command_type' <> 'account_opening'
       or p_payload->>'environment' not in ('paper', 'contract_test')
       or p_payload->>'reason_code' <> 'approved_account_opening' then
      raise exception 'account_opening_request_draft_invalid' using errcode = '22023';
    end if;
  elsif p_bound_action = 'review' then
    perform private.assert_exact_json_keys(p_payload, array[
      'schema_version', 'review_id', 'command_id', 'command_type',
      'reviewer_role', 'decision', 'reason_code',
      'expected_receipt_revision', 'reviewed_at'
    ]);
    if jsonb_typeof(p_payload->'schema_version') <> 'number'
       or jsonb_typeof(p_payload->'expected_receipt_revision') <> 'number'
       or coalesce((p_payload->>'schema_version')::integer, -1) <> 1
       or p_payload->>'command_type' <> 'account_opening'
       or p_payload->>'reviewer_role' <> 'risk_approver'
       or p_payload->>'decision' not in ('approve', 'reject')
       or p_payload->>'reason_code' not in (
         'policy_satisfied', 'evidence_incomplete', 'separation_of_duties'
       ) then
      raise exception 'account_opening_review_draft_invalid' using errcode = '22023';
    end if;
  else
    raise exception 'account_opening_bound_action_invalid' using errcode = '22023';
  end if;
exception
  when invalid_text_representation or numeric_value_out_of_range then
    raise exception 'account_opening_draft_invalid' using errcode = '22023';
end;
$$;

create or replace function private.issue_account_opening_step_up_grant_v1_impl(
  p_request_payload jsonb
)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  action_name text;
  draft_payload jsonb;
  command_hash text;
  result_row record;
  issued_time timestamptz;
begin
  perform private.assert_exact_json_keys(p_request_payload, array[
    'schema_version', 'bound_action', 'bound_command_type', 'command_payload'
  ]);
  if jsonb_typeof(p_request_payload->'schema_version') <> 'number'
     or coalesce((p_request_payload->>'schema_version')::integer, -1) <> 1
     or p_request_payload->>'bound_command_type' <> 'account_opening' then
    raise exception 'account_opening_step_up_request_invalid' using errcode = '22023';
  end if;
  action_name := p_request_payload->>'bound_action';
  draft_payload := p_request_payload->'command_payload';
  perform private.validate_account_opening_draft_v1(action_name, draft_payload);
  command_hash := private.compute_command_sha256(
    'operation_v1:' || action_name, draft_payload
  );
  select * into result_row from private.issue_step_up_grant(command_hash);
  update private.step_up_grants
  set bound_action = action_name,
      bound_command_type = 'account_opening'
  where id = result_row.grant_id
  returning issued_at into issued_time;
  return jsonb_build_object(
    'schema_version', 1,
    'step_up_grant_id', result_row.grant_id,
    'command_hash', command_hash,
    'step_up_grant_issued_at', issued_time,
    'step_up_grant_expires_at', result_row.expires_at,
    'step_up_grant_one_time', true,
    'step_up_grant_consumed_at', null,
    'bound_action', action_name,
    'bound_command_type', 'account_opening'
  );
exception
  when invalid_text_representation or numeric_value_out_of_range then
    raise exception 'account_opening_step_up_request_invalid' using errcode = '22023';
end;
$$;

create or replace function private.account_opening_receipt_v1(p_command_id uuid)
returns jsonb
language plpgsql
stable
security definer
set search_path = ''
as $$
declare
  command_row private.operation_commands%rowtype;
begin
  select * into command_row from private.operation_commands where id = p_command_id;
  if not found or command_row.command_type <> 'account_opening' then
    raise exception 'account_opening_command_not_found' using errcode = 'P0002';
  end if;
  return jsonb_build_object(
    'schema_version', 1,
    'command_id', command_row.id,
    'command_type', 'account_opening',
    'environment', command_row.requested_change->>'environment',
    'account_id', command_row.requested_change->>'account_id',
    'opening_capital_krw', (command_row.requested_change->>'opening_capital_krw')::bigint,
    'state', command_row.state,
    'requested_by', private.actor_ref_v1(command_row.requester_user_id),
    'reviewed_by', private.actor_ref_v1(command_row.reviewer_user_id),
    'requested_at', command_row.requested_at,
    'reviewed_at', command_row.reviewed_at,
    'claimed_at', command_row.claimed_at,
    'claim_expires_at', command_row.claim_expires_at,
    'applied_at', command_row.applied_at,
    'expires_at', command_row.expires_at,
    'evidence_id', command_row.evidence_id,
    'command_hash', command_row.command_sha256,
    'receipt_revision', command_row.revision,
    'failure_code', command_row.failure_code
  );
exception
  when invalid_text_representation or numeric_value_out_of_range then
    raise exception 'account_opening_receipt_invalid' using errcode = '23514';
end;
$$;

create or replace function private.request_account_opening_v1_impl(
  p_request_payload jsonb
)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  actor uuid;
  draft_payload jsonb;
  request_id_value uuid;
  account_key text;
  environment_value text;
  opening_amount bigint;
  evidence_id_value uuid;
  requested_time timestamptz;
  expiry_time timestamptz;
  command_hash text;
  existing_row private.operation_commands%rowtype;
begin
  perform private.assert_exact_json_keys(p_request_payload, array[
    'schema_version', 'request_id', 'environment', 'account_id',
    'opening_capital_krw', 'idempotency_key', 'requested_at', 'expires_at',
    'command_type', 'evidence_id', 'reason_code', 'step_up_grant_id',
    'command_hash', 'step_up_grant_issued_at', 'step_up_grant_expires_at',
    'step_up_grant_one_time', 'step_up_grant_consumed_at',
    'bound_action', 'bound_command_type'
  ]);
  if jsonb_typeof(p_request_payload->'schema_version') <> 'number'
     or jsonb_typeof(p_request_payload->'opening_capital_krw') <> 'number'
     or jsonb_typeof(p_request_payload->'step_up_grant_one_time') <> 'boolean'
     or jsonb_typeof(p_request_payload->'step_up_grant_consumed_at') <> 'null' then
    raise exception 'account_opening_request_json_type_invalid' using errcode = '22023';
  end if;
  draft_payload := private.command_draft_v1(p_request_payload);
  perform private.validate_account_opening_draft_v1('request', draft_payload);
  actor := private.require_human_roles(array['operator'], true);
  perform private.require_recent_aal2();
  if private.has_active_role(actor, 'platform_admin') then
    raise exception 'platform_admin_trading_action_forbidden' using errcode = '42501';
  end if;
  request_id_value := (p_request_payload->>'request_id')::uuid;
  account_key := p_request_payload->>'account_id';
  environment_value := p_request_payload->>'environment';
  opening_amount := (p_request_payload->>'opening_capital_krw')::bigint;
  evidence_id_value := (p_request_payload->>'evidence_id')::uuid;
  requested_time := (p_request_payload->>'requested_at')::timestamptz;
  expiry_time := (p_request_payload->>'expires_at')::timestamptz;
  if opening_amount <= 0
     or requested_time < clock_timestamp() - interval '5 minutes'
     or requested_time > clock_timestamp() + interval '30 seconds'
     or expiry_time <= requested_time
     or expiry_time > requested_time + interval '24 hours'
     or p_request_payload->>'idempotency_key'
       !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$' then
    raise exception 'account_opening_request_values_invalid' using errcode = '22023';
  end if;
  if not exists (
    select 1
    from private.trading_accounts as account
    where account.account_id = account_key
      and account.environment = environment_value
      and account.state = 'pending_open'
      and account.opening_journal_entry_id is null
      and account.opening_capital_krw = opening_amount
  ) then
    raise exception 'account_opening_precondition_not_satisfied' using errcode = '40001';
  end if;
  if not exists (
    select 1
    from private.control_evidence as evidence
    where evidence.id = evidence_id_value
      and evidence.evidence_type = 'account_opening'
      and evidence.environment = environment_value
      and evidence.verified_at <= requested_time
  ) then
    raise exception 'account_opening_evidence_required' using errcode = '23514';
  end if;
  command_hash := private.compute_command_sha256('operation_v1:request', draft_payload);
  if p_request_payload->>'command_hash' is distinct from command_hash then
    raise exception 'account_opening_request_hash_mismatch' using errcode = '42501';
  end if;
  perform private.consume_bound_step_up_v1(
    p_request_payload, 'request', 'account_opening', requested_time,
    'request_account_opening_v1'
  );
  insert into private.operation_commands (
    id, command_type, state, requested_change, command_sha256, revision,
    evidence_id, requester_user_id, requested_at, expires_at, idempotency_key
  ) values (
    request_id_value, 'account_opening', 'requested', draft_payload,
    command_hash, 0, evidence_id_value, actor, requested_time, expiry_time,
    p_request_payload->>'idempotency_key'
  ) on conflict (idempotency_key) do nothing;
  if not found then
    select * into existing_row from private.operation_commands
    where idempotency_key = p_request_payload->>'idempotency_key';
    if existing_row.id <> request_id_value
       or existing_row.command_type <> 'account_opening'
       or existing_row.command_sha256 <> command_hash then
      raise exception 'account_opening_idempotency_conflict' using errcode = '23505';
    end if;
    return private.account_opening_receipt_v1(existing_row.id);
  end if;
  insert into private.operation_command_events (
    command_id, event_type, actor_type, actor_user_id,
    event_summary, occurred_at
  ) values (
    request_id_value, 'requested', 'human', actor,
    jsonb_build_object('command_hash', command_hash), requested_time
  );
  perform private.write_audit_event(
    'human', actor, 'operator', null, null, null,
    'account_opening_requested', 'operation_command', request_id_value::text,
    request_id_value, request_id_value, 'approved_account_opening', null,
    array['state', 'command_sha256'], null, command_hash, evidence_id_value
  );
  return private.account_opening_receipt_v1(request_id_value);
exception
  when invalid_text_representation or numeric_value_out_of_range then
    raise exception 'account_opening_request_values_invalid' using errcode = '22023';
end;
$$;

create or replace function private.review_account_opening_v1_impl(
  p_review_payload jsonb
)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  actor uuid;
  draft_payload jsonb;
  review_id_value uuid;
  command_id_value uuid;
  expected_revision bigint;
  reviewed_time timestamptz;
  decision_value text;
  reason_value text;
  command_hash text;
  command_row private.operation_commands%rowtype;
  next_state text;
begin
  perform private.assert_exact_json_keys(p_review_payload, array[
    'schema_version', 'review_id', 'command_id', 'command_type',
    'reviewer_role', 'decision', 'reason_code', 'expected_receipt_revision',
    'reviewed_at', 'step_up_grant_id', 'command_hash',
    'step_up_grant_issued_at', 'step_up_grant_expires_at',
    'step_up_grant_one_time', 'step_up_grant_consumed_at',
    'bound_action', 'bound_command_type'
  ]);
  if jsonb_typeof(p_review_payload->'schema_version') <> 'number'
     or jsonb_typeof(p_review_payload->'expected_receipt_revision') <> 'number'
     or jsonb_typeof(p_review_payload->'step_up_grant_one_time') <> 'boolean'
     or jsonb_typeof(p_review_payload->'step_up_grant_consumed_at') <> 'null' then
    raise exception 'account_opening_review_json_type_invalid' using errcode = '22023';
  end if;
  draft_payload := private.command_draft_v1(p_review_payload);
  perform private.validate_account_opening_draft_v1('review', draft_payload);
  actor := private.require_human_roles(array['risk_approver'], true);
  perform private.require_recent_aal2();
  if private.has_active_role(actor, 'platform_admin') then
    raise exception 'platform_admin_trading_action_forbidden' using errcode = '42501';
  end if;
  review_id_value := (p_review_payload->>'review_id')::uuid;
  command_id_value := (p_review_payload->>'command_id')::uuid;
  expected_revision := (p_review_payload->>'expected_receipt_revision')::bigint;
  reviewed_time := (p_review_payload->>'reviewed_at')::timestamptz;
  decision_value := p_review_payload->>'decision';
  reason_value := p_review_payload->>'reason_code';
  if expected_revision < 0
     or reviewed_time < clock_timestamp() - interval '5 minutes'
     or reviewed_time > clock_timestamp() + interval '30 seconds'
     or (decision_value = 'approve' and reason_value <> 'policy_satisfied')
     or (decision_value = 'reject' and reason_value = 'policy_satisfied') then
    raise exception 'account_opening_review_values_invalid' using errcode = '22023';
  end if;
  command_hash := private.compute_command_sha256('operation_v1:review', draft_payload);
  if p_review_payload->>'command_hash' is distinct from command_hash then
    raise exception 'account_opening_review_hash_mismatch' using errcode = '42501';
  end if;
  perform private.consume_bound_step_up_v1(
    p_review_payload, 'review', 'account_opening', reviewed_time,
    'review_account_opening_v1'
  );
  select * into command_row
  from private.operation_commands
  where id = command_id_value
  for update;
  if not found
     or command_row.command_type <> 'account_opening'
     or command_row.state <> 'requested'
     or command_row.revision <> expected_revision
     or command_row.expires_at <= reviewed_time then
    raise exception 'account_opening_not_reviewable_or_stale' using errcode = '40001';
  end if;
  if command_row.requester_user_id = actor then
    raise exception 'account_opening_self_review_forbidden' using errcode = '42501';
  end if;
  next_state := case when decision_value = 'approve' then 'approved' else 'rejected' end;
  update private.operation_commands
  set state = next_state, reviewer_user_id = actor, reviewed_at = reviewed_time,
      review_reason = reason_value, revision = revision + 1
  where id = command_id_value;
  insert into private.operation_command_reviews (
    id, command_id, reviewer_user_id, reviewer_role, decision, reason_code,
    step_up_grant_id, reviewed_at
  ) values (
    review_id_value, command_id_value, actor, 'risk_approver',
    case when decision_value = 'approve' then 'approved' else 'rejected' end,
    reason_value, (p_review_payload->>'step_up_grant_id')::uuid, reviewed_time
  );
  insert into private.operation_command_events (
    command_id, event_type, actor_type, actor_user_id,
    event_summary, occurred_at
  ) values (
    command_id_value, next_state, 'human', actor,
    jsonb_build_object('review_id', review_id_value, 'reason_code', reason_value),
    reviewed_time
  );
  perform private.write_audit_event(
    'human', actor, 'risk_approver', null, null, null,
    'account_opening_' || next_state, 'operation_command', command_id_value::text,
    review_id_value, command_id_value, reason_value, null,
    array['state', 'reviewer_user_id', 'revision'],
    null, command_hash, command_row.evidence_id
  );
  return private.account_opening_receipt_v1(command_id_value);
exception
  when invalid_text_representation or numeric_value_out_of_range then
    raise exception 'account_opening_review_values_invalid' using errcode = '22023';
end;
$$;

create or replace function private.request_unknown_resolution_v1_impl(
  p_request_payload jsonb
)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  actor uuid;
  draft_payload jsonb;
  request_id_value uuid;
  break_id_value uuid;
  expected_break_revision bigint;
  requested_time timestamptz;
  expires_time timestamptz;
  command_hash text;
  account_key text;
  environment_value text;
  intent_release text;
  existing_command private.operation_commands%rowtype;
  break_row private.reconciliation_breaks%rowtype;
begin
  perform private.assert_exact_json_keys(p_request_payload, array[
    'schema_version', 'request_id', 'environment', 'idempotency_key',
    'command_type', 'break_id', 'evidence_sha256', 'reason_code',
    'expected_break_state', 'expected_break_revision',
    'requested_at', 'expires_at',
    'step_up_grant_id', 'command_hash', 'step_up_grant_issued_at',
    'step_up_grant_expires_at', 'step_up_grant_one_time',
    'step_up_grant_consumed_at', 'bound_action', 'bound_command_type'
  ]);
  draft_payload := private.command_draft_v1(p_request_payload);
  perform private.validate_command_draft_v1(
    'request', 'resolve_unknown_execution', draft_payload
  );
  actor := private.require_human_roles(array['operator'], true);
  perform private.require_recent_aal2();
  if private.has_active_role(actor, 'platform_admin') then
    raise exception 'platform_admin_trading_action_forbidden' using errcode = '42501';
  end if;
  request_id_value := (p_request_payload->>'request_id')::uuid;
  break_id_value := (p_request_payload->>'break_id')::uuid;
  expected_break_revision := (p_request_payload->>'expected_break_revision')::bigint;
  requested_time := (p_request_payload->>'requested_at')::timestamptz;
  expires_time := (p_request_payload->>'expires_at')::timestamptz;
  if p_request_payload->>'command_type' <> 'resolve_unknown_execution'
     or p_request_payload->>'expected_break_state' <> 'open'
     or expected_break_revision < 0
     or p_request_payload->>'evidence_sha256' !~ '^[0-9a-f]{64}$'
     or p_request_payload->>'reason_code' <> 'evidence_review_requested'
     or requested_time < clock_timestamp() - interval '5 minutes'
     or requested_time > clock_timestamp() + interval '30 seconds'
     or expires_time <= requested_time
     or expires_time > requested_time + interval '24 hours'
     or p_request_payload->>'idempotency_key'
       !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$' then
    raise exception 'unknown_resolution_request_values_invalid' using errcode = '22023';
  end if;
  command_hash := private.compute_command_sha256(
    'operation_v1:request', draft_payload
  );
  if p_request_payload->>'command_hash' is distinct from command_hash then
    raise exception 'unknown_resolution_request_hash_mismatch' using errcode = '42501';
  end if;
  perform private.consume_bound_step_up_v1(
    p_request_payload, 'request', 'resolve_unknown_execution',
    requested_time, 'request_unknown_resolution_v1'
  );
  select * into existing_command
  from private.operation_commands as command
  where command.idempotency_key = p_request_payload->>'idempotency_key';
  if found then
    if existing_command.id <> request_id_value
       or existing_command.command_type <> 'unknown_resolution'
       or existing_command.command_sha256 <> command_hash then
      raise exception 'unknown_resolution_idempotency_conflict' using errcode = '23505';
    end if;
    return jsonb_build_object(
      'schema_version', 1, 'command_id', existing_command.id,
      'break_id', break_id_value, 'state', existing_command.state,
      'receipt_revision', existing_command.revision,
      'accounting_mutation_allowed', false,
      'resolution_complete', false
    );
  end if;
  select break_value.* into break_row
  from private.reconciliation_breaks as break_value
  where break_value.id = break_id_value
  for update;
  if not found
     or break_row.break_type <> 'execution'
     or break_row.state <> 'open'
     or break_row.revision <> expected_break_revision
     or not exists (
       select 1
       from private.order_events as event
       join private.execution_observations as observation
         on observation.id = event.observation_id
       where event.event_summary->>'reconciliation_break_id' = break_id_value::text
         and observation.event_type = 'unknown_requires_manual_check'
     ) then
    raise exception 'unknown_resolution_break_not_open_or_stale'
      using errcode = '40001';
  end if;
  select account.environment into environment_value
  from private.trading_accounts as account
  where account.account_id = break_row.account_id;
  account_key := break_row.account_id;
  if environment_value is distinct from p_request_payload->>'environment' then
    raise exception 'unknown_resolution_environment_mismatch' using errcode = '40001';
  end if;
  select intent.release_sha into intent_release
  from private.order_events as event
  join private.order_intents as intent on intent.id = event.intent_id
  where event.event_summary->>'reconciliation_break_id' = break_id_value::text
  order by event.occurred_at desc
  limit 1;
  insert into private.operation_commands (
    id, command_type, state, requested_change, command_sha256, revision,
    target_release_sha, requester_user_id, requested_at, expires_at,
    idempotency_key
  ) values (
    request_id_value, 'unknown_resolution', 'requested',
    draft_payload || jsonb_build_object(
      'account_id', account_key,
      'accounting_mutation_allowed', false,
      'resolution_complete', false
    ),
    command_hash, 0, intent_release, actor, requested_time, expires_time,
    p_request_payload->>'idempotency_key'
  );
  update private.reconciliation_breaks
  set state = 'resolution_requested',
      resolution_command_id = request_id_value,
      revision = revision + 1
  where id = break_id_value and revision = expected_break_revision;
  if not found then
    raise exception 'unknown_resolution_break_not_open_or_stale'
      using errcode = '40001';
  end if;
  insert into private.operation_command_events (
    command_id, event_type, actor_type, actor_user_id,
    event_summary, occurred_at
  ) values (
    request_id_value, 'requested', 'human', actor,
    jsonb_build_object(
      'break_id', break_id_value,
      'evidence_sha256', p_request_payload->>'evidence_sha256',
      'accounting_mutation_allowed', false
    ),
    requested_time
  );
  perform private.write_audit_event(
    'human', actor, 'operator', null, null, intent_release,
    'unknown_resolution_requested', 'reconciliation_break',
    break_id_value::text, request_id_value, request_id_value,
    p_request_payload->>'reason_code', null,
    array['state', 'revision', 'resolution_command_id', 'evidence_sha256'],
    null, p_request_payload->>'evidence_sha256', null
  );
  return jsonb_build_object(
    'schema_version', 1, 'command_id', request_id_value,
    'break_id', break_id_value, 'state', 'requested',
    'receipt_revision', 0, 'break_revision', expected_break_revision + 1,
    'accounting_mutation_allowed', false,
    'resolution_complete', false
  );
exception
  when invalid_text_representation or numeric_value_out_of_range then
    raise exception 'unknown_resolution_request_values_invalid' using errcode = '22023';
end;
$$;

create or replace function private.review_unknown_resolution_v1_impl(
  p_review_payload jsonb
)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  actor uuid;
  draft_payload jsonb;
  review_id_value uuid;
  command_id_value uuid;
  expected_receipt_revision bigint;
  expected_break_revision bigint;
  reviewed_time timestamptz;
  command_hash text;
  decision_value text;
  next_state text;
  break_id_value uuid;
  command_row private.operation_commands%rowtype;
  break_row private.reconciliation_breaks%rowtype;
begin
  perform private.assert_exact_json_keys(p_review_payload, array[
    'schema_version', 'review_id', 'command_id', 'command_type',
    'reviewer_role', 'decision', 'reason_code',
    'expected_receipt_revision', 'expected_break_revision',
    'evidence_sha256', 'reviewed_at',
    'step_up_grant_id', 'command_hash', 'step_up_grant_issued_at',
    'step_up_grant_expires_at', 'step_up_grant_one_time',
    'step_up_grant_consumed_at', 'bound_action', 'bound_command_type'
  ]);
  draft_payload := private.command_draft_v1(p_review_payload);
  perform private.validate_command_draft_v1(
    'review', 'resolve_unknown_execution', draft_payload
  );
  actor := private.require_human_roles(array['risk_approver'], true);
  perform private.require_recent_aal2();
  if private.has_active_role(actor, 'platform_admin') then
    raise exception 'platform_admin_trading_action_forbidden' using errcode = '42501';
  end if;
  review_id_value := (p_review_payload->>'review_id')::uuid;
  command_id_value := (p_review_payload->>'command_id')::uuid;
  expected_receipt_revision := (
    p_review_payload->>'expected_receipt_revision'
  )::bigint;
  expected_break_revision := (p_review_payload->>'expected_break_revision')::bigint;
  reviewed_time := (p_review_payload->>'reviewed_at')::timestamptz;
  decision_value := p_review_payload->>'decision';
  if p_review_payload->>'command_type' <> 'resolve_unknown_execution'
     or p_review_payload->>'reviewer_role' <> 'risk_approver'
     or decision_value not in ('approve', 'reject')
     or p_review_payload->>'evidence_sha256' !~ '^[0-9a-f]{64}$'
     or (
       decision_value = 'approve'
       and p_review_payload->>'reason_code' <> 'evidence_sufficient'
     )
     or (
       decision_value = 'reject'
       and p_review_payload->>'reason_code' not in (
         'evidence_incomplete', 'accounting_adjustment_required'
       )
     )
     or expected_receipt_revision < 0
     or expected_break_revision < 0
     or reviewed_time < clock_timestamp() - interval '5 minutes'
     or reviewed_time > clock_timestamp() + interval '30 seconds' then
    raise exception 'unknown_resolution_review_values_invalid' using errcode = '22023';
  end if;
  command_hash := private.compute_command_sha256(
    'operation_v1:review', draft_payload
  );
  if p_review_payload->>'command_hash' is distinct from command_hash then
    raise exception 'unknown_resolution_review_hash_mismatch' using errcode = '42501';
  end if;
  perform private.consume_bound_step_up_v1(
    p_review_payload, 'review', 'resolve_unknown_execution',
    reviewed_time, 'review_unknown_resolution_v1'
  );
  select * into command_row
  from private.operation_commands as command
  where command.id = command_id_value
  for update;
  if not found
     or command_row.command_type <> 'unknown_resolution'
     or command_row.state <> 'requested'
     or command_row.revision <> expected_receipt_revision
     or command_row.expires_at <= reviewed_time then
    raise exception 'unknown_resolution_command_not_reviewable_or_stale'
      using errcode = '40001';
  end if;
  if command_row.requester_user_id = actor then
    raise exception 'unknown_resolution_self_review_forbidden' using errcode = '42501';
  end if;
  break_id_value := (command_row.requested_change->>'break_id')::uuid;
  select * into break_row
  from private.reconciliation_breaks as break_value
  where break_value.id = break_id_value
  for update;
  if not found
     or break_row.state <> 'resolution_requested'
     or break_row.resolution_command_id <> command_id_value
     or break_row.revision <> expected_break_revision then
    raise exception 'unknown_resolution_break_not_reviewable_or_stale'
      using errcode = '40001';
  end if;
  next_state := case when decision_value = 'approve' then 'approved' else 'rejected' end;
  update private.operation_commands
  set state = next_state,
      reviewer_user_id = actor,
      reviewed_at = reviewed_time,
      review_reason = p_review_payload->>'reason_code',
      revision = revision + 1
  where id = command_id_value;
  insert into private.operation_command_reviews (
    id, command_id, reviewer_user_id, reviewer_role, decision, reason_code,
    step_up_grant_id, reviewed_at, evidence_sha256, request_digest_sha256
  ) values (
    review_id_value, command_id_value, actor, 'risk_approver',
    case when decision_value = 'approve' then 'approved' else 'rejected' end,
    p_review_payload->>'reason_code',
    (p_review_payload->>'step_up_grant_id')::uuid, reviewed_time,
    p_review_payload->>'evidence_sha256', command_row.command_sha256
  );
  update private.reconciliation_breaks
  set state = case
        when decision_value = 'approve' then 'resolution_requested'
        else 'open'
      end,
      resolution_command_id = case
        when decision_value = 'approve' then command_id_value
        else null
      end,
      revision = revision + 1
  where id = break_id_value and revision = expected_break_revision;
  if not found then
    raise exception 'unknown_resolution_break_not_reviewable_or_stale'
      using errcode = '40001';
  end if;
  insert into private.operation_command_events (
    command_id, event_type, actor_type, actor_user_id,
    event_summary, occurred_at
  ) values (
    command_id_value, next_state, 'human', actor,
    jsonb_build_object(
      'review_id', review_id_value,
      'break_id', break_id_value,
      'evidence_sha256', p_review_payload->>'evidence_sha256',
      'accounting_mutation_allowed', false,
      'resolution_complete', false
    ),
    reviewed_time
  );
  perform private.write_audit_event(
    'human', actor, 'risk_approver', null, null, command_row.target_release_sha,
    'unknown_resolution_reviewed', 'reconciliation_break',
    break_id_value::text, review_id_value, command_id_value,
    p_review_payload->>'reason_code', null,
    array['command_state', 'break_state', 'revision', 'evidence_sha256'],
    command_row.command_sha256, p_review_payload->>'evidence_sha256', null
  );
  return jsonb_build_object(
    'schema_version', 1, 'command_id', command_id_value,
    'review_id', review_id_value, 'break_id', break_id_value,
    'state', next_state,
    'break_state', case
      when decision_value = 'approve' then 'resolution_requested' else 'open' end,
    'receipt_revision', expected_receipt_revision + 1,
    'break_revision', expected_break_revision + 1,
    'accounting_mutation_allowed', false,
    'resolution_complete', false,
    'requires_balanced_accounting_adjustment', decision_value = 'approve'
  );
exception
  when invalid_text_representation or numeric_value_out_of_range then
    raise exception 'unknown_resolution_review_values_invalid' using errcode = '22023';
end;
$$;

create or replace function api.request_unknown_resolution_v1(
  request_payload jsonb
)
returns jsonb
language sql
security invoker
set search_path = ''
as $$ select private.request_unknown_resolution_v1_impl(request_payload); $$;

create or replace function api.review_unknown_resolution_v1(
  review_payload jsonb
)
returns jsonb
language sql
security invoker
set search_path = ''
as $$ select private.review_unknown_resolution_v1_impl(review_payload); $$;

create or replace function api.issue_access_step_up_grant_v1(
  request_payload jsonb
)
returns jsonb
language sql
security invoker
set search_path = ''
as $$ select private.issue_access_step_up_grant_v1_impl(request_payload); $$;

create or replace function api.request_access_change_v1(request_payload jsonb)
returns jsonb
language sql
security invoker
set search_path = ''
as $$ select private.request_access_change_v1_impl(request_payload); $$;

create or replace function api.review_access_change_v1(review_payload jsonb)
returns jsonb
language sql
security invoker
set search_path = ''
as $$ select private.review_access_change_v1_impl(review_payload); $$;

create or replace function api.issue_account_opening_step_up_grant_v1(
  request_payload jsonb
)
returns jsonb
language sql
security invoker
set search_path = ''
as $$
  select private.issue_account_opening_step_up_grant_v1_impl(request_payload);
$$;

create or replace function api.request_account_opening_v1(request_payload jsonb)
returns jsonb
language sql
security invoker
set search_path = ''
as $$ select private.request_account_opening_v1_impl(request_payload); $$;

create or replace function api.review_account_opening_v1(review_payload jsonb)
returns jsonb
language sql
security invoker
set search_path = ''
as $$ select private.review_account_opening_v1_impl(review_payload); $$;

revoke execute on all functions in schema private
from public, anon, authenticated, service_role;

grant execute on function
  private.get_my_access_profile(),
  private.get_platform_status(),
  private.list_operation_commands(integer),
  private.list_incidents(integer),
  private.list_audit_events(integer),
  private.get_accounting_summary(),
  private.get_desktop_operations_snapshot_v1_impl(),
  private.issue_step_up_grant_v1_impl(jsonb),
  private.request_operation_command_v1_impl(jsonb),
  private.review_operation_command_v1_impl(jsonb),
  private.act_on_operation_incident_v1_impl(jsonb),
  private.issue_access_step_up_grant_v1_impl(jsonb),
  private.request_access_change_v1_impl(jsonb),
  private.review_access_change_v1_impl(jsonb),
  private.issue_account_opening_step_up_grant_v1_impl(jsonb),
  private.request_account_opening_v1_impl(jsonb),
  private.review_account_opening_v1_impl(jsonb),
  private.request_unknown_resolution_v1_impl(jsonb),
  private.review_unknown_resolution_v1_impl(jsonb)
to authenticated;

grant execute on function
  private.acquire_worker_lease_impl(text, text, timestamptz, integer, text),
  private.renew_worker_lease_impl(
    text, text, bigint, timestamptz, integer, text
  ),
  private.reserve_order_intent_impl(
    uuid, text, text, text, text, uuid, text, uuid, boolean, text[],
    timestamptz, timestamptz, text, text, bigint, bigint,
    timestamptz, timestamptz, timestamptz, text, text, text, bigint,
    timestamptz, timestamptz, bigint, text, bigint, text
  ),
  private.mark_dispatch_started_impl(
    uuid, text, text, text, bigint, bigint, timestamptz, text, text
  ),
  private.record_execution_observation_impl(
    uuid, integer, text, text, text, text, timestamptz,
    bigint, bigint, bigint, bigint, bigint, bigint, date,
    jsonb, text, text, bigint
  ),
  private.claim_delivery_outbox_impl(text, timestamptz, integer, integer),
  private.complete_outbox_delivery_impl(uuid, text, timestamptz, text, text),
  private.fail_outbox_delivery_impl(uuid, text, timestamptz, text, integer),
  private.acknowledge_operation_command_impl(
    uuid, text, text, text, timestamptz, jsonb, text
  ),
  private.claim_execution_reconciliation_batch_impl(
    text, text, timestamptz, integer, integer, uuid, integer
  ),
  private.complete_execution_reconciliation_impl(
    uuid, text, timestamptz, text, timestamptz, text
  ),
  private.release_worker_lease_impl(text, text, bigint, timestamptz, text),
  private.record_worker_heartbeat_impl(text, text, jsonb, timestamptz, text),
  private.fail_reserved_intent_pre_dispatch_impl(
    uuid, text, bigint, bigint, text, timestamptz, text
  ),
  private.claim_operation_command_batch_impl(text, text, timestamptz, integer),
  private.load_paper_execution_checkpoint_impl(
    uuid, text, text, bigint, bigint, text, timestamptz
  ),
  private.expire_paper_intent_remainder_impl(
    uuid, text, bigint, bigint, text, timestamptz, text
  )
to service_role;

revoke execute on all functions in schema api
from public, anon, authenticated, service_role;
grant execute on function
  api.get_my_access_profile(),
  api.get_platform_status(),
  api.list_operation_commands(integer),
  api.list_incidents(integer),
  api.list_audit_events(integer),
  api.get_accounting_summary(),
  api.get_desktop_operations_snapshot_v1(),
  api.issue_step_up_grant_v1(jsonb),
  api.request_operation_command_v1(jsonb),
  api.review_operation_command_v1(jsonb),
  api.act_on_operation_incident_v1(jsonb),
  api.issue_access_step_up_grant_v1(jsonb),
  api.request_access_change_v1(jsonb),
  api.review_access_change_v1(jsonb),
  api.issue_account_opening_step_up_grant_v1(jsonb),
  api.request_account_opening_v1(jsonb),
  api.review_account_opening_v1(jsonb),
  api.request_unknown_resolution_v1(jsonb),
  api.review_unknown_resolution_v1(jsonb)
to authenticated;

revoke execute on all functions in schema worker_api
from public, anon, authenticated, service_role;
grant execute on all functions in schema worker_api to service_role;

grant execute on function
  api.issue_access_step_up_grant_v1(jsonb),
  api.request_access_change_v1(jsonb),
  api.review_access_change_v1(jsonb),
  api.issue_account_opening_step_up_grant_v1(jsonb),
  api.request_account_opening_v1(jsonb),
  api.review_account_opening_v1(jsonb)
to authenticated;

do $$
begin
  if (
    select count(*)
    from pg_proc as proc
    join pg_namespace as namespace on namespace.oid = proc.pronamespace
    where namespace.nspname = 'worker_api'
  ) <> 17 then
    raise exception 'worker_api_allowlist_must_contain_exactly_seventeen_functions';
  end if;
  if exists (
    select 1
    from pg_proc as proc
    join pg_namespace as namespace on namespace.oid = proc.pronamespace
    where namespace.nspname in ('api', 'worker_api') and proc.prosecdef
  ) then
    raise exception 'exposed_schema_security_definer_forbidden';
  end if;
  if exists (
    select 1
    from information_schema.role_table_grants
    where table_schema in ('private', 'public')
      and grantee in ('anon', 'authenticated', 'service_role')
  ) then
    raise exception 'direct_table_grants_for_runtime_roles_forbidden';
  end if;
  if (
    select array_agg(
      namespace.nspname || '.' || class.relname
      order by namespace.nspname, class.relname
    )
    from pg_publication_rel as publication_relation
    join pg_publication as publication
      on publication.oid = publication_relation.prpubid
    join pg_class as class on class.oid = publication_relation.prrelid
    join pg_namespace as namespace on namespace.oid = class.relnamespace
    where publication.pubname = 'supabase_realtime'
  ) is distinct from array['api.control_plane_signal']::text[] then
    raise exception 'realtime_publication_must_contain_only_safe_signal_projection';
  end if;
end $$;
