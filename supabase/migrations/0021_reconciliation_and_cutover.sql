-- Restart-safe reconciliation claims and final Data API cutover boundary.

create or replace function private.claim_execution_reconciliation_batch_impl(
  p_worker_id text,
  p_release_sha text,
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
  if p_worker_id !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
     or p_release_sha !~ '^[0-9a-f]{40}([0-9a-f]{24})?$'
     or p_limit < 1 or p_limit > 50
     or p_lease_seconds < 5 or p_lease_seconds > 300
     or (p_after_priority is null) <> (p_after_intent_id is null)
     or p_now < authorization_time - interval '5 minutes'
     or p_now > authorization_time + interval '30 seconds' then
    raise exception 'reconciliation_claim_parameters_invalid' using errcode = '22023';
  end if;
  return query
  with candidates as (
    select
      state.intent_id,
      intent.account_id,
      intent.environment,
      intent.release_sha as intent_release_sha,
      intent.correlation_id,
      intent.release_sha <> p_release_sha as release_mismatch,
      exists (
        select 1
        from private.order_attempts as candidate_attempt
        where candidate_attempt.intent_id = state.intent_id
      ) as has_attempt,
      gen_random_uuid() as manual_run_id,
      gen_random_uuid() as manual_break_id
    from private.execution_reconciliation_state as state
    join private.order_intents as intent on intent.id = state.intent_id
    where state.next_reconcile_at <= p_now
      and (
        state.state = 'pending'
        or (state.state = 'leased' and state.lease_expires_at <= p_now)
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
      and (
        not exists (
          select 1 from private.worker_leases as current_lease
          where current_lease.account_id = intent.account_id
        )
        or exists (
          select 1 from private.worker_leases as current_lease
          where current_lease.account_id = intent.account_id
            and (
              current_lease.expires_at <= authorization_time
              or (
                current_lease.holder_id = p_worker_id
                and current_lease.release_sha = p_release_sha
              )
            )
        )
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
    for update skip locked
  ), candidate_accounts as (
    select distinct candidate.account_id
    from candidates as candidate
  ), leased_accounts as (
    insert into private.worker_leases as current_lease (
      account_id, holder_id, fencing_token, acquired_at, renewed_at,
      expires_at, release_sha
    )
    select
      account.account_id, p_worker_id, 1, authorization_time,
      authorization_time,
      authorization_time + make_interval(secs => p_lease_seconds),
      p_release_sha
    from candidate_accounts as account
    on conflict on constraint worker_leases_pkey do update
    set holder_id = excluded.holder_id,
        fencing_token = case
          when current_lease.holder_id = p_worker_id
            and current_lease.release_sha = p_release_sha
            and current_lease.expires_at > authorization_time
            then current_lease.fencing_token
          else current_lease.fencing_token + 1
        end,
        acquired_at = case
          when current_lease.holder_id = p_worker_id
            and current_lease.release_sha = p_release_sha
            and current_lease.expires_at > authorization_time
            then current_lease.acquired_at
          else authorization_time
        end,
        renewed_at = authorization_time,
        expires_at = excluded.expires_at,
        release_sha = excluded.release_sha
    where current_lease.expires_at <= authorization_time
       or (
         current_lease.holder_id = p_worker_id
         and current_lease.release_sha = p_release_sha
       )
    returning current_lease.*
  ), claimed as (
    update private.execution_reconciliation_state as state
    set state = case
          when candidates.release_mismatch and candidates.has_attempt
            then 'manual'
          else 'leased'
        end,
        lease_owner = case
          when candidates.release_mismatch and candidates.has_attempt
            then null
          else p_worker_id
        end,
        lease_expires_at = case
          when candidates.release_mismatch and candidates.has_attempt
            then null
          else authorization_time + make_interval(secs => p_lease_seconds)
        end,
        attempt_count = state.attempt_count + 1,
        last_reason_code = case
          when candidates.release_mismatch and candidates.has_attempt
            then 'release_takeover_manual_required'
          else state.last_reason_code
        end,
        updated_at = p_now
    from candidates
    join leased_accounts as lease on lease.account_id = candidates.account_id
    where state.intent_id = candidates.intent_id
    returning
      state.*,
      candidates.account_id,
      candidates.environment,
      candidates.intent_release_sha,
      candidates.correlation_id,
      candidates.release_mismatch,
      candidates.has_attempt,
      candidates.manual_run_id,
      candidates.manual_break_id
  ), manual_runs as (
    insert into private.reconciliation_runs (
      id, account_id, environment, started_at, completed_at, result, release_sha
    )
    select
      claimed.manual_run_id,
      claimed.account_id,
      claimed.environment,
      authorization_time,
      authorization_time,
      'breaks_found',
      p_release_sha
    from claimed
    where claimed.state = 'manual'
    returning id
  ), manual_breaks as (
    insert into private.reconciliation_breaks (
      id, run_id, account_id, break_type, state, detected_at, summary_code
    )
    select
      claimed.manual_break_id,
      claimed.manual_run_id,
      claimed.account_id,
      'execution',
      'open',
      authorization_time,
      'release_takeover_manual_required'
    from claimed
    join manual_runs on manual_runs.id = claimed.manual_run_id
    where claimed.state = 'manual'
    returning id
  ), manual_incidents as (
    insert into private.incidents (
      severity, incident_type, summary_code, correlation_id, opened_at
    )
    select
      'critical',
      'execution_release_takeover',
      'release_takeover_manual_required',
      claimed.correlation_id,
      authorization_time
    from claimed
    join manual_breaks on manual_breaks.id = claimed.manual_break_id
    where claimed.state = 'manual'
    returning id
  ), manual_outbox as (
    insert into private.delivery_outbox (
      event_type, aggregate_type, aggregate_id, dedupe_key, payload,
      destination_type, available_at
    )
    select
      'execution_release_takeover_manual_required',
      'order_intent',
      claimed.intent_id::text,
      'release-takeover-manual:' || claimed.intent_id::text,
      jsonb_build_object(
        'intent_id', claimed.intent_id,
        'reconciliation_run_id', claimed.manual_run_id,
        'reconciliation_break_id', claimed.manual_break_id,
        'intent_release_sha', claimed.intent_release_sha,
        'lease_release_sha', p_release_sha,
        'severity', 'critical'
      ),
      'incident_alert',
      authorization_time
    from claimed
    join manual_breaks on manual_breaks.id = claimed.manual_break_id
    where claimed.state = 'manual'
    on conflict (dedupe_key) do nothing
    returning id
  ), manual_audits as (
    select private.write_audit_event(
      'worker', null, null, 'trading_worker', p_worker_id, p_release_sha,
      'execution_release_takeover_manual_required', 'order_intent',
      claimed.intent_id::text, claimed.correlation_id, null,
      'release_takeover_manual_required', null,
      array['release_sha', 'reconciliation_state'],
      pg_catalog.encode(
        public.digest(
          pg_catalog.convert_to(claimed.intent_release_sha, 'UTF8'),
          'sha256'
        ),
        'hex'
      ),
      pg_catalog.encode(
        public.digest(
          pg_catalog.convert_to(p_release_sha, 'UTF8'),
          'sha256'
        ),
        'hex'
      ),
      null
    ) as audit_event_id
    from claimed
    where claimed.state = 'manual'
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
    lease.fencing_token,
    reservation.fencing_token,
    control.control_epoch,
    reservation.control_epoch,
    intent.release_sha,
    lease.release_sha,
    case
      when claimed.release_mismatch and claimed.has_attempt
        then 'manual_release_takeover'
      when claimed.release_mismatch
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
    lease.expires_at
  from claimed
  join private.order_intents as intent on intent.id = claimed.intent_id
  join leased_accounts as lease on lease.account_id = intent.account_id
  join private.execution_controls as control
    on control.account_id = intent.account_id
   and control.environment = intent.environment
  left join private.order_reservations as reservation
    on reservation.intent_id = intent.id
  left join private.order_attempts as attempt on attempt.intent_id = intent.id
  left join private.provider_order_bindings as binding on binding.attempt_id = attempt.id
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
  cross join lateral (
    select
      (select count(*) from manual_runs) as run_count,
      (select count(*) from manual_breaks) as break_count,
      (select count(*) from manual_incidents) as incident_count,
      (select count(*) from manual_outbox) as outbox_count,
      (select count(*) from manual_audits) as audit_count
  ) as manual_effects
  order by claimed.priority, claimed.intent_id;
end;
$$;

create or replace function private.complete_execution_reconciliation_impl(
  p_intent_id uuid,
  p_worker_id text,
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
  next_state text;
  next_time timestamptz;
begin
  perform private.require_service_role();
  if p_outcome not in ('reschedule', 'complete', 'manual')
     or nullif(btrim(p_reason_code), '') is null
     or (p_outcome = 'reschedule' and (
       p_next_reconcile_at is null or p_next_reconcile_at <= p_now
     ))
     or (p_outcome <> 'reschedule' and p_next_reconcile_at is not null) then
    raise exception 'reconciliation_completion_parameters_invalid' using errcode = '22023';
  end if;
  if p_outcome = 'complete' and not exists (
    select 1 from private.execution_observations
    where private.execution_observations.intent_id = p_intent_id
      and event_type in ('filled', 'canceled', 'expired', 'rejected', 'failed_pre_dispatch')
  ) then
    raise exception 'terminal_observation_required_for_completion' using errcode = '23514';
  end if;
  next_state := case when p_outcome = 'reschedule' then 'pending' else p_outcome end;
  next_time := coalesce(p_next_reconcile_at, p_now);
  update private.execution_reconciliation_state as current_state
  set state = next_state,
      next_reconcile_at = next_time,
      lease_owner = null,
      lease_expires_at = null,
      last_reason_code = p_reason_code,
      updated_at = p_now
  where current_state.intent_id = p_intent_id
    and current_state.state = 'leased'
    and current_state.lease_owner = p_worker_id
    and current_state.lease_expires_at > p_now;
  if not found then
    raise exception 'reconciliation_lease_not_owned_or_expired' using errcode = '40001';
  end if;
  return query select p_intent_id, next_state, next_time;
end;
$$;

create or replace function worker_api.claim_execution_reconciliation_batch(
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
language sql
security invoker
set search_path = ''
as $$
  select * from private.claim_execution_reconciliation_batch_impl(
    p_worker_id, p_release_sha, p_now, p_limit, p_after_priority, p_after_intent_id,
    p_lease_seconds
  );
$$;

create or replace function worker_api.complete_execution_reconciliation(
  p_intent_id uuid,
  p_worker_id text,
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
    p_intent_id, p_worker_id, p_now, p_outcome,
    p_next_reconcile_at, p_reason_code
  );
$$;

revoke execute on function
  private.claim_execution_reconciliation_batch_impl(
    text, text, timestamptz, integer, integer, uuid, integer
  ),
  private.complete_execution_reconciliation_impl(
    uuid, text, timestamptz, text, timestamptz, text
  )
from public, anon, authenticated, service_role;
grant execute on function
  private.claim_execution_reconciliation_batch_impl(
    text, text, timestamptz, integer, integer, uuid, integer
  ),
  private.complete_execution_reconciliation_impl(
    uuid, text, timestamptz, text, timestamptz, text
  )
to service_role;
grant execute on all functions in schema worker_api to service_role;

-- Final cutover: no desktop or worker role has direct public-schema access.
revoke all on all tables in schema public
  from public, anon, authenticated, service_role;
revoke all on all sequences in schema public
  from public, anon, authenticated, service_role;
revoke execute on all functions in schema public
  from public, anon, authenticated, service_role;

do $$
begin
  if (
    select count(*)
    from pg_proc as proc
    join pg_namespace as namespace on namespace.oid = proc.pronamespace
    where namespace.nspname = 'worker_api'
  ) <> 11 then
    raise exception 'worker_api_allowlist_must_contain_exactly_eleven_functions';
  end if;
  if exists (
    select 1
    from pg_proc as proc
    join pg_namespace as namespace on namespace.oid = proc.pronamespace
    where namespace.nspname in ('api', 'worker_api') and proc.prosecdef
  ) then
    raise exception 'exposed_schema_security_definer_forbidden';
  end if;
end $$;
