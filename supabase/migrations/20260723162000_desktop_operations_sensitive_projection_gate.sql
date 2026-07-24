begin;

set local lock_timeout = '5s';
set local statement_timeout = '90s';

-- Preserve the v1 response shape while enforcing the documented auditor-only
-- evidence boundary before either sensitive private-table query executes.
-- The coarse reconciliation health signal intentionally remains available to
-- minimum-status callers; row-level audit and reconciliation evidence does not.
do $$
begin
  if to_regprocedure(
       'private.get_desktop_operations_snapshot_v1_impl()'
     ) is null
     or to_regprocedure(
       'api.get_desktop_operations_snapshot_v1()'
     ) is null
     or to_regclass('private.audit_events') is null
     or to_regclass('private.reconciliation_breaks') is null then
    raise exception 'desktop_operations_sensitive_projection_dependency_missing'
      using errcode = '55000';
  end if;
end;
$$;

create or replace function private.get_desktop_operations_snapshot_v1_impl()
returns jsonb
language plpgsql
volatile
security definer
set search_path = ''
as $$
declare
  actor uuid;
  roles text[];
  permissions text[] := array[]::text[];
  now_value timestamptz := clock_timestamp();
  environment_value text;
  control_row private.execution_controls%rowtype;
  heartbeat_time timestamptz;
  heartbeat_release text;
  heartbeat_status text;
  heartbeat_worker_id text;
  heartbeat_component text;
  heartbeat_checkpoint text;
  heartbeat_completed_at text;
  active_lease_holder text;
  active_lease_release text;
  active_lease_expires_at timestamptz;
  runtime_state text;
  worker_state text;
  contract_state text;
  grant_rows jsonb;
  qualification_value jsonb;
  commands_value jsonb;
  pending_reviews_value jsonb;
  reviews_value jsonb;
  access_changes_value jsonb;
  incidents_value jsonb;
  orders_value jsonb;
  positions_value jsonb;
  audit_value jsonb := '[]'::jsonb;
  reconciliation_value jsonb := '[]'::jsonb;
begin
  actor := private.require_human_roles(
    array[
      'platform_admin', 'operator', 'risk_approver', 'strategy_reviewer',
      'auditor', 'release_manager', 'viewer'
    ],
    true
  );
  select coalesce(array_agg(role order by role), array[]::text[])
  into roles
  from private.role_assignments
  where user_id = actor
    and revoked_at is null
    and valid_from <= now_value
    and (valid_until is null or valid_until > now_value);
  if roles && array['operator']::text[]
     and not roles && array['platform_admin']::text[] then
    permissions := permissions || array['request_command', 'acknowledge_incident'];
  end if;
  if roles && array['risk_approver', 'strategy_reviewer', 'release_manager']::text[]
     and not roles && array['platform_admin']::text[] then
    permissions := permissions || array['review_command'];
  end if;
  if roles && array['risk_approver']::text[]
     and not roles && array['platform_admin']::text[] then
    permissions := permissions || array['resolve_incident'];
  end if;
  if roles && array['auditor']::text[] then
    permissions := permissions || array['view_audit', 'view_reconciliation'];
  end if;

  select environment into environment_value
  from private.environment_policy where id = 'singleton';
  select * into control_row
  from private.execution_controls
  where environment = environment_value
  order by account_id
  limit 1;
  select lease.holder_id, lease.release_sha, lease.expires_at
  into active_lease_holder, active_lease_release, active_lease_expires_at
  from private.worker_leases as lease
  where lease.account_id = control_row.account_id
    and lease.expires_at > now_value
  limit 1;
  select
    heartbeat.created_at,
    case
      when heartbeat.details->>'release_sha' ~ '^[0-9a-f]{40}([0-9a-f]{24})?$'
        then heartbeat.details->>'release_sha'
      else null
    end,
    heartbeat.status,
    case
      when heartbeat.worker_name like 'trading-worker:%'
        then substr(heartbeat.worker_name, length('trading-worker:') + 1)
      else null
    end,
    heartbeat.details->>'component',
    heartbeat.details->>'checkpoint',
    heartbeat.details->>'completed_at'
  into heartbeat_time, heartbeat_release, heartbeat_status,
    heartbeat_worker_id, heartbeat_component, heartbeat_checkpoint,
    heartbeat_completed_at
  from public.worker_heartbeats as heartbeat
  order by heartbeat.created_at desc
  limit 1;
  worker_state := case
    when heartbeat_time is null then 'offline'
    when heartbeat_status <> 'ok' then 'degraded'
    when active_lease_holder is null then 'offline'
    when heartbeat_worker_id is distinct from active_lease_holder
      or heartbeat_release is distinct from active_lease_release
      or heartbeat_component not in ('operations_v2', 'trading_cycle')
      or heartbeat_checkpoint not in ('cycle_completed', 'operations_completed')
      or nullif(heartbeat_completed_at, '') is null then 'degraded'
    when active_lease_expires_at <= now_value then 'offline'
    when heartbeat_time < now_value - interval '120 seconds' then 'stale'
    else 'fresh'
  end;
  contract_state := case
    when environment_value = 'paper' then 'fresh'
    when control_row.provider_contract_version is null
      or control_row.provider_openapi_sha256 is null then 'contract_error'
    when exists (
      select 1 from private.provider_contract_registry as contract
      where contract.provider = 'toss'
        and contract.qualification_environment = 'contract_test'
        and contract.execution_transport = 'local_contract_simulator'
        and contract.contract_version = control_row.provider_contract_version
        and contract.openapi_sha256 = control_row.provider_openapi_sha256
        and contract.status = 'approved'
        and contract.effective_from <= now_value
        and contract.effective_until > now_value
    ) then 'fresh'
    else 'contract_error'
  end;
  runtime_state := case
    when control_row.account_id is null then 'contract_error'
    when contract_state = 'contract_error' then 'contract_error'
    when worker_state in ('offline', 'stale') then worker_state
    when worker_state = 'degraded'
      or exists (select 1 from private.incidents where status <> 'resolved')
      or exists (select 1 from private.reconciliation_breaks where state <> 'resolved')
      then 'degraded'
    else 'fresh'
  end;

  select coalesce(jsonb_agg(jsonb_build_object(
    'step_up_grant_id', grant_row.id,
    'command_hash', grant_row.command_sha256,
    'step_up_grant_issued_at', grant_row.issued_at,
    'step_up_grant_expires_at', grant_row.expires_at,
    'step_up_grant_one_time', true,
    'step_up_grant_consumed_at', null,
    'bound_action', grant_row.bound_action,
    'bound_command_type', grant_row.bound_command_type
  ) order by grant_row.expires_at), '[]'::jsonb)
  into grant_rows
  from private.step_up_grants as grant_row
  where grant_row.user_id = actor
    and grant_row.consumed_at is null
    and grant_row.expires_at > now_value
    and grant_row.bound_action in ('request', 'review')
    and grant_row.bound_command_type in (
      'pause_paper', 'resume_paper', 'activate_paper_strategy',
      'start_contract_test', 'apply_risk_policy_version'
    );

  select jsonb_build_object(
    'schema_version', 1,
    'qualification_id', qualification.id,
    'environment', qualification.environment,
    'status', qualification.status,
    'release_sha', qualification.release_sha,
    'ledger_checkpoint', qualification.ledger_checkpoint,
    'dataset_version', qualification.dataset_version,
    'strategy_version_id', qualification.strategy_version_id,
    'risk_policy_version_id', qualification.risk_policy_version_id,
    'valid_from', qualification.valid_from,
    'valid_until', qualification.valid_until,
    'g1', jsonb_build_object(
      'status', qualification.g1_status,
      'checked_at', qualification.g1_checked_at,
      'evidence_ref', qualification.g1_evidence_id::text
    ),
    'g2', jsonb_build_object(
      'status', qualification.g2_status,
      'checked_at', qualification.g2_checked_at,
      'evidence_ref', qualification.g2_evidence_id::text
    )
  ) into qualification_value
  from private.qualifications as qualification
  where qualification.environment = environment_value
  order by
    (qualification.status = 'qualified' and qualification.valid_until > now_value) desc,
    qualification.valid_until desc,
    qualification.created_at desc
  limit 1;

  select coalesce(jsonb_agg(
    private.operation_command_receipt_v1(command.id)
    order by command.requested_at desc, command.id desc
  ), '[]'::jsonb)
  into commands_value
  from (
    select * from private.operation_commands
    where private.external_command_type_v1(command_type) is not null
    order by requested_at desc, id desc
    limit 100
  ) as command;
  select coalesce(jsonb_agg(
    private.operation_command_receipt_v1(command.id)
    order by command.requested_at, command.id
  ), '[]'::jsonb)
  into pending_reviews_value
  from private.operation_commands as command
  where command.state = 'requested'
    and private.external_command_type_v1(command.command_type) is not null;
  select coalesce(jsonb_agg(
    private.command_review_v1(review.id)
    order by review.reviewed_at desc, review.id desc
  ), '[]'::jsonb)
  into reviews_value
  from (
    select review.*
    from private.operation_command_reviews as review
    join private.operation_commands as command on command.id = review.command_id
    where private.external_command_type_v1(command.command_type) is not null
    order by review.reviewed_at desc, review.id desc
    limit 100
  ) as review;

  select coalesce(jsonb_agg(jsonb_build_object(
    'schema_version', 1,
    'request_id', change_request.id,
    'subject_user_id', change_request.subject_user_id,
    'requested_role', change_request.requested_role,
    'change_type', change_request.change_type,
    'state', case change_request.state
      when 'approved' then 'applied'
      else change_request.state
    end,
    'requested_by', private.actor_ref_v1(change_request.requester_user_id),
    'reviewed_by', private.actor_ref_v1(change_request.reviewer_user_id),
    'requested_at', change_request.requested_at,
    'reviewed_at', change_request.reviewed_at,
    'applied_at', change_request.applied_at,
    'expires_at', change_request.expires_at,
    'reason_code', change_request.reason_code
  ) order by change_request.requested_at desc, change_request.id desc), '[]'::jsonb)
  into access_changes_value
  from (
    select * from private.access_change_requests
    order by requested_at desc, id desc
    limit 100
  ) as change_request;

  select coalesce(jsonb_agg(jsonb_build_object(
    'schema_version', 1,
    'incident_id', incident.id,
    'severity', case incident.severity
      when 'critical' then 'sev1' when 'high' then 'sev2' else 'sev3' end,
    'status', incident.status,
    'kind', case
      when incident.incident_type like '%reconcil%' or incident.incident_type like '%execution%'
        then 'reconciliation_required'
      when incident.incident_type like '%access%' then 'access_violation'
      when incident.incident_type like '%worker%' then 'worker_offline'
      when incident.incident_type like '%timeout%' then 'command_timeout'
      when incident.incident_type like '%stale%' then 'stale_data'
      else 'contract_error'
    end,
    'title', left(replace(incident.incident_type, '_', ' '), 160),
    'summary', left(incident.summary_code, 500),
    'detected_at', incident.opened_at,
    'acknowledged_at', incident.acknowledged_at,
    'resolved_at', incident.resolved_at,
    'owner', case when incident.acknowledged_by is null then null
      else private.actor_ref_v1(incident.acknowledged_by) end,
    'evidence_refs', jsonb_build_array('incident:' || incident.id::text),
    'ack_due_at', incident.ack_due_at,
    'escalation_status', incident.escalation_status
  ) order by incident.opened_at desc, incident.id desc), '[]'::jsonb)
  into incidents_value
  from (
    select * from private.incidents
    order by opened_at desc, id desc
    limit 100
  ) as incident;

  select coalesce(jsonb_agg(jsonb_build_object(
    'schema_version', 1,
    'order_id', intent.id,
    'command_id', null,
    'environment', intent.environment,
    'symbol', intent.symbol,
    'side', intent.side,
    'order_type', 'limit',
    'requested_quantity', intent.quantity,
    'filled_quantity', coalesce(observation.cumulative_quantity, 0),
    'requested_price_krw', intent.limit_price_krw,
    'average_fill_price_krw', case
      when coalesce(observation.cumulative_quantity, 0) = 0 then null
      else observation.cumulative_gross_krw::numeric / observation.cumulative_quantity
    end,
    'status', case
      when observation.event_type = 'partial_filled' then 'partial_filled'
      when observation.event_type = 'filled' then 'filled'
      when observation.event_type = 'canceled' then 'canceled'
      when observation.event_type = 'rejected' then 'rejected'
      when observation.event_type = 'expired' then 'expired'
      when observation.event_type = 'failed_pre_dispatch' then 'failed'
      when observation.event_type = 'unknown_requires_manual_check' then 'reconciliation_required'
      when observation.id is null then 'proposed'
      when observation.event_type = 'open' and intent.environment = 'paper' then 'paper_simulated'
      when observation.event_type = 'open' and intent.environment = 'contract_test' then 'contract_simulated'
      else 'reconciliation_required'
    end,
    'strategy_version_id', intent.strategy_version_id::uuid,
    'risk_policy_version_id', qualification.risk_policy_version_id,
    'idempotency_key', intent.semantic_key_sha256,
    'created_at', intent.created_at,
    'updated_at', coalesce(observation.observed_at, intent.created_at)
  ) order by intent.created_at desc, intent.id desc), '[]'::jsonb)
  into orders_value
  from private.order_intents as intent
  join private.execution_controls as control on control.account_id = intent.account_id
  join lateral (
    select q.risk_policy_version_id
    from private.qualifications as q
    where q.environment = intent.environment
      and q.strategy_version_id::text = intent.strategy_version_id
      and q.risk_policy_sha256 = intent.risk_policy_sha256
      and q.release_sha = intent.release_sha
    order by q.created_at desc
    limit 1
  ) as qualification on true
  left join lateral (
    select * from private.execution_observations as latest
    where latest.intent_id = intent.id
    order by latest.sequence desc
    limit 1
  ) as observation on true
  where intent.strategy_version_id
    ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$';

  select coalesce(jsonb_agg(jsonb_build_object(
    'schema_version', 1,
    'position_id', md5(position.account_id || ':' || position.symbol)::uuid,
    'environment', account.environment,
    'symbol', position.symbol,
    'quantity', position.quantity,
    'average_price_krw', position.average_cost_krw,
    'market_price_krw', null,
    'market_value_krw', null,
    'unrealized_pnl_krw', null,
    'as_of', position.projected_at,
    'market_data_status', 'unavailable',
    'market_data_source', null,
    'market_data_as_of', null
  ) order by position.symbol), '[]'::jsonb)
  into positions_value
  from private.position_projection as position
  join private.trading_accounts as account using (account_id)
  where account.environment = environment_value and position.quantity > 0;

  if 'view_audit' = any(permissions) then
    select coalesce(jsonb_agg(jsonb_build_object(
      'schema_version', 1,
      'audit_id', event.id,
      'occurred_at', event.occurred_at,
      'actor', case when event.actor_type = 'human'
        then private.actor_ref_v1(event.actor_user_id) else null end,
      'action', case
        when event.action in ('command_requested', 'operation_command_requested') then 'command_requested'
        when event.action like '%review%' or event.action like '%approved%' or event.action like '%rejected%' then 'command_reviewed'
        when event.action like '%claimed%' then 'command_claimed'
        when event.action like '%applied%' then 'command_applied'
        when event.action like '%failed%' then 'command_failed'
        when event.action = 'incident_acknowledged' then 'incident_acknowledged'
        when event.action = 'incident_resolved' then 'incident_resolved'
        when event.action like '%reconciliation%resolved%' then 'reconciliation_resolved'
        when event.action like '%reconciliation%' or event.action like '%quarantined%' then 'reconciliation_opened'
        else null
      end,
      'resource_type', case
        when event.resource_type like '%command%' then 'command'
        when event.resource_type = 'incident' then 'incident'
        when event.resource_type like '%order%' or event.resource_type like '%execution%' then 'order'
        when event.resource_type like '%position%' then 'position'
        when event.resource_type like '%qualification%' then 'qualification'
        else null
      end,
      'resource_id', event.resource_id::uuid,
      'outcome', case
        when event.action like '%failed%' then 'failed'
        when event.action like '%denied%' then 'denied'
        else 'success'
      end,
      'reason_code', left(coalesce(nullif(event.reason_code, ''), 'recorded'), 120),
      'correlation_id', coalesce(event.correlation_id, event.id)
    ) order by event.occurred_at desc, event.id desc), '[]'::jsonb)
    into audit_value
    from (
      select * from private.audit_events
      where resource_id ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
        and (
          action in ('command_requested', 'operation_command_requested',
            'incident_acknowledged', 'incident_resolved')
          or action like '%review%'
          or action like '%approved%'
          or action like '%rejected%'
          or action like '%claimed%'
          or action like '%applied%'
          or action like '%failed%'
          or action like '%reconciliation%'
          or action like '%quarantined%'
        )
        and (
          resource_type like '%command%'
          or resource_type = 'incident'
          or resource_type like '%order%'
          or resource_type like '%execution%'
          or resource_type like '%position%'
          or resource_type like '%qualification%'
        )
      order by occurred_at desc, id desc
      limit 100
    ) as event;
  end if;

  if 'view_reconciliation' = any(permissions) then
    select coalesce(jsonb_agg(jsonb_build_object(
      'schema_version', 1,
      'case_id', break_row.id,
      'order_id', event.intent_id,
      'environment', intent.environment,
      'status', case break_row.state
        when 'open' then 'open' when 'resolution_requested' then 'investigating'
        else 'resolved' end,
      'reason_code', case
        when break_row.summary_code like '%fill%' then 'fill_mismatch'
        when break_row.summary_code like '%timeout%' then 'ack_timeout'
        when break_row.summary_code like '%operator%' then 'operator_escalation'
        else 'ambiguous_order_state' end,
      'opened_at', break_row.detected_at,
      'updated_at', coalesce(break_row.resolved_at, break_row.detected_at),
      'owner', null,
      'evidence_refs', jsonb_build_array('reconciliation-run:' || break_row.run_id::text),
      'resolution_code', case when break_row.state = 'resolved'
        then 'manual_follow_up' else null end
    ) order by break_row.detected_at desc, break_row.id desc), '[]'::jsonb)
    into reconciliation_value
    from private.reconciliation_breaks as break_row
    join private.order_events as event
      on event.event_summary->>'reconciliation_break_id' = break_row.id::text
    join private.order_intents as intent on intent.id = event.intent_id;
  end if;

  return jsonb_build_object(
    'schema_version', 1,
    'generated_at', now_value,
    'runtime_health', jsonb_build_object(
      'schema_version', 1,
      'environment', environment_value,
      'live_permitted', false,
      'overall_state', runtime_state,
      'as_of', now_value,
      'state_version', coalesce(control_row.control_epoch, 0),
      'worker_release_sha', heartbeat_release,
      'worker_heartbeat_at', heartbeat_time,
      'realtime_connected', false,
      'realtime_last_seen_at', null,
      'execution_enabled', coalesce(control_row.execution_enabled, false),
      'active_strategy_version_id', case
        when control_row.active_strategy_version_id
          ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
          then control_row.active_strategy_version_id::uuid else null end,
      'active_risk_policy_version_id', control_row.active_risk_policy_version_id,
      'execution_policy_version', control_row.execution_policy_version,
      'execution_policy_sha256', control_row.execution_policy_sha256,
      'risk_policy_sha256', control_row.risk_policy_sha256,
      'provider_contract_version', control_row.provider_contract_version,
      'provider_openapi_sha256', control_row.provider_openapi_sha256,
      'components', jsonb_build_array(
        jsonb_build_object(
          'schema_version', 1, 'component', 'control_plane',
          'state', case when control_row.account_id is null then 'contract_error' else 'fresh' end,
          'observed_at', now_value,
          'detail_code', case when control_row.account_id is null
            then 'execution_control_missing' else 'control_projection_loaded' end
        ),
        jsonb_build_object(
          'schema_version', 1, 'component', 'worker',
          'state', worker_state,
          'observed_at', coalesce(heartbeat_time, now_value),
          'detail_code', case worker_state
            when 'fresh' then 'heartbeat_fresh'
            when 'stale' then 'heartbeat_stale'
            when 'offline' then 'heartbeat_missing'
            else 'heartbeat_degraded' end
        ),
        jsonb_build_object(
          'schema_version', 1,
          'component', 'realtime',
          'state', 'offline',
          'observed_at', now_value,
          'detail_code', 'canonical_realtime_feed_not_configured'
        ),
        jsonb_build_object(
          'schema_version', 1,
          'component', case when environment_value = 'contract_test'
            then 'contract_test' else 'market_data' end,
          'state', contract_state,
          'observed_at', now_value,
          'detail_code', case when contract_state = 'fresh'
            then 'evidence_boundary_satisfied' else 'provider_contract_pin_invalid' end
        )
      ),
      'freshness_policy', jsonb_build_object(
        'snapshot_max_age_seconds', 60,
        'worker_heartbeat_max_age_seconds', 120,
        'realtime_max_age_seconds', 120
      )
    ),
    'access', jsonb_build_object(
      'schema_version', 1,
      'signed_in', true,
      'actor', private.actor_ref_v1(actor),
      'session_state', 'active',
      'assurance_level', private.current_aal(),
      'active_step_up_grants', grant_rows,
      'permissions', to_jsonb(permissions)
    ),
    'qualification', qualification_value,
    'commands', commands_value,
    'pending_reviews', pending_reviews_value,
    'reviews', reviews_value,
    'access_changes', access_changes_value,
    'incidents', incidents_value,
    'orders', orders_value,
    'positions', positions_value,
    'audit_events', audit_value,
    'reconciliation_cases', reconciliation_value
  );
end;
$$;

revoke execute on function
  private.get_desktop_operations_snapshot_v1_impl()
from public, anon, authenticated, authenticator, service_role;
grant execute on function
  private.get_desktop_operations_snapshot_v1_impl()
to authenticated;

revoke execute on function
  api.get_desktop_operations_snapshot_v1()
from public, anon, authenticated, authenticator, service_role;
grant execute on function
  api.get_desktop_operations_snapshot_v1()
to authenticated;

do $$
declare
  implementation_contract_count integer;
  wrapper_contract_count integer;
  authenticated_execute_count integer;
  forbidden_effective_execute_count integer;
  forbidden_owner_count integer;
  unexpected_execute_count integer;
begin
  select count(*) into implementation_contract_count
  from pg_catalog.pg_proc as procedure
  where procedure.oid =
      'private.get_desktop_operations_snapshot_v1_impl()'::regprocedure
    and procedure.prokind = 'f'
    and procedure.provolatile = 'v'
    and procedure.prosecdef
    and procedure.proconfig = array['search_path=""']::text[];

  select count(*) into wrapper_contract_count
  from pg_catalog.pg_proc as procedure
  where procedure.oid =
      'api.get_desktop_operations_snapshot_v1()'::regprocedure
    and procedure.prokind = 'f'
    and procedure.provolatile = 'v'
    and not procedure.prosecdef
    and procedure.proconfig = array['search_path=""']::text[]
    and pg_catalog.btrim(
      pg_catalog.regexp_replace(procedure.prosrc, '\s+', ' ', 'g')
    ) = 'select private.get_desktop_operations_snapshot_v1_impl();';

  select count(*) into authenticated_execute_count
  from pg_catalog.pg_proc as procedure
  where procedure.oid in (
      'private.get_desktop_operations_snapshot_v1_impl()'::regprocedure,
      'api.get_desktop_operations_snapshot_v1()'::regprocedure
    )
    and pg_catalog.has_function_privilege(
      'authenticated', procedure.oid, 'EXECUTE'
    );

  select count(*) into forbidden_effective_execute_count
  from pg_catalog.pg_proc as procedure
  cross join (
    values ('anon'), ('authenticator'), ('service_role')
  ) as forbidden(role_name)
  where procedure.oid in (
      'private.get_desktop_operations_snapshot_v1_impl()'::regprocedure,
      'api.get_desktop_operations_snapshot_v1()'::regprocedure
    )
    and pg_catalog.has_function_privilege(
      forbidden.role_name, procedure.oid, 'EXECUTE'
    );

  select count(*) into unexpected_execute_count
  from pg_catalog.pg_proc as procedure
  cross join lateral pg_catalog.aclexplode(
    coalesce(procedure.proacl, pg_catalog.acldefault('f', procedure.proowner))
  ) as acl
  left join pg_catalog.pg_roles as grantee on grantee.oid = acl.grantee
  where procedure.oid in (
      'private.get_desktop_operations_snapshot_v1_impl()'::regprocedure,
      'api.get_desktop_operations_snapshot_v1()'::regprocedure
    )
    and acl.privilege_type = 'EXECUTE'
    and (
      acl.grantee = 0
      or (
        acl.grantee <> procedure.proowner
        and grantee.rolname <> 'authenticated'
      )
    );

  select count(*) into forbidden_owner_count
  from pg_catalog.pg_proc as procedure
  join pg_catalog.pg_roles as owner_role
    on owner_role.oid = procedure.proowner
  where procedure.oid in (
      'private.get_desktop_operations_snapshot_v1_impl()'::regprocedure,
      'api.get_desktop_operations_snapshot_v1()'::regprocedure
    )
    and owner_role.rolname in (
      'anon', 'authenticated', 'authenticator', 'service_role'
    );

  if implementation_contract_count <> 1
     or wrapper_contract_count <> 1
     or authenticated_execute_count <> 2
     or forbidden_effective_execute_count <> 0
     or forbidden_owner_count <> 0
     or unexpected_execute_count <> 0 then
    raise exception
      'desktop_operations_sensitive_projection_security_contract_failed'
      using errcode = '55000';
  end if;
end;
$$;

commit;
