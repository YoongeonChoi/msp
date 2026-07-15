-- G2 step-up, maker-checker, worker RPC, and Data API access contract.
--
-- api functions are SECURITY INVOKER wrappers. The narrowly scoped definer
-- implementations live only in the non-exposed private schema, have an empty
-- search_path, validate the JWT/service identity, and are individually granted.

create or replace function private.current_jwt_claims()
returns jsonb
language plpgsql
stable
security definer
set search_path = ''
as $$
declare
  claims jsonb;
begin
  begin
    claims := nullif(current_setting('request.jwt.claims', true), '')::jsonb;
  exception when others then
    claims := '{}'::jsonb;
  end;
  return coalesce(claims, '{}'::jsonb);
end;
$$;

create or replace function private.issue_step_up_grant_v1_impl(p_request_payload jsonb)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  result_row record;
  command_hash text;
  action_name text;
  command_type_name text;
begin
  if coalesce(jsonb_typeof(p_request_payload), '') <> 'object'
     or coalesce((p_request_payload->>'schema_version')::integer, -1) <> 1 then
    raise exception 'step_up_request_schema_version_invalid' using errcode = '22023';
  end if;
  command_hash := p_request_payload->>'command_hash';
  action_name := p_request_payload->>'bound_action';
  command_type_name := p_request_payload->>'bound_command_type';
  if coalesce(action_name, '') not in ('request', 'review')
     or coalesce(command_type_name, '') not in (
       'pause_paper', 'resume_paper', 'activate_paper_strategy',
       'start_contract_test', 'apply_risk_policy_version'
     ) then
    raise exception 'step_up_binding_invalid' using errcode = '22023';
  end if;
  select * into result_row from private.issue_step_up_grant(command_hash);
  update private.step_up_grants
  set bound_action = action_name, bound_command_type = command_type_name
  where id = result_row.grant_id;
  return jsonb_build_object(
    'schema_version', 1,
    'step_up_grant_id', result_row.grant_id,
    'command_hash', command_hash,
    'step_up_grant_issued_at', (
      select issued_at from private.step_up_grants where id = result_row.grant_id
    ),
    'step_up_grant_expires_at', result_row.expires_at,
    'step_up_grant_one_time', true,
    'step_up_grant_consumed_at', null,
    'bound_action', action_name,
    'bound_command_type', command_type_name
  );
exception when invalid_text_representation then
  raise exception 'step_up_request_schema_version_invalid' using errcode = '22023';
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
  control_epoch_value bigint := 0;
  heartbeat_time timestamptz;
  heartbeat_release text;
  grant_rows jsonb;
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
  if roles && array['operator']::text[] then
    permissions := permissions || array['request_command', 'acknowledge_incident'];
  end if;
  if roles && array['risk_approver', 'strategy_reviewer', 'release_manager']::text[] then
    permissions := permissions || array['review_command'];
  end if;
  if roles && array['risk_approver']::text[] then
    permissions := permissions || array['resolve_incident'];
  end if;
  if roles && array['auditor']::text[] then
    permissions := permissions || array['view_audit', 'view_reconciliation'];
  end if;
  select policy.environment into environment_value
  from private.environment_policy as policy where policy.id = 'singleton';
  select coalesce(max(control_epoch), 0) into control_epoch_value
  from private.execution_controls;
  select
    heartbeat.created_at,
    case
      when heartbeat.details->>'release_sha' ~ '^[0-9a-f]{40}([0-9a-f]{24})?$'
        then heartbeat.details->>'release_sha'
      else null
    end
  into heartbeat_time, heartbeat_release
  from public.worker_heartbeats as heartbeat
  order by heartbeat.created_at desc
  limit 1;
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

  return jsonb_build_object(
    'schema_version', 1,
    'generated_at', now_value,
    'runtime_health', jsonb_build_object(
      'schema_version', 1,
      'environment', environment_value,
      'live_permitted', false,
      'overall_state', 'contract_error',
      'as_of', now_value,
      'state_version', control_epoch_value,
      'worker_release_sha', heartbeat_release,
      'worker_heartbeat_at', heartbeat_time,
      'components', jsonb_build_array(jsonb_build_object(
        'schema_version', 1,
        'component', 'control_plane',
        'state', 'contract_error',
        'observed_at', now_value,
        'detail_code', 'canonical_mutation_projection_not_activated'
      )),
      'freshness_policy', jsonb_build_object(
        'snapshot_max_age_seconds', 60,
        'worker_heartbeat_max_age_seconds', 120,
        'realtime_max_age_seconds', 120
      )
    ),
    'access', jsonb_build_object(
      'schema_version', 1,
      'signed_in', true,
      'actor', jsonb_build_object(
        'actor_id', actor,
        'display_name', 'user-' || left(actor::text, 8),
        'roles', to_jsonb(roles)
      ),
      'session_state', 'active',
      'assurance_level', private.current_aal(),
      'active_step_up_grants', grant_rows,
      'permissions', to_jsonb(permissions)
    ),
    'qualification', null,
    'commands', jsonb_build_array(),
    'pending_reviews', jsonb_build_array(),
    'reviews', jsonb_build_array(),
    'incidents', jsonb_build_array(),
    'orders', jsonb_build_array(),
    'positions', jsonb_build_array(),
    'audit_events', jsonb_build_array(),
    'reconciliation_cases', jsonb_build_array()
  );
end;
$$;

create or replace function private.record_risk_result_impl(
  p_risk_result_id uuid,
  p_decision_id uuid,
  p_account_id text,
  p_environment text,
  p_strategy_version_id text,
  p_risk_policy_sha256 text,
  p_control_epoch bigint,
  p_allowed boolean,
  p_reason_codes text[],
  p_result_sha256 text,
  p_evaluated_at timestamptz,
  p_expires_at timestamptz,
  p_release_sha text,
  p_holder_id text,
  p_fencing_token bigint
)
returns table (risk_result_id uuid, inserted boolean)
language plpgsql
security definer
set search_path = ''
as $$
declare
  existing_row private.risk_results%rowtype;
begin
  perform private.require_service_role();
  if p_environment not in ('paper', 'contract_test')
     or p_risk_policy_sha256 !~ '^[0-9a-f]{64}$'
     or p_result_sha256 !~ '^[0-9a-f]{64}$'
     or p_release_sha !~ '^[0-9a-f]{40}([0-9a-f]{24})?$'
     or p_expires_at <= p_evaluated_at then
    raise exception 'risk_result_payload_invalid' using errcode = '22023';
  end if;
  select * into existing_row from private.risk_results where id = p_risk_result_id;
  if found then
    if existing_row.result_sha256 <> p_result_sha256 then
      raise exception 'risk_result_idempotency_conflict' using errcode = '23505';
    end if;
    return query select existing_row.id, false;
    return;
  end if;
  if not exists (
    select 1 from private.worker_leases
    where account_id = p_account_id
      and holder_id = p_holder_id
      and fencing_token = p_fencing_token
      and expires_at > clock_timestamp()
      and release_sha = p_release_sha
  ) then
    raise exception 'worker_fencing_token_stale' using errcode = '40001';
  end if;
  if not exists (
    select 1 from private.execution_controls
    where account_id = p_account_id
      and environment = p_environment
      and control_epoch = p_control_epoch
      and active_strategy_version_id = p_strategy_version_id
      and risk_policy_sha256 = p_risk_policy_sha256
  ) then
    raise exception 'risk_result_control_snapshot_mismatch' using errcode = '40001';
  end if;
  insert into private.risk_results (
    id, decision_id, account_id, environment, strategy_version_id,
    risk_policy_sha256, control_epoch, allowed, reason_codes,
    result_sha256, evaluated_at, expires_at, release_sha
  ) values (
    p_risk_result_id, p_decision_id, p_account_id, p_environment,
    p_strategy_version_id, p_risk_policy_sha256, p_control_epoch, p_allowed,
    coalesce(p_reason_codes, array[]::text[]), p_result_sha256,
    p_evaluated_at, p_expires_at, p_release_sha
  );
  return query select p_risk_result_id, true;
end;
$$;

create or replace function private.utc_iso8601(p_value timestamptz)
returns text
language sql
immutable
set search_path = ''
as $$
  select case
    when extract(microseconds from p_value at time zone 'UTC')::bigint % 1000000 = 0
      then to_char(p_value at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS') || '+00:00'
    else to_char(p_value at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US') || '+00:00'
  end;
$$;

create or replace function private.compute_order_semantic_key(
  p_account_id text,
  p_environment text,
  p_strategy_version_id text,
  p_symbol text,
  p_side text,
  p_signal_valid_from timestamptz,
  p_signal_valid_until timestamptz,
  p_execution_policy_version text
)
returns text
language sql
immutable
set search_path = ''
as $$
  select pg_catalog.encode(
    public.digest(
      pg_catalog.convert_to(
        '{"account_id":' || to_jsonb(p_account_id)::text
        || ',"environment":' || to_jsonb(p_environment)::text
        || ',"execution_policy_version":' || to_jsonb(p_execution_policy_version)::text
        || ',"side":' || to_jsonb(p_side)::text
        || ',"signal_valid_from":' || to_jsonb(private.utc_iso8601(p_signal_valid_from))::text
        || ',"signal_valid_until":' || to_jsonb(private.utc_iso8601(p_signal_valid_until))::text
        || ',"strategy_version_id":' || to_jsonb(p_strategy_version_id)::text
        || ',"symbol":' || to_jsonb(p_symbol)::text || '}',
        'UTF8'
      ),
      'sha256'
    ),
    'hex'
  );
$$;

create or replace function private.acquire_worker_lease_impl(
  p_account_id text,
  p_holder_id text,
  p_now timestamptz,
  p_ttl_seconds integer,
  p_release_sha text
)
returns table (
  account_id text,
  holder_id text,
  fencing_token bigint,
  acquired_at timestamptz,
  expires_at timestamptz
)
language plpgsql
security definer
set search_path = ''
as $$
declare
  lease_row private.worker_leases%rowtype;
begin
  perform private.require_service_role();
  if nullif(btrim(p_holder_id), '') is null or length(p_holder_id) > 160 then
    raise exception 'lease_holder_invalid' using errcode = '22023';
  end if;
  if p_ttl_seconds < 5 or p_ttl_seconds > 300 then
    raise exception 'lease_ttl_out_of_range' using errcode = '22023';
  end if;
  if p_now < clock_timestamp() - interval '5 minutes'
     or p_now > clock_timestamp() + interval '30 seconds' then
    raise exception 'lease_clock_out_of_range' using errcode = '22023';
  end if;
  if p_release_sha !~ '^[0-9a-f]{40}([0-9a-f]{24})?$' then
    raise exception 'release_sha_invalid' using errcode = '22023';
  end if;
  if not exists (
    select 1 from private.trading_accounts
    where private.trading_accounts.account_id = p_account_id and state = 'open'
  ) then
    raise exception 'open_trading_account_required' using errcode = '23514';
  end if;

  insert into private.worker_leases as current_lease (
    account_id,
    holder_id,
    fencing_token,
    acquired_at,
    renewed_at,
    expires_at,
    release_sha
  ) values (
    p_account_id,
    p_holder_id,
    1,
    p_now,
    p_now,
    p_now + make_interval(secs => p_ttl_seconds),
    p_release_sha
  )
  on conflict on constraint worker_leases_pkey do update
  set holder_id = excluded.holder_id,
      fencing_token = current_lease.fencing_token + 1,
      acquired_at = excluded.acquired_at,
      renewed_at = excluded.renewed_at,
      expires_at = excluded.expires_at,
      release_sha = excluded.release_sha
  where current_lease.expires_at <= p_now
     or current_lease.holder_id = p_holder_id
  returning * into lease_row;
  if not found then
    raise exception 'worker_lease_held_by_another_instance' using errcode = '55P03';
  end if;

  perform private.write_audit_event(
    'worker', null, null, 'trading_worker', p_holder_id, p_release_sha,
    'worker_lease_acquired', 'worker_lease', p_account_id,
    gen_random_uuid(), null, 'lease_acquired', null,
    array['holder_id', 'fencing_token', 'expires_at'], null, null, null
  );
  return query
  select
    lease_row.account_id,
    lease_row.holder_id,
    lease_row.fencing_token,
    lease_row.acquired_at,
    lease_row.expires_at;
end;
$$;

create or replace function private.renew_worker_lease_impl(
  p_account_id text,
  p_holder_id text,
  p_fencing_token bigint,
  p_now timestamptz,
  p_ttl_seconds integer,
  p_release_sha text
)
returns table (
  account_id text,
  holder_id text,
  fencing_token bigint,
  acquired_at timestamptz,
  expires_at timestamptz
)
language plpgsql
security definer
set search_path = ''
as $$
declare
  lease_row private.worker_leases%rowtype;
begin
  perform private.require_service_role();
  if p_ttl_seconds < 5 or p_ttl_seconds > 300 then
    raise exception 'lease_ttl_out_of_range' using errcode = '22023';
  end if;
  if p_now < clock_timestamp() - interval '5 minutes'
     or p_now > clock_timestamp() + interval '30 seconds' then
    raise exception 'lease_clock_out_of_range' using errcode = '22023';
  end if;
  update private.worker_leases
  set renewed_at = p_now,
      expires_at = p_now + make_interval(secs => p_ttl_seconds),
      release_sha = p_release_sha
  where private.worker_leases.account_id = p_account_id
    and private.worker_leases.holder_id = p_holder_id
    and private.worker_leases.fencing_token = p_fencing_token
    and private.worker_leases.expires_at > p_now
    and private.worker_leases.release_sha = p_release_sha
  returning * into lease_row;
  if not found then
    raise exception 'worker_lease_stale_or_expired' using errcode = '40001';
  end if;
  return query
  select
    lease_row.account_id,
    lease_row.holder_id,
    lease_row.fencing_token,
    lease_row.acquired_at,
    lease_row.expires_at;
end;
$$;

create or replace function private.reserve_order_intent_impl(
  p_intent_id uuid,
  p_semantic_key text,
  p_account_id text,
  p_environment text,
  p_strategy_version_id text,
  p_decision_id uuid,
  p_decision_feature_sha256 text,
  p_risk_result_id uuid,
  p_risk_allowed boolean,
  p_risk_reason_codes text[],
  p_risk_evaluated_at timestamptz,
  p_risk_expires_at timestamptz,
  p_symbol text,
  p_side text,
  p_quantity bigint,
  p_limit_price_krw bigint,
  p_decision_at timestamptz,
  p_signal_valid_from timestamptz,
  p_signal_valid_until timestamptz,
  p_execution_policy_version text,
  p_cost_schedule_version text,
  p_cost_schedule_evidence_sha256 text,
  p_cash_commitment_krw bigint,
  p_eligible_at timestamptz,
  p_expires_at timestamptz,
  p_gate_epoch bigint,
  p_holder_id text,
  p_fencing_token bigint,
  p_release_sha text
)
returns table (
  reserved boolean,
  intent_id uuid,
  reservation_id uuid,
  reason_code text
)
language plpgsql
security definer
set search_path = ''
as $$
declare
  current_timestamp_value timestamptz := clock_timestamp();
  account_row private.trading_accounts%rowtype;
  control_row private.execution_controls%rowtype;
  existing_intent private.order_intents%rowtype;
  existing_reservation private.order_reservations%rowtype;
  new_reservation_id uuid;
  reservation_hash text;
  reserve_cash bigint := 0;
  reserve_quantity bigint := 0;
  reserve_cost_rate numeric(18,12);
  cost_schedule_id uuid;
  expected_semantic_key text;
  risk_row private.risk_results%rowtype;
  decision_row private.execution_decisions%rowtype;
  decision_digest text;
  risk_digest text;
  position_quantity_snapshot_value bigint;
  position_average_cost_value numeric(24,4);
  position_total_cost_value bigint;
  position_projection_version_value bigint;
  position_cost_basis_hash text;
begin
  perform private.require_service_role();
  if p_environment not in ('paper', 'contract_test') then
    raise exception 'unsupported_execution_environment' using errcode = '22023';
  end if;
  if p_semantic_key !~ '^[0-9a-f]{64}$'
     or p_decision_feature_sha256 !~ '^[0-9a-f]{64}$'
     or p_release_sha !~ '^[0-9a-f]{40}([0-9a-f]{24})?$' then
    raise exception 'execution_identity_digest_invalid' using errcode = '22023';
  end if;
  expected_semantic_key := private.compute_order_semantic_key(
    p_account_id,
    p_environment,
    p_strategy_version_id,
    p_symbol,
    p_side,
    p_signal_valid_from,
    p_signal_valid_until,
    p_execution_policy_version
  );
  if p_semantic_key <> expected_semantic_key then
    raise exception 'semantic_key_does_not_match_canonical_payload' using errcode = '23514';
  end if;
  if p_eligible_at <> date_trunc('minute', p_decision_at) + interval '1 minute' then
    raise exception 'execution_must_start_on_next_full_minute' using errcode = '23514';
  end if;
  if p_expires_at < p_eligible_at or p_signal_valid_until < p_eligible_at then
    raise exception 'execution_window_invalid' using errcode = '23514';
  end if;
  if p_risk_allowed is not true
     or p_risk_evaluated_at > current_timestamp_value
     or p_risk_expires_at <= current_timestamp_value
     or p_risk_expires_at <= p_risk_evaluated_at then
    raise exception 'fresh_allowed_risk_result_required' using errcode = '23514';
  end if;

  perform pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(p_semantic_key, 0));
  select * into existing_intent
  from private.order_intents
  where account_id = p_account_id
    and environment = p_environment
    and semantic_key_sha256 = p_semantic_key;
  if found then
    if existing_intent.strategy_version_id <> p_strategy_version_id
       or existing_intent.decision_id <> p_decision_id
       or existing_intent.risk_result_id <> p_risk_result_id
       or existing_intent.symbol <> p_symbol
       or existing_intent.side <> p_side
       or existing_intent.quantity <> p_quantity
       or existing_intent.limit_price_krw <> p_limit_price_krw
       or existing_intent.execution_policy_version <> p_execution_policy_version
       or existing_intent.cost_schedule_version <> p_cost_schedule_version
       or existing_intent.cost_schedule_evidence_sha256 <> p_cost_schedule_evidence_sha256
       or existing_intent.cash_commitment_krw <> p_cash_commitment_krw
       or not exists (
         select 1
         from private.execution_decisions as decision
         where decision.id = p_decision_id
           and decision.account_id = p_account_id
           and decision.environment = p_environment
           and decision.strategy_version_id = p_strategy_version_id
           and decision.symbol = p_symbol
           and decision.action = p_side
           and decision.decision_at = p_decision_at
           and decision.signal_valid_from = p_signal_valid_from
           and decision.signal_valid_until = p_signal_valid_until
           and decision.feature_snapshot_sha256 = p_decision_feature_sha256
           and decision.release_sha = p_release_sha
       )
       or not exists (
         select 1
         from private.risk_results as risk
         where risk.id = p_risk_result_id
           and risk.decision_id = p_decision_id
           and risk.allowed = p_risk_allowed
           and risk.reason_codes = coalesce(p_risk_reason_codes, array[]::text[])
           and risk.evaluated_at = p_risk_evaluated_at
           and risk.expires_at = p_risk_expires_at
           and risk.release_sha = p_release_sha
       ) then
      raise exception 'semantic_reservation_payload_conflict' using errcode = '23505';
    end if;
    select * into existing_reservation
    from private.order_reservations where private.order_reservations.intent_id = existing_intent.id;
    return query
    select false, existing_intent.id, existing_reservation.id, 'duplicate_semantic_intent'::text;
    return;
  end if;

  select * into account_row
  from private.trading_accounts
  where account_id = p_account_id
  for share;
  if not found or account_row.state <> 'open' or account_row.environment <> p_environment then
    raise exception 'open_matching_trading_account_required' using errcode = '23514';
  end if;
  if not exists (
    select 1 from private.environment_policy
    where id = 'singleton'
      and environment = p_environment
      and production_live_enabled is false
      and production_order_credentials_present is false
  ) then
    raise exception 'execution_environment_not_authorized' using errcode = '23514';
  end if;
  select * into control_row
  from private.execution_controls
  where account_id = p_account_id
  for share;
  if not found
     or control_row.execution_enabled is not true
     or control_row.environment <> p_environment
     or control_row.control_epoch <> p_gate_epoch
     or control_row.active_strategy_version_id <> p_strategy_version_id
     or control_row.execution_policy_version <> p_execution_policy_version
     or current_timestamp_value < control_row.effective_at
     or current_timestamp_value >= control_row.expires_at then
    raise exception 'execution_control_stale_or_disabled' using errcode = '40001';
  end if;
  if not exists (
    select 1 from private.worker_leases
    where account_id = p_account_id
      and holder_id = p_holder_id
      and fencing_token = p_fencing_token
      and expires_at > current_timestamp_value
      and release_sha = p_release_sha
  ) then
    raise exception 'worker_fencing_token_stale' using errcode = '40001';
  end if;

  decision_digest := pg_catalog.encode(
    public.digest(
      pg_catalog.convert_to(
        jsonb_build_object(
          'account_id', p_account_id,
          'environment', p_environment,
          'strategy_version_id', p_strategy_version_id,
          'symbol', p_symbol,
          'action', p_side,
          'decision_at', private.utc_iso8601(p_decision_at),
          'signal_valid_from', private.utc_iso8601(p_signal_valid_from),
          'signal_valid_until', private.utc_iso8601(p_signal_valid_until),
          'feature_snapshot_sha256', p_decision_feature_sha256,
          'release_sha', p_release_sha
        )::text,
        'UTF8'
      ),
      'sha256'
    ),
    'hex'
  );
  insert into private.execution_decisions (
    id, account_id, environment, strategy_version_id, symbol, action,
    decision_at, signal_valid_from, signal_valid_until,
    feature_snapshot_sha256, decision_sha256, release_sha
  ) values (
    p_decision_id, p_account_id, p_environment, p_strategy_version_id,
    p_symbol, p_side, p_decision_at, p_signal_valid_from,
    p_signal_valid_until, p_decision_feature_sha256, decision_digest,
    p_release_sha
  ) on conflict (id) do nothing;
  select * into decision_row
  from private.execution_decisions
  where id = p_decision_id;
  if decision_row.decision_sha256 is distinct from decision_digest then
    raise exception 'decision_evidence_idempotency_conflict' using errcode = '23505';
  end if;

  risk_digest := pg_catalog.encode(
    public.digest(
      pg_catalog.convert_to(
        jsonb_build_object(
          'decision_id', p_decision_id,
          'account_id', p_account_id,
          'environment', p_environment,
          'strategy_version_id', p_strategy_version_id,
          'risk_policy_sha256', control_row.risk_policy_sha256,
          'control_epoch', p_gate_epoch,
          'allowed', p_risk_allowed,
          'reason_codes', to_jsonb(coalesce(p_risk_reason_codes, array[]::text[])),
          'evaluated_at', private.utc_iso8601(p_risk_evaluated_at),
          'expires_at', private.utc_iso8601(p_risk_expires_at),
          'release_sha', p_release_sha
        )::text,
        'UTF8'
      ),
      'sha256'
    ),
    'hex'
  );
  insert into private.risk_results (
    id, decision_id, account_id, environment, strategy_version_id,
    risk_policy_sha256, control_epoch, allowed, reason_codes,
    result_sha256, evaluated_at, expires_at, release_sha
  ) values (
    p_risk_result_id, p_decision_id, p_account_id, p_environment,
    p_strategy_version_id, control_row.risk_policy_sha256, p_gate_epoch,
    p_risk_allowed, coalesce(p_risk_reason_codes, array[]::text[]),
    risk_digest, p_risk_evaluated_at, p_risk_expires_at, p_release_sha
  ) on conflict (id) do nothing;
  select * into risk_row
  from private.risk_results
  where id = p_risk_result_id;
  if risk_row.result_sha256 is distinct from risk_digest
     or risk_row.allowed is not true
     or risk_row.expires_at <= current_timestamp_value then
    raise exception 'risk_result_evidence_idempotency_conflict' using errcode = '23505';
  end if;
  if not exists (
    select 1
    from private.paper_execution_policies as policy
    join private.paper_execution_model_registry as model
      on model.environment = p_environment
     and model.model_version = policy.parameters->>'execution_model_version'
     and model.status = 'approved'
     and model.effective_from <= p_eligible_at
     and model.effective_until >= p_expires_at
    join private.market_calendars as calendar
      on calendar.id = model.market_calendar_id
     and calendar.environment = p_environment
     and calendar.status = 'approved'
     and calendar.calendar_version = policy.parameters->>'market_calendar_version'
     and calendar.calendar_sha256 = policy.parameters->>'market_calendar_sha256'
     and calendar.valid_from <= p_signal_valid_from::date
     and calendar.valid_until >= p_expires_at::date
    where policy.account_id = p_account_id
      and policy.policy_version = p_execution_policy_version
      and policy.policy_sha256 = control_row.execution_policy_sha256
      and policy.status = 'approved'
      and policy.effective_from <= current_timestamp_value
      and policy.effective_until > current_timestamp_value
      and model.tick_size_evidence_sha256 = policy.parameters->>'tick_size_evidence_sha256'
      and model.volume_model_evidence_sha256 = policy.parameters->>'volume_model_evidence_sha256'
      and model.corporate_action_evidence_sha256 = policy.parameters->>'corporate_action_evidence_sha256'
      and (
        select array_agg(key order by key)
        from jsonb_object_keys(policy.parameters) as key
      ) = array[
        'corporate_action_evidence_sha256',
        'execution_model_version',
        'market_calendar_sha256',
        'market_calendar_version',
        'tick_size_evidence_sha256',
        'volume_model_evidence_sha256'
      ]::text[]
      and exists (
        select 1
        from private.market_calendar_sessions as session
        where session.calendar_id = calendar.id
          and session.session_date = p_eligible_at::date
          and session.is_open is true
      )
  ) then
    raise exception 'approved_paper_execution_policy_required' using errcode = '23514';
  end if;
  if p_environment = 'contract_test' and not exists (
    select 1 from private.provider_contract_registry as contract
    where contract.provider = 'toss'
      and contract.qualification_environment = 'contract_test'
      and contract.execution_transport = 'local_contract_simulator'
      and contract.status = 'approved'
      and contract.effective_from <= current_timestamp_value
      and contract.effective_until > current_timestamp_value
      and contract.release_sha = p_release_sha
      and contract.contract_version = control_row.provider_contract_version
      and contract.openapi_sha256 = control_row.provider_openapi_sha256
  ) then
    raise exception 'approved_provider_contract_required' using errcode = '23514';
  end if;

  if p_side = 'buy' then
    select schedule.id, schedule.buy_commission_rate
    into cost_schedule_id, reserve_cost_rate
    from private.execution_cost_schedules as schedule
    join private.control_evidence as evidence on evidence.id = schedule.evidence_id
    where schedule.account_id = p_account_id
      and schedule.schedule_version = p_cost_schedule_version
      and evidence.artifact_sha256 = p_cost_schedule_evidence_sha256
      and schedule.status = 'approved'
      and schedule.effective_from <= p_eligible_at
      and schedule.effective_until >= p_expires_at
    order by schedule.effective_from desc
    limit 1;
    if cost_schedule_id is null then
      raise exception 'approved_execution_cost_schedule_required' using errcode = '23514';
    end if;
    reserve_cash := (p_quantity * p_limit_price_krw)
      + ceil((p_quantity * p_limit_price_krw) * reserve_cost_rate)::bigint;
    if p_cash_commitment_krw <> reserve_cash then
      raise exception 'cash_commitment_does_not_match_approved_cost_schedule' using errcode = '23514';
    end if;
    update private.cash_balance_projection
    set reserved_cash_krw = reserved_cash_krw + reserve_cash,
        projection_version = projection_version + 1,
        projected_at = current_timestamp_value
    where account_id = p_account_id
      and available_cash_krw >= reserve_cash;
    if not found then
      raise exception 'insufficient_available_cash_for_reservation' using errcode = '23514';
    end if;
  elsif p_side = 'sell' then
    select schedule.id into cost_schedule_id
    from private.execution_cost_schedules as schedule
    join private.control_evidence as evidence on evidence.id = schedule.evidence_id
    where schedule.account_id = p_account_id
      and schedule.schedule_version = p_cost_schedule_version
      and evidence.artifact_sha256 = p_cost_schedule_evidence_sha256
      and schedule.status = 'approved'
      and schedule.effective_from <= p_eligible_at
      and schedule.effective_until >= p_expires_at
    order by schedule.effective_from desc
    limit 1;
    if cost_schedule_id is null then
      raise exception 'approved_execution_cost_schedule_required' using errcode = '23514';
    end if;
    if p_cash_commitment_krw <> 0 then
      raise exception 'sell_intent_cash_commitment_must_be_zero' using errcode = '23514';
    end if;
    reserve_quantity := p_quantity;
    select
      position.quantity,
      position.average_cost_krw,
      position.projection_version
    into
      position_quantity_snapshot_value,
      position_average_cost_value,
      position_projection_version_value
    from private.position_projection as position
    where position.account_id = p_account_id
      and position.symbol = p_symbol
      and position.available_quantity >= reserve_quantity
    for update;
    if not found then
      raise exception 'insufficient_available_quantity_for_reservation' using errcode = '23514';
    end if;
    position_total_cost_value := round(
      position_quantity_snapshot_value * position_average_cost_value
    )::bigint;
    position_cost_basis_hash := pg_catalog.encode(
      public.digest(
        pg_catalog.convert_to(
          jsonb_build_object(
            'method', 'moving_weighted_average_v1',
            'account_id', p_account_id,
            'symbol', p_symbol,
            'quantity', position_quantity_snapshot_value,
            'average_cost_krw_4dp', to_char(
              position_average_cost_value,
              'FM99999999999999999999.0000'
            ),
            'total_cost_krw', position_total_cost_value,
            'projection_version', position_projection_version_value
          )::text,
          'UTF8'
        ),
        'sha256'
      ),
      'hex'
    );
    update private.position_projection
    set reserved_quantity = reserved_quantity + reserve_quantity,
        projection_version = projection_version + 1,
        projected_at = current_timestamp_value
    where account_id = p_account_id
      and symbol = p_symbol
      and projection_version = position_projection_version_value
      and available_quantity >= reserve_quantity;
    if not found then
      raise exception 'insufficient_available_quantity_for_reservation' using errcode = '23514';
    end if;
  else
    raise exception 'unsupported_execution_side' using errcode = '22023';
  end if;

  insert into private.order_intents (
    id, semantic_key_sha256, account_id, environment, strategy_version_id,
    decision_id, risk_result_id, correlation_id, symbol, side, quantity, limit_price_krw, decision_at,
    signal_valid_from, signal_valid_until, eligible_at, expires_at,
    execution_policy_version, execution_policy_sha256, cost_schedule_version,
    cost_schedule_evidence_sha256, cash_commitment_krw,
    position_cost_basis_method, position_quantity_snapshot,
    position_average_cost_krw, position_total_cost_krw,
    position_projection_version,
    position_cost_basis_sha256, risk_policy_sha256,
    provider_contract_version, provider_openapi_sha256,
    control_epoch, release_sha
  ) values (
    p_intent_id, p_semantic_key, p_account_id, p_environment, p_strategy_version_id,
    p_decision_id, p_risk_result_id, p_intent_id, p_symbol, p_side, p_quantity,
    p_limit_price_krw, p_decision_at,
    p_signal_valid_from, p_signal_valid_until, p_eligible_at, p_expires_at,
    p_execution_policy_version, control_row.execution_policy_sha256,
    p_cost_schedule_version, p_cost_schedule_evidence_sha256, p_cash_commitment_krw,
    case when p_side = 'sell' then 'moving_weighted_average_v1' else null end,
    position_quantity_snapshot_value,
    position_average_cost_value,
    position_total_cost_value,
    position_projection_version_value,
    position_cost_basis_hash,
    control_row.risk_policy_sha256, control_row.provider_contract_version,
    control_row.provider_openapi_sha256, p_gate_epoch, p_release_sha
  );
  insert into private.execution_reconciliation_state (
    intent_id, priority, state, next_reconcile_at, updated_at
  ) values (
    p_intent_id, 30, 'pending', p_eligible_at, current_timestamp_value
  );
  reservation_hash := pg_catalog.encode(
    public.digest(
      pg_catalog.convert_to(
        concat_ws(
          '|', p_intent_id::text, p_account_id, p_environment, p_holder_id,
          p_fencing_token::text, p_gate_epoch::text, reserve_cash::text,
          reserve_quantity::text
        ),
        'UTF8'
      ),
      'sha256'
    ),
    'hex'
  );
  insert into private.order_reservations (
    intent_id, account_id, environment, lease_holder_id, fencing_token,
    control_epoch, reserved_cash_krw, reserved_quantity, reserved_at,
    expires_at, reservation_sha256
  ) values (
    p_intent_id, p_account_id, p_environment, p_holder_id, p_fencing_token,
    p_gate_epoch, reserve_cash, reserve_quantity, current_timestamp_value,
    p_expires_at, reservation_hash
  ) returning id into new_reservation_id;
  insert into private.reservation_events (
    reservation_id, intent_id, event_sequence, event_type, cash_delta_krw,
    quantity_delta, remaining_cash_krw, remaining_quantity, occurred_at
  ) values (
    new_reservation_id, p_intent_id, 1, 'reserved', reserve_cash,
    reserve_quantity, reserve_cash, reserve_quantity, current_timestamp_value
  );
  insert into private.order_events (
    intent_id, event_key, event_type, correlation_id, event_summary, occurred_at
  ) values (
    p_intent_id,
    'reservation:' || new_reservation_id::text,
    'intent_reserved',
    p_intent_id,
    jsonb_build_object('reservation_id', new_reservation_id),
    current_timestamp_value
  );
  perform private.write_audit_event(
    'worker', null, null, 'trading_worker', p_holder_id, p_release_sha,
    'order_intent_reserved', 'order_reservation', new_reservation_id::text,
    p_intent_id, null, 'reserved', null,
    array['semantic_key_sha256', 'fencing_token', 'control_epoch'],
    null, reservation_hash, null
  );
  return query select true, p_intent_id, new_reservation_id, 'reserved'::text;
end;
$$;

create or replace function private.current_session_binding_sha256()
returns text
language plpgsql
stable
security definer
set search_path = ''
as $$
declare
  claims jsonb := private.current_jwt_claims();
  actor uuid := (select auth.uid());
  session_id text;
begin
  session_id := coalesce(nullif(claims->>'session_id', ''), nullif(claims->>'jti', ''));
  if actor is null or session_id is null then
    raise exception 'bound_auth_session_required' using errcode = '42501';
  end if;
  return pg_catalog.encode(
    public.digest(pg_catalog.convert_to(actor::text || '|' || session_id, 'UTF8'), 'sha256'),
    'hex'
  );
end;
$$;

create or replace function private.require_recent_aal2()
returns timestamptz
language plpgsql
stable
security definer
set search_path = ''
as $$
declare
  claims jsonb := private.current_jwt_claims();
  verified_epoch numeric;
  verified_at timestamptz;
begin
  if private.current_aal() <> 'aal2' then
    raise exception 'aal2_step_up_required' using errcode = '42501';
  end if;
  select max((method->>'timestamp')::numeric)
  into verified_epoch
  from jsonb_array_elements(coalesce(claims->'amr', '[]'::jsonb)) as method
  where lower(coalesce(method->>'method', '')) = 'totp'
    and nullif(method->>'timestamp', '') is not null;
  if verified_epoch is null then
    raise exception 'recent_totp_verification_required' using errcode = '42501';
  end if;
  verified_at := to_timestamp(verified_epoch);
  if verified_at < clock_timestamp() - interval '5 minutes'
     or verified_at > clock_timestamp() + interval '30 seconds' then
    raise exception 'aal2_verification_is_stale' using errcode = '42501';
  end if;
  return verified_at;
exception when invalid_text_representation or numeric_value_out_of_range then
  raise exception 'aal2_verification_time_invalid' using errcode = '42501';
end;
$$;

create or replace function private.compute_command_sha256(p_action text, p_payload jsonb)
returns text
language sql
immutable
security definer
set search_path = ''
as $$
  select pg_catalog.encode(
    public.digest(
      pg_catalog.convert_to(
        p_action || '|' || coalesce(p_payload, '{}'::jsonb)::text,
        'UTF8'
      ),
      'sha256'
    ),
    'hex'
  );
$$;

create or replace function private.issue_step_up_grant(p_command_sha256 text)
returns table (grant_id uuid, expires_at timestamptz)
language plpgsql
security definer
set search_path = ''
as $$
declare
  actor uuid;
  new_id uuid;
  expiry timestamptz;
begin
  actor := private.require_human_roles(
    array[
      'platform_admin', 'operator', 'risk_approver', 'strategy_reviewer',
      'auditor', 'release_manager'
    ],
    false
  );
  perform private.require_recent_aal2();
  if p_command_sha256 !~ '^[0-9a-f]{64}$' then
    raise exception 'command_sha256_invalid' using errcode = '22023';
  end if;
  expiry := clock_timestamp() + interval '5 minutes';
  insert into private.step_up_grants (
    user_id,
    command_sha256,
    bound_action,
    bound_command_type,
    session_binding_sha256,
    expires_at
  ) values (
    actor,
    p_command_sha256,
    'request',
    'legacy_internal',
    private.current_session_binding_sha256(),
    expiry
  ) returning id into new_id;
  return query select new_id, expiry;
end;
$$;

create or replace function private.consume_step_up_grant(
  p_grant_id uuid,
  p_expected_command_sha256 text,
  p_consumed_for text
)
returns uuid
language plpgsql
security definer
set search_path = ''
as $$
declare
  actor uuid := (select auth.uid());
begin
  if actor is null then
    raise exception 'authenticated_user_required' using errcode = '42501';
  end if;
  update private.step_up_grants
  set consumed_at = clock_timestamp(), consumed_for = p_consumed_for
  where id = p_grant_id
    and user_id = actor
    and command_sha256 = p_expected_command_sha256
    and session_binding_sha256 = private.current_session_binding_sha256()
    and consumed_at is null
    and expires_at > clock_timestamp();
  if not found then
    raise exception 'step_up_grant_invalid_expired_or_consumed' using errcode = '42501';
  end if;
  return actor;
end;
$$;

create or replace function private.register_control_evidence_v2(
  p_evidence_type text,
  p_environment text,
  p_artifact_uri text,
  p_artifact_sha256 text,
  p_captured_at timestamptz,
  p_metadata_summary jsonb,
  p_step_up_grant_id uuid
)
returns table (evidence_id uuid, verified_at timestamptz)
language plpgsql
security definer
set search_path = ''
as $$
declare
  expected_hash text;
begin
  expected_hash := private.compute_command_sha256(
    'register_control_evidence',
    jsonb_build_object(
      'artifact_sha256', p_artifact_sha256,
      'artifact_uri', p_artifact_uri,
      'captured_at', p_captured_at,
      'environment', p_environment,
      'evidence_type', p_evidence_type,
      'metadata_summary', coalesce(p_metadata_summary, '{}'::jsonb)
    )
  );
  perform private.consume_step_up_grant(
    p_step_up_grant_id, expected_hash, 'register_control_evidence'
  );
  return query
  select * from private.register_control_evidence(
    p_evidence_type,
    p_environment,
    p_artifact_uri,
    p_artifact_sha256,
    p_captured_at,
    coalesce(p_metadata_summary, '{}'::jsonb)
  );
end;
$$;

create or replace function private.request_operation_command_v2(
  p_command_type text,
  p_requested_change jsonb,
  p_evidence_id uuid,
  p_target_release_sha text,
  p_expires_at timestamptz,
  p_idempotency_key text,
  p_step_up_grant_id uuid
)
returns table (command_id uuid, state text, requested_at timestamptz, expires_at timestamptz)
language plpgsql
security definer
set search_path = ''
as $$
declare
  expected_hash text;
  result_row record;
begin
  expected_hash := private.compute_command_sha256(
    'request_operation_command',
    jsonb_build_object(
      'command_type', p_command_type,
      'evidence_id', p_evidence_id,
      'expires_at', p_expires_at,
      'idempotency_key', p_idempotency_key,
      'requested_change', coalesce(p_requested_change, '{}'::jsonb),
      'target_release_sha', p_target_release_sha
    )
  );
  perform private.consume_step_up_grant(
    p_step_up_grant_id, expected_hash, 'request_operation_command'
  );
  select * into result_row
  from private.request_operation_command(
    p_command_type,
    coalesce(p_requested_change, '{}'::jsonb),
    p_evidence_id,
    p_target_release_sha,
    p_expires_at,
    p_idempotency_key
  );
  insert into private.operation_command_events (
    command_id, event_type, actor_type, actor_user_id, event_summary
  ) values (
    result_row.command_id,
    'requested',
    'human',
    (select auth.uid()),
    jsonb_build_object('state', result_row.state)
  );
  return query
  select result_row.command_id, result_row.state, result_row.requested_at, result_row.expires_at;
end;
$$;

create or replace function private.review_operation_command_v2(
  p_command_id uuid,
  p_approve boolean,
  p_review_reason text,
  p_step_up_grant_id uuid
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
  expected_hash text;
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
    else null
  end;
  if required_role is null then
    raise exception 'operation_command_reviewer_role_missing' using errcode = '23514';
  end if;
  actor := private.require_human_roles(array[required_role], false);
  expected_hash := private.compute_command_sha256(
    'review_operation_command',
    jsonb_build_object(
      'approve', p_approve,
      'command_id', p_command_id,
      'review_reason', p_review_reason
    )
  );
  perform private.consume_step_up_grant(
    p_step_up_grant_id, expected_hash, 'review_operation_command'
  );
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

  insert into private.operation_command_reviews (
    command_id,
    reviewer_user_id,
    reviewer_role,
    decision,
    reason_code,
    step_up_grant_id,
    reviewed_at
  ) values (
    p_command_id,
    actor,
    required_role,
    next_state,
    p_review_reason,
    p_step_up_grant_id,
    review_time
  );
  insert into private.operation_command_events (
    command_id, event_type, actor_type, actor_user_id, event_summary, occurred_at
  ) values (
    p_command_id,
    next_state,
    'human',
    actor,
    jsonb_build_object('reviewer_role', required_role),
    review_time
  );
  perform private.write_audit_event(
    'human', actor, required_role, null, null, command_row.target_release_sha,
    'operation_command_' || next_state, 'operation_command', p_command_id::text,
    gen_random_uuid(), p_command_id, next_state, null,
    array['state', 'reviewer_user_id', 'reviewed_at'], null, null,
    command_row.evidence_id
  );
  return query select p_command_id, next_state, review_time;
end;
$$;

create or replace function private.emergency_stop_v2(
  p_account_id text,
  p_reason_code text,
  p_ticket_ref text
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
  actor := private.require_human_roles(array['operator'], false);
  perform private.require_recent_aal2();
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

create or replace function private.acknowledge_incident_v2(
  p_incident_id uuid
)
returns table (incident_id uuid, status text, acknowledged_at timestamptz)
language plpgsql
security definer
set search_path = ''
as $$
declare
  actor uuid;
  action_time timestamptz := clock_timestamp();
begin
  actor := private.require_human_roles(array['operator'], false);
  perform private.require_recent_aal2();
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

create or replace function private.resolve_incident_v2(
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
  acknowledging_actor uuid;
  action_time timestamptz := clock_timestamp();
begin
  actor := private.require_human_roles(array['risk_approver'], false);
  perform private.require_recent_aal2();
  if nullif(btrim(p_resolution_code), '') is null then
    raise exception 'incident_resolution_required' using errcode = '22023';
  end if;
  select acknowledged_by into acknowledging_actor
  from private.incidents
  where id = p_incident_id and status = 'acknowledged'
  for update;
  if not found then
    raise exception 'incident_not_acknowledged' using errcode = '23514';
  end if;
  if acknowledging_actor = actor then
    raise exception 'incident_resolution_requires_distinct_approver' using errcode = '42501';
  end if;
  update private.incidents
  set status = 'resolved',
      resolved_at = action_time,
      resolved_by = actor,
      resolution_code = p_resolution_code
  where id = p_incident_id;
  perform private.write_audit_event(
    'human', actor, 'risk_approver', null, null, null,
    'incident_resolved', 'incident', p_incident_id::text,
    gen_random_uuid(), null, p_resolution_code, null,
    array['status', 'resolved_at'], null, null, null
  );
  return query select p_incident_id, 'resolved'::text, action_time;
end;
$$;

create or replace function private.mark_dispatch_started_impl(
  p_intent_id uuid,
  p_account_id text,
  p_environment text,
  p_holder_id text,
  p_fencing_token bigint,
  p_gate_epoch bigint,
  p_now timestamptz,
  p_request_sha256 text,
  p_client_order_key text
)
returns table (attempt_id uuid, prepared_at timestamptz, reason_code text)
language plpgsql
security definer
set search_path = ''
as $$
declare
  intent_row private.order_intents%rowtype;
  reservation_row private.order_reservations%rowtype;
  attempt_row private.order_attempts%rowtype;
  broker_name text;
  authorization_time timestamptz := clock_timestamp();
begin
  perform private.require_service_role();
  if p_environment not in ('paper', 'contract_test') then
    raise exception 'unsupported_execution_environment' using errcode = '22023';
  end if;
  if p_request_sha256 !~ '^[0-9a-f]{64}$'
     or nullif(btrim(p_client_order_key), '') is null
     or length(p_client_order_key) > 200
     or p_now is null
     or p_now < authorization_time - interval '5 minutes'
     or p_now > authorization_time + interval '30 seconds' then
    raise exception 'dispatch_identity_invalid' using errcode = '22023';
  end if;
  select * into intent_row
  from private.order_intents
  where id = p_intent_id and account_id = p_account_id and environment = p_environment;
  if not found then
    raise exception 'reserved_intent_not_found' using errcode = 'P0002';
  end if;
  select * into reservation_row
  from private.order_reservations
  where intent_id = p_intent_id;
  if not found then
    raise exception 'order_reservation_not_found' using errcode = 'P0002';
  end if;
  if authorization_time < intent_row.eligible_at
     or authorization_time > intent_row.expires_at then
    raise exception 'dispatch_outside_execution_window' using errcode = '23514';
  end if;
  if p_gate_epoch <> reservation_row.control_epoch
     or p_gate_epoch <> intent_row.control_epoch then
    raise exception 'reservation_fencing_or_epoch_mismatch' using errcode = '40001';
  end if;
  if not exists (
    select 1 from private.worker_leases as lease
    where lease.account_id = p_account_id
      and lease.holder_id = p_holder_id
      and lease.fencing_token = p_fencing_token
      and lease.expires_at > authorization_time
      and lease.release_sha = intent_row.release_sha
  ) then
    raise exception 'worker_fencing_token_stale' using errcode = '40001';
  end if;
  if not exists (
    select 1 from private.execution_controls as control
    where control.account_id = p_account_id
      and control.environment = p_environment
      and control.execution_enabled is true
      and control.control_epoch = p_gate_epoch
      and control.effective_at <= authorization_time
      and control.expires_at > authorization_time
      and control.execution_policy_version = intent_row.execution_policy_version
      and control.execution_policy_sha256 = intent_row.execution_policy_sha256
      and control.risk_policy_sha256 = intent_row.risk_policy_sha256
      and control.provider_contract_version
        is not distinct from intent_row.provider_contract_version
      and control.provider_openapi_sha256
        is not distinct from intent_row.provider_openapi_sha256
    for share
  ) then
    raise exception 'pre_dispatch_control_revalidation_failed' using errcode = '40001';
  end if;
  select * into attempt_row
  from private.order_attempts
  where reservation_id = reservation_row.id;
  if found then
    if attempt_row.request_sha256 <> p_request_sha256
       or attempt_row.client_order_key <> p_client_order_key then
      raise exception 'dispatch_idempotency_conflict' using errcode = '23505';
    end if;
    return query select attempt_row.id, attempt_row.prepared_at, 'already_prepared'::text;
    return;
  end if;
  if reservation_row.fencing_token <> p_fencing_token
     or reservation_row.control_epoch <> p_gate_epoch then
    raise exception 'reservation_fencing_or_epoch_mismatch' using errcode = '40001';
  end if;
  select broker into broker_name
  from private.trading_accounts where account_id = p_account_id;
  insert into private.order_attempts (
    reservation_id, intent_id, account_id, environment, broker,
    lease_holder_id, fencing_token, control_epoch, client_order_key,
    request_sha256, prepared_at
  ) values (
    reservation_row.id, p_intent_id, p_account_id, p_environment, broker_name,
    p_holder_id, p_fencing_token, p_gate_epoch, p_client_order_key,
    p_request_sha256, p_now
  ) returning * into attempt_row;
  insert into private.order_events (
    intent_id, attempt_id, event_key, event_type, correlation_id,
    event_summary, occurred_at
  ) values (
    p_intent_id,
    attempt_row.id,
    'attempt:' || attempt_row.id::text,
    'dispatch_prepared',
    intent_row.correlation_id,
    jsonb_build_object('request_sha256', p_request_sha256),
    p_now
  );
  perform private.write_audit_event(
    'worker', null, null, 'trading_worker', p_holder_id, intent_row.release_sha,
    'dispatch_prepared', 'order_attempt', attempt_row.id::text,
    intent_row.correlation_id, null, 'pre_dispatch_persisted', null,
    array['fencing_token', 'control_epoch', 'request_sha256'],
    null, p_request_sha256, null
  );
  return query select attempt_row.id, attempt_row.prepared_at, 'prepared'::text;
end;
$$;

create or replace function private.execution_observation_append_block_reason(
  p_intent_id uuid
)
returns text
language sql
stable
set search_path = ''
as $$
  select case
    when exists (
      select 1
      from private.execution_reconciliation_state as reconciliation
      where reconciliation.intent_id = p_intent_id
        and reconciliation.state = 'manual'
    ) or exists (
      select 1
      from private.quarantined_execution_observations as quarantined
      where quarantined.intent_id = p_intent_id
    ) then 'execution_intent_manual_resolution_required'::text
    else null::text
  end;
$$;

create or replace function private.execution_observation_transition_violation(
  p_previous_sequence integer,
  p_previous_status text,
  p_previous_observed_at timestamptz,
  p_previous_quantity bigint,
  p_previous_gross_krw bigint,
  p_previous_commission_krw bigint,
  p_previous_tax_krw bigint,
  p_sequence integer,
  p_status text,
  p_observed_at timestamptz,
  p_intent_quantity bigint,
  p_cumulative_quantity bigint,
  p_cumulative_gross_krw bigint,
  p_cumulative_commission_krw bigint,
  p_cumulative_tax_krw bigint,
  p_last_fill_quantity bigint,
  p_last_fill_price_krw bigint,
  p_last_fill_settlement_date date,
  p_accounting_postings jsonb
)
returns text
language plpgsql
immutable
set search_path = ''
as $$
declare
  quantity_delta bigint;
  gross_delta bigint;
  commission_delta bigint;
  tax_delta bigint;
  fill_tuple_complete boolean;
  postings_complete boolean;
begin
  if p_sequence is null
     or p_sequence <= 0
     or p_status is null
     or p_observed_at is null
     or p_intent_quantity is null
     or p_intent_quantity <= 0
     or p_previous_quantity is null
     or p_previous_gross_krw is null
     or p_previous_commission_krw is null
     or p_previous_tax_krw is null
     or p_cumulative_quantity is null
     or p_cumulative_gross_krw is null
     or p_cumulative_commission_krw is null
     or p_cumulative_tax_krw is null then
    return 'execution_observation_transition_values_invalid';
  end if;
  if p_sequence <> coalesce(p_previous_sequence, 0) + 1 then
    return 'execution_observation_sequence_gap';
  end if;
  if p_previous_observed_at is not null
     and p_observed_at < p_previous_observed_at then
    return 'execution_observed_at_regressed';
  end if;
  if p_previous_status in (
    'filled', 'canceled', 'expired', 'rejected', 'failed_pre_dispatch',
    'unknown_requires_manual_check'
  ) then
    return 'terminal_state_regression';
  end if;
  if p_previous_quantity < 0
     or p_previous_gross_krw < 0
     or p_previous_commission_krw < 0
     or p_previous_tax_krw < 0
     or p_cumulative_quantity < p_previous_quantity
     or p_cumulative_quantity > p_intent_quantity then
    return 'execution_cumulative_quantity_regressed_or_exceeded';
  end if;
  if p_cumulative_gross_krw < p_previous_gross_krw
     or p_cumulative_commission_krw < p_previous_commission_krw
     or p_cumulative_tax_krw < p_previous_tax_krw then
    return 'execution_cumulative_value_regressed';
  end if;

  quantity_delta := p_cumulative_quantity - p_previous_quantity;
  gross_delta := p_cumulative_gross_krw - p_previous_gross_krw;
  commission_delta := p_cumulative_commission_krw - p_previous_commission_krw;
  tax_delta := p_cumulative_tax_krw - p_previous_tax_krw;
  fill_tuple_complete := p_last_fill_quantity is not null
    and p_last_fill_price_krw is not null
    and p_last_fill_settlement_date is not null;
  postings_complete := jsonb_typeof(p_accounting_postings) = 'array'
    and jsonb_array_length(p_accounting_postings) between 2 and 12;

  if (p_last_fill_quantity is null) <> (p_last_fill_price_krw is null)
     or (p_last_fill_quantity is null) <> (p_last_fill_settlement_date is null) then
    return 'execution_observation_fill_fields_must_be_paired';
  end if;
  if quantity_delta = 0 then
    if gross_delta <> 0 or commission_delta <> 0 or tax_delta <> 0
       or fill_tuple_complete
       or coalesce(p_accounting_postings, '[]'::jsonb) <> '[]'::jsonb then
      return 'non_fill_observation_delta_or_evidence_forbidden';
    end if;
  elsif quantity_delta > 0 then
    if gross_delta <= 0
       or not fill_tuple_complete
       or not postings_complete then
      return 'fill_delta_requires_complete_evidence';
    end if;
    if p_last_fill_quantity <= 0
       or p_last_fill_price_krw <= 0
       or p_last_fill_quantity <> quantity_delta then
      return 'execution_last_fill_quantity_mismatch';
    end if;
    if p_last_fill_quantity::numeric * p_last_fill_price_krw::numeric
       <> gross_delta::numeric then
      return 'execution_last_fill_gross_mismatch';
    end if;
  else
    return 'execution_cumulative_quantity_regressed_or_exceeded';
  end if;

  if p_status = 'open' and p_cumulative_quantity <> 0 then
    return 'open_observation_requires_zero_quantity';
  elsif p_status = 'partial_filled'
    and not (
      p_cumulative_quantity > 0
      and p_cumulative_quantity < p_intent_quantity
    ) then
    return 'partial_fill_observation_quantity_invalid';
  elsif p_status = 'filled'
    and p_cumulative_quantity <> p_intent_quantity then
    return 'filled_observation_quantity_invalid';
  elsif p_status in ('rejected', 'failed_pre_dispatch')
    and p_cumulative_quantity <> 0 then
    return 'non_executed_terminal_observation_quantity_invalid';
  end if;
  return null;
end;
$$;

create or replace function private.record_execution_observation_impl(
  p_intent_id uuid,
  p_sequence integer,
  p_status text,
  p_provider_order_id text,
  p_provider_execution_id text,
  p_provider_observation_sha256 text,
  p_observed_at timestamptz,
  p_cumulative_quantity bigint,
  p_cumulative_gross_krw bigint,
  p_cumulative_commission_krw bigint,
  p_cumulative_tax_krw bigint,
  p_last_fill_quantity bigint,
  p_last_fill_price_krw bigint,
  p_last_fill_settlement_date date,
  p_accounting_postings jsonb,
  p_reason_code text,
  p_holder_id text,
  p_fencing_token bigint
)
returns table (
  observation_id uuid,
  inserted boolean,
  quarantined boolean,
  reason_code text
)
language plpgsql
security definer
set search_path = ''
as $$
declare
  intent_row private.order_intents%rowtype;
  reservation_row private.order_reservations%rowtype;
  attempt_row private.order_attempts%rowtype;
  existing_row private.execution_observations%rowtype;
  previous_sequence integer;
  previous_cumulative bigint := 0;
  previous_gross bigint := 0;
  previous_commission bigint := 0;
  previous_tax bigint := 0;
  previous_observed_at timestamptz;
  new_observation_id uuid;
  observation_hash text;
  target_remaining_cash bigint;
  target_remaining_quantity bigint;
  previous_remaining_cash bigint;
  previous_remaining_quantity bigint;
  release_cash bigint := 0;
  release_quantity bigint := 0;
  next_reservation_sequence integer;
  reservation_event_type text;
  terminal_status boolean;
  fill_quantity_delta bigint := 0;
  cash_debit_delta bigint := 0;
  gross_delta bigint := 0;
  commission_delta bigint := 0;
  tax_delta bigint := 0;
  settlement_days_value integer;
  broker_name text;
  accounting_key text;
  provider_binding private.provider_order_bindings%rowtype;
  payload_hash text;
  quarantine_id uuid;
  previous_status text;
  append_block_reason text;
  transition_violation text;
  manual_reconciliation_run_id uuid;
  manual_reconciliation_break_id uuid;
  observation_authorization_time timestamptz := clock_timestamp();
  expected_settlement_date date;
  calendar_id_value uuid;
  buy_commission_rate_value numeric(18,12);
  sell_commission_rate_value numeric(18,12);
  sell_tax_rate_value numeric(18,12);
begin
  perform private.require_service_role();
  if p_status not in (
    'open', 'partial_filled', 'filled', 'canceled', 'expired', 'rejected',
    'failed_pre_dispatch', 'unknown_requires_manual_check'
  ) then
    raise exception 'execution_observation_status_invalid' using errcode = '22023';
  end if;
  if p_sequence <= 0
     or p_observed_at is null
     or p_cumulative_quantity < 0
     or p_cumulative_gross_krw < 0
     or p_cumulative_commission_krw < 0
     or p_cumulative_tax_krw < 0 then
    raise exception 'execution_observation_totals_invalid' using errcode = '22023';
  end if;
  if (p_last_fill_quantity is null) <> (p_last_fill_price_krw is null)
     or (p_last_fill_quantity is null) <> (p_last_fill_settlement_date is null) then
    raise exception 'execution_observation_fill_fields_must_be_paired' using errcode = '22023';
  end if;
  if p_last_fill_quantity is not null
     and (coalesce(jsonb_typeof(p_accounting_postings), '') <> 'array'
       or jsonb_array_length(p_accounting_postings) < 2) then
    raise exception 'fill_accounting_postings_required' using errcode = '22023';
  end if;
  if p_last_fill_quantity is null
     and coalesce(p_accounting_postings, '[]'::jsonb) <> '[]'::jsonb then
    raise exception 'non_fill_observation_must_not_include_postings' using errcode = '22023';
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
  if not exists (
    select 1 from private.worker_leases as lease
    where lease.account_id = intent_row.account_id
      and lease.holder_id = p_holder_id
      and lease.fencing_token = p_fencing_token
      and lease.expires_at > clock_timestamp()
      and lease.release_sha = intent_row.release_sha
  ) then
    raise exception 'worker_fencing_token_stale' using errcode = '40001';
  end if;
  select * into reservation_row
  from private.order_reservations where intent_id = p_intent_id;
  select * into attempt_row
  from private.order_attempts where intent_id = p_intent_id;
  if attempt_row.id is null and p_status <> 'failed_pre_dispatch' then
    raise exception 'pre_dispatch_attempt_required' using errcode = '23514';
  end if;

  payload_hash := pg_catalog.encode(
    public.digest(
      pg_catalog.convert_to(
        concat_ws(
          '|', p_intent_id::text, p_sequence::text, p_status,
          coalesce(p_provider_order_id, ''),
          coalesce(p_provider_execution_id, ''),
          private.utc_iso8601(p_observed_at), p_cumulative_quantity::text,
          p_cumulative_gross_krw::text, p_cumulative_commission_krw::text,
          p_cumulative_tax_krw::text, coalesce(p_last_fill_quantity::text, ''),
          coalesce(p_last_fill_price_krw::text, ''),
          coalesce(p_last_fill_settlement_date::text, ''),
          coalesce(p_reason_code, '')
        ),
        'UTF8'
      ),
      'sha256'
    ),
    'hex'
  );
  observation_hash := payload_hash;

  if p_provider_observation_sha256 !~ '^[0-9a-f]{64}$'
     or p_provider_observation_sha256 <> payload_hash then
    quarantine_id := private.quarantine_execution_observation(
      p_intent_id, attempt_row.id, p_sequence, p_provider_order_id,
      p_provider_execution_id, payload_hash, payload_hash,
      'provider_observation_digest_mismatch', p_observed_at, p_holder_id
    );
    return query select quarantine_id, false, true,
      'provider_observation_digest_mismatch'::text;
    return;
  end if;
  select * into existing_row
  from private.execution_observations
  where intent_id = p_intent_id and sequence = p_sequence;
  if found then
    if existing_row.observation_sha256 <> observation_hash then
      quarantine_id := private.quarantine_execution_observation(
        p_intent_id, attempt_row.id, p_sequence, p_provider_order_id,
        p_provider_execution_id, p_provider_observation_sha256, payload_hash,
        'execution_observation_idempotency_conflict', p_observed_at, p_holder_id
      );
      return query select quarantine_id, false, true,
        'execution_observation_idempotency_conflict'::text;
      return;
    end if;
    return query select existing_row.id, false, false, 'duplicate_observation'::text;
    return;
  end if;
  append_block_reason := private.execution_observation_append_block_reason(
    p_intent_id
  );
  if append_block_reason is not null then
    raise exception '%', append_block_reason using errcode = '40001';
  end if;
  if p_observed_at > observation_authorization_time + interval '30 seconds'
     and not (
       (p_last_fill_quantity is not null or p_status in ('partial_filled', 'filled'))
       and p_observed_at > intent_row.expires_at
     ) then
    quarantine_id := private.quarantine_execution_observation(
      p_intent_id, attempt_row.id, p_sequence, p_provider_order_id,
      p_provider_execution_id, p_provider_observation_sha256, payload_hash,
      'execution_observed_at_future', p_observed_at, p_holder_id
    );
    return query select quarantine_id, false, true,
      'execution_observed_at_future'::text;
    return;
  end if;
  if (p_last_fill_quantity is not null or p_status in ('partial_filled', 'filled'))
     and p_observed_at > intent_row.expires_at then
    quarantine_id := private.quarantine_execution_observation(
      p_intent_id, attempt_row.id, p_sequence, p_provider_order_id,
      p_provider_execution_id, p_provider_observation_sha256, payload_hash,
      'execution_fill_observed_after_intent_expiry', p_observed_at, p_holder_id
    );
    return query select quarantine_id, false, true,
      'execution_fill_observed_after_intent_expiry'::text;
    return;
  end if;
  if p_status = 'expired' and p_observed_at < intent_row.expires_at then
    quarantine_id := private.quarantine_execution_observation(
      p_intent_id, attempt_row.id, p_sequence, p_provider_order_id,
      p_provider_execution_id, p_provider_observation_sha256, payload_hash,
      'execution_expired_before_intent_expiry', p_observed_at, p_holder_id
    );
    return query select quarantine_id, false, true,
      'execution_expired_before_intent_expiry'::text;
    return;
  end if;
  if (attempt_row.id is not null and nullif(btrim(p_provider_order_id), '') is null)
     or (p_last_fill_quantity is not null and nullif(btrim(p_provider_execution_id), '') is null)
     or (p_last_fill_quantity is null and p_provider_execution_id is not null) then
    quarantine_id := private.quarantine_execution_observation(
      p_intent_id, attempt_row.id, p_sequence, p_provider_order_id,
      p_provider_execution_id, p_provider_observation_sha256, payload_hash,
      'provider_execution_identity_invalid', p_observed_at, p_holder_id
    );
    return query select quarantine_id, false, true,
      'provider_execution_identity_invalid'::text;
    return;
  end if;
  if attempt_row.id is not null then
    select * into provider_binding
    from private.provider_order_bindings
    where attempt_id = attempt_row.id;
    if found and provider_binding.provider_order_id <> p_provider_order_id then
      quarantine_id := private.quarantine_execution_observation(
        p_intent_id, attempt_row.id, p_sequence, p_provider_order_id,
        p_provider_execution_id, p_provider_observation_sha256, payload_hash,
        'provider_order_identity_mismatch', p_observed_at, p_holder_id
      );
      return query select quarantine_id, false, true,
        'provider_order_identity_mismatch'::text;
      return;
    elsif not found then
      insert into private.provider_order_bindings (
        attempt_id, intent_id, provider_order_id, binding_sha256, bound_at
      ) values (
        attempt_row.id, p_intent_id, p_provider_order_id,
        pg_catalog.encode(
          public.digest(
            pg_catalog.convert_to(
              attempt_row.id::text || '|' || p_provider_order_id,
              'UTF8'
            ),
            'sha256'
          ),
          'hex'
        ),
        p_observed_at
      );
    end if;
  end if;
  if p_provider_execution_id is not null and exists (
    select 1 from private.fills
    where account_id = intent_row.account_id
      and broker = attempt_row.broker
      and provider_execution_id = p_provider_execution_id
      and intent_id <> p_intent_id
  ) then
    quarantine_id := private.quarantine_execution_observation(
      p_intent_id, attempt_row.id, p_sequence, p_provider_order_id,
      p_provider_execution_id, p_provider_observation_sha256, payload_hash,
      'provider_execution_identity_reused', p_observed_at, p_holder_id
    );
    return query select quarantine_id, false, true,
      'provider_execution_identity_reused'::text;
    return;
  end if;
  select
    sequence,
    coalesce(cumulative_quantity, 0),
    coalesce(cumulative_gross_krw, 0),
    coalesce(cumulative_commission_krw, 0),
    coalesce(cumulative_tax_krw, 0),
    event_type,
    observed_at
  into previous_sequence, previous_cumulative, previous_gross,
    previous_commission, previous_tax, previous_status, previous_observed_at
  from private.execution_observations
  where intent_id = p_intent_id
  order by sequence desc
  limit 1;
  if not found then
    previous_sequence := null;
    previous_cumulative := 0;
    previous_gross := 0;
    previous_commission := 0;
    previous_tax := 0;
    previous_status := null;
    previous_observed_at := null;
  end if;
  transition_violation := private.execution_observation_transition_violation(
    previous_sequence,
    previous_status,
    previous_observed_at,
    previous_cumulative,
    previous_gross,
    previous_commission,
    previous_tax,
    p_sequence,
    p_status,
    p_observed_at,
    intent_row.quantity,
    p_cumulative_quantity,
    p_cumulative_gross_krw,
    p_cumulative_commission_krw,
    p_cumulative_tax_krw,
    p_last_fill_quantity,
    p_last_fill_price_krw,
    p_last_fill_settlement_date,
    p_accounting_postings
  );
  if transition_violation is not null then
    quarantine_id := private.quarantine_execution_observation(
      p_intent_id, attempt_row.id, p_sequence, p_provider_order_id,
      p_provider_execution_id, p_provider_observation_sha256, payload_hash,
      transition_violation, p_observed_at, p_holder_id
    );
    return query select quarantine_id, false, true, transition_violation;
    return;
  end if;
  fill_quantity_delta := p_cumulative_quantity - previous_cumulative;
  gross_delta := p_cumulative_gross_krw - previous_gross;
  commission_delta := p_cumulative_commission_krw - previous_commission;
  tax_delta := p_cumulative_tax_krw - previous_tax;
  cash_debit_delta := gross_delta + commission_delta + tax_delta;

  select
    schedule.settlement_days,
    schedule.buy_commission_rate,
    schedule.sell_commission_rate,
    schedule.sell_tax_rate,
    calendar.id
  into
    settlement_days_value,
    buy_commission_rate_value,
    sell_commission_rate_value,
    sell_tax_rate_value,
    calendar_id_value
  from private.execution_cost_schedules as schedule
  join private.paper_execution_policies as policy
    on policy.account_id = intent_row.account_id
   and policy.policy_version = intent_row.execution_policy_version
   and policy.status = 'approved'
  join private.paper_execution_model_registry as model
    on model.environment = intent_row.environment
   and model.model_version = policy.parameters->>'execution_model_version'
   and model.status = 'approved'
  join private.market_calendars as calendar
    on calendar.id = model.market_calendar_id
   and calendar.status = 'approved'
   and calendar.calendar_version = policy.parameters->>'market_calendar_version'
   and calendar.calendar_sha256 = policy.parameters->>'market_calendar_sha256'
  where schedule.account_id = intent_row.account_id
    and schedule.schedule_version = intent_row.cost_schedule_version
    and schedule.status = 'approved'
    and schedule.effective_from <= p_observed_at
    and schedule.effective_until > p_observed_at
  limit 1;
  if settlement_days_value is null then
    quarantine_id := private.quarantine_execution_observation(
      p_intent_id, attempt_row.id, p_sequence, p_provider_order_id,
      p_provider_execution_id, p_provider_observation_sha256, payload_hash,
      'approved_execution_cost_or_calendar_evidence_missing', p_observed_at, p_holder_id
    );
    return query select quarantine_id, false, true,
      'approved_execution_cost_or_calendar_evidence_missing'::text;
    return;
  end if;
  if p_last_fill_quantity is not null then
    select session_date into expected_settlement_date
    from private.market_calendar_sessions
    where calendar_id = calendar_id_value
      and session_date >= p_observed_at::date
      and is_open is true
    order by session_date
    offset settlement_days_value
    limit 1;
    if expected_settlement_date is null
       or p_last_fill_settlement_date <> expected_settlement_date
       or (intent_row.side = 'buy' and (
         tax_delta <> 0
         or commission_delta
           <> ceil(gross_delta * buy_commission_rate_value)::bigint
       ))
       or (intent_row.side = 'sell' and (
         commission_delta
           <> ceil(gross_delta * sell_commission_rate_value)::bigint
         or tax_delta
           <> ceil(gross_delta * sell_tax_rate_value)::bigint
       )) then
      quarantine_id := private.quarantine_execution_observation(
        p_intent_id, attempt_row.id, p_sequence, p_provider_order_id,
        p_provider_execution_id, p_provider_observation_sha256, payload_hash,
        'fill_cost_or_settlement_evidence_mismatch', p_observed_at, p_holder_id
      );
      return query select quarantine_id, false, true,
        'fill_cost_or_settlement_evidence_mismatch'::text;
      return;
    end if;
  end if;

  insert into private.execution_observations (
    intent_id, attempt_id, sequence, event_type, observed_at,
    cumulative_quantity, cumulative_gross_krw, cumulative_commission_krw,
    cumulative_tax_krw, reason_code, observation_sha256,
    provider_order_id, provider_execution_id, provider_observation_sha256
  ) values (
    p_intent_id, attempt_row.id, p_sequence, p_status, p_observed_at,
    p_cumulative_quantity, p_cumulative_gross_krw, p_cumulative_commission_krw,
    p_cumulative_tax_krw, p_reason_code, observation_hash,
    p_provider_order_id, p_provider_execution_id, p_provider_observation_sha256
  ) returning id into new_observation_id;
  if p_status = 'unknown_requires_manual_check' then
    insert into private.reconciliation_runs (
      account_id, environment, started_at, completed_at, result, release_sha
    ) values (
      intent_row.account_id, intent_row.environment, p_observed_at,
      p_observed_at, 'breaks_found', intent_row.release_sha
    ) returning id into manual_reconciliation_run_id;
    insert into private.reconciliation_breaks (
      run_id, account_id, break_type, state, detected_at, summary_code
    ) values (
      manual_reconciliation_run_id, intent_row.account_id, 'execution', 'open',
      p_observed_at, coalesce(p_reason_code, 'unknown_requires_manual_check')
    ) returning id into manual_reconciliation_break_id;
  end if;
  insert into private.order_events (
    intent_id, attempt_id, observation_id, event_key, event_type,
    correlation_id, event_summary, occurred_at
  ) values (
    p_intent_id,
    attempt_row.id,
    new_observation_id,
    'observation:' || p_sequence::text,
    case
      when p_status = 'unknown_requires_manual_check' then 'manual_check_quarantined'
      when p_status in ('filled', 'canceled', 'expired', 'rejected', 'failed_pre_dispatch') then 'terminal_confirmed'
      else 'observation_recorded'
    end,
    intent_row.correlation_id,
    jsonb_build_object('status', p_status, 'sequence', p_sequence)
      || case
        when p_status = 'unknown_requires_manual_check' then jsonb_build_object(
          'reconciliation_run_id', manual_reconciliation_run_id,
          'reconciliation_break_id', manual_reconciliation_break_id,
          'evidence_refs', jsonb_build_array(
            'observation:' || new_observation_id::text,
            'observation-sha256:' || observation_hash
          )
        )
        else '{}'::jsonb
      end,
    p_observed_at
  );

  if p_last_fill_quantity is not null then
    select broker into broker_name
    from private.trading_accounts where account_id = intent_row.account_id;
    insert into private.fills (
      event_id, intent_id, attempt_id, account_id, broker,
      provider_execution_id, quantity, price_krw, commission_krw, tax_krw,
      filled_at, settlement_date
    ) values (
      new_observation_id, p_intent_id, attempt_row.id, intent_row.account_id,
      broker_name, p_provider_execution_id,
      p_last_fill_quantity, p_last_fill_price_krw,
      p_cumulative_commission_krw - previous_commission,
      p_cumulative_tax_krw - previous_tax,
      p_observed_at, p_last_fill_settlement_date
    );
  end if;

  select
    remaining_cash_krw,
    remaining_quantity,
    event_sequence
  into
    previous_remaining_cash,
    previous_remaining_quantity,
    next_reservation_sequence
  from private.reservation_events
  where reservation_id = reservation_row.id
  order by event_sequence desc
  limit 1
  for update;
  next_reservation_sequence := next_reservation_sequence + 1;
  terminal_status := p_status in (
    'filled', 'canceled', 'expired', 'rejected', 'failed_pre_dispatch'
  );
  if intent_row.side = 'buy' then
    target_remaining_cash := case
      when terminal_status then 0
      else ((intent_row.quantity - p_cumulative_quantity) * intent_row.limit_price_krw)
        + ceil(
          ((intent_row.quantity - p_cumulative_quantity) * intent_row.limit_price_krw)
          * buy_commission_rate_value
        )::bigint
    end;
    release_cash := previous_remaining_cash - target_remaining_cash;
    if release_cash < 0 then
      raise exception 'reservation_cash_cannot_increase_from_observation' using errcode = '23514';
    end if;
    if release_cash > 0 then
      update private.cash_balance_projection
      set reserved_cash_krw = reserved_cash_krw - release_cash,
          pending_debit_cash_krw = pending_debit_cash_krw + cash_debit_delta,
          projection_version = projection_version + 1,
          projected_at = clock_timestamp()
      where account_id = intent_row.account_id and reserved_cash_krw >= release_cash;
      if not found then
        raise exception 'reservation_cash_projection_underflow' using errcode = '23514';
      end if;
    end if;
  else
    target_remaining_quantity := case
      when terminal_status then 0
      else intent_row.quantity - p_cumulative_quantity
    end;
    release_quantity := previous_remaining_quantity - target_remaining_quantity;
    if release_quantity < 0 then
      raise exception 'reservation_quantity_cannot_increase_from_observation' using errcode = '23514';
    end if;
    if release_quantity > 0 then
      update private.position_projection
      set reserved_quantity = reserved_quantity - release_quantity,
          pending_sell_quantity = pending_sell_quantity + fill_quantity_delta,
          projection_version = projection_version + 1,
          projected_at = clock_timestamp()
      where account_id = intent_row.account_id
        and symbol = intent_row.symbol
        and reserved_quantity >= release_quantity;
      if not found then
        raise exception 'reservation_quantity_projection_underflow' using errcode = '23514';
      end if;
    end if;
  end if;
  if release_cash > 0 or release_quantity > 0 then
    reservation_event_type := case
      when terminal_status and p_status = 'filled' then 'fully_consumed'
      when terminal_status then 'released'
      else 'partially_consumed'
    end;
    insert into private.reservation_events (
      reservation_id, intent_id, event_sequence, event_type,
      cash_delta_krw, quantity_delta, remaining_cash_krw,
      remaining_quantity, source_observation_id, occurred_at
    ) values (
      reservation_row.id, p_intent_id, next_reservation_sequence,
      reservation_event_type, -release_cash, -release_quantity,
      coalesce(target_remaining_cash, 0), coalesce(target_remaining_quantity, 0),
      new_observation_id, p_observed_at
    );
  end if;

  if p_status = 'unknown_requires_manual_check' then
    insert into private.incidents (
      severity, incident_type, summary_code, correlation_id
    ) values (
      'critical', 'execution_unknown', 'unknown_requires_manual_check',
      intent_row.correlation_id
    );
    insert into private.delivery_outbox (
      event_type, aggregate_type, aggregate_id, dedupe_key, payload, destination_type
    ) values (
      'execution_unknown', 'execution_observation', new_observation_id::text,
      'execution-unknown:' || new_observation_id::text,
      jsonb_build_object(
        'observation_id', new_observation_id,
        'intent_id', p_intent_id,
        'reason_code', p_reason_code
      ),
      'incident_alert'
    );
  end if;
  if p_last_fill_quantity is not null then
    accounting_key := pg_catalog.encode(
      public.digest(
        pg_catalog.convert_to(
          p_intent_id::text || ':fill:' || p_sequence::text,
          'UTF8'
        ),
        'sha256'
      ),
      'hex'
    );
    perform * from private.post_accounting_transaction_impl(
      accounting_key,
      p_intent_id,
      p_sequence,
      p_observed_at,
      p_accounting_postings,
      p_holder_id,
      p_fencing_token
    );
  end if;
  update private.execution_reconciliation_state
  set priority = case
        when p_status = 'partial_filled' then 10
        when p_status = 'open' then 20
        else priority
      end,
      state = case
        when p_status = 'unknown_requires_manual_check' then 'manual'
        when terminal_status then 'complete'
        else 'pending'
      end,
      next_reconcile_at = case
        when p_status = 'unknown_requires_manual_check' or terminal_status
          then p_observed_at
        else p_observed_at + interval '15 seconds'
      end,
      lease_owner = null,
      lease_expires_at = null,
      last_reason_code = p_reason_code,
      updated_at = clock_timestamp()
  where intent_id = p_intent_id;
  perform private.write_audit_event(
    'worker', null, null, 'trading_worker', p_holder_id, intent_row.release_sha,
    'execution_observation_recorded', 'execution_observation', new_observation_id::text,
    intent_row.correlation_id, null, p_status, null,
    array['event_type', 'sequence', 'cumulative_quantity'],
    null, observation_hash, null
  );
  return query select new_observation_id, true, false, 'recorded'::text;
end;
$$;

create or replace function private.post_accounting_transaction_impl(
  p_transaction_key text,
  p_intent_id uuid,
  p_observation_sequence integer,
  p_posted_at timestamptz,
  p_postings jsonb,
  p_holder_id text,
  p_fencing_token bigint
)
returns table (transaction_id uuid, inserted boolean)
language plpgsql
security definer
set search_path = ''
as $$
declare
  intent_row private.order_intents%rowtype;
  observation_row private.execution_observations%rowtype;
  fill_row private.fills%rowtype;
  existing_id uuid;
  new_transaction_id uuid;
  posting jsonb;
  posting_ledger_code text;
  debit_amount bigint;
  credit_amount bigint;
  total_debit bigint := 0;
  total_credit bigint := 0;
  cash_delta bigint := 0;
  old_quantity bigint;
  old_average numeric(24,4);
  new_quantity bigint;
  new_average numeric(24,4);
  old_total_cost bigint := 0;
  new_total_cost bigint := 0;
  cost_relief bigint := 0;
  realized_amount bigint := 0;
  seen_codes text[] := array[]::text[];
  cash_debit_total bigint := 0;
  cash_credit_total bigint := 0;
  position_debit_total bigint := 0;
  position_credit_total bigint := 0;
  fees_debit_total bigint := 0;
  taxes_debit_total bigint := 0;
  realized_debit_total bigint := 0;
  realized_credit_total bigint := 0;
  snapshot_sequence bigint;
begin
  perform private.require_service_role();
  if p_transaction_key !~ '^[0-9a-f]{64}$'
     or jsonb_typeof(p_postings) <> 'array'
     or jsonb_array_length(p_postings) < 2
     or jsonb_array_length(p_postings) > 12 then
    raise exception 'accounting_transaction_payload_invalid' using errcode = '22023';
  end if;
  select * into intent_row from private.order_intents where id = p_intent_id;
  if not found then
    raise exception 'execution_intent_not_found' using errcode = 'P0002';
  end if;
  if not exists (
    select 1 from private.worker_leases
    where account_id = intent_row.account_id
      and holder_id = p_holder_id
      and fencing_token = p_fencing_token
      and expires_at > clock_timestamp()
  ) then
    raise exception 'worker_fencing_token_stale' using errcode = '40001';
  end if;
  select id into existing_id
  from private.accounting_transactions
  where account_id = intent_row.account_id
    and source_type = 'fill'
    and source_id = p_transaction_key;
  if existing_id is not null then
    return query select existing_id, false;
    return;
  end if;
  select * into observation_row
  from private.execution_observations
  where intent_id = p_intent_id and sequence = p_observation_sequence;
  if not found then
    raise exception 'execution_observation_not_found' using errcode = 'P0002';
  end if;
  select * into fill_row from private.fills where event_id = observation_row.id;
  if not found then
    raise exception 'fill_not_found_for_accounting_transaction' using errcode = 'P0002';
  end if;

  select quantity, average_cost_krw
  into old_quantity, old_average
  from private.position_projection
  where account_id = intent_row.account_id and symbol = intent_row.symbol
  for update;
  if not found then
    if intent_row.side = 'sell' then
      raise exception 'position_projection_underflow' using errcode = '23514';
    end if;
    old_quantity := 0;
    old_average := 0;
    insert into private.position_projection (account_id, symbol)
    values (intent_row.account_id, intent_row.symbol);
  end if;
  old_total_cost := round(old_quantity * old_average)::bigint;
  if intent_row.side = 'sell' then
    if old_quantity < fill_row.quantity or old_quantity <= 0 then
      raise exception 'position_projection_underflow' using errcode = '23514';
    end if;
    cost_relief := case
      when fill_row.quantity = old_quantity then old_total_cost
      else floor(old_total_cost::numeric * fill_row.quantity / old_quantity)::bigint
    end;
    realized_amount := (fill_row.quantity * fill_row.price_krw) - cost_relief;
  end if;

  insert into private.accounting_transactions (
    account_id, environment, source_type, source_id, correlation_id,
    occurred_at, posted_at, release_sha
  ) values (
    intent_row.account_id, intent_row.environment, 'fill', p_transaction_key,
    intent_row.correlation_id, observation_row.observed_at, p_posted_at,
    intent_row.release_sha
  ) returning id into new_transaction_id;

  for posting in select value from jsonb_array_elements(p_postings)
  loop
    if jsonb_typeof(posting) <> 'object'
       or posting - array['account', 'debit_krw', 'credit_krw'] <> '{}'::jsonb then
      raise exception 'accounting_posting_shape_invalid' using errcode = '22023';
    end if;
    posting_ledger_code := nullif(btrim(posting->>'account'), '');
    debit_amount := coalesce((posting->>'debit_krw')::bigint, 0);
    credit_amount := coalesce((posting->>'credit_krw')::bigint, 0);
    if posting_ledger_code is null
       or debit_amount < 0
       or credit_amount < 0
       or (debit_amount > 0) = (credit_amount > 0) then
      raise exception 'accounting_posting_amount_invalid' using errcode = '22023';
    end if;
    if posting_ledger_code = any(seen_codes)
       or posting_ledger_code not in ('CASH', 'POSITION_COST', 'FEES', 'TAXES', 'REALIZED_PNL') then
      raise exception 'accounting_posting_ledger_vector_invalid' using errcode = '23514';
    end if;
    seen_codes := array_append(seen_codes, posting_ledger_code);
    insert into private.accounting_postings (
      journal_entry_id, ledger_account_id, side, amount_krw
    )
    select
      new_transaction_id,
      ledger.id,
      case when debit_amount > 0 then 'debit' else 'credit' end,
      greatest(debit_amount, credit_amount)
    from private.ledger_accounts as ledger
    where ledger.account_id = intent_row.account_id
      and ledger.ledger_code = posting_ledger_code;
    if not found then
      raise exception 'ledger_account_not_found' using errcode = '23514';
    end if;
    total_debit := total_debit + debit_amount;
    total_credit := total_credit + credit_amount;
    if posting_ledger_code = 'CASH' then
      cash_delta := cash_delta + debit_amount - credit_amount;
      cash_debit_total := debit_amount;
      cash_credit_total := credit_amount;
    elsif posting_ledger_code = 'POSITION_COST' then
      position_debit_total := debit_amount;
      position_credit_total := credit_amount;
    elsif posting_ledger_code = 'FEES' then
      fees_debit_total := debit_amount;
    elsif posting_ledger_code = 'TAXES' then
      taxes_debit_total := debit_amount;
    elsif posting_ledger_code = 'REALIZED_PNL' then
      realized_debit_total := debit_amount;
      realized_credit_total := credit_amount;
    end if;
  end loop;
  if total_debit <> total_credit then
    raise exception 'accounting_transaction_is_not_balanced' using errcode = '23514';
  end if;
  if intent_row.side = 'buy' then
    if cash_debit_total <> 0
       or cash_credit_total <> fill_row.quantity * fill_row.price_krw
         + fill_row.commission_krw + fill_row.tax_krw
       or position_debit_total <> fill_row.quantity * fill_row.price_krw
       or position_credit_total <> 0
       or fees_debit_total <> fill_row.commission_krw
       or taxes_debit_total <> fill_row.tax_krw
       or realized_debit_total <> 0
       or realized_credit_total <> 0 then
      raise exception 'buy_fill_accounting_vector_mismatch' using errcode = '23514';
    end if;
  else
    if cash_debit_total <> fill_row.quantity * fill_row.price_krw
         - fill_row.commission_krw - fill_row.tax_krw
       or cash_credit_total <> 0
       or position_debit_total <> 0
       or position_credit_total <> cost_relief
       or fees_debit_total <> fill_row.commission_krw
       or taxes_debit_total <> fill_row.tax_krw
       or realized_debit_total <> greatest(-realized_amount, 0)
       or realized_credit_total <> greatest(realized_amount, 0) then
      raise exception 'sell_fill_accounting_vector_mismatch' using errcode = '23514';
    end if;
  end if;

  if cash_delta < 0 then
    update private.cash_balance_projection
    set settled_cash_krw = settled_cash_krw + cash_delta,
        pending_debit_cash_krw = pending_debit_cash_krw + cash_delta,
        last_journal_entry_id = new_transaction_id,
        projection_version = projection_version + 1,
        projected_at = p_posted_at
    where account_id = intent_row.account_id
      and pending_debit_cash_krw >= -cash_delta
      and settled_cash_krw >= -cash_delta;
  else
    update private.cash_balance_projection
    set settled_cash_krw = settled_cash_krw + cash_delta,
        last_journal_entry_id = new_transaction_id,
        projection_version = projection_version + 1,
        projected_at = p_posted_at
    where account_id = intent_row.account_id;
  end if;
  if not found then
    raise exception 'cash_projection_settlement_failed' using errcode = '23514';
  end if;

  if intent_row.side = 'buy' then
    new_quantity := old_quantity + fill_row.quantity;
    new_total_cost := old_total_cost + (fill_row.quantity * fill_row.price_krw);
    new_average := new_total_cost::numeric / new_quantity;
    update private.position_projection
    set quantity = new_quantity,
        average_cost_krw = new_average,
        projection_version = projection_version + 1,
        projected_at = p_posted_at
    where account_id = intent_row.account_id and symbol = intent_row.symbol;
  else
    new_quantity := old_quantity - fill_row.quantity;
    if new_quantity < 0 then
      raise exception 'position_projection_underflow' using errcode = '23514';
    end if;
    new_total_cost := old_total_cost - cost_relief;
    new_average := case
      when new_quantity = 0 then 0
      else new_total_cost::numeric / new_quantity
    end;
    update private.position_projection
    set quantity = new_quantity,
        pending_sell_quantity = pending_sell_quantity - fill_row.quantity,
        average_cost_krw = new_average,
        projection_version = projection_version + 1,
        projected_at = p_posted_at
    where account_id = intent_row.account_id
      and symbol = intent_row.symbol
      and pending_sell_quantity >= fill_row.quantity;
    if not found then
      raise exception 'pending_sell_projection_underflow' using errcode = '23514';
    end if;
  end if;
  insert into private.position_movements (
    account_id, symbol, source_fill_id, source_transaction_id, movement_type,
    quantity_delta, resulting_quantity, unit_cost_krw, occurred_at
  ) values (
    intent_row.account_id,
    intent_row.symbol,
    fill_row.id,
    new_transaction_id,
    case when intent_row.side = 'buy' then 'buy_fill' else 'sell_fill' end,
    case when intent_row.side = 'buy' then fill_row.quantity else -fill_row.quantity end,
    new_quantity,
    case when intent_row.side = 'buy' then fill_row.price_krw else old_average end,
    observation_row.observed_at
  );

  select coalesce(max(sequence), 0) + 1 into snapshot_sequence
  from private.account_snapshots
  where account_id = intent_row.account_id and environment = intent_row.environment;
  insert into private.account_snapshots (
    account_id, environment, sequence, cash_krw, reserved_cash_krw,
    positions_sha256, source_type, source_id, observed_at
  )
  select
    intent_row.account_id,
    intent_row.environment,
    snapshot_sequence,
    balance.settled_cash_krw,
    balance.reserved_cash_krw,
    pg_catalog.encode(
      public.digest(
        pg_catalog.convert_to(
          coalesce(string_agg(position.symbol || ':' || position.quantity::text, '|' order by position.symbol), ''),
          'UTF8'
        ),
        'sha256'
      ),
      'hex'
    ),
    'ledger_projection',
    new_transaction_id,
    p_posted_at
  from private.cash_balance_projection as balance
  left join private.position_projection as position
    on position.account_id = balance.account_id
  where balance.account_id = intent_row.account_id
  group by balance.settled_cash_krw, balance.reserved_cash_krw;

  perform private.write_audit_event(
    'worker', null, null, 'trading_worker', p_holder_id, intent_row.release_sha,
    'accounting_transaction_posted', 'accounting_transaction', new_transaction_id::text,
    intent_row.correlation_id, null, 'balanced_posting', null,
    array['source_id', 'posted_at'], null, p_transaction_key, null
  );
  return query select new_transaction_id, true;
exception
  when invalid_text_representation or numeric_value_out_of_range then
    raise exception 'accounting_posting_numeric_value_invalid' using errcode = '22023';
end;
$$;

create or replace function private.acknowledge_operation_command_impl(
  p_command_id uuid,
  p_phase text,
  p_holder_id text,
  p_release_sha text,
  p_now timestamptz,
  p_result_summary jsonb,
  p_failure_code text
)
returns table (
  command_id uuid,
  state text,
  claimed_at timestamptz,
  applied_at timestamptz,
  post_control_epoch bigint,
  failure_code text
)
language plpgsql
security definer
set search_path = ''
as $$
declare
  command_row private.operation_commands%rowtype;
  account_row private.trading_accounts%rowtype;
  transaction_id uuid;
  account_key text;
  expected_epoch bigint;
  resulting_epoch bigint;
  opening_amount bigint;
  cash_account_id uuid;
  equity_account_id uuid;
  next_state text;
  authorization_time timestamptz := clock_timestamp();
begin
  perform private.require_service_role();
  if p_phase not in ('claimed', 'applied', 'failed')
     or nullif(btrim(p_holder_id), '') is null
     or p_holder_id !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
     or p_release_sha !~ '^[0-9a-f]{40}([0-9a-f]{24})?$'
     or jsonb_typeof(coalesce(p_result_summary, '{}'::jsonb)) <> 'object'
     or p_now is null
     or p_now < authorization_time - interval '5 minutes'
     or p_now > authorization_time + interval '30 seconds' then
    raise exception 'operation_command_ack_payload_invalid' using errcode = '22023';
  end if;
  select * into command_row
  from private.operation_commands
  where id = p_command_id
  for update;
  if not found then
    raise exception 'operation_command_not_found' using errcode = 'P0002';
  end if;
  if command_row.target_release_sha is not null
     and command_row.target_release_sha <> p_release_sha then
    raise exception 'operation_command_release_mismatch' using errcode = '40001';
  end if;

  if p_phase = 'claimed' then
    if command_row.state = 'claimed'
       and command_row.claimed_by_service = p_holder_id
       and command_row.claim_expires_at > authorization_time then
      return query
      select command_row.id, command_row.state, command_row.claimed_at,
        command_row.applied_at, null::bigint, command_row.failure_code;
      return;
    end if;
    if command_row.state <> 'approved'
       or command_row.expires_at <= authorization_time then
      raise exception 'operation_command_not_claimable' using errcode = '23514';
    end if;
    update private.operation_commands
    set state = 'claimed', claimed_by_service = p_holder_id, claimed_at = p_now,
        claim_expires_at = authorization_time + interval '30 seconds',
        revision = revision + 1
    where id = p_command_id
    returning * into command_row;
    insert into private.operation_command_events (
      command_id, event_type, actor_type, service_principal, event_summary, occurred_at
    ) values (
      p_command_id, 'claimed', 'worker', p_holder_id,
      jsonb_build_object('release_sha', p_release_sha), p_now
    );
    perform private.write_audit_event(
      'worker', null, null, 'trading_worker', p_holder_id, p_release_sha,
      'operation_command_claimed', 'operation_command', p_command_id::text,
      gen_random_uuid(), p_command_id, 'claimed', null,
      array['state', 'claimed_at'], null, null, command_row.evidence_id
    );
    return query
    select command_row.id, command_row.state, command_row.claimed_at,
      command_row.applied_at, null::bigint, command_row.failure_code;
    return;
  end if;

  if command_row.state <> 'claimed'
     or command_row.claimed_by_service <> p_holder_id
     or command_row.claim_expires_at <= authorization_time then
    raise exception 'operation_command_not_owned_by_worker' using errcode = '40001';
  end if;
  if p_phase = 'applied' and command_row.expires_at <= authorization_time then
    raise exception 'operation_command_expired_before_application'
      using errcode = '40001';
  end if;
  if p_phase = 'failed' then
    if nullif(btrim(p_failure_code), '') is null then
      raise exception 'operation_command_failure_code_required' using errcode = '22023';
    end if;
    update private.operation_commands
    set state = 'failed', applied_at = p_now, failure_code = p_failure_code,
        result_summary = coalesce(p_result_summary, '{}'::jsonb)
          || jsonb_build_object('release_sha', p_release_sha),
        revision = revision + 1
    where id = p_command_id
    returning * into command_row;
    next_state := 'failed';
  else
    account_key := nullif(command_row.requested_change->>'account_id', '');
    if account_key is null then
      raise exception 'operation_command_account_id_required' using errcode = '23514';
    end if;
    select * into account_row
    from private.trading_accounts where account_id = account_key for update;
    if not found then
      raise exception 'operation_command_account_not_found' using errcode = 'P0002';
    end if;

    if command_row.command_type = 'emergency_stop' then
      expected_epoch := (command_row.requested_change->>'expected_state_version')::bigint;
      select control_epoch into resulting_epoch
      from private.execution_controls
      where account_id = account_key
        and execution_enabled is false
        and control_epoch >= expected_epoch + 1;
      if resulting_epoch is null then
        raise exception 'emergency_stop_postcondition_not_satisfied' using errcode = '40001';
      end if;
    elsif command_row.command_type = 'account_opening' then
      opening_amount := (command_row.requested_change->>'opening_capital_krw')::bigint;
      if account_row.state <> 'pending_open'
         or account_row.opening_journal_entry_id is not null
         or opening_amount <> account_row.opening_capital_krw
         or (account_key = 'paper-primary' and opening_amount <> 10000000) then
        raise exception 'account_opening_invariant_failed' using errcode = '23514';
      end if;
      select id into cash_account_id from private.ledger_accounts
      where account_id = account_key and ledger_code = 'CASH';
      select id into equity_account_id from private.ledger_accounts
      where account_id = account_key and ledger_code = 'OPENING_EQUITY';
      insert into private.accounting_transactions (
        account_id, environment, source_type, source_id, control_command_id,
        correlation_id, occurred_at, posted_at, release_sha
      ) values (
        account_key, account_row.environment, 'opening_capital', p_command_id::text,
        p_command_id, p_command_id, p_now, p_now, p_release_sha
      ) returning id into transaction_id;
      insert into private.accounting_postings (
        journal_entry_id, ledger_account_id, side, amount_krw
      ) values
        (transaction_id, cash_account_id, 'debit', opening_amount),
        (transaction_id, equity_account_id, 'credit', opening_amount);
      update private.trading_accounts
      set state = 'open', opening_journal_entry_id = transaction_id, opened_at = p_now
      where account_id = account_key;
      update private.cash_balance_projection
      set settled_cash_krw = opening_amount,
          last_journal_entry_id = transaction_id,
          projection_version = projection_version + 1,
          projected_at = p_now
      where account_id = account_key and projection_version = 0;
      if not found then
        raise exception 'account_opening_projection_not_pristine' using errcode = '23514';
      end if;
      resulting_epoch := null;
    elsif command_row.command_type = 'pause_paper' then
      expected_epoch := (command_row.requested_change->>'expected_state_version')::bigint;
      update private.execution_controls
      set execution_enabled = false,
          control_epoch = control_epoch + 1,
          effective_at = p_now,
          expires_at = greatest(expires_at, p_now + interval '1 second'),
          last_command_id = p_command_id,
          updated_reason_code = coalesce(command_row.requested_change->>'reason_code', 'operator_pause'),
          updated_at = p_now
      where account_id = account_key and control_epoch = expected_epoch
      returning control_epoch into resulting_epoch;
      if resulting_epoch is null then
        raise exception 'operation_command_stale_state_version' using errcode = '40001';
      end if;
    elsif command_row.command_type in ('paper_resume', 'contract_test_enable') then
      expected_epoch := (command_row.requested_change->>'expected_state_version')::bigint;
      if account_row.state <> 'open' then
        raise exception 'open_trading_account_required' using errcode = '23514';
      end if;
      update private.execution_controls
      set execution_enabled = true,
          control_epoch = control_epoch + 1,
          active_strategy_version_id = command_row.requested_change->>'strategy_version_id',
          active_risk_policy_version_id = (
            command_row.requested_change->>'risk_policy_version_id'
          )::uuid,
          execution_policy_version = command_row.requested_change->>'execution_policy_version',
          execution_policy_sha256 = command_row.requested_change->>'execution_policy_sha256',
          risk_policy_sha256 = command_row.requested_change->>'risk_policy_sha256',
          provider_contract_version = case
            when command_row.command_type = 'contract_test_enable'
              then command_row.requested_change->>'provider_contract_version'
            else null
          end,
          provider_openapi_sha256 = case
            when command_row.command_type = 'contract_test_enable'
              then command_row.requested_change->>'provider_openapi_sha256'
            else null
          end,
          effective_at = p_now,
          expires_at = least(command_row.expires_at, p_now + interval '24 hours'),
          last_command_id = p_command_id,
          updated_reason_code = command_row.command_type,
          updated_at = p_now
      where account_id = account_key
        and environment = case
          when command_row.command_type = 'paper_resume' then 'paper'
          else 'contract_test'
        end
        and control_epoch = expected_epoch
      returning control_epoch into resulting_epoch;
      if resulting_epoch is null then
        raise exception 'operation_command_stale_state_or_environment' using errcode = '40001';
      end if;
      if command_row.command_type = 'contract_test_enable' then
        update private.environment_policy
        set environment = 'contract_test',
            provider_connectivity = 'local_contract_simulator',
            updated_at = p_now
        where id = 'singleton';
      else
        update private.environment_policy
        set environment = 'paper',
            provider_connectivity = 'disabled',
            updated_at = p_now
        where id = 'singleton';
      end if;
    elsif command_row.command_type = 'strategy_promotion' then
      expected_epoch := (command_row.requested_change->>'expected_state_version')::bigint;
      update private.execution_controls
      set control_epoch = control_epoch + 1,
          active_strategy_version_id = command_row.requested_change->>'strategy_version_id',
          active_risk_policy_version_id = (
            command_row.requested_change->>'risk_policy_version_id'
          )::uuid,
          execution_policy_version = command_row.requested_change->>'execution_policy_version',
          execution_policy_sha256 = command_row.requested_change->>'execution_policy_sha256',
          risk_policy_sha256 = command_row.requested_change->>'risk_policy_sha256',
          effective_at = p_now,
          expires_at = greatest(expires_at, p_now + interval '1 second'),
          last_command_id = p_command_id,
          updated_reason_code = 'strategy_promotion',
          updated_at = p_now
      where account_id = account_key and control_epoch = expected_epoch
      returning control_epoch into resulting_epoch;
      if resulting_epoch is null then
        raise exception 'operation_command_stale_state_version' using errcode = '40001';
      end if;
    elsif command_row.command_type = 'risk_policy_change' then
      expected_epoch := (command_row.requested_change->>'expected_state_version')::bigint;
      update private.execution_controls
      set control_epoch = control_epoch + 1,
          active_risk_policy_version_id = (
            command_row.requested_change->>'risk_policy_version_id'
          )::uuid,
          risk_policy_sha256 = command_row.requested_change->>'risk_policy_sha256',
          effective_at = p_now,
          expires_at = greatest(expires_at, p_now + interval '1 second'),
          last_command_id = p_command_id,
          updated_reason_code = 'risk_policy_change',
          updated_at = p_now
      where account_id = account_key and control_epoch = expected_epoch
      returning control_epoch into resulting_epoch;
      if resulting_epoch is null then
        raise exception 'operation_command_stale_state_version' using errcode = '40001';
      end if;
    else
      raise exception 'operation_command_worker_application_not_implemented'
        using errcode = '0A000';
    end if;
    update private.operation_commands
    set state = 'applied', applied_at = p_now, failure_code = null,
        result_summary = coalesce(p_result_summary, '{}'::jsonb)
          || jsonb_build_object(
            'post_control_epoch', resulting_epoch,
            'release_sha', p_release_sha
          ),
        revision = revision + 1
    where id = p_command_id
    returning * into command_row;
    next_state := 'applied';
  end if;

  insert into private.operation_command_events (
    command_id, event_type, actor_type, service_principal, event_summary, occurred_at
  ) values (
    p_command_id, next_state, 'worker', p_holder_id,
    coalesce(p_result_summary, '{}'::jsonb)
      || jsonb_build_object(
        'failure_code', p_failure_code,
        'post_control_epoch', resulting_epoch,
        'release_sha', p_release_sha
      ),
    p_now
  );
  perform private.write_audit_event(
    'worker', null, null, 'trading_worker', p_holder_id, p_release_sha,
    'operation_command_' || next_state, 'operation_command', p_command_id::text,
    gen_random_uuid(), p_command_id, coalesce(p_failure_code, next_state), null,
    array['state', 'applied_at', 'result_summary'], null, null, command_row.evidence_id
  );
  return query
  select command_row.id, command_row.state, command_row.claimed_at,
    command_row.applied_at, resulting_epoch, command_row.failure_code;
exception
  when invalid_text_representation or numeric_value_out_of_range then
    raise exception 'operation_command_numeric_value_invalid' using errcode = '22023';
end;
$$;

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
begin
  perform private.require_service_role();
  if nullif(btrim(p_worker_id), '') is null
     or p_limit < 1 or p_limit > 100
     or p_lease_seconds < 5 or p_lease_seconds > 300 then
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
      (candidate.status = 'pending' and candidate.available_at <= p_now)
      or (candidate.status = 'leased' and candidate.lease_expires_at <= p_now)
    )
      and candidate.attempt_count < candidate.max_attempts
    order by candidate.available_at, candidate.created_at, candidate.id
    limit p_limit
    for update skip locked
  ), claimed as (
    update private.delivery_outbox as outbox
    set status = 'leased',
        lease_owner = p_worker_id,
        lease_expires_at = p_now + make_interval(secs => p_lease_seconds),
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
begin
  perform private.require_service_role();
  if nullif(btrim(p_external_receipt_id), '') is null
     or p_external_receipt_sha256 !~ '^[0-9a-f]{64}$' then
    raise exception 'outbox_receipt_invalid' using errcode = '22023';
  end if;
  if exists (
    select 1
    from private.delivery_outbox
    where id = p_outbox_id
      and destination_type = 'audit_archive'
      and payload->>'event_hash' is distinct from p_external_receipt_sha256
  ) then
    raise exception 'audit_archive_receipt_hash_mismatch' using errcode = '23514';
  end if;
  update private.delivery_outbox
  set status = 'delivered',
      lease_owner = null,
      lease_expires_at = null,
      external_receipt_id = p_external_receipt_id,
      external_receipt_digest = p_external_receipt_sha256,
      delivered_at = p_now,
      last_error_code = null
  where id = p_outbox_id
    and status = 'leased'
    and lease_owner = p_worker_id
    and lease_expires_at > p_now;
  if not found then
    raise exception 'outbox_lease_not_owned_or_expired' using errcode = '40001';
  end if;
  return query select p_outbox_id, 'delivered'::text, p_now;
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
  next_status text;
  next_available timestamptz;
begin
  perform private.require_service_role();
  if nullif(btrim(p_error_code), '') is null
     or length(p_error_code) > 120
     or p_retry_after_seconds < 1
     or p_retry_after_seconds > 86400 then
    raise exception 'outbox_failure_parameters_invalid' using errcode = '22023';
  end if;
  update private.delivery_outbox
  set status = case when attempt_count >= max_attempts then 'dead_letter' else 'pending' end,
      available_at = case
        when attempt_count >= max_attempts then available_at
        else p_now + make_interval(secs => p_retry_after_seconds)
      end,
      lease_owner = null,
      lease_expires_at = null,
      last_error_code = p_error_code
  where id = p_outbox_id
    and status = 'leased'
    and lease_owner = p_worker_id
  returning private.delivery_outbox.status, private.delivery_outbox.available_at
  into next_status, next_available;
  if not found then
    raise exception 'outbox_lease_not_owned' using errcode = '40001';
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

-- SECURITY INVOKER Data API wrappers.
create or replace function api.get_my_access_profile()
returns table (user_id uuid, aal text, active_roles text[])
language sql
stable
security invoker
set search_path = ''
as $$ select * from private.get_my_access_profile(); $$;

create or replace function api.get_platform_status()
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
language sql
stable
security invoker
set search_path = ''
as $$ select * from private.get_platform_status(); $$;

create or replace function api.list_operation_commands(p_limit integer default 50)
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
language sql
stable
security invoker
set search_path = ''
as $$ select * from private.list_operation_commands(p_limit); $$;

create or replace function api.list_incidents(p_limit integer default 50)
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
language sql
stable
security invoker
set search_path = ''
as $$ select * from private.list_incidents(p_limit); $$;

create or replace function api.list_audit_events(p_limit integer default 50)
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
language sql
stable
security invoker
set search_path = ''
as $$ select * from private.list_audit_events(p_limit); $$;

create or replace function api.get_accounting_summary()
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
language sql
stable
security invoker
set search_path = ''
as $$ select * from private.get_accounting_summary(); $$;

create or replace function api.get_desktop_operations_snapshot_v1()
returns jsonb
language sql
volatile
security invoker
set search_path = ''
as $$ select private.get_desktop_operations_snapshot_v1_impl(); $$;

create or replace function api.issue_step_up_grant_v1(request_payload jsonb)
returns jsonb
language sql
security invoker
set search_path = ''
as $$ select private.issue_step_up_grant_v1_impl(request_payload); $$;

create or replace function api.request_operation_command_v1(request_payload jsonb)
returns jsonb
language plpgsql
security invoker
set search_path = ''
as $$
begin
  raise exception 'canonical_mutation_contract_not_activated' using errcode = '0A000';
end;
$$;

create or replace function api.review_operation_command_v1(review_payload jsonb)
returns jsonb
language plpgsql
security invoker
set search_path = ''
as $$
begin
  raise exception 'canonical_mutation_contract_not_activated' using errcode = '0A000';
end;
$$;

create or replace function api.act_on_operation_incident_v1(action_payload jsonb)
returns jsonb
language plpgsql
security invoker
set search_path = ''
as $$
begin
  raise exception 'canonical_mutation_contract_not_activated' using errcode = '0A000';
end;
$$;

create or replace function worker_api.acquire_worker_lease(
  p_account_id text,
  p_holder_id text,
  p_now timestamptz,
  p_ttl_seconds integer,
  p_release_sha text
)
returns table (
  account_id text,
  holder_id text,
  fencing_token bigint,
  acquired_at timestamptz,
  expires_at timestamptz
)
language sql
security invoker
set search_path = ''
as $$
  select * from private.acquire_worker_lease_impl(
    p_account_id, p_holder_id, p_now, p_ttl_seconds, p_release_sha
  );
$$;

create or replace function worker_api.renew_worker_lease(
  p_account_id text,
  p_holder_id text,
  p_fencing_token bigint,
  p_now timestamptz,
  p_ttl_seconds integer,
  p_release_sha text
)
returns table (
  account_id text,
  holder_id text,
  fencing_token bigint,
  acquired_at timestamptz,
  expires_at timestamptz
)
language sql
security invoker
set search_path = ''
as $$
  select * from private.renew_worker_lease_impl(
    p_account_id, p_holder_id, p_fencing_token, p_now, p_ttl_seconds, p_release_sha
  );
$$;

create or replace function worker_api.reserve_order_intent(
  p_intent_id uuid,
  p_semantic_key text,
  p_account_id text,
  p_environment text,
  p_strategy_version_id text,
  p_decision_id uuid,
  p_decision_feature_sha256 text,
  p_risk_result_id uuid,
  p_risk_allowed boolean,
  p_risk_reason_codes text[],
  p_risk_evaluated_at timestamptz,
  p_risk_expires_at timestamptz,
  p_symbol text,
  p_side text,
  p_quantity bigint,
  p_limit_price_krw bigint,
  p_decision_at timestamptz,
  p_signal_valid_from timestamptz,
  p_signal_valid_until timestamptz,
  p_execution_policy_version text,
  p_cost_schedule_version text,
  p_cost_schedule_evidence_sha256 text,
  p_cash_commitment_krw bigint,
  p_eligible_at timestamptz,
  p_expires_at timestamptz,
  p_gate_epoch bigint,
  p_holder_id text,
  p_fencing_token bigint,
  p_release_sha text
)
returns table (reserved boolean, intent_id uuid, reservation_id uuid, reason_code text)
language sql
security invoker
set search_path = ''
as $$
  select * from private.reserve_order_intent_impl(
    p_intent_id, p_semantic_key, p_account_id, p_environment,
    p_strategy_version_id, p_decision_id, p_decision_feature_sha256,
    p_risk_result_id, p_risk_allowed, p_risk_reason_codes,
    p_risk_evaluated_at, p_risk_expires_at, p_symbol, p_side,
    p_quantity, p_limit_price_krw, p_decision_at, p_signal_valid_from,
    p_signal_valid_until, p_execution_policy_version, p_cost_schedule_version,
    p_cost_schedule_evidence_sha256, p_cash_commitment_krw, p_eligible_at,
    p_expires_at, p_gate_epoch, p_holder_id, p_fencing_token, p_release_sha
  );
$$;

create or replace function worker_api.mark_dispatch_started(
  p_intent_id uuid,
  p_account_id text,
  p_environment text,
  p_holder_id text,
  p_fencing_token bigint,
  p_gate_epoch bigint,
  p_now timestamptz,
  p_request_sha256 text,
  p_client_order_key text
)
returns table (attempt_id uuid, prepared_at timestamptz, reason_code text)
language sql
security invoker
set search_path = ''
as $$
  select * from private.mark_dispatch_started_impl(
    p_intent_id, p_account_id, p_environment, p_holder_id, p_fencing_token,
    p_gate_epoch, p_now, p_request_sha256, p_client_order_key
  );
$$;

create or replace function worker_api.record_execution_observation(
  p_intent_id uuid,
  p_sequence integer,
  p_status text,
  p_provider_order_id text,
  p_provider_execution_id text,
  p_provider_observation_sha256 text,
  p_observed_at timestamptz,
  p_cumulative_quantity bigint,
  p_cumulative_gross_krw bigint,
  p_cumulative_commission_krw bigint,
  p_cumulative_tax_krw bigint,
  p_last_fill_quantity bigint,
  p_last_fill_price_krw bigint,
  p_last_fill_settlement_date date,
  p_accounting_postings jsonb,
  p_reason_code text,
  p_holder_id text,
  p_fencing_token bigint
)
returns table (
  observation_id uuid,
  inserted boolean,
  quarantined boolean,
  reason_code text
)
language sql
security invoker
set search_path = ''
as $$
  select * from private.record_execution_observation_impl(
    p_intent_id, p_sequence, p_status, p_provider_order_id,
    p_provider_execution_id, p_provider_observation_sha256,
    p_observed_at, p_cumulative_quantity,
    p_cumulative_gross_krw, p_cumulative_commission_krw,
    p_cumulative_tax_krw, p_last_fill_quantity, p_last_fill_price_krw,
    p_last_fill_settlement_date, p_accounting_postings, p_reason_code,
    p_holder_id, p_fencing_token
  );
$$;

create or replace function worker_api.claim_delivery_outbox(
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
language sql
security invoker
set search_path = ''
as $$
  select * from private.claim_delivery_outbox_impl(
    p_worker_id, p_now, p_limit, p_lease_seconds
  );
$$;

create or replace function worker_api.complete_outbox_delivery(
  p_outbox_id uuid,
  p_worker_id text,
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
    p_outbox_id, p_worker_id, p_now, p_external_receipt_id,
    p_external_receipt_sha256
  );
$$;

create or replace function worker_api.fail_outbox_delivery(
  p_outbox_id uuid,
  p_worker_id text,
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
    p_outbox_id, p_worker_id, p_now, p_error_code, p_retry_after_seconds
  );
$$;

create or replace function worker_api.acknowledge_operation_command(
  p_command_id uuid,
  p_phase text,
  p_holder_id text,
  p_release_sha text,
  p_now timestamptz,
  p_result_summary jsonb,
  p_failure_code text
)
returns table (
  command_id uuid,
  state text,
  claimed_at timestamptz,
  applied_at timestamptz,
  post_control_epoch bigint,
  failure_code text
)
language sql
security invoker
set search_path = ''
as $$
  select * from private.acknowledge_operation_command_impl(
    p_command_id, p_phase, p_holder_id, p_release_sha, p_now,
    p_result_summary, p_failure_code
  );
$$;

revoke all on schema private, api, worker_api from public, anon, authenticated, service_role;
grant usage on schema private, api to authenticated;
grant usage on schema private, worker_api to service_role;

revoke execute on all functions in schema private
  from public, anon, authenticated, service_role;
revoke execute on all functions in schema api
  from public, anon, authenticated, service_role;
revoke execute on all functions in schema worker_api
  from public, anon, authenticated, service_role;

grant execute on function
  private.get_my_access_profile(),
  private.get_platform_status(),
  private.list_operation_commands(integer),
  private.list_incidents(integer),
  private.list_audit_events(integer),
  private.get_accounting_summary(),
  private.get_desktop_operations_snapshot_v1_impl(),
  private.issue_step_up_grant_v1_impl(jsonb)
to authenticated;

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
  api.act_on_operation_incident_v1(jsonb)
to authenticated;

grant execute on function
  private.acquire_worker_lease_impl(text, text, timestamptz, integer, text),
  private.renew_worker_lease_impl(text, text, bigint, timestamptz, integer, text),
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
  )
to service_role;

grant execute on all functions in schema worker_api to service_role;

do $$
begin
  if exists (
    select 1
    from pg_proc as proc
    join pg_namespace as namespace on namespace.oid = proc.pronamespace
    where namespace.nspname in ('api', 'worker_api')
      and proc.prosecdef is true
  ) then
    raise exception 'exposed_schema_security_definer_forbidden';
  end if;
  if (
    select count(*)
    from pg_proc as proc
    join pg_namespace as namespace on namespace.oid = proc.pronamespace
    where namespace.nspname = 'worker_api'
  ) <> 9 then
    raise exception 'worker_api_allowlist_must_contain_exactly_nine_functions';
  end if;
end $$;
