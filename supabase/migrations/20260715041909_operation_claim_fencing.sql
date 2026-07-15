-- Bind operational commands and reconciliation work to the exact account
-- worker-lease generation. Legacy tokenless RPC overloads remain discoverable
-- only to fail closed during a rolling worker upgrade.

alter table private.operation_commands
  add column claim_release_sha text,
  add column claim_fencing_token bigint,
  add constraint operation_commands_claim_release_sha_check check (
    claim_release_sha is null
    or claim_release_sha ~ '^[0-9a-f]{40}([0-9a-f]{24})?$'
  ),
  add constraint operation_commands_claim_fencing_token_check check (
    claim_fencing_token is null or claim_fencing_token > 0
  ),
  add constraint operation_commands_claim_generation_pair_check check (
    (claim_release_sha is null) = (claim_fencing_token is null)
  );

-- Account opening is itself a Worker-applied command, so a pending account
-- needs one narrowly scoped bootstrap path into the normal lease/fence model.
-- No lease is granted or renewed unless a still-valid, approved/claimed
-- account-opening command exists for the same account, environment and release.
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
  authorization_time timestamptz := clock_timestamp();
begin
  perform private.require_service_role();
  if nullif(btrim(p_holder_id), '') is null or length(p_holder_id) > 160 then
    raise exception 'lease_holder_invalid' using errcode = '22023';
  end if;
  if p_ttl_seconds < 5 or p_ttl_seconds > 300 then
    raise exception 'lease_ttl_out_of_range' using errcode = '22023';
  end if;
  if p_now < authorization_time - interval '5 minutes'
     or p_now > authorization_time + interval '30 seconds' then
    raise exception 'lease_clock_out_of_range' using errcode = '22023';
  end if;
  if p_release_sha !~ '^[0-9a-f]{40}([0-9a-f]{24})?$' then
    raise exception 'release_sha_invalid' using errcode = '22023';
  end if;
  if not exists (
    select 1
    from private.trading_accounts as account
    where account.account_id = p_account_id
      and (
        account.state = 'open'
        or (
          account.state = 'pending_open'
          and exists (
            select 1
            from private.operation_commands as command
            where command.command_type = 'account_opening'
              and command.state in ('approved', 'claimed')
              and command.expires_at > authorization_time
              and command.requested_change->>'account_id' = account.account_id
              and command.requested_change->>'environment' = account.environment
              and (
                command.target_release_sha is null
                or command.target_release_sha = p_release_sha
              )
          )
        )
      )
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
  authorization_time timestamptz := clock_timestamp();
begin
  perform private.require_service_role();
  if p_ttl_seconds < 5 or p_ttl_seconds > 300 then
    raise exception 'lease_ttl_out_of_range' using errcode = '22023';
  end if;
  if p_now < authorization_time - interval '5 minutes'
     or p_now > authorization_time + interval '30 seconds' then
    raise exception 'lease_clock_out_of_range' using errcode = '22023';
  end if;
  if not exists (
    select 1
    from private.trading_accounts as account
    where account.account_id = p_account_id
      and (
        account.state = 'open'
        or (
          account.state = 'pending_open'
          and exists (
            select 1
            from private.operation_commands as command
            where command.command_type = 'account_opening'
              and command.state in ('approved', 'claimed')
              and command.expires_at > authorization_time
              and command.requested_change->>'account_id' = account.account_id
              and command.requested_change->>'environment' = account.environment
              and (
                command.target_release_sha is null
                or command.target_release_sha = p_release_sha
              )
          )
        )
      )
  ) then
    raise exception 'open_trading_account_required' using errcode = '23514';
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

create function private.claim_operation_command_batch_fenced_impl(
  p_account_id text,
  p_holder_id text,
  p_release_sha text,
  p_fencing_token bigint,
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
  authorization_time timestamptz := clock_timestamp();
  account_row private.trading_accounts%rowtype;
  candidate private.operation_commands%rowtype;
  claimed private.operation_commands%rowtype;
begin
  perform private.require_service_role();
  if nullif(btrim(p_account_id), '') is null
     or p_holder_id !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
     or p_release_sha !~ '^[0-9a-f]{40}([0-9a-f]{24})?$'
     or p_fencing_token is null
     or p_fencing_token <= 0
     or p_limit not between 1 and 25
     or p_now is null
     or p_now < authorization_time - interval '5 minutes'
     or p_now > authorization_time + interval '30 seconds' then
    raise exception 'operation_command_claim_parameters_invalid'
      using errcode = '22023';
  end if;

  select account.* into account_row
  from private.trading_accounts as account
  where account.account_id = p_account_id;
  if not found then
    raise exception 'operation_command_account_not_found' using errcode = 'P0002';
  end if;

  perform 1
  from private.worker_leases as lease
  where lease.account_id = p_account_id
    and lease.holder_id = p_holder_id
    and lease.release_sha = p_release_sha
    and lease.fencing_token = p_fencing_token
    and lease.expires_at > authorization_time
  for update;
  if not found then
    raise exception 'operation_command_worker_lease_stale_or_missing'
      using errcode = '40001';
  end if;

  for candidate in
    select command.*
    from private.operation_commands as command
    where command.expires_at > authorization_time
      and command.requested_change->>'account_id' = p_account_id
      and coalesce(
        nullif(command.requested_change->>'environment', ''),
        account_row.environment
      ) = account_row.environment
      and command.command_type in (
        'emergency_stop', 'account_opening', 'pause_paper', 'paper_resume',
        'strategy_promotion', 'contract_test_enable', 'risk_policy_change'
      )
      and (
        command.target_release_sha is null
        or command.target_release_sha = p_release_sha
      )
      and (
        command.state = 'approved'
        or (
          command.state = 'claimed'
          and (
            command.claim_expires_at <= authorization_time
            or (
              command.claimed_by_service = p_holder_id
              and command.claim_expires_at > authorization_time
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
       and candidate.claim_release_sha = p_release_sha
       and candidate.claim_fencing_token = p_fencing_token
       and candidate.claim_expires_at > authorization_time then
      claimed := candidate;
    else
      update private.operation_commands as command
      set state = 'claimed',
          claimed_by_service = p_holder_id,
          claimed_at = authorization_time,
          claim_expires_at = authorization_time + interval '30 seconds',
          claim_release_sha = p_release_sha,
          claim_fencing_token = p_fencing_token,
          revision = command.revision + 1
      where command.id = candidate.id
      returning command.* into claimed;

      insert into private.operation_command_events (
        command_id, event_type, actor_type, service_principal,
        event_summary, occurred_at
      ) values (
        claimed.id, 'claimed', 'worker', p_holder_id,
        jsonb_build_object(
          'account_id', p_account_id,
          'release_sha', p_release_sha,
          'fencing_token', p_fencing_token,
          'claim_revision', claimed.revision,
          'claim_expires_at', claimed.claim_expires_at,
          'reclaimed', candidate.state = 'claimed'
        ),
        authorization_time
      );
      perform private.write_audit_event(
        'worker', null, null, 'trading_worker', p_holder_id, p_release_sha,
        'operation_command_claimed', 'operation_command', claimed.id::text,
        gen_random_uuid(), claimed.id,
        case when candidate.state = 'claimed' then 'lease_reclaimed' else 'claimed' end,
        null,
        array[
          'state', 'claimed_at', 'claim_expires_at', 'revision',
          'account_id', 'fencing_token'
        ],
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
      account_row.environment,
      account_row.account_id,
      claimed.requested_change,
      claimed.requested_at,
      claimed.expires_at,
      claimed.revision,
      claimed.claimed_at,
      claimed.claim_expires_at;
  end loop;
end;
$$;

create function private.acknowledge_operation_command_fenced_impl(
  p_command_id uuid,
  p_phase text,
  p_account_id text,
  p_holder_id text,
  p_release_sha text,
  p_fencing_token bigint,
  p_expected_revision bigint,
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
  authorization_time timestamptz := clock_timestamp();
  command_row private.operation_commands%rowtype;
begin
  perform private.require_service_role();
  if p_command_id is null
     or p_phase not in ('applied', 'failed')
     or nullif(btrim(p_account_id), '') is null
     or p_holder_id !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
     or p_release_sha !~ '^[0-9a-f]{40}([0-9a-f]{24})?$'
     or p_fencing_token is null
     or p_fencing_token <= 0
     or p_expected_revision is null
     or p_expected_revision <= 0
     or p_now is null
     or p_now < authorization_time - interval '5 minutes'
     or p_now > authorization_time + interval '30 seconds' then
    raise exception 'operation_command_ack_payload_invalid' using errcode = '22023';
  end if;

  perform 1
  from private.worker_leases as lease
  where lease.account_id = p_account_id
    and lease.holder_id = p_holder_id
    and lease.release_sha = p_release_sha
    and lease.fencing_token = p_fencing_token
    and lease.expires_at > authorization_time
  for update;
  if not found then
    raise exception 'operation_command_worker_lease_stale_or_missing'
      using errcode = '40001';
  end if;

  select command.* into command_row
  from private.operation_commands as command
  where command.id = p_command_id
  for update;
  if not found then
    raise exception 'operation_command_not_found' using errcode = 'P0002';
  end if;
  if command_row.command_type not in (
    'emergency_stop', 'account_opening', 'pause_paper', 'paper_resume',
    'strategy_promotion', 'contract_test_enable', 'risk_policy_change'
  ) then
    raise exception 'operation_command_not_worker_applicable'
      using errcode = '0A000';
  end if;
  if command_row.state is distinct from 'claimed'
     or command_row.claimed_by_service is distinct from p_holder_id
     or command_row.claim_expires_at is null
     or command_row.claim_expires_at <= authorization_time
     or command_row.revision is distinct from p_expected_revision
     or command_row.claim_release_sha is distinct from p_release_sha
     or command_row.claim_fencing_token is distinct from p_fencing_token
     or command_row.requested_change->>'account_id' is distinct from p_account_id then
    raise exception 'operation_command_claim_generation_stale'
      using errcode = '40001';
  end if;

  return query
  select * from private.acknowledge_operation_command_impl(
    p_command_id, p_phase, p_holder_id, p_release_sha, p_now,
    p_result_summary, p_failure_code
  );
end;
$$;

drop function worker_api.claim_operation_command_batch(
  text, text, timestamptz, integer
);
drop function worker_api.acknowledge_operation_command(
  uuid, text, text, text, timestamptz, jsonb, text
);

create function worker_api.claim_operation_command_batch(
  p_account_id text,
  p_holder_id text,
  p_release_sha text,
  p_fencing_token bigint,
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
  select * from private.claim_operation_command_batch_fenced_impl(
    p_account_id, p_holder_id, p_release_sha, p_fencing_token, p_now, p_limit
  );
$$;

create function worker_api.acknowledge_operation_command(
  p_command_id uuid,
  p_phase text,
  p_account_id text,
  p_holder_id text,
  p_release_sha text,
  p_fencing_token bigint,
  p_expected_revision bigint,
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
  select * from private.acknowledge_operation_command_fenced_impl(
    p_command_id, p_phase, p_account_id, p_holder_id, p_release_sha,
    p_fencing_token, p_expected_revision, p_now, p_result_summary,
    p_failure_code
  );
$$;

create function worker_api.claim_operation_command_batch(
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
language plpgsql
security invoker
set search_path = ''
as $$
begin
  raise exception 'worker_upgrade_required' using errcode = '0A000';
end;
$$;

create function worker_api.acknowledge_operation_command(
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
security invoker
set search_path = ''
as $$
begin
  raise exception 'worker_upgrade_required' using errcode = '0A000';
end;
$$;

-- Reconciliation work consumes the scheduler's existing account lease. It
-- never acquires, renews, shortens, or expands worker_leases on its own.
create function private.claim_execution_reconciliation_batch_fenced_impl(
  p_account_id text,
  p_worker_id text,
  p_release_sha text,
  p_fencing_token bigint,
  p_now timestamptz,
  p_limit integer,
  p_after_priority integer,
  p_after_intent_id uuid,
  p_lease_seconds integer
)
returns table (
  intent_id uuid,
  attempt_id uuid,
  provider_order_id text,
  latest_observation_id uuid,
  latest_sequence integer,
  latest_status text,
  latest_observed_at timestamptz,
  latest_cumulative_quantity bigint,
  latest_cumulative_gross_krw bigint,
  latest_cumulative_commission_krw bigint,
  latest_cumulative_tax_krw bigint,
  observation_history_sha256 text,
  environment text,
  account_id text,
  symbol text,
  side text,
  quantity bigint,
  limit_price_krw bigint,
  eligible_at timestamptz,
  expires_at timestamptz,
  lease_fencing_token bigint,
  reservation_fencing_token bigint,
  control_epoch bigint,
  reservation_control_epoch bigint,
  intent_release_sha text,
  lease_release_sha text,
  recovery_disposition text,
  position_cost_basis_method text,
  position_quantity_snapshot bigint,
  position_average_cost_krw text,
  position_total_cost_krw bigint,
  position_projection_version bigint,
  position_cost_basis_sha256 text,
  semantic_key_sha256 text,
  decision_id uuid,
  risk_result_id uuid,
  execution_policy_version text,
  cost_schedule_version text,
  risk_policy_sha256 text,
  provider_contract_version text,
  provider_openapi_sha256 text,
  priority integer,
  next_reconcile_at timestamptz,
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
  if nullif(btrim(p_account_id), '') is null
     or p_worker_id !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
     or p_release_sha !~ '^[0-9a-f]{40}([0-9a-f]{24})?$'
     or p_fencing_token is null
     or p_fencing_token <= 0
     or p_limit < 1
     or p_limit > 50
     or p_lease_seconds < 5
     or p_lease_seconds > 300
     or (p_after_priority is null) <> (p_after_intent_id is null)
     or p_now is null
     or p_now < authorization_time - interval '5 minutes'
     or p_now > authorization_time + interval '30 seconds' then
    raise exception 'reconciliation_claim_parameters_invalid' using errcode = '22023';
  end if;

  perform 1
  from private.worker_leases as lease
  where lease.account_id = p_account_id
    and lease.holder_id = p_worker_id
    and lease.release_sha = p_release_sha
    and lease.fencing_token = p_fencing_token
    and lease.expires_at > authorization_time
  for update;
  if not found then
    raise exception 'reconciliation_worker_lease_stale_or_missing'
      using errcode = '40001';
  end if;

  return query
  with candidates as (
    select
      state.intent_id,
      intent.release_sha as intent_release_sha,
      exists (
        select 1
        from private.order_attempts as candidate_attempt
        where candidate_attempt.intent_id = state.intent_id
      ) as has_attempt
    from private.execution_reconciliation_state as state
    join private.order_intents as intent on intent.id = state.intent_id
    where intent.account_id = p_account_id
      and state.next_reconcile_at <= authorization_time
      and (
        state.state = 'pending'
        or (state.state = 'leased' and state.lease_expires_at <= authorization_time)
      )
      and (
        p_after_priority is null
        or state.priority > p_after_priority
        or (state.priority = p_after_priority and state.intent_id > p_after_intent_id)
      )
      and not exists (
        select 1
        from private.quarantined_execution_observations as quarantine
        where quarantine.intent_id = state.intent_id
      )
      and not exists (
        select 1
        from private.execution_observations as terminal
        where terminal.intent_id = state.intent_id
          and terminal.event_type in (
            'filled', 'canceled', 'expired', 'rejected',
            'failed_pre_dispatch', 'unknown_requires_manual_check'
          )
      )
    order by state.priority, state.intent_id
    limit p_limit
    for update of state skip locked
  ), claimed as (
    update private.execution_reconciliation_state as state
    set state = 'leased',
        lease_owner = p_worker_id,
        lease_expires_at = authorization_time
          + make_interval(secs => p_lease_seconds),
        attempt_count = state.attempt_count + 1,
        updated_at = greatest(
          authorization_time,
          state.updated_at + interval '1 microsecond'
        )
    from candidates
    where state.intent_id = candidates.intent_id
    returning state.*, candidates.intent_release_sha, candidates.has_attempt
  )
  select
    intent.id,
    attempt.id,
    binding.provider_order_id,
    observation.id,
    observation.sequence,
    observation.event_type,
    observation.observed_at,
    observation.cumulative_quantity,
    observation.cumulative_gross_krw,
    observation.cumulative_commission_krw,
    observation.cumulative_tax_krw,
    history.observation_history_sha256,
    intent.environment,
    intent.account_id,
    intent.symbol,
    intent.side,
    intent.quantity,
    intent.limit_price_krw,
    intent.eligible_at,
    intent.expires_at,
    claimed.claim_fencing_token,
    reservation.fencing_token,
    control.control_epoch,
    reservation.control_epoch,
    intent.release_sha,
    claimed.claim_release_sha,
    case
      when intent.release_sha <> p_release_sha and claimed.has_attempt
        then 'manual_release_takeover'
      when intent.release_sha <> p_release_sha
        then 'pre_dispatch_release_takeover'
      else 'same_release'
    end,
    intent.position_cost_basis_method,
    intent.position_quantity_snapshot,
    case
      when intent.position_average_cost_krw is null then null
      else to_char(
        intent.position_average_cost_krw,
        'FM99999999999999999999.0000'
      )
    end,
    intent.position_total_cost_krw,
    intent.position_projection_version,
    intent.position_cost_basis_sha256,
    intent.semantic_key_sha256,
    intent.decision_id,
    intent.risk_result_id,
    intent.execution_policy_version,
    intent.cost_schedule_version,
    intent.risk_policy_sha256,
    intent.provider_contract_version,
    intent.provider_openapi_sha256,
    claimed.priority,
    claimed.next_reconcile_at,
    claimed.lease_expires_at
  from claimed
  join private.order_intents as intent on intent.id = claimed.intent_id
  join private.execution_controls as control
    on control.account_id = intent.account_id
   and control.environment = intent.environment
  left join private.order_reservations as reservation
    on reservation.intent_id = intent.id
  left join private.order_attempts as attempt on attempt.intent_id = intent.id
  left join private.provider_order_bindings as binding
    on binding.attempt_id = attempt.id
  left join lateral (
    select latest.*
    from private.execution_observations as latest
    where latest.intent_id = intent.id
    order by latest.sequence desc
    limit 1
  ) as observation on true
  left join lateral (
    select pg_catalog.encode(
      public.digest(
        pg_catalog.convert_to(
          string_agg(
            history_observation.sequence::text || ':'
              || history_observation.provider_observation_sha256,
            '|' order by history_observation.sequence
          ),
          'UTF8'
        ),
        'sha256'
      ),
      'hex'
    ) as observation_history_sha256
    from private.execution_observations as history_observation
    where history_observation.intent_id = intent.id
  ) as history on true
  order by claimed.priority, claimed.intent_id;
end;
$$;

drop function worker_api.claim_execution_reconciliation_batch(
  text, text, timestamptz, integer, integer, uuid, integer
);

create function worker_api.claim_execution_reconciliation_batch(
  p_account_id text,
  p_worker_id text,
  p_release_sha text,
  p_fencing_token bigint,
  p_now timestamptz,
  p_limit integer,
  p_after_priority integer default null,
  p_after_intent_id uuid default null,
  p_lease_seconds integer default 30
)
returns table (
  intent_id uuid,
  attempt_id uuid,
  provider_order_id text,
  latest_observation_id uuid,
  latest_sequence integer,
  latest_status text,
  latest_observed_at timestamptz,
  latest_cumulative_quantity bigint,
  latest_cumulative_gross_krw bigint,
  latest_cumulative_commission_krw bigint,
  latest_cumulative_tax_krw bigint,
  observation_history_sha256 text,
  environment text,
  account_id text,
  symbol text,
  side text,
  quantity bigint,
  limit_price_krw bigint,
  eligible_at timestamptz,
  expires_at timestamptz,
  lease_fencing_token bigint,
  reservation_fencing_token bigint,
  control_epoch bigint,
  reservation_control_epoch bigint,
  intent_release_sha text,
  lease_release_sha text,
  recovery_disposition text,
  position_cost_basis_method text,
  position_quantity_snapshot bigint,
  position_average_cost_krw text,
  position_total_cost_krw bigint,
  position_projection_version bigint,
  position_cost_basis_sha256 text,
  semantic_key_sha256 text,
  decision_id uuid,
  risk_result_id uuid,
  execution_policy_version text,
  cost_schedule_version text,
  risk_policy_sha256 text,
  provider_contract_version text,
  provider_openapi_sha256 text,
  priority integer,
  next_reconcile_at timestamptz,
  lease_expires_at timestamptz
)
language sql
security invoker
set search_path = ''
as $$
  select * from private.claim_execution_reconciliation_batch_fenced_impl(
    p_account_id, p_worker_id, p_release_sha, p_fencing_token, p_now,
    p_limit, p_after_priority, p_after_intent_id, p_lease_seconds
  );
$$;

create function worker_api.claim_execution_reconciliation_batch(
  p_worker_id text,
  p_release_sha text,
  p_now timestamptz,
  p_limit integer,
  p_after_priority integer default null,
  p_after_intent_id uuid default null,
  p_lease_seconds integer default 30
)
returns table (
  intent_id uuid,
  attempt_id uuid,
  provider_order_id text,
  latest_observation_id uuid,
  latest_sequence integer,
  latest_status text,
  latest_observed_at timestamptz,
  latest_cumulative_quantity bigint,
  latest_cumulative_gross_krw bigint,
  latest_cumulative_commission_krw bigint,
  latest_cumulative_tax_krw bigint,
  observation_history_sha256 text,
  environment text,
  account_id text,
  symbol text,
  side text,
  quantity bigint,
  limit_price_krw bigint,
  eligible_at timestamptz,
  expires_at timestamptz,
  lease_fencing_token bigint,
  reservation_fencing_token bigint,
  control_epoch bigint,
  reservation_control_epoch bigint,
  intent_release_sha text,
  lease_release_sha text,
  recovery_disposition text,
  position_cost_basis_method text,
  position_quantity_snapshot bigint,
  position_average_cost_krw text,
  position_total_cost_krw bigint,
  position_projection_version bigint,
  position_cost_basis_sha256 text,
  semantic_key_sha256 text,
  decision_id uuid,
  risk_result_id uuid,
  execution_policy_version text,
  cost_schedule_version text,
  risk_policy_sha256 text,
  provider_contract_version text,
  provider_openapi_sha256 text,
  priority integer,
  next_reconcile_at timestamptz,
  lease_expires_at timestamptz
)
language plpgsql
security invoker
set search_path = ''
as $$
begin
  raise exception 'worker_upgrade_required' using errcode = '0A000';
end;
$$;

revoke all on function
  private.claim_operation_command_batch_impl(text, text, timestamptz, integer),
  private.acknowledge_operation_command_impl(
    uuid, text, text, text, timestamptz, jsonb, text
  ),
  private.claim_execution_reconciliation_batch_impl(
    text, text, timestamptz, integer, integer, uuid, integer
  ),
  private.claim_operation_command_batch_fenced_impl(
    text, text, text, bigint, timestamptz, integer
  ),
  private.acknowledge_operation_command_fenced_impl(
    uuid, text, text, text, text, bigint, bigint, timestamptz, jsonb, text
  ),
  private.claim_execution_reconciliation_batch_fenced_impl(
    text, text, text, bigint, timestamptz, integer, integer, uuid, integer
  )
from public, anon, authenticated, service_role;

grant execute on function
  private.claim_operation_command_batch_fenced_impl(
    text, text, text, bigint, timestamptz, integer
  ),
  private.acknowledge_operation_command_fenced_impl(
    uuid, text, text, text, text, bigint, bigint, timestamptz, jsonb, text
  ),
  private.claim_execution_reconciliation_batch_fenced_impl(
    text, text, text, bigint, timestamptz, integer, integer, uuid, integer
  )
to service_role;

revoke all on function
  worker_api.claim_operation_command_batch(
    text, text, text, bigint, timestamptz, integer
  ),
  worker_api.claim_operation_command_batch(text, text, timestamptz, integer),
  worker_api.acknowledge_operation_command(
    uuid, text, text, text, text, bigint, bigint, timestamptz, jsonb, text
  ),
  worker_api.acknowledge_operation_command(
    uuid, text, text, text, timestamptz, jsonb, text
  ),
  worker_api.claim_execution_reconciliation_batch(
    text, text, text, bigint, timestamptz, integer, integer, uuid, integer
  ),
  worker_api.claim_execution_reconciliation_batch(
    text, text, timestamptz, integer, integer, uuid, integer
  )
from public, anon, authenticated, service_role;

grant execute on function
  worker_api.claim_operation_command_batch(
    text, text, text, bigint, timestamptz, integer
  ),
  worker_api.acknowledge_operation_command(
    uuid, text, text, text, text, bigint, bigint, timestamptz, jsonb, text
  ),
  worker_api.claim_execution_reconciliation_batch(
    text, text, text, bigint, timestamptz, integer, integer, uuid, integer
  )
to service_role;
