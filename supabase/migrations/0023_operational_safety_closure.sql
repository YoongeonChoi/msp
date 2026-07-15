-- Close operational correctness gaps found during G1/G2 local fault review.
-- Existing migrations remain immutable; this migration replaces only affected
-- RPC contracts and adds fail-closed transition guards.

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
  where id = (command_row.requested_change->>'qualification_id')::uuid
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
exception
  when invalid_text_representation then
    raise exception 'qualified_evidence_bundle_expired_or_mismatched_at_application'
      using errcode = '23514';
end;
$$;

drop trigger if exists guard_execution_control_qualification_freshness_v1
  on private.execution_controls;
create trigger guard_execution_control_qualification_freshness_v1
  before update on private.execution_controls
  for each row execute function
    private.guard_execution_control_qualification_freshness_v1();

create or replace function private.stop_execution_for_reconciliation_break_v1()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  if new.break_type = 'legacy_unreconciled' or new.state = 'resolved' then
    return new;
  end if;

  update private.execution_controls as control
  set execution_enabled = false,
      control_epoch = control.control_epoch + 1,
      effective_at = greatest(
        clock_timestamp(), control.updated_at + interval '1 microsecond'
      ),
      expires_at = greatest(
        control.expires_at,
        greatest(
          clock_timestamp(), control.updated_at + interval '1 microsecond'
        ) + interval '1 second'
      ),
      updated_reason_code = 'unresolved_reconciliation_break',
      updated_at = greatest(
        clock_timestamp(), control.updated_at + interval '1 microsecond'
      )
  where control.account_id = new.account_id;

  if not found then
    raise exception 'reconciliation_break_execution_control_missing'
      using errcode = '23514';
  end if;
  return new;
end;
$$;

drop trigger if exists stop_execution_for_reconciliation_break_v1
  on private.reconciliation_breaks;
create trigger stop_execution_for_reconciliation_break_v1
  after insert on private.reconciliation_breaks
  for each row execute function
    private.stop_execution_for_reconciliation_break_v1();

create or replace function private.guard_order_intent_reconciliation_state_v1()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  if exists (
    select 1
    from private.reconciliation_breaks as reconciliation_break
    where reconciliation_break.account_id = new.account_id
      and reconciliation_break.break_type <> 'legacy_unreconciled'
      and reconciliation_break.state <> 'resolved'
  ) then
    raise exception 'unresolved_reconciliation_break_blocks_order_intent'
      using errcode = '40001';
  end if;
  return new;
end;
$$;

drop trigger if exists guard_order_intent_reconciliation_state_v1
  on private.order_intents;
create trigger guard_order_intent_reconciliation_state_v1
  before insert on private.order_intents
  for each row execute function
    private.guard_order_intent_reconciliation_state_v1();

create or replace function private.claim_delivery_outbox_impl(
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
     or p_limit < 1 or p_limit > 100
     or p_lease_seconds < 5 or p_lease_seconds > 300
     or p_now < authorization_time - interval '5 minutes'
     or p_now > authorization_time + interval '30 seconds' then
    raise exception 'outbox_claim_parameters_invalid' using errcode = '22023';
  end if;

  update private.delivery_outbox as stale_escalation
  set status = 'canceled', lease_owner = null, lease_expires_at = null
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
      (candidate.status = 'pending' and candidate.available_at <= authorization_time)
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
    claimed.lease_expires_at
  from claimed
  order by claimed.available_at, claimed.created_at, claimed.id;
end;
$$;

create or replace function private.complete_outbox_delivery_impl(
  p_outbox_id uuid,
  p_worker_id text,
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
  if nullif(btrim(p_worker_id), '') is null
     or nullif(btrim(p_external_receipt_id), '') is null
     or p_external_receipt_sha256 !~ '^[0-9a-f]{64}$'
     or p_now < authorization_time - interval '5 minutes'
     or p_now > authorization_time + interval '30 seconds' then
    raise exception 'outbox_receipt_invalid' using errcode = '22023';
  end if;
  if exists (
    select 1
    from private.delivery_outbox as outbox
    where outbox.id = p_outbox_id
      and outbox.destination_type = 'audit_archive'
      and outbox.payload->>'event_hash' is distinct from p_external_receipt_sha256
  ) then
    raise exception 'audit_archive_receipt_hash_mismatch' using errcode = '23514';
  end if;

  update private.delivery_outbox as outbox
  set status = 'delivered',
      lease_owner = null,
      lease_expires_at = null,
      external_receipt_id = p_external_receipt_id,
      external_receipt_digest = p_external_receipt_sha256,
      delivered_at = authorization_time,
      last_error_code = null
  where outbox.id = p_outbox_id
    and outbox.status = 'leased'
    and outbox.lease_owner = p_worker_id
    and outbox.lease_expires_at > authorization_time;
  if not found then
    raise exception 'outbox_lease_not_owned_or_expired' using errcode = '40001';
  end if;
  return query select p_outbox_id, 'delivered'::text, authorization_time;
end;
$$;

create or replace function private.fail_outbox_delivery_impl(
  p_outbox_id uuid,
  p_worker_id text,
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
  if nullif(btrim(p_worker_id), '') is null
     or nullif(btrim(p_error_code), '') is null
     or length(p_error_code) > 120
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
      last_error_code = p_error_code
  where outbox.id = p_outbox_id
    and outbox.status = 'leased'
    and outbox.lease_owner = p_worker_id
    and outbox.lease_expires_at > authorization_time
  returning outbox.status, outbox.available_at
  into next_status, next_available;
  if not found then
    raise exception 'outbox_lease_not_owned_or_expired' using errcode = '40001';
  end if;
  if next_status = 'dead_letter' then
    insert into private.incidents (
      severity, incident_type, summary_code, correlation_id
    ) values (
      'high', 'delivery_dead_letter', p_error_code, p_outbox_id
    );
  end if;
  return query select p_outbox_id, next_status, next_available;
end;
$$;

drop function if exists worker_api.complete_execution_reconciliation(
  uuid, text, timestamptz, text, timestamptz, text
);
drop function if exists private.complete_execution_reconciliation_impl(
  uuid, text, timestamptz, text, timestamptz, text
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
begin
  perform private.require_service_role();
  if p_worker_id !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
     or p_release_sha !~ '^[0-9a-f]{40}([0-9a-f]{24})?$'
     or p_fencing_token <= 0
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
      updated_at = authorization_time
  from private.order_intents as intent,
       private.worker_leases as current_lease
  where current_state.intent_id = p_intent_id
    and intent.id = current_state.intent_id
    and current_lease.account_id = intent.account_id
    and current_state.state = 'leased'
    and current_state.lease_owner = p_worker_id
    and current_state.lease_expires_at > authorization_time
    and current_lease.holder_id = p_worker_id
    and current_lease.fencing_token = p_fencing_token
    and current_lease.release_sha = p_release_sha
    and current_lease.expires_at > authorization_time;
  if not found then
    raise exception 'reconciliation_lease_not_owned_current_or_fenced'
      using errcode = '40001';
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

revoke execute on function
  private.guard_execution_control_qualification_freshness_v1(),
  private.stop_execution_for_reconciliation_break_v1(),
  private.guard_order_intent_reconciliation_state_v1(),
  private.claim_delivery_outbox_impl(text, timestamptz, integer, integer),
  private.complete_outbox_delivery_impl(uuid, text, timestamptz, text, text),
  private.fail_outbox_delivery_impl(uuid, text, timestamptz, text, integer),
  private.complete_execution_reconciliation_impl(
    uuid, text, text, bigint, timestamptz, text, timestamptz, text
  )
from public, anon, authenticated, service_role;

grant execute on function
  private.claim_delivery_outbox_impl(text, timestamptz, integer, integer),
  private.complete_outbox_delivery_impl(uuid, text, timestamptz, text, text),
  private.fail_outbox_delivery_impl(uuid, text, timestamptz, text, integer),
  private.complete_execution_reconciliation_impl(
    uuid, text, text, bigint, timestamptz, text, timestamptz, text
  )
to service_role;

revoke all on function worker_api.complete_execution_reconciliation(
  uuid, text, text, bigint, timestamptz, text, timestamptz, text
) from public, anon, authenticated;
grant execute on function worker_api.complete_execution_reconciliation(
  uuid, text, text, bigint, timestamptz, text, timestamptz, text
) to service_role;
