-- G2 human control plane and narrow read API.
--
-- Every exposed function is SECURITY DEFINER only because the underlying
-- source-of-truth tables have no direct role grants or RLS policies. Functions
-- use an empty search_path, qualify every relation, validate caller identity,
-- return bounded records, and receive explicit EXECUTE grants in 0019.

create or replace function private.current_aal()
returns text
language plpgsql
stable
security definer
set search_path = ''
as $$
declare
  claim_value text;
  claims jsonb;
begin
  claim_value := nullif(current_setting('request.jwt.claim.aal', true), '');
  if claim_value is not null then
    return claim_value;
  end if;
  begin
    claims := nullif(current_setting('request.jwt.claims', true), '')::jsonb;
  exception when others then
    claims := null;
  end;
  return coalesce(claims->>'aal', 'aal1');
end;
$$;

create or replace function private.has_active_role(p_user_id uuid, p_role text)
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select exists (
    select 1
    from private.role_assignments as assignment
    where assignment.user_id = p_user_id
      and assignment.role = p_role
      and assignment.revoked_at is null
      and assignment.valid_from <= now()
      and (assignment.valid_until is null or assignment.valid_until > now())
  );
$$;

create or replace function private.require_human_roles(
  p_roles text[],
  p_require_aal2 boolean
)
returns uuid
language plpgsql
stable
security definer
set search_path = ''
as $$
declare
  actor uuid;
begin
  if (select auth.role()) <> 'authenticated' then
    raise exception 'authenticated_user_required' using errcode = '42501';
  end if;
  actor := (select auth.uid());
  if actor is null then
    raise exception 'authenticated_user_required' using errcode = '42501';
  end if;
  if not exists (
    select 1
    from unnest(p_roles) as required_role(role_name)
    where private.has_active_role(actor, required_role.role_name)
  ) then
    raise exception 'operation_role_required' using errcode = '42501';
  end if;
  if p_require_aal2 and private.current_aal() <> 'aal2' then
    raise exception 'aal2_step_up_required' using errcode = '42501';
  end if;
  return actor;
end;
$$;

create or replace function private.require_service_role()
returns void
language plpgsql
stable
security definer
set search_path = ''
as $$
begin
  if (select auth.role()) <> 'service_role' then
    raise exception 'service_role_required' using errcode = '42501';
  end if;
end;
$$;

create or replace function private.write_audit_event(
  p_actor_type text,
  p_actor_user_id uuid,
  p_actor_role text,
  p_service_principal text,
  p_worker_instance_id text,
  p_release_sha text,
  p_action text,
  p_resource_type text,
  p_resource_id text,
  p_correlation_id uuid,
  p_control_command_id uuid,
  p_reason_code text,
  p_ticket_ref text,
  p_changed_fields text[],
  p_before_digest text,
  p_after_digest text,
  p_evidence_id uuid
)
returns uuid
language plpgsql
security definer
set search_path = ''
as $$
declare
  event_id uuid := gen_random_uuid();
  event_time timestamptz := clock_timestamp();
  event_transaction_id bigint := txid_current();
  actor_session_hash_value text;
  actor_session_id_value text;
  canonical_event jsonb;
  prior_hash text;
  computed_hash text;
begin
  if p_actor_type not in ('human', 'worker', 'system') then
    raise exception 'audit_actor_type_invalid' using errcode = '23514';
  end if;
  if p_actor_type = 'human' and p_actor_user_id is null then
    raise exception 'audit_human_actor_required' using errcode = '23514';
  end if;
  if p_actor_type = 'worker' and nullif(btrim(p_service_principal), '') is null then
    raise exception 'audit_service_principal_required' using errcode = '23514';
  end if;
  if p_actor_type = 'human' then
    actor_session_id_value := nullif((select auth.jwt())->>'session_id', '');
    if actor_session_id_value is null then
      raise exception 'audit_human_session_required' using errcode = '42501';
    end if;
    actor_session_hash_value := pg_catalog.encode(
      public.digest(
        pg_catalog.convert_to(actor_session_id_value, 'UTF8'),
        'sha256'
      ),
      'hex'
    );
  end if;

  perform pg_catalog.pg_advisory_xact_lock(68491723011701::bigint);
  select event_hash into prior_hash
  from private.audit_events
  order by occurred_at desc, id desc
  limit 1;

  canonical_event := jsonb_build_object(
    'id', event_id,
    'occurred_at', to_char(
      event_time at time zone 'UTC',
      'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'
    ),
    'transaction_id', event_transaction_id,
    'actor_type', p_actor_type,
    'actor_user_id', p_actor_user_id,
    'actor_role', p_actor_role,
    'actor_session_hash', actor_session_hash_value,
    'service_principal', p_service_principal,
    'worker_instance_id', p_worker_instance_id,
    'release_sha', p_release_sha,
    'action', p_action,
    'resource_type', p_resource_type,
    'resource_id', p_resource_id,
    'correlation_id', p_correlation_id,
    'control_command_id', p_control_command_id,
    'reason_code', p_reason_code,
    'ticket_ref', p_ticket_ref,
    'changed_fields', to_jsonb(coalesce(p_changed_fields, array[]::text[])),
    'before_digest', p_before_digest,
    'after_digest', p_after_digest,
    'evidence_id', p_evidence_id,
    'previous_event_hash', prior_hash
  );
  computed_hash := pg_catalog.encode(
    public.digest(
      pg_catalog.convert_to(canonical_event::text, 'UTF8'),
      'sha256'
    ),
    'hex'
  );

  insert into private.audit_events (
    id,
    occurred_at,
    transaction_id,
    actor_type,
    actor_user_id,
    actor_role,
    actor_session_hash,
    service_principal,
    worker_instance_id,
    release_sha,
    action,
    resource_type,
    resource_id,
    correlation_id,
    control_command_id,
    reason_code,
    ticket_ref,
    changed_fields,
    before_digest,
    after_digest,
    evidence_id,
    previous_event_hash,
    event_hash
  ) values (
    event_id,
    event_time,
    event_transaction_id,
    p_actor_type,
    p_actor_user_id,
    p_actor_role,
    actor_session_hash_value,
    p_service_principal,
    p_worker_instance_id,
    p_release_sha,
    p_action,
    p_resource_type,
    p_resource_id,
    p_correlation_id,
    p_control_command_id,
    p_reason_code,
    p_ticket_ref,
    coalesce(p_changed_fields, array[]::text[]),
    p_before_digest,
    p_after_digest,
    p_evidence_id,
    prior_hash,
    computed_hash
  );

  insert into private.delivery_outbox (
    event_type,
    aggregate_type,
    aggregate_id,
    dedupe_key,
    payload,
    destination_type
  ) values (
    'audit_event_created',
    'audit_event',
    event_id::text,
    'audit:' || event_id::text,
    jsonb_build_object(
      'event_id', event_id,
      'event_hash', computed_hash,
      'previous_event_hash', prior_hash,
      'occurred_at', event_time,
      'action', p_action,
      'resource_type', p_resource_type,
      'envelope', canonical_event
    ),
    'audit_archive'
  );
  return event_id;
end;
$$;

create or replace function private.get_my_access_profile()
returns table (
  user_id uuid,
  aal text,
  active_roles text[]
)
language plpgsql
stable
security definer
set search_path = ''
as $$
declare
  actor uuid;
begin
  actor := private.require_human_roles(
    array[
      'platform_admin', 'operator', 'risk_approver', 'strategy_reviewer',
      'auditor', 'release_manager', 'viewer'
    ],
    true
  );
  return query
  select
    actor,
    private.current_aal(),
    coalesce(array_agg(assignment.role order by assignment.role), array[]::text[])
  from private.role_assignments as assignment
  where assignment.user_id = actor
    and assignment.revoked_at is null
    and assignment.valid_from <= now()
    and (assignment.valid_until is null or assignment.valid_until > now());
end;
$$;

create or replace function private.get_platform_status()
returns table (
  environment text,
  provider_connectivity text,
  production_live_enabled boolean,
  production_order_credentials_present boolean,
  account_id text,
  account_state text,
  execution_enabled boolean,
  control_epoch bigint,
  control_expires_at timestamptz,
  available_cash_krw numeric,
  open_incident_count bigint,
  pending_delivery_count bigint
)
language plpgsql
stable
security definer
set search_path = ''
as $$
begin
  perform private.require_human_roles(
    array[
      'platform_admin', 'operator', 'risk_approver', 'strategy_reviewer',
      'auditor', 'release_manager', 'viewer'
    ],
    true
  );
  return query
  select
    policy.environment,
    policy.provider_connectivity,
    policy.production_live_enabled,
    policy.production_order_credentials_present,
    account.account_id,
    account.state,
    control.execution_enabled,
    control.control_epoch,
    control.expires_at,
    balance.available_cash_krw,
    (select count(*) from private.incidents where status <> 'resolved'),
    (select count(*) from private.delivery_outbox where status <> 'delivered')
  from private.environment_policy as policy
  cross join private.trading_accounts as account
  join private.execution_controls as control using (account_id)
  left join private.cash_balance_projection as balance using (account_id)
  where policy.id = 'singleton'
  order by account.account_id
  limit 20;
end;
$$;

create or replace function private.list_operation_commands(p_limit integer default 50)
returns table (
  command_id uuid,
  command_type text,
  state text,
  requested_at timestamptz,
  reviewed_at timestamptz,
  claimed_at timestamptz,
  applied_at timestamptz,
  expires_at timestamptz,
  target_release_sha text,
  reason_code text
)
language plpgsql
stable
security definer
set search_path = ''
as $$
begin
  perform private.require_human_roles(
    array[
      'platform_admin', 'operator', 'risk_approver', 'strategy_reviewer',
      'auditor', 'release_manager', 'viewer'
    ],
    true
  );
  if p_limit < 1 or p_limit > 100 then
    raise exception 'limit_out_of_range' using errcode = '22023';
  end if;
  return query
  select
    command.id,
    command.command_type,
    command.state,
    command.requested_at,
    command.reviewed_at,
    command.claimed_at,
    command.applied_at,
    command.expires_at,
    command.target_release_sha,
    coalesce(command.failure_code, command.review_reason)
  from private.operation_commands as command
  order by command.requested_at desc, command.id desc
  limit p_limit;
end;
$$;

create or replace function private.list_incidents(p_limit integer default 50)
returns table (
  incident_id uuid,
  severity text,
  status text,
  incident_type text,
  summary_code text,
  opened_at timestamptz,
  acknowledged_at timestamptz,
  resolved_at timestamptz
)
language plpgsql
stable
security definer
set search_path = ''
as $$
begin
  perform private.require_human_roles(
    array[
      'platform_admin', 'operator', 'risk_approver', 'auditor',
      'release_manager', 'viewer'
    ],
    true
  );
  if p_limit < 1 or p_limit > 100 then
    raise exception 'limit_out_of_range' using errcode = '22023';
  end if;
  return query
  select
    incident.id,
    incident.severity,
    incident.status,
    incident.incident_type,
    incident.summary_code,
    incident.opened_at,
    incident.acknowledged_at,
    incident.resolved_at
  from private.incidents as incident
  order by incident.opened_at desc, incident.id desc
  limit p_limit;
end;
$$;

create or replace function private.list_audit_events(p_limit integer default 50)
returns table (
  event_id uuid,
  occurred_at timestamptz,
  actor_type text,
  actor_role text,
  action text,
  resource_type text,
  resource_id text,
  correlation_id uuid,
  reason_code text,
  event_hash text
)
language plpgsql
stable
security definer
set search_path = ''
as $$
begin
  perform private.require_human_roles(array['platform_admin', 'auditor'], true);
  if p_limit < 1 or p_limit > 100 then
    raise exception 'limit_out_of_range' using errcode = '22023';
  end if;
  return query
  select
    event.id,
    event.occurred_at,
    event.actor_type,
    event.actor_role,
    event.action,
    event.resource_type,
    event.resource_id,
    event.correlation_id,
    event.reason_code,
    event.event_hash
  from private.audit_events as event
  order by event.occurred_at desc, event.id desc
  limit p_limit;
end;
$$;

create or replace function private.get_accounting_summary()
returns table (
  account_id text,
  environment text,
  state text,
  settled_cash_krw numeric,
  reserved_cash_krw numeric,
  available_cash_krw numeric,
  projection_version bigint,
  position_count bigint,
  open_reconciliation_breaks bigint
)
language plpgsql
stable
security definer
set search_path = ''
as $$
begin
  perform private.require_human_roles(
    array[
      'platform_admin', 'operator', 'risk_approver', 'strategy_reviewer',
      'auditor', 'release_manager', 'viewer'
    ],
    true
  );
  return query
  select
    account.account_id,
    account.environment,
    account.state,
    balance.settled_cash_krw,
    balance.reserved_cash_krw,
    balance.available_cash_krw,
    balance.projection_version,
    (
      select count(*)
      from private.position_projection as position
      where position.account_id = account.account_id and position.quantity > 0
    ),
    (
      select count(*)
      from private.reconciliation_breaks as break_row
      where break_row.account_id = account.account_id and break_row.state <> 'resolved'
    )
  from private.trading_accounts as account
  join private.cash_balance_projection as balance using (account_id)
  order by account.account_id
  limit 20;
end;
$$;

create or replace function private.register_control_evidence(
  p_evidence_type text,
  p_environment text,
  p_artifact_uri text,
  p_artifact_sha256 text,
  p_captured_at timestamptz,
  p_metadata_summary jsonb default '{}'::jsonb
)
returns table (evidence_id uuid, verified_at timestamptz)
language plpgsql
security definer
set search_path = ''
as $$
declare
  actor uuid;
  created_id uuid;
  created_time timestamptz := clock_timestamp();
begin
  actor := private.require_human_roles(
    array['platform_admin', 'operator', 'risk_approver', 'release_manager', 'auditor'],
    true
  );
  if jsonb_typeof(coalesce(p_metadata_summary, '{}'::jsonb)) <> 'object' then
    raise exception 'evidence_metadata_must_be_object' using errcode = '22023';
  end if;
  insert into private.control_evidence (
    evidence_type,
    environment,
    artifact_uri,
    artifact_sha256,
    captured_at,
    verified_at,
    verified_by,
    metadata_summary
  ) values (
    p_evidence_type,
    p_environment,
    p_artifact_uri,
    p_artifact_sha256,
    p_captured_at,
    created_time,
    actor,
    coalesce(p_metadata_summary, '{}'::jsonb)
  ) returning id into created_id;

  perform private.write_audit_event(
    'human', actor, 'evidence_verifier', null, null, null,
    'control_evidence_registered', 'control_evidence', created_id::text,
    gen_random_uuid(), null, 'evidence_registered', null,
    array['artifact_sha256', 'verified_at'], null, p_artifact_sha256, created_id
  );
  return query select created_id, created_time;
end;
$$;

create or replace function private.request_operation_command(
  p_command_type text,
  p_requested_change jsonb,
  p_evidence_id uuid,
  p_target_release_sha text,
  p_expires_at timestamptz,
  p_idempotency_key text
)
returns table (command_id uuid, state text, requested_at timestamptz, expires_at timestamptz)
language plpgsql
security definer
set search_path = ''
as $$
declare
  actor uuid;
  created_id uuid;
  created_at_value timestamptz;
  expires_at_value timestamptz;
  created_state text;
  existing_command private.operation_commands%rowtype;
begin
  actor := private.require_human_roles(
    array['platform_admin', 'operator', 'risk_approver', 'strategy_reviewer', 'release_manager'],
    true
  );
  if p_command_type not in (
    'account_opening', 'contract_test_enable', 'pause_paper', 'paper_resume',
    'strategy_promotion', 'risk_policy_change', 'release_promotion',
    'unknown_resolution'
  ) then
    raise exception 'operation_command_type_invalid' using errcode = '22023';
  end if;
  if jsonb_typeof(coalesce(p_requested_change, '{}'::jsonb)) <> 'object' then
    raise exception 'requested_change_must_be_object' using errcode = '22023';
  end if;
  if nullif(btrim(p_idempotency_key), '') is null or length(p_idempotency_key) > 200 then
    raise exception 'idempotency_key_invalid' using errcode = '22023';
  end if;
  if p_evidence_id is null then
    raise exception 'control_evidence_required' using errcode = '23514';
  end if;

  perform pg_catalog.pg_advisory_xact_lock(68491723011702::bigint);
  select * into existing_command
  from private.operation_commands
  where idempotency_key = p_idempotency_key;
  if found then
    if existing_command.requester_user_id <> actor
       or existing_command.command_type <> p_command_type
       or existing_command.requested_change is distinct from coalesce(p_requested_change, '{}'::jsonb)
       or existing_command.evidence_id is distinct from p_evidence_id
       or existing_command.target_release_sha is distinct from p_target_release_sha then
      raise exception 'operation_command_idempotency_conflict' using errcode = '23505';
    end if;
    return query
    select
      existing_command.id,
      existing_command.state,
      existing_command.requested_at,
      existing_command.expires_at;
    return;
  end if;

  insert into private.operation_commands (
    command_type,
    requested_change,
    evidence_id,
    target_release_sha,
    requester_user_id,
    expires_at,
    idempotency_key
  ) values (
    p_command_type,
    coalesce(p_requested_change, '{}'::jsonb),
    p_evidence_id,
    p_target_release_sha,
    actor,
    p_expires_at,
    p_idempotency_key
  )
  returning id, private.operation_commands.state, requested_at, private.operation_commands.expires_at
  into created_id, created_state, created_at_value, expires_at_value;

  perform private.write_audit_event(
    'human', actor, 'command_requester', null, null, p_target_release_sha,
    'operation_command_requested', 'control_command', created_id::text,
    gen_random_uuid(), created_id, 'requested', null,
    array['state'], null, null, p_evidence_id
  );
  return query select created_id, created_state, created_at_value, expires_at_value;
end;
$$;

create or replace function private.review_operation_command(
  p_command_id uuid,
  p_approve boolean,
  p_review_reason text default null
)
returns table (command_id uuid, state text, reviewed_at timestamptz)
language plpgsql
security definer
set search_path = ''
as $$
declare
  command_row private.operation_commands%rowtype;
  actor uuid;
  required_role text;
  next_state text;
  review_time timestamptz := clock_timestamp();
begin
  select * into command_row
  from private.operation_commands
  where id = p_command_id
  for update;
  if not found then
    raise exception 'operation_command_not_found' using errcode = 'P0002';
  end if;
  required_role := case
    when command_row.command_type in (
      'account_opening', 'contract_test_enable', 'pause_paper', 'paper_resume',
      'risk_policy_change', 'unknown_resolution'
    ) then 'risk_approver'
    when command_row.command_type = 'strategy_promotion' then 'strategy_reviewer'
    when command_row.command_type = 'release_promotion' then 'release_manager'
    else 'platform_admin'
  end;
  actor := private.require_human_roles(array['platform_admin', required_role], true);
  if command_row.state <> 'requested' then
    raise exception 'operation_command_not_reviewable' using errcode = '23514';
  end if;
  if command_row.expires_at <= review_time then
    raise exception 'operation_command_expired' using errcode = '23514';
  end if;
  if command_row.requester_user_id = actor then
    raise exception 'operation_command_self_review_forbidden' using errcode = '42501';
  end if;
  if not p_approve and nullif(btrim(p_review_reason), '') is null then
    raise exception 'rejection_reason_required' using errcode = '23514';
  end if;
  next_state := case when p_approve then 'approved' else 'rejected' end;
  update private.operation_commands
  set state = next_state,
      reviewer_user_id = actor,
      reviewed_at = review_time,
      review_reason = case when p_approve then null else p_review_reason end
  where id = p_command_id;

  perform private.write_audit_event(
    'human', actor, required_role, null, null, command_row.target_release_sha,
    'operation_command_' || next_state, 'control_command', p_command_id::text,
    gen_random_uuid(), p_command_id, next_state, null,
    array['state', 'reviewer_user_id', 'reviewed_at'], null, null,
    command_row.evidence_id
  );
  return query select p_command_id, next_state, review_time;
end;
$$;

create or replace function private.emergency_stop(
  p_account_id text,
  p_reason_code text,
  p_ticket_ref text default null
)
returns table (account_id text, execution_enabled boolean, control_epoch bigint, stopped_at timestamptz)
language plpgsql
security definer
set search_path = ''
as $$
declare
  actor uuid;
  stopped_time timestamptz := clock_timestamp();
  new_epoch bigint;
begin
  actor := private.require_human_roles(array['platform_admin', 'operator'], true);
  if nullif(btrim(p_reason_code), '') is null then
    raise exception 'emergency_stop_reason_required' using errcode = '22023';
  end if;
  update private.execution_controls
  set execution_enabled = false,
      control_epoch = control_epoch + 1,
      effective_at = stopped_time,
      expires_at = greatest(expires_at, stopped_time + interval '1 second'),
      last_command_id = null,
      updated_reason_code = p_reason_code,
      updated_at = stopped_time
  where private.execution_controls.account_id = p_account_id
  returning private.execution_controls.control_epoch into new_epoch;
  if new_epoch is null then
    raise exception 'execution_account_not_found' using errcode = 'P0002';
  end if;

  perform private.write_audit_event(
    'human', actor, 'operator', null, null, null,
    'emergency_stop_applied', 'execution_control', p_account_id,
    gen_random_uuid(), null, p_reason_code, p_ticket_ref,
    array['execution_enabled', 'control_epoch'], null, null, null
  );
  return query select p_account_id, false, new_epoch, stopped_time;
end;
$$;

create or replace function private.acknowledge_incident(p_incident_id uuid)
returns table (incident_id uuid, status text, acknowledged_at timestamptz)
language plpgsql
security definer
set search_path = ''
as $$
declare
  actor uuid;
  action_time timestamptz := clock_timestamp();
begin
  actor := private.require_human_roles(array['platform_admin', 'operator'], true);
  update private.incidents
  set status = 'acknowledged', acknowledged_at = action_time, acknowledged_by = actor
  where id = p_incident_id and status = 'open';
  if not found then
    raise exception 'incident_not_open' using errcode = '23514';
  end if;
  perform private.write_audit_event(
    'human', actor, 'operator', null, null, null,
    'incident_acknowledged', 'incident', p_incident_id::text,
    gen_random_uuid(), null, 'operator_ack', null,
    array['status', 'acknowledged_at'], null, null, null
  );
  return query select p_incident_id, 'acknowledged'::text, action_time;
end;
$$;

create or replace function private.resolve_incident(
  p_incident_id uuid,
  p_resolution_code text
)
returns table (incident_id uuid, status text, resolved_at timestamptz)
language plpgsql
security definer
set search_path = ''
as $$
declare
  actor uuid;
  action_time timestamptz := clock_timestamp();
begin
  actor := private.require_human_roles(array['platform_admin', 'risk_approver'], true);
  if nullif(btrim(p_resolution_code), '') is null then
    raise exception 'incident_resolution_required' using errcode = '22023';
  end if;
  update private.incidents
  set status = 'resolved',
      resolved_at = action_time,
      resolved_by = actor,
      resolution_code = p_resolution_code
  where id = p_incident_id and status = 'acknowledged';
  if not found then
    raise exception 'incident_not_acknowledged' using errcode = '23514';
  end if;
  perform private.write_audit_event(
    'human', actor, 'risk_approver', null, null, null,
    'incident_resolved', 'incident', p_incident_id::text,
    gen_random_uuid(), null, p_resolution_code, null,
    array['status', 'resolved_at'], null, null, null
  );
  return query select p_incident_id, 'resolved'::text, action_time;
end;
$$;

revoke execute on all functions in schema private
  from public, anon, authenticated, service_role;
revoke execute on all functions in schema api
  from public, anon, authenticated, service_role;
