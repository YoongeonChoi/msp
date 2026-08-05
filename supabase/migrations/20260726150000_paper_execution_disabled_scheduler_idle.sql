-- Keep the durable worker alive while Paper execution is intentionally disabled.
--
-- The execution source implementation deliberately rejects stale leases,
-- qualifications, and enabled controls.  Before an account has a finalized
-- qualification, however, execution_controls.execution_enabled=false is the
-- expected fail-closed state.  Treating that state as an RPC failure causes the
-- effectful scheduler job to dead-letter even though there is no execution work
-- that may legally be claimed.

create or replace function private.paper_execution_claim_is_disabled_v1(
  p_account_id text,
  p_worker_id uuid,
  p_release_sha text,
  p_now timestamptz,
  p_lease_seconds integer
)
returns boolean
language plpgsql
stable
security definer
set search_path = ''
as $$
declare
  authorization_time timestamptz;
  execution_enabled_value boolean;
begin
  perform private.require_service_role();
  authorization_time := private.paper_source_clock_v1(p_now);

  if p_account_id is null
     or p_worker_id is null
     or p_release_sha is null
     or p_release_sha !~ '^[0-9a-f]{40}([0-9a-f]{24})?$'
     or p_lease_seconds is null
     or p_lease_seconds < 5
     or p_lease_seconds > 300 then
    raise exception 'paper_execution_claim_values_invalid'
      using errcode = '22023';
  end if;

  select control.execution_enabled
  into execution_enabled_value
  from private.execution_controls as control
  where control.account_id = p_account_id
    and control.environment = 'paper';

  if not found then
    raise exception 'paper_source_control_missing'
      using errcode = 'P0002';
  end if;

  if execution_enabled_value then
    return false;
  end if;

  if not exists (
    select 1
    from private.worker_leases as lease
    where lease.account_id = p_account_id
      and lease.holder_id = p_worker_id::text
      and lease.release_sha = p_release_sha
      and lease.expires_at > authorization_time
  ) then
    raise exception 'paper_source_gate_lease_or_qualification_stale'
      using errcode = '40001';
  end if;

  return true;
end;
$$;

create or replace function worker_api.claim_paper_execution_v1(
  p_account_id text,
  p_worker_id uuid,
  p_release_sha text,
  p_now timestamptz,
  p_lease_seconds integer
)
returns table (
  command_id uuid,
  intent_id uuid,
  kind text,
  claim_token uuid,
  source_revision bigint,
  worker_id uuid,
  release_sha text,
  available_at timestamptz,
  claimed_at timestamptz,
  claim_expires_at timestamptz
)
language plpgsql
volatile
security invoker
set search_path = ''
as $$
begin
  if private.paper_execution_claim_is_disabled_v1(
    p_account_id,
    p_worker_id,
    p_release_sha,
    p_now,
    p_lease_seconds
  ) then
    return;
  end if;

  return query
  select *
  from private.claim_paper_execution_v1_impl(
    p_account_id,
    p_worker_id,
    p_release_sha,
    p_now,
    p_lease_seconds
  );
end;
$$;

revoke all on function private.paper_execution_claim_is_disabled_v1(
  text, uuid, text, timestamptz, integer
) from public, anon, authenticated;
grant execute on function private.paper_execution_claim_is_disabled_v1(
  text, uuid, text, timestamptz, integer
) to service_role;

revoke all on function worker_api.claim_paper_execution_v1(
  text, uuid, text, timestamptz, integer
) from public, anon, authenticated;
grant execute on function worker_api.claim_paper_execution_v1(
  text, uuid, text, timestamptz, integer
) to service_role;
