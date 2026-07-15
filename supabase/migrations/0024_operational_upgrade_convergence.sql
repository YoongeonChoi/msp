-- Operational upgrade convergence and attempt-level fencing.
--
-- This migration is intentionally append-only. It converges rows created by
-- pre-0024 workers, fences reconciliation and delivery attempts independently
-- of worker identity, and keeps every compatibility path fail closed.

alter table private.execution_reconciliation_state
  add column claim_release_sha text,
  add column claim_fencing_token bigint;

alter table private.delivery_outbox
  add column lease_token uuid;

alter table private.execution_reconciliation_state
  drop constraint execution_reconciliation_lease_shape_check;
alter table private.delivery_outbox
  drop constraint delivery_outbox_lease_shape_check;

-- A pre-0024 reconciliation lease has no attempt-level token. Requeue it
-- instead of guessing which worker invocation still owns the attempt.
update private.execution_reconciliation_state as state
set state = 'pending',
    next_reconcile_at = least(state.next_reconcile_at, clock_timestamp()),
    lease_owner = null,
    lease_expires_at = null,
    claim_release_sha = null,
    claim_fencing_token = null,
    last_reason_code = 'worker_upgrade_requeued_tokenless_claim',
    updated_at = greatest(
      clock_timestamp(), state.updated_at + interval '1 microsecond'
    )
where state.state = 'leased';

-- A delivery whose final attempt crashed must not become permanently leased.
-- The incident insert is guarded so a partially prepared environment cannot
-- emit more than one convergence incident for the same outbox row.
with convergence_time as (
  select clock_timestamp() as value
), dead_lettered as (
  update private.delivery_outbox as outbox
  set status = 'dead_letter',
      lease_owner = null,
      lease_expires_at = null,
      lease_token = null,
      last_error_code = 'delivery_attempt_lease_expired_at_max_attempts'
  from convergence_time
  where outbox.status = 'leased'
    and outbox.attempt_count >= outbox.max_attempts
  returning outbox.id
)
insert into private.incidents (
  severity, incident_type, summary_code, correlation_id, opened_at
)
select
  'high',
  'delivery_dead_letter',
  'delivery_attempt_lease_expired_at_max_attempts',
  dead_lettered.id,
  convergence_time.value
from dead_lettered
cross join convergence_time
where not exists (
  select 1
  from private.incidents as incident
  where incident.incident_type = 'delivery_dead_letter'
    and incident.correlation_id = dead_lettered.id
);

-- All other tokenless delivery leases are safely retried from the database
-- clock. Caller-clock retry timestamps outside the previous one-day contract
-- are normalized as well.
with convergence_time as (
  select clock_timestamp() as value
)
update private.delivery_outbox as outbox
set status = 'pending',
    available_at = convergence_time.value,
    lease_owner = null,
    lease_expires_at = null,
    lease_token = null,
    last_error_code = 'worker_upgrade_requeued_tokenless_lease'
from convergence_time
where outbox.status = 'leased';

with convergence_time as (
  select clock_timestamp() as value
)
update private.delivery_outbox as outbox
set available_at = convergence_time.value,
    last_error_code = 'legacy_caller_clock_normalized'
from convergence_time
where outbox.status = 'pending'
  and outbox.available_at > convergence_time.value + interval '1 day';

alter table private.execution_reconciliation_state
  add constraint execution_reconciliation_claim_release_sha_check check (
    claim_release_sha is null
    or claim_release_sha ~ '^[0-9a-f]{40}([0-9a-f]{24})?$'
  ),
  add constraint execution_reconciliation_claim_fencing_token_check check (
    claim_fencing_token is null or claim_fencing_token > 0
  ),
  add constraint execution_reconciliation_lease_shape_check check (
    (
      state = 'leased'
      and nullif(btrim(lease_owner), '') is not null
      and lease_expires_at is not null
      and claim_release_sha is not null
      and claim_fencing_token is not null
    )
    or (
      state = 'pending'
      and lease_owner is null
      and lease_expires_at is null
      and claim_release_sha is null
      and claim_fencing_token is null
    )
    or (
      state in ('complete', 'manual')
      and lease_owner is null
      and lease_expires_at is null
      and (
        (claim_release_sha is null and claim_fencing_token is null)
        or (claim_release_sha is not null and claim_fencing_token is not null)
      )
    )
  );

alter table private.delivery_outbox
  add constraint delivery_outbox_lease_shape_check check (
    (
      status = 'leased'
      and nullif(btrim(lease_owner), '') is not null
      and lease_expires_at is not null
      and lease_token is not null
    )
    or (
      status <> 'leased'
      and lease_owner is null
      and lease_expires_at is null
      and lease_token is null
    )
  );

create or replace function private.fence_execution_reconciliation_claim_v1()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
declare
  lease_row private.worker_leases%rowtype;
begin
  if new.state = 'leased' then
    if tg_op <> 'UPDATE'
       or new.attempt_count <= old.attempt_count then
      if tg_op = 'UPDATE'
         and old.state = 'leased'
         and new.attempt_count = old.attempt_count
         and old.claim_release_sha is not null
         and old.claim_fencing_token is not null then
        new.claim_release_sha := old.claim_release_sha;
        new.claim_fencing_token := old.claim_fencing_token;
        return new;
      end if;
      raise exception 'reconciliation_claim_attempt_token_required'
        using errcode = '23514';
    end if;

    select current_lease.* into lease_row
    from private.order_intents as intent
    join private.worker_leases as current_lease
      on current_lease.account_id = intent.account_id
    where intent.id = new.intent_id
      and current_lease.holder_id = new.lease_owner
      and current_lease.expires_at > clock_timestamp();

    if not found then
      raise exception 'reconciliation_claim_worker_lease_missing_or_expired'
        using errcode = '40001';
    end if;
    new.claim_release_sha := lease_row.release_sha;
    new.claim_fencing_token := lease_row.fencing_token;
  elsif new.state = 'pending' then
    new.claim_release_sha := null;
    new.claim_fencing_token := null;
  elsif tg_op = 'UPDATE' and old.state = 'leased' then
    new.claim_release_sha := old.claim_release_sha;
    new.claim_fencing_token := old.claim_fencing_token;
  else
    new.claim_release_sha := null;
    new.claim_fencing_token := null;
  end if;
  return new;
end;
$$;

drop trigger if exists fence_execution_reconciliation_claim_v1
  on private.execution_reconciliation_state;
create trigger fence_execution_reconciliation_claim_v1
  before insert or update on private.execution_reconciliation_state
  for each row execute function
    private.fence_execution_reconciliation_claim_v1();

-- Resume and promotion updates must remain blocked while any non-legacy
-- reconciliation break is unresolved, even when last_command_id is unchanged.
create or replace function private.guard_execution_control_qualification_freshness_v1()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
declare
  command_row private.operation_commands%rowtype;
  qualification_row private.qualifications%rowtype;
  authorization_time timestamptz := clock_timestamp();
begin
  if new.execution_enabled and exists (
    select 1
    from private.reconciliation_breaks as reconciliation_break
    where reconciliation_break.account_id = new.account_id
      and reconciliation_break.break_type <> 'legacy_unreconciled'
      and reconciliation_break.state <> 'resolved'
  ) then
    raise exception 'unresolved_reconciliation_break_blocks_execution_enable'
      using errcode = '40001';
  end if;

  if new.last_command_id is not distinct from old.last_command_id then
    return new;
  end if;

  select * into command_row
  from private.operation_commands
  where id = new.last_command_id;

  if not found or command_row.command_type not in (
    'paper_resume', 'contract_test_enable',
    'strategy_promotion', 'risk_policy_change'
  ) then
    return new;
  end if;

  select * into qualification_row
  from private.qualifications
  where id::text = command_row.requested_change->>'qualification_id'
    and status = 'qualified'
    and environment = new.environment
    and valid_from <= authorization_time
    and valid_until > authorization_time
    and g1_status = 'pass'
    and g2_status = 'pass'
    and release_sha = command_row.target_release_sha
    and release_sha = command_row.requested_change->>'release_sha'
    and ledger_checkpoint = command_row.requested_change->>'ledger_checkpoint'
    and strategy_version_id::text = command_row.requested_change->>'strategy_version_id'
    and risk_policy_version_id::text = command_row.requested_change->>'risk_policy_version_id'
    and strategy_version_id::text = new.active_strategy_version_id
    and risk_policy_version_id = new.active_risk_policy_version_id
    and execution_policy_version = new.execution_policy_version
    and execution_policy_sha256 = new.execution_policy_sha256
    and risk_policy_sha256 = new.risk_policy_sha256
    and provider_contract_version is not distinct from new.provider_contract_version
    and provider_openapi_sha256 is not distinct from new.provider_openapi_sha256;

  if not found then
    raise exception 'qualified_evidence_bundle_expired_or_mismatched_at_application'
      using errcode = '23514';
  end if;

  new.expires_at := least(new.expires_at, qualification_row.valid_until);
  if new.expires_at <= new.effective_at then
    raise exception 'qualified_execution_window_expired_at_application'
      using errcode = '23514';
  end if;
  return new;
end;
$$;

-- Revalidate controls that were enabled before the stricter application-time
-- guard existed. Valid controls are clamped to qualification expiry; every
-- invalid or unverifiable control is disabled.
with convergence_time as (
  select clock_timestamp() as value
), validation as (
  select
    control.account_id,
    qualification.valid_until as qualification_valid_until,
    (
      command.id is not null
      and command.state = 'applied'
      and command.command_type in (
        'paper_resume', 'contract_test_enable',
        'strategy_promotion', 'risk_policy_change'
      )
      and qualification.id is not null
      and qualification.status = 'qualified'
      and qualification.environment = control.environment
      and qualification.valid_from <= convergence_time.value
      and qualification.valid_until > convergence_time.value
      and qualification.g1_status = 'pass'
      and qualification.g2_status = 'pass'
      and qualification.release_sha = command.target_release_sha
      and qualification.release_sha = command.requested_change->>'release_sha'
      and qualification.ledger_checkpoint = command.requested_change->>'ledger_checkpoint'
      and qualification.strategy_version_id::text
        = command.requested_change->>'strategy_version_id'
      and qualification.risk_policy_version_id::text
        = command.requested_change->>'risk_policy_version_id'
      and qualification.strategy_version_id::text
        = control.active_strategy_version_id
      and qualification.risk_policy_version_id
        = control.active_risk_policy_version_id
      and qualification.execution_policy_version
        = control.execution_policy_version
      and qualification.execution_policy_sha256
        = control.execution_policy_sha256
      and qualification.risk_policy_sha256 = control.risk_policy_sha256
      and qualification.provider_contract_version
        is not distinct from control.provider_contract_version
      and qualification.provider_openapi_sha256
        is not distinct from control.provider_openapi_sha256
      and control.effective_at <= convergence_time.value
      and control.expires_at > convergence_time.value
    ) as is_valid,
    convergence_time.value as authorization_time
  from private.execution_controls as control
  cross join convergence_time
  left join private.operation_commands as command
    on command.id = control.last_command_id
  left join private.qualifications as qualification
    on qualification.id::text = command.requested_change->>'qualification_id'
  where control.execution_enabled
)
update private.execution_controls as control
set execution_enabled = validation.is_valid,
    control_epoch = control.control_epoch + 1,
    effective_at = case
      when validation.is_valid then control.effective_at
      else greatest(
        validation.authorization_time,
        control.updated_at + interval '1 microsecond'
      )
    end,
    expires_at = case
      when validation.is_valid then
        least(control.expires_at, validation.qualification_valid_until)
      else greatest(
        control.expires_at,
        greatest(
          validation.authorization_time,
          control.updated_at + interval '1 microsecond'
        ) + interval '1 second'
      )
    end,
    updated_reason_code = case
      when validation.is_valid
        then 'operational_upgrade_qualification_revalidated'
      else 'operational_upgrade_qualification_invalid'
    end,
    updated_at = greatest(
      validation.authorization_time,
      control.updated_at + interval '1 microsecond'
    )
from validation
where control.account_id = validation.account_id;

-- Backfill fail-closed behavior for breaks that existed before the 0023
-- insert trigger. Epoch always advances, including when already disabled, so
-- reservations authorized under the prior epoch are fenced.
with convergence_time as (
  select clock_timestamp() as value
)
update private.execution_controls as control
set execution_enabled = false,
    control_epoch = control.control_epoch + 1,
    effective_at = greatest(
      convergence_time.value,
      control.updated_at + interval '1 microsecond'
    ),
    expires_at = greatest(
      control.expires_at,
      greatest(
        convergence_time.value,
        control.updated_at + interval '1 microsecond'
      ) + interval '1 second'
    ),
    updated_reason_code = 'unresolved_reconciliation_break',
    updated_at = greatest(
      convergence_time.value,
      control.updated_at + interval '1 microsecond'
    )
from convergence_time
where exists (
  select 1
  from private.reconciliation_breaks as reconciliation_break
  where reconciliation_break.account_id = control.account_id
    and reconciliation_break.break_type <> 'legacy_unreconciled'
    and reconciliation_break.state <> 'resolved'
);

-- Return shape changes require dropping the SQL wrapper before its private
-- implementation. Completion/failure compatibility overloads remain present
-- but deliberately refuse tokenless calls.
drop function worker_api.claim_delivery_outbox(
  text, timestamptz, integer, integer
);
drop function private.claim_delivery_outbox_impl(
  text, timestamptz, integer, integer
);

create or replace function worker_api.complete_outbox_delivery(
  p_outbox_id uuid,
  p_worker_id text,
  p_now timestamptz,
  p_external_receipt_id text,
  p_external_receipt_sha256 text
)
returns table (outbox_id uuid, status text, delivered_at timestamptz)
language plpgsql
security invoker
set search_path = ''
as $$
begin
  raise exception 'worker_upgrade_required' using errcode = '0A000';
end;
$$;

create or replace function worker_api.fail_outbox_delivery(
  p_outbox_id uuid,
  p_worker_id text,
  p_now timestamptz,
  p_error_code text,
  p_retry_after_seconds integer
)
returns table (outbox_id uuid, status text, available_at timestamptz)
language plpgsql
security invoker
set search_path = ''
as $$
begin
  raise exception 'worker_upgrade_required' using errcode = '0A000';
end;
$$;

drop function private.complete_outbox_delivery_impl(
  uuid, text, timestamptz, text, text
);
drop function private.fail_outbox_delivery_impl(
  uuid, text, timestamptz, text, integer
);

create function private.claim_delivery_outbox_impl(
  p_worker_id text,
  p_now timestamptz,
  p_limit integer,
  p_lease_seconds integer
)
returns table (
  outbox_id uuid,
  event_type text,
  payload_version integer,
  aggregate_type text,
  aggregate_id text,
  dedupe_key text,
  payload jsonb,
  destination_type text,
  attempt_count integer,
  lease_token uuid,
  lease_expires_at timestamptz
)
language plpgsql
security definer
set search_path = ''
as $$
declare
  authorization_time timestamptz := clock_timestamp();
begin
  perform private.require_service_role();
  if nullif(btrim(p_worker_id), '') is null
     or p_now is null
     or p_limit is null
     or p_limit < 1 or p_limit > 100
     or p_lease_seconds is null
     or p_lease_seconds < 5 or p_lease_seconds > 300
     or p_now < authorization_time - interval '5 minutes'
     or p_now > authorization_time + interval '30 seconds' then
    raise exception 'outbox_claim_parameters_invalid' using errcode = '22023';
  end if;

  -- Crash after the final claim is itself a terminal delivery failure. Move
  -- it to dead letter before selecting retry candidates and surface it once.
  with expired_final as (
    update private.delivery_outbox as outbox
    set status = 'dead_letter',
        lease_owner = null,
        lease_expires_at = null,
        lease_token = null,
        last_error_code = 'delivery_attempt_lease_expired_at_max_attempts'
    where outbox.status = 'leased'
      and outbox.lease_expires_at <= authorization_time
      and outbox.attempt_count >= outbox.max_attempts
    returning outbox.id
  )
  insert into private.incidents (
    severity, incident_type, summary_code, correlation_id, opened_at
  )
  select
    'high',
    'delivery_dead_letter',
    'delivery_attempt_lease_expired_at_max_attempts',
    expired_final.id,
    authorization_time
  from expired_final
  where not exists (
    select 1
    from private.incidents as incident
    where incident.incident_type = 'delivery_dead_letter'
      and incident.correlation_id = expired_final.id
  );

  update private.delivery_outbox as stale_escalation
  set status = 'canceled',
      lease_owner = null,
      lease_expires_at = null,
      lease_token = null
  where stale_escalation.event_type = 'critical_incident_ack_escalation'
    and stale_escalation.status in ('pending', 'leased')
    and exists (
      select 1
      from private.incidents as incident
      where incident.id::text = stale_escalation.aggregate_id
        and incident.status <> 'open'
    );

  return query
  with candidates as (
    select candidate.id
    from private.delivery_outbox as candidate
    where (
      (candidate.status = 'pending'
        and candidate.available_at <= authorization_time)
      or (
        candidate.status = 'leased'
        and candidate.lease_expires_at <= authorization_time
      )
    )
      and candidate.attempt_count < candidate.max_attempts
    order by candidate.available_at, candidate.created_at, candidate.id
    limit p_limit
    for update skip locked
  ), claimed as (
    update private.delivery_outbox as outbox
    set status = 'leased',
        lease_owner = p_worker_id,
        lease_expires_at = authorization_time
          + make_interval(secs => p_lease_seconds),
        lease_token = gen_random_uuid(),
        attempt_count = outbox.attempt_count + 1
    from candidates
    where outbox.id = candidates.id
    returning outbox.*
  ), escalated as (
    update private.incidents as incident
    set escalation_status = 'escalated'
    from claimed
    where claimed.event_type = 'critical_incident_ack_escalation'
      and incident.id::text = claimed.aggregate_id
      and incident.status = 'open'
    returning incident.id
  )
  select
    claimed.id,
    claimed.event_type,
    claimed.payload_version,
    claimed.aggregate_type,
    claimed.aggregate_id,
    claimed.dedupe_key,
    claimed.payload,
    claimed.destination_type,
    claimed.attempt_count,
    claimed.lease_token,
    claimed.lease_expires_at
  from claimed
  order by claimed.available_at, claimed.created_at, claimed.id;
end;
$$;

create function private.complete_outbox_delivery_impl(
  p_outbox_id uuid,
  p_worker_id text,
  p_lease_token uuid,
  p_now timestamptz,
  p_external_receipt_id text,
  p_external_receipt_sha256 text
)
returns table (outbox_id uuid, status text, delivered_at timestamptz)
language plpgsql
security definer
set search_path = ''
as $$
declare
  authorization_time timestamptz := clock_timestamp();
begin
  perform private.require_service_role();
  if p_outbox_id is null
     or nullif(btrim(p_worker_id), '') is null
     or p_lease_token is null
     or p_now is null
     or nullif(btrim(p_external_receipt_id), '') is null
     or p_external_receipt_sha256 is null
     or p_external_receipt_sha256 !~ '^[0-9a-f]{64}$'
     or p_now < authorization_time - interval '5 minutes'
     or p_now > authorization_time + interval '30 seconds' then
    raise exception 'outbox_receipt_invalid' using errcode = '22023';
  end if;
  if exists (
    select 1
    from private.delivery_outbox as outbox
    where outbox.id = p_outbox_id
      and outbox.status = 'leased'
      and outbox.lease_owner = p_worker_id
      and outbox.lease_token = p_lease_token
      and outbox.destination_type = 'audit_archive'
      and outbox.payload->>'event_hash' is distinct from p_external_receipt_sha256
  ) then
    raise exception 'audit_archive_receipt_hash_mismatch' using errcode = '23514';
  end if;

  update private.delivery_outbox as outbox
  set status = 'delivered',
      lease_owner = null,
      lease_expires_at = null,
      lease_token = null,
      external_receipt_id = p_external_receipt_id,
      external_receipt_digest = p_external_receipt_sha256,
      delivered_at = authorization_time,
      last_error_code = null
  where outbox.id = p_outbox_id
    and outbox.status = 'leased'
    and outbox.lease_owner = p_worker_id
    and outbox.lease_token = p_lease_token
    and outbox.lease_expires_at > authorization_time;
  if not found then
    raise exception 'outbox_lease_not_owned_current_or_expired'
      using errcode = '40001';
  end if;
  return query select p_outbox_id, 'delivered'::text, authorization_time;
end;
$$;

create function private.fail_outbox_delivery_impl(
  p_outbox_id uuid,
  p_worker_id text,
  p_lease_token uuid,
  p_now timestamptz,
  p_error_code text,
  p_retry_after_seconds integer
)
returns table (outbox_id uuid, status text, available_at timestamptz)
language plpgsql
security definer
set search_path = ''
as $$
declare
  authorization_time timestamptz := clock_timestamp();
  next_status text;
  next_available timestamptz;
begin
  perform private.require_service_role();
  if p_outbox_id is null
     or nullif(btrim(p_worker_id), '') is null
     or p_lease_token is null
     or p_now is null
     or nullif(btrim(p_error_code), '') is null
     or length(p_error_code) > 120
     or p_retry_after_seconds is null
     or p_retry_after_seconds < 1
     or p_retry_after_seconds > 86400
     or p_now < authorization_time - interval '5 minutes'
     or p_now > authorization_time + interval '30 seconds' then
    raise exception 'outbox_failure_parameters_invalid' using errcode = '22023';
  end if;

  update private.delivery_outbox as outbox
  set status = case
        when outbox.attempt_count >= outbox.max_attempts
          then 'dead_letter'
        else 'pending'
      end,
      available_at = case
        when outbox.attempt_count >= outbox.max_attempts
          then outbox.available_at
        else authorization_time + make_interval(secs => p_retry_after_seconds)
      end,
      lease_owner = null,
      lease_expires_at = null,
      lease_token = null,
      last_error_code = p_error_code
  where outbox.id = p_outbox_id
    and outbox.status = 'leased'
    and outbox.lease_owner = p_worker_id
    and outbox.lease_token = p_lease_token
    and outbox.lease_expires_at > authorization_time
  returning outbox.status, outbox.available_at
  into next_status, next_available;
  if not found then
    raise exception 'outbox_lease_not_owned_current_or_expired'
      using errcode = '40001';
  end if;
  if next_status = 'dead_letter' then
    insert into private.incidents (
      severity, incident_type, summary_code, correlation_id, opened_at
    ) values (
      'high', 'delivery_dead_letter', p_error_code, p_outbox_id,
      authorization_time
    );
  end if;
  return query select p_outbox_id, next_status, next_available;
end;
$$;

create function worker_api.claim_delivery_outbox(
  p_worker_id text,
  p_now timestamptz,
  p_limit integer,
  p_lease_seconds integer
)
returns table (
  outbox_id uuid,
  event_type text,
  payload_version integer,
  aggregate_type text,
  aggregate_id text,
  dedupe_key text,
  payload jsonb,
  destination_type text,
  attempt_count integer,
  lease_token uuid,
  lease_expires_at timestamptz
)
language sql
security invoker
set search_path = ''
as $$
  select * from private.claim_delivery_outbox_impl(
    p_worker_id, p_now, p_limit, p_lease_seconds
  );
$$;

create function worker_api.complete_outbox_delivery(
  p_outbox_id uuid,
  p_worker_id text,
  p_lease_token uuid,
  p_now timestamptz,
  p_external_receipt_id text,
  p_external_receipt_sha256 text
)
returns table (outbox_id uuid, status text, delivered_at timestamptz)
language sql
security invoker
set search_path = ''
as $$
  select * from private.complete_outbox_delivery_impl(
    p_outbox_id, p_worker_id, p_lease_token, p_now,
    p_external_receipt_id, p_external_receipt_sha256
  );
$$;

create function worker_api.fail_outbox_delivery(
  p_outbox_id uuid,
  p_worker_id text,
  p_lease_token uuid,
  p_now timestamptz,
  p_error_code text,
  p_retry_after_seconds integer
)
returns table (outbox_id uuid, status text, available_at timestamptz)
language sql
security invoker
set search_path = ''
as $$
  select * from private.fail_outbox_delivery_impl(
    p_outbox_id, p_worker_id, p_lease_token, p_now,
    p_error_code, p_retry_after_seconds
  );
$$;

drop function worker_api.complete_execution_reconciliation(
  uuid, text, text, bigint, timestamptz, text, timestamptz, text
);
drop function private.complete_execution_reconciliation_impl(
  uuid, text, text, bigint, timestamptz, text, timestamptz, text
);

create function private.complete_execution_reconciliation_impl(
  p_intent_id uuid,
  p_worker_id text,
  p_release_sha text,
  p_fencing_token bigint,
  p_now timestamptz,
  p_outcome text,
  p_next_reconcile_at timestamptz,
  p_reason_code text
)
returns table (intent_id uuid, state text, next_reconcile_at timestamptz)
language plpgsql
security definer
set search_path = ''
as $$
declare
  authorization_time timestamptz := clock_timestamp();
  next_state text;
  next_time timestamptz;
  intent_row private.order_intents%rowtype;
  manual_run_id uuid;
  manual_break_id uuid;
  manual_incident_id uuid;
  manual_marker_id uuid;
  reason_digest text;
  existing_terminal_state text;
  existing_terminal_time timestamptz;
begin
  perform private.require_service_role();
  if p_intent_id is null
     or p_worker_id is null
     or p_worker_id !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
     or p_release_sha is null
     or p_release_sha !~ '^[0-9a-f]{40}([0-9a-f]{24})?$'
     or p_fencing_token is null
     or p_fencing_token <= 0
     or p_now is null
     or p_outcome is null
     or p_outcome not in ('reschedule', 'complete', 'manual')
     or nullif(btrim(p_reason_code), '') is null
     or p_now < authorization_time - interval '5 minutes'
     or p_now > authorization_time + interval '30 seconds'
     or (
       p_outcome = 'reschedule'
       and (
         p_next_reconcile_at is null
         or p_next_reconcile_at <= authorization_time
       )
     )
     or (p_outcome <> 'reschedule' and p_next_reconcile_at is not null) then
    raise exception 'reconciliation_completion_parameters_invalid'
      using errcode = '22023';
  end if;
  if p_outcome = 'complete' and not exists (
    select 1
    from private.execution_observations as observation
    where observation.intent_id = p_intent_id
      and observation.event_type in (
        'filled', 'canceled', 'expired', 'rejected', 'failed_pre_dispatch'
      )
  ) then
    raise exception 'terminal_observation_required_for_completion'
      using errcode = '23514';
  end if;

  perform pg_catalog.pg_advisory_xact_lock(
    pg_catalog.hashtextextended(
      p_intent_id::text, 68491723011702::bigint
    )
  );
  select * into intent_row
  from private.order_intents
  where id = p_intent_id
  for update;
  if not found then
    raise exception 'execution_intent_not_found' using errcode = 'P0002';
  end if;

  next_state := case
    when p_outcome = 'reschedule' then 'pending'
    else p_outcome
  end;
  next_time := coalesce(p_next_reconcile_at, authorization_time);

  update private.execution_reconciliation_state as current_state
  set state = next_state,
      next_reconcile_at = next_time,
      lease_owner = null,
      lease_expires_at = null,
      last_reason_code = p_reason_code,
      updated_at = greatest(
        authorization_time,
        current_state.updated_at + interval '1 microsecond'
      )
  from private.worker_leases as current_lease
  where current_state.intent_id = p_intent_id
    and current_lease.account_id = intent_row.account_id
    and current_state.state = 'leased'
    and current_state.lease_owner = p_worker_id
    and current_state.lease_expires_at > authorization_time
    and current_state.claim_release_sha = p_release_sha
    and current_state.claim_fencing_token = p_fencing_token
    and current_lease.holder_id = p_worker_id
    and current_lease.fencing_token = p_fencing_token
    and current_lease.release_sha = p_release_sha
    and current_lease.expires_at > authorization_time;
  if not found then
    -- A response can be lost after commit. Terminal retries are safe only when
    -- both the persisted attempt token and the current worker lease still
    -- match exactly; no side effects are emitted again.
    if next_state in ('complete', 'manual') then
      select current_state.state, current_state.next_reconcile_at
      into existing_terminal_state, existing_terminal_time
      from private.execution_reconciliation_state as current_state
      join private.worker_leases as current_lease
        on current_lease.account_id = intent_row.account_id
      where current_state.intent_id = p_intent_id
        and current_state.state = next_state
        and current_state.last_reason_code = p_reason_code
        and current_state.claim_release_sha = p_release_sha
        and current_state.claim_fencing_token = p_fencing_token
        and current_lease.holder_id = p_worker_id
        and current_lease.release_sha = p_release_sha
        and current_lease.fencing_token = p_fencing_token
        and current_lease.expires_at > authorization_time;
      if found then
        return query
          select p_intent_id, existing_terminal_state, existing_terminal_time;
        return;
      end if;
    end if;
    raise exception 'reconciliation_lease_not_owned_current_or_fenced'
      using errcode = '40001';
  end if;

  if p_outcome = 'manual' then
    reason_digest := pg_catalog.encode(
      public.digest(
        pg_catalog.convert_to(p_reason_code, 'UTF8'),
        'sha256'
      ),
      'hex'
    );

    -- Reuse a semantically identical break already attached to this intent
    -- (including the unknown-observation workflow) instead of opening another.
    select reconciliation_break.run_id, reconciliation_break.id
    into manual_run_id, manual_break_id
    from private.order_events as event
    join private.reconciliation_breaks as reconciliation_break
      on event.event_summary->>'reconciliation_break_id'
        = reconciliation_break.id::text
    where event.intent_id = p_intent_id
      and reconciliation_break.break_type <> 'legacy_unreconciled'
      and reconciliation_break.state <> 'resolved'
      and reconciliation_break.summary_code = p_reason_code
    order by reconciliation_break.detected_at, reconciliation_break.id
    limit 1;

    if manual_break_id is null then
      manual_run_id := md5(
        'reconciliation-manual-run:' || p_intent_id::text || ':' || reason_digest
      )::uuid;
      manual_break_id := md5(
        'reconciliation-manual-break:' || p_intent_id::text || ':' || reason_digest
      )::uuid;

      insert into private.reconciliation_runs (
        id, account_id, environment, started_at, completed_at, result,
        release_sha
      ) values (
        manual_run_id, intent_row.account_id, intent_row.environment,
        authorization_time, authorization_time, 'breaks_found', p_release_sha
      )
      on conflict (id) do nothing;

      insert into private.reconciliation_breaks (
        id, run_id, account_id, break_type, state, detected_at, summary_code
      ) values (
        manual_break_id, manual_run_id, intent_row.account_id, 'execution',
        'open', authorization_time, p_reason_code
      )
      on conflict (id) do nothing;
    end if;

    select incident.id into manual_incident_id
    from private.incidents as incident
    where incident.correlation_id = intent_row.correlation_id
      and (
        incident.summary_code = p_reason_code
        or (
          p_reason_code = 'unknown_requires_manual_check'
          and incident.incident_type = 'execution_unknown'
        )
      )
    order by incident.opened_at, incident.id
    limit 1;

    if manual_incident_id is null then
      manual_incident_id := md5(
        'reconciliation-manual-incident:'
          || p_intent_id::text || ':' || reason_digest
      )::uuid;
      insert into private.incidents (
        id, severity, incident_type, summary_code, correlation_id, opened_at
      ) values (
        manual_incident_id, 'critical',
        'execution_reconciliation_manual_required', p_reason_code,
        intent_row.correlation_id, authorization_time
      )
      on conflict (id) do nothing;
    end if;

    insert into private.delivery_outbox (
      event_type, aggregate_type, aggregate_id, dedupe_key, payload,
      destination_type, available_at
    ) values (
      'execution_reconciliation_manual_required',
      'order_intent',
      p_intent_id::text,
      'reconciliation-manual:' || p_intent_id::text || ':' || reason_digest,
      jsonb_build_object(
        'intent_id', p_intent_id,
        'reconciliation_run_id', manual_run_id,
        'reconciliation_break_id', manual_break_id,
        'incident_id', manual_incident_id,
        'reason_code', p_reason_code,
        'release_sha', p_release_sha,
        'fencing_token', p_fencing_token,
        'severity', 'critical'
      ),
      'incident_alert',
      authorization_time
    )
    on conflict (dedupe_key) do nothing;

    insert into private.order_events (
      intent_id, event_key, event_type, correlation_id, event_summary,
      occurred_at
    ) values (
      p_intent_id,
      'reconciliation-manual:' || reason_digest,
      'manual_check_quarantined',
      intent_row.correlation_id,
      jsonb_build_object(
        'reason_code', p_reason_code,
        'reconciliation_run_id', manual_run_id,
        'reconciliation_break_id', manual_break_id,
        'incident_id', manual_incident_id
      ),
      authorization_time
    )
    on conflict on constraint order_events_intent_id_event_key_key do nothing
    returning id into manual_marker_id;

    if manual_marker_id is not null then
      perform private.write_audit_event(
        'worker', null, null, 'trading_worker', p_worker_id, p_release_sha,
        'execution_reconciliation_manual_required', 'order_intent',
        p_intent_id::text, intent_row.correlation_id, null,
        p_reason_code, null,
        array[
          'reconciliation_state', 'reconciliation_break_id',
          'incident_id'
        ],
        null,
        reason_digest,
        null
      );
    end if;
  end if;

  return query select p_intent_id, next_state, next_time;
end;
$$;

create function worker_api.complete_execution_reconciliation(
  p_intent_id uuid,
  p_worker_id text,
  p_release_sha text,
  p_fencing_token bigint,
  p_now timestamptz,
  p_outcome text,
  p_next_reconcile_at timestamptz,
  p_reason_code text
)
returns table (intent_id uuid, state text, next_reconcile_at timestamptz)
language sql
security invoker
set search_path = ''
as $$
  select * from private.complete_execution_reconciliation_impl(
    p_intent_id, p_worker_id, p_release_sha, p_fencing_token, p_now,
    p_outcome, p_next_reconcile_at, p_reason_code
  );
$$;

-- The pre-fencing six-argument overload remains discoverable so old workers
-- fail with an actionable upgrade error instead of an ambiguous missing RPC.
create function worker_api.complete_execution_reconciliation(
  p_intent_id uuid,
  p_worker_id text,
  p_now timestamptz,
  p_outcome text,
  p_next_reconcile_at timestamptz,
  p_reason_code text
)
returns table (intent_id uuid, state text, next_reconcile_at timestamptz)
language plpgsql
security invoker
set search_path = ''
as $$
begin
  raise exception 'worker_upgrade_required' using errcode = '0A000';
end;
$$;

revoke execute on function
  private.fence_execution_reconciliation_claim_v1(),
  private.guard_execution_control_qualification_freshness_v1(),
  private.claim_delivery_outbox_impl(text, timestamptz, integer, integer),
  private.complete_outbox_delivery_impl(
    uuid, text, uuid, timestamptz, text, text
  ),
  private.fail_outbox_delivery_impl(
    uuid, text, uuid, timestamptz, text, integer
  ),
  private.complete_execution_reconciliation_impl(
    uuid, text, text, bigint, timestamptz, text, timestamptz, text
  )
from public, anon, authenticated, service_role;

grant execute on function
  private.claim_delivery_outbox_impl(text, timestamptz, integer, integer),
  private.complete_outbox_delivery_impl(
    uuid, text, uuid, timestamptz, text, text
  ),
  private.fail_outbox_delivery_impl(
    uuid, text, uuid, timestamptz, text, integer
  ),
  private.complete_execution_reconciliation_impl(
    uuid, text, text, bigint, timestamptz, text, timestamptz, text
  )
to service_role;

revoke all on function
  worker_api.claim_delivery_outbox(text, timestamptz, integer, integer),
  worker_api.complete_outbox_delivery(
    uuid, text, timestamptz, text, text
  ),
  worker_api.complete_outbox_delivery(
    uuid, text, uuid, timestamptz, text, text
  ),
  worker_api.fail_outbox_delivery(
    uuid, text, timestamptz, text, integer
  ),
  worker_api.fail_outbox_delivery(
    uuid, text, uuid, timestamptz, text, integer
  ),
  worker_api.complete_execution_reconciliation(
    uuid, text, timestamptz, text, timestamptz, text
  ),
  worker_api.complete_execution_reconciliation(
    uuid, text, text, bigint, timestamptz, text, timestamptz, text
  )
from public, anon, authenticated, service_role;

grant execute on function
  worker_api.claim_delivery_outbox(text, timestamptz, integer, integer),
  worker_api.complete_outbox_delivery(
    uuid, text, timestamptz, text, text
  ),
  worker_api.complete_outbox_delivery(
    uuid, text, uuid, timestamptz, text, text
  ),
  worker_api.fail_outbox_delivery(
    uuid, text, timestamptz, text, integer
  ),
  worker_api.fail_outbox_delivery(
    uuid, text, uuid, timestamptz, text, integer
  ),
  worker_api.complete_execution_reconciliation(
    uuid, text, timestamptz, text, timestamptz, text
  ),
  worker_api.complete_execution_reconciliation(
    uuid, text, text, bigint, timestamptz, text, timestamptz, text
  )
to service_role;
