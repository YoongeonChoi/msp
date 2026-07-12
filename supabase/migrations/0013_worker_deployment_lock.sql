alter table public.bot_settings
  add column if not exists deployment_lock boolean not null default false,
  add column if not exists deployment_target_sha text,
  add column if not exists deployment_started_at timestamptz,
  add column if not exists deployment_triggered_at timestamptz,
  add column if not exists deployment_completed_at timestamptz;

update public.bot_settings
set enabled = false,
    live_order_allowed = false,
    deployment_lock = false,
    deployment_target_sha = null,
    deployment_started_at = null,
    deployment_triggered_at = null
where deployment_lock is true
   or deployment_target_sha is not null
   or deployment_started_at is not null
   or deployment_triggered_at is not null;

alter table public.bot_settings
  drop constraint if exists bot_settings_deployment_lock_state_check;

alter table public.bot_settings
  add constraint bot_settings_deployment_lock_state_check
  check (
    (
      deployment_lock is false
      and deployment_target_sha is null
      and deployment_started_at is null
      and deployment_triggered_at is null
    )
    or (
      deployment_lock is true
      and deployment_target_sha is not null
      and deployment_target_sha ~ '^[0-9a-f]{40}([0-9a-f]{24})?$'
      and deployment_started_at is not null
      and (
        deployment_triggered_at is null
        or deployment_triggered_at >= deployment_started_at
      )
      and enabled is false
      and live_order_allowed is false
    )
  );

create or replace function public.guard_bot_settings_live_enable()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
declare
  live_enable_command_id uuid;
  old_executable boolean;
  new_executable boolean;
begin
  old_executable := old.enabled is true
    and old.mode = 'live'
    and old.live_order_allowed is true;
  new_executable := new.enabled is true
    and new.mode = 'live'
    and new.live_order_allowed is true;

  if (
    new.deployment_lock is distinct from old.deployment_lock
    or new.deployment_target_sha is distinct from old.deployment_target_sha
    or new.deployment_started_at is distinct from old.deployment_started_at
    or new.deployment_triggered_at is distinct from old.deployment_triggered_at
    or new.deployment_completed_at is distinct from old.deployment_completed_at
  ) and (select auth.role()) is distinct from 'service_role' then
    raise exception 'deployment_lock_requires_service_role'
      using errcode = '42501';
  end if;

  if new.deployment_lock is true and (new.enabled is true or new.live_order_allowed is true) then
    raise exception 'deployment_lock_requires_disabled_live_execution'
      using errcode = '23514';
  end if;

  if new.live_order_allowed is true and new.mode <> 'live' then
    raise exception 'live_order_allowed_requires_live_mode'
      using errcode = '23514';
  end if;

  if new.live_order_allowed is true and new.enabled is not true then
    raise exception 'live_order_allowed_requires_enabled_bot'
      using errcode = '23514';
  end if;

  if new_executable and not old_executable then
    if new.deployment_lock is true then
      raise exception 'live_execution_blocked_by_deployment_lock'
        using errcode = '23514';
    end if;

    select id
    into live_enable_command_id
      from public.manual_commands
      where command_type = 'request_live_enable'
        and status = 'accepted'
        and expires_at > now()
        and applied_at is null
        and requested_by is not null
        and reviewed_by is not null
        and reviewed_at is not null
        and reviewed_at > coalesce(
          new.deployment_completed_at,
          '-infinity'::timestamptz
        )
        and reviewed_by <> requested_by
        and nullif(btrim(payload->>'provider_contract_version'), '') is not null
        and nullif(btrim(payload->>'risk_report_id'), '') is not null
        and nullif(btrim(payload->>'release_version'), '') is not null
      order by reviewed_at desc
      limit 1
      for update skip locked;

    if live_enable_command_id is null then
      raise exception 'live_execution_requires_fresh_accepted_manual_command'
        using errcode = '23514';
    end if;

    update public.manual_commands
    set status = 'applied',
        applied_at = now()
    where id = live_enable_command_id;
  end if;

  return new;
end;
$$;

create or replace function public.begin_worker_deployment(target_sha text)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  normalized_sha text := lower(btrim(target_sha));
  current_lock boolean;
begin
  if (select auth.role()) is distinct from 'service_role' then
    raise exception 'begin_worker_deployment_requires_service_role'
      using errcode = '42501';
  end if;

  if normalized_sha is null
     or normalized_sha !~ '^[0-9a-f]{40}([0-9a-f]{24})?$' then
    raise exception 'deployment_target_sha_invalid'
      using errcode = '22023';
  end if;

  select deployment_lock
  into current_lock
  from public.bot_settings
  where id = 'singleton'
  for update;

  if not found then
    raise exception 'bot_settings_singleton_missing';
  end if;

  if current_lock is true then
    raise exception 'deployment_already_locked'
      using errcode = '55000';
  end if;

  update public.bot_settings
  set enabled = false,
      live_order_allowed = false,
      deployment_lock = true,
      deployment_target_sha = normalized_sha,
      deployment_started_at = now(),
      deployment_triggered_at = null,
      updated_at = now()
  where id = 'singleton';

  return jsonb_build_object(
    'deployment_lock', true,
    'target_sha_short', left(normalized_sha, 12)
  );
end;
$$;

create or replace function public.mark_worker_deployment_triggered(target_sha text)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  normalized_sha text := lower(btrim(target_sha));
  triggered_at timestamptz;
begin
  if (select auth.role()) is distinct from 'service_role' then
    raise exception 'mark_worker_deployment_triggered_requires_service_role'
      using errcode = '42501';
  end if;

  if normalized_sha is null
     or normalized_sha !~ '^[0-9a-f]{40}([0-9a-f]{24})?$' then
    raise exception 'deployment_target_sha_invalid'
      using errcode = '22023';
  end if;

  triggered_at := clock_timestamp();
  update public.bot_settings
  set deployment_triggered_at = triggered_at,
      updated_at = now()
  where id = 'singleton'
    and deployment_lock is true
    and deployment_target_sha = normalized_sha
    and deployment_triggered_at is null;

  if not found then
    raise exception 'deployment_trigger_state_mismatch'
      using errcode = '23514';
  end if;

  return jsonb_build_object(
    'deployment_lock', true,
    'target_sha_short', left(normalized_sha, 12),
    'deployment_triggered', true,
    'deployment_triggered_at', triggered_at
  );
end;
$$;

create or replace function public.complete_worker_deployment(
  target_sha text,
  max_age_seconds integer default 300
)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  normalized_sha text := lower(btrim(target_sha));
  heartbeat_status text;
  heartbeat_release_sha text;
  heartbeat_deployment_lock boolean;
  heartbeat_deployment_target_sha text;
  heartbeat_created_at timestamptz;
  deployment_triggered_at_value timestamptz;
begin
  if (select auth.role()) is distinct from 'service_role' then
    raise exception 'complete_worker_deployment_requires_service_role'
      using errcode = '42501';
  end if;

  if normalized_sha is null
     or normalized_sha !~ '^[0-9a-f]{40}([0-9a-f]{24})?$' then
    raise exception 'deployment_target_sha_invalid'
      using errcode = '22023';
  end if;

  if max_age_seconds is null
     or max_age_seconds < 1
     or max_age_seconds > 3600 then
    raise exception 'deployment_heartbeat_max_age_invalid'
      using errcode = '22023';
  end if;

  select settings.deployment_triggered_at
  into deployment_triggered_at_value
  from public.bot_settings as settings
  where settings.id = 'singleton'
    and settings.deployment_lock is true
    and settings.deployment_target_sha = normalized_sha
  for update;

  if not found then
    raise exception 'deployment_lock_target_mismatch'
      using errcode = '23514';
  end if;

  if deployment_triggered_at_value is null then
    raise exception 'deployment_not_marked_triggered'
      using errcode = '23514';
  end if;

  select status,
         lower(details->>'release_sha'),
         case
           when jsonb_typeof(details->'deployment_lock') = 'boolean'
             then (details->>'deployment_lock')::boolean
           else null
         end,
         lower(details->>'deployment_target_sha'),
         created_at
  into heartbeat_status,
       heartbeat_release_sha,
       heartbeat_deployment_lock,
       heartbeat_deployment_target_sha,
       heartbeat_created_at
  from public.worker_heartbeats
  order by created_at desc
  limit 1;

  if heartbeat_created_at is null then
    raise exception 'deployment_heartbeat_missing'
      using errcode = '23514';
  end if;

  if heartbeat_status <> 'ok' then
    raise exception 'deployment_heartbeat_not_ok'
      using errcode = '23514';
  end if;

  if heartbeat_release_sha is distinct from normalized_sha then
    raise exception 'deployment_heartbeat_release_mismatch'
      using errcode = '23514';
  end if;

  if heartbeat_deployment_lock is not true
     or heartbeat_deployment_target_sha is distinct from normalized_sha then
    raise exception 'deployment_heartbeat_lock_mismatch'
      using errcode = '23514';
  end if;

  if heartbeat_created_at < now() - make_interval(secs => max_age_seconds)
     or heartbeat_created_at > now() + interval '5 minutes' then
    raise exception 'deployment_heartbeat_not_fresh'
      using errcode = '23514';
  end if;

  if heartbeat_created_at <= deployment_triggered_at_value then
    raise exception 'deployment_heartbeat_precedes_trigger'
      using errcode = '23514';
  end if;

  update public.bot_settings
  set deployment_lock = false,
      deployment_target_sha = null,
      deployment_started_at = null,
      deployment_triggered_at = null,
      deployment_completed_at = now(),
      updated_at = now()
  where id = 'singleton';

  return jsonb_build_object(
    'deployment_lock', false,
    'target_sha_short', left(normalized_sha, 12)
  );
end;
$$;

create or replace function public.abort_worker_deployment(target_sha text)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  normalized_sha text := lower(btrim(target_sha));
begin
  if (select auth.role()) is distinct from 'service_role' then
    raise exception 'abort_worker_deployment_requires_service_role'
      using errcode = '42501';
  end if;

  if normalized_sha is null
     or normalized_sha !~ '^[0-9a-f]{40}([0-9a-f]{24})?$' then
    raise exception 'deployment_target_sha_invalid'
      using errcode = '22023';
  end if;

  update public.bot_settings
  set enabled = false,
      live_order_allowed = false,
      deployment_lock = false,
      deployment_target_sha = null,
      deployment_started_at = null,
      deployment_triggered_at = null,
      deployment_completed_at = now(),
      updated_at = now()
  where id = 'singleton'
    and deployment_lock is true
    and deployment_target_sha = normalized_sha;

  if not found then
    raise exception 'deployment_lock_target_mismatch'
      using errcode = '23514';
  end if;

  return jsonb_build_object(
    'deployment_lock', false,
    'target_sha_short', left(normalized_sha, 12),
    'deployment_aborted', true
  );
end;
$$;

revoke execute on function public.begin_worker_deployment(text)
  from public, anon, authenticated;
grant execute on function public.begin_worker_deployment(text)
  to service_role;

revoke execute on function public.mark_worker_deployment_triggered(text)
  from public, anon, authenticated;
grant execute on function public.mark_worker_deployment_triggered(text)
  to service_role;

revoke execute on function public.complete_worker_deployment(text, integer)
  from public, anon, authenticated;
grant execute on function public.complete_worker_deployment(text, integer)
  to service_role;

revoke execute on function public.abort_worker_deployment(text)
  from public, anon, authenticated;
grant execute on function public.abort_worker_deployment(text)
  to service_role;
