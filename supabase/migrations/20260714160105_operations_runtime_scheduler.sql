-- Independent operations stage cadence and monitor-safe heartbeat evidence.

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
  scheduler_stage_keys text[];
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
    if p_details->>'checkpoint' not in (
         'cycle_completed',
         'operations_completed',
         'independent_scheduler_running'
       )
       or nullif(p_details->>'completed_at', '') is null
       or (
         p_details->>'checkpoint' = 'cycle_completed'
         and p_details->>'component' is distinct from 'trading_cycle'
       )
       or (
         p_details->>'checkpoint' in (
           'operations_completed', 'independent_scheduler_running'
         )
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

    if p_details->>'checkpoint' = 'independent_scheduler_running' then
      if jsonb_typeof(p_details->'stage_last_completed_at') <> 'object' then
        raise exception 'scheduler_heartbeat_stage_evidence_required'
          using errcode = '22023';
      end if;
      select array_agg(stage.key order by stage.key)
      into scheduler_stage_keys
      from jsonb_object_keys(p_details->'stage_last_completed_at') as stage(key);
      if scheduler_stage_keys is distinct from array[
           'commands', 'execution', 'outbox', 'reconciliation', 'settlement'
         ]::text[]
         or exists (
           select 1
           from jsonb_each(p_details->'stage_last_completed_at') as stage(key, value)
           where case
             when jsonb_typeof(stage.value) <> 'string' then true
             else (stage.value #>> '{}')::timestamptz
               not between p_now - interval '1 hour'
                   and p_now + interval '30 seconds'
           end
         ) then
        raise exception 'scheduler_heartbeat_stage_evidence_invalid'
          using errcode = '22023';
      end if;
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

revoke execute on function private.record_worker_heartbeat_impl(
  text, text, jsonb, timestamptz, text
) from public, anon, authenticated, service_role;
grant execute on function private.record_worker_heartbeat_impl(
  text, text, jsonb, timestamptz, text
) to service_role;

create function private.get_dead_man_snapshot_v1_impl(
  p_account_id text,
  p_now timestamptz,
  p_monitor_release_sha text
)
returns table (
  observed_at timestamptz,
  latest_heartbeat_at timestamptz,
  latest_heartbeat_status text,
  latest_heartbeat_release_sha text,
  commands_last_completed_at timestamptz,
  execution_last_completed_at timestamptz,
  settlement_last_completed_at timestamptz,
  reconciliation_last_completed_at timestamptz,
  outbox_last_completed_at timestamptz,
  lease_holder_id text,
  lease_expires_at timestamptz,
  lease_release_sha text,
  oldest_pending_outbox_at timestamptz,
  dead_letter_count bigint,
  active_incident_opened_at timestamptz,
  active_incident_acknowledged_at timestamptz
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
     or p_monitor_release_sha !~ '^[0-9a-f]{40}([0-9a-f]{24})?$'
     or p_now < authorization_time - interval '5 minutes'
     or p_now > authorization_time + interval '30 seconds'
     or not exists (
       select 1
       from private.trading_accounts as account
       where account.account_id = p_account_id
     ) then
    raise exception 'dead_man_snapshot_parameters_invalid' using errcode = '22023';
  end if;

  return query
  with current_lease as (
    select lease.holder_id, lease.expires_at, lease.release_sha
    from private.worker_leases as lease
    where lease.account_id = p_account_id
  ), latest_heartbeat as (
    select heartbeat.created_at,
           heartbeat.status,
           heartbeat.details->>'release_sha' as release_sha,
           case
             when heartbeat.details->>'checkpoint' = 'independent_scheduler_running'
             then (heartbeat.details->'stage_last_completed_at'->>'commands')::timestamptz
           end as commands_last_completed_at,
           case
             when heartbeat.details->>'checkpoint' = 'independent_scheduler_running'
             then (heartbeat.details->'stage_last_completed_at'->>'execution')::timestamptz
           end as execution_last_completed_at,
           case
             when heartbeat.details->>'checkpoint' = 'independent_scheduler_running'
             then (heartbeat.details->'stage_last_completed_at'->>'settlement')::timestamptz
           end as settlement_last_completed_at,
           case
             when heartbeat.details->>'checkpoint' = 'independent_scheduler_running'
             then (heartbeat.details->'stage_last_completed_at'->>'reconciliation')::timestamptz
           end as reconciliation_last_completed_at,
           case
             when heartbeat.details->>'checkpoint' = 'independent_scheduler_running'
             then (heartbeat.details->'stage_last_completed_at'->>'outbox')::timestamptz
           end as outbox_last_completed_at
    from public.worker_heartbeats as heartbeat
    join current_lease as lease
      on heartbeat.worker_name = 'trading-worker:' || lease.holder_id
    order by heartbeat.created_at desc, heartbeat.id desc
    limit 1
  ), outbox_state as (
    select
      min(outbox.created_at) filter (
        where outbox.status in ('pending', 'leased')
      ) as oldest_pending_at,
      count(*) filter (where outbox.status = 'dead_letter') as dead_letters
    from private.delivery_outbox as outbox
  ), active_incident as (
    select incident.opened_at, incident.acknowledged_at
    from private.incidents as incident
    where incident.status <> 'resolved'
      and incident.severity = 'critical'
    order by (incident.acknowledged_at is not null), incident.opened_at, incident.id
    limit 1
  )
  select authorization_time,
         heartbeat.created_at,
         heartbeat.status,
         heartbeat.release_sha,
         heartbeat.commands_last_completed_at,
         heartbeat.execution_last_completed_at,
         heartbeat.settlement_last_completed_at,
         heartbeat.reconciliation_last_completed_at,
         heartbeat.outbox_last_completed_at,
         lease.holder_id,
         lease.expires_at,
         lease.release_sha,
         outbox.oldest_pending_at,
         outbox.dead_letters,
         incident.opened_at,
         incident.acknowledged_at
  from outbox_state as outbox
  left join current_lease as lease on true
  left join latest_heartbeat as heartbeat on true
  left join active_incident as incident on true;
end;
$$;

create function worker_api.get_dead_man_snapshot_v1(
  p_account_id text,
  p_now timestamptz,
  p_monitor_release_sha text
)
returns table (
  observed_at timestamptz,
  latest_heartbeat_at timestamptz,
  latest_heartbeat_status text,
  latest_heartbeat_release_sha text,
  commands_last_completed_at timestamptz,
  execution_last_completed_at timestamptz,
  settlement_last_completed_at timestamptz,
  reconciliation_last_completed_at timestamptz,
  outbox_last_completed_at timestamptz,
  lease_holder_id text,
  lease_expires_at timestamptz,
  lease_release_sha text,
  oldest_pending_outbox_at timestamptz,
  dead_letter_count bigint,
  active_incident_opened_at timestamptz,
  active_incident_acknowledged_at timestamptz
)
language sql
security invoker
set search_path = ''
as $$
  select * from private.get_dead_man_snapshot_v1_impl(
    p_account_id, p_now, p_monitor_release_sha
  );
$$;

revoke execute on function private.get_dead_man_snapshot_v1_impl(
  text, timestamptz, text
) from public, anon, authenticated, service_role;
grant execute on function private.get_dead_man_snapshot_v1_impl(
  text, timestamptz, text
) to service_role;

revoke all on function worker_api.get_dead_man_snapshot_v1(
  text, timestamptz, text
) from public, anon, authenticated, service_role;
grant execute on function worker_api.get_dead_man_snapshot_v1(
  text, timestamptz, text
) to service_role;
