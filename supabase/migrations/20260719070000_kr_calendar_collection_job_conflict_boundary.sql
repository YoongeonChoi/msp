begin;

set local lock_timeout = '5s';
set local statement_timeout = '90s';

-- Keep deterministic calendar-job CAS conflicts bounded at the PostgREST
-- boundary. SQLSTATE 40001 represents a retryable serialization failure to
-- database clients, but these conflicts require a state reload and explicit
-- operator decision instead of repeating the same request.

do $$
begin
  if to_regprocedure(
       'private.begin_kr_calendar_collection_date_attempt_v1_impl(text,text,bigint,text,text,date,timestamptz)'
     ) is null
     or to_regprocedure(
       'private.pause_kr_calendar_collection_date_attempt_v1_impl(text,text,bigint,text,text,date,text,timestamptz)'
     ) is null
     or to_regprocedure(
       'private.block_kr_calendar_collection_date_attempt_v1_impl(text,text,bigint,text,text,date,text,timestamptz)'
     ) is null
     or to_regprocedure(
       'private.confirm_kr_calendar_collection_date_v1_impl(text,text,bigint,text,text,date,jsonb,jsonb,timestamptz)'
     ) is null then
    raise exception
      'kr_calendar_collection_job_conflict_boundary_requires_store_migration';
  end if;
end
$$;

create or replace function worker_api.begin_kr_calendar_collection_date_attempt_v1(
  p_job_id text,
  p_spec_sha256 text,
  p_expected_revision bigint,
  p_attempt_id text,
  p_holder_id text,
  p_target_date date,
  p_now timestamptz
)
returns table(snapshot jsonb)
language plpgsql
volatile
security invoker
set search_path = ''
as $$
begin
  return query
  select *
  from private.begin_kr_calendar_collection_date_attempt_v1_impl(
    p_job_id, p_spec_sha256, p_expected_revision, p_attempt_id, p_holder_id,
    p_target_date, p_now
  );
exception
  when sqlstate '40001' then
    if sqlerrm in (
      'kr_calendar_collection_job_spec_hash_mismatch',
      'kr_calendar_collection_job_revision_conflict',
      'kr_calendar_collection_job_clock_regressed',
      'kr_calendar_collection_job_attempt_fence_mismatch'
    ) then
      raise sqlstate 'PT409' using message = sqlerrm;
    end if;
    raise;
end;
$$;

create or replace function worker_api.pause_kr_calendar_collection_date_attempt_v1(
  p_job_id text,
  p_spec_sha256 text,
  p_expected_revision bigint,
  p_attempt_id text,
  p_holder_id text,
  p_target_date date,
  p_reason_code text,
  p_now timestamptz
)
returns table(snapshot jsonb)
language plpgsql
volatile
security invoker
set search_path = ''
as $$
begin
  return query
  select *
  from private.pause_kr_calendar_collection_date_attempt_v1_impl(
    p_job_id, p_spec_sha256, p_expected_revision, p_attempt_id, p_holder_id,
    p_target_date, p_reason_code, p_now
  );
exception
  when sqlstate '40001' then
    if sqlerrm in (
      'kr_calendar_collection_job_spec_hash_mismatch',
      'kr_calendar_collection_job_revision_conflict',
      'kr_calendar_collection_job_clock_regressed',
      'kr_calendar_collection_job_attempt_fence_mismatch'
    ) then
      raise sqlstate 'PT409' using message = sqlerrm;
    end if;
    raise;
end;
$$;

create or replace function worker_api.block_kr_calendar_collection_date_attempt_v1(
  p_job_id text,
  p_spec_sha256 text,
  p_expected_revision bigint,
  p_attempt_id text,
  p_holder_id text,
  p_target_date date,
  p_reason_code text,
  p_now timestamptz
)
returns table(snapshot jsonb)
language plpgsql
volatile
security invoker
set search_path = ''
as $$
begin
  return query
  select *
  from private.block_kr_calendar_collection_date_attempt_v1_impl(
    p_job_id, p_spec_sha256, p_expected_revision, p_attempt_id, p_holder_id,
    p_target_date, p_reason_code, p_now
  );
exception
  when sqlstate '40001' then
    if sqlerrm in (
      'kr_calendar_collection_job_spec_hash_mismatch',
      'kr_calendar_collection_job_revision_conflict',
      'kr_calendar_collection_job_clock_regressed',
      'kr_calendar_collection_job_attempt_fence_mismatch'
    ) then
      raise sqlstate 'PT409' using message = sqlerrm;
    end if;
    raise;
end;
$$;

create or replace function worker_api.confirm_kr_calendar_collection_date_v1(
  p_job_id text,
  p_spec_sha256 text,
  p_expected_revision bigint,
  p_attempt_id text,
  p_holder_id text,
  p_target_date date,
  p_session jsonb,
  p_receipt jsonb,
  p_now timestamptz
)
returns table(snapshot jsonb)
language plpgsql
volatile
security invoker
set search_path = ''
as $$
begin
  return query
  select *
  from private.confirm_kr_calendar_collection_date_v1_impl(
    p_job_id, p_spec_sha256, p_expected_revision, p_attempt_id, p_holder_id,
    p_target_date, p_session, p_receipt, p_now
  );
exception
  when sqlstate '40001' then
    if sqlerrm in (
      'kr_calendar_collection_job_spec_hash_mismatch',
      'kr_calendar_collection_job_revision_conflict',
      'kr_calendar_collection_job_clock_regressed',
      'kr_calendar_collection_job_attempt_fence_mismatch'
    ) then
      raise sqlstate 'PT409' using message = sqlerrm;
    end if;
    raise;
end;
$$;

revoke all on function
  worker_api.begin_kr_calendar_collection_date_attempt_v1(
    text,text,bigint,text,text,date,timestamptz
  ),
  worker_api.pause_kr_calendar_collection_date_attempt_v1(
    text,text,bigint,text,text,date,text,timestamptz
  ),
  worker_api.block_kr_calendar_collection_date_attempt_v1(
    text,text,bigint,text,text,date,text,timestamptz
  ),
  worker_api.confirm_kr_calendar_collection_date_v1(
    text,text,bigint,text,text,date,jsonb,jsonb,timestamptz
  )
from public, anon, authenticated, authenticator, service_role;

grant execute on function
  worker_api.begin_kr_calendar_collection_date_attempt_v1(
    text,text,bigint,text,text,date,timestamptz
  ),
  worker_api.pause_kr_calendar_collection_date_attempt_v1(
    text,text,bigint,text,text,date,text,timestamptz
  ),
  worker_api.block_kr_calendar_collection_date_attempt_v1(
    text,text,bigint,text,text,date,text,timestamptz
  ),
  worker_api.confirm_kr_calendar_collection_date_v1(
    text,text,bigint,text,text,date,jsonb,jsonb,timestamptz
  )
to service_role;

comment on function worker_api.begin_kr_calendar_collection_date_attempt_v1(
  text,text,bigint,text,text,date,timestamptz
) is 'Service-only manual calendar job begin; deterministic CAS conflicts return PT409 and require reload.';

comment on function worker_api.pause_kr_calendar_collection_date_attempt_v1(
  text,text,bigint,text,text,date,text,timestamptz
) is 'Service-only manual calendar job pause; deterministic CAS conflicts return PT409 and require reload.';

comment on function worker_api.block_kr_calendar_collection_date_attempt_v1(
  text,text,bigint,text,text,date,text,timestamptz
) is 'Service-only manual calendar job block; deterministic CAS conflicts return PT409 and require reload.';

comment on function worker_api.confirm_kr_calendar_collection_date_v1(
  text,text,bigint,text,text,date,jsonb,jsonb,timestamptz
) is 'Service-only manual calendar job confirm; deterministic CAS conflicts return PT409 and require reload.';

do $$
declare
  function_oid regprocedure;
begin
  foreach function_oid in array array[
    'worker_api.begin_kr_calendar_collection_date_attempt_v1(text,text,bigint,text,text,date,timestamptz)'::regprocedure,
    'worker_api.pause_kr_calendar_collection_date_attempt_v1(text,text,bigint,text,text,date,text,timestamptz)'::regprocedure,
    'worker_api.block_kr_calendar_collection_date_attempt_v1(text,text,bigint,text,text,date,text,timestamptz)'::regprocedure,
    'worker_api.confirm_kr_calendar_collection_date_v1(text,text,bigint,text,text,date,jsonb,jsonb,timestamptz)'::regprocedure
  ] loop
    if (select procedure.prosecdef from pg_catalog.pg_proc as procedure
        where procedure.oid = function_oid::oid)
       or pg_catalog.has_function_privilege('public', function_oid, 'EXECUTE')
       or pg_catalog.has_function_privilege('anon', function_oid, 'EXECUTE')
       or pg_catalog.has_function_privilege('authenticated', function_oid, 'EXECUTE')
       or pg_catalog.has_function_privilege('authenticator', function_oid, 'EXECUTE')
       or not pg_catalog.has_function_privilege(
         'service_role', function_oid, 'EXECUTE'
       ) then
      raise exception 'kr_calendar_collection_job_conflict_boundary_security_failed'
        using errcode = '55000';
    end if;
  end loop;
end;
$$;

commit;
