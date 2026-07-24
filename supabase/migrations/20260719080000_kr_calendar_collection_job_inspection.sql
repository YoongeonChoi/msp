begin;

set local lock_timeout = '5s';
set local statement_timeout = '90s';

do $$
begin
  if pg_catalog.to_regclass('private.kr_calendar_collection_jobs') is null
     or pg_catalog.to_regprocedure(
       'private.require_service_role()'
     ) is null
     or pg_catalog.to_regprocedure(
       'private.kr_calendar_collection_uuid4_v1(text)'
     ) is null
     or pg_catalog.to_regprocedure(
       'private.kr_calendar_collection_snapshot_v1(uuid)'
     ) is null then
    raise exception 'kr_calendar_collection_job_inspection_dependency_missing'
      using errcode = '55000';
  end if;
end;
$$;

create or replace function private.inspect_kr_calendar_collection_job_v1_impl(
  p_job_id text
)
returns table(job_found boolean, snapshot jsonb)
language plpgsql
stable
security definer
set search_path = ''
as $$
declare
  job_id_is_valid boolean;
  job_id_value uuid;
begin
  perform private.require_service_role();

  begin
    job_id_is_valid := coalesce(
      private.kr_calendar_collection_uuid4_v1(p_job_id),
      false
    );
  exception
    when invalid_text_representation then
      job_id_is_valid := false;
  end;

  if not job_id_is_valid then
    raise exception 'kr_calendar_collection_job_argument_invalid'
      using errcode = '22023';
  end if;

  job_id_value := p_job_id::uuid;

  if not exists (
    select 1
    from private.kr_calendar_collection_jobs as stored
    where stored.job_id = job_id_value
  ) then
    return query
    select false as job_found, null::jsonb as snapshot;
    return;
  end if;

  return query
  select
    true as job_found,
    private.kr_calendar_collection_snapshot_v1(job_id_value) as snapshot;
end;
$$;

create or replace function worker_api.inspect_kr_calendar_collection_job_v1(
  p_job_id text
)
returns table(job_found boolean, snapshot jsonb)
language sql
stable
security invoker
set search_path = ''
as $$
  select *
  from private.inspect_kr_calendar_collection_job_v1_impl(p_job_id);
$$;

revoke all on function
  private.inspect_kr_calendar_collection_job_v1_impl(text),
  worker_api.inspect_kr_calendar_collection_job_v1(text)
from public, anon, authenticated, authenticator, service_role;

grant execute on function
  private.inspect_kr_calendar_collection_job_v1_impl(text),
  worker_api.inspect_kr_calendar_collection_job_v1(text)
to service_role;

comment on function private.inspect_kr_calendar_collection_job_v1_impl(text)
is 'Service-only read-only calendar job inspection implementation; does not authorize mutation, retry, or recovery.';

comment on function worker_api.inspect_kr_calendar_collection_job_v1(text)
is 'Service-only read-only calendar job inspection; missing jobs return job_found=false and snapshot=null; does not authorize mutation, retry, or recovery.';

do $$
declare
  impl_contract_count bigint;
  wrapper_contract_count bigint;
  function_owner_count bigint;
  function_count bigint;
  service_function_acl_count bigint;
  forbidden_function_acl_count bigint;
  forbidden_effective_privilege_count bigint;
begin
  select count(*) into impl_contract_count
  from pg_catalog.pg_proc as procedure
  where procedure.oid =
      'private.inspect_kr_calendar_collection_job_v1_impl(text)'::regprocedure
    and procedure.prosecdef
    and procedure.provolatile = 's'
    and procedure.proretset
    and procedure.proconfig = array['search_path=""']::text[];

  select count(*) into wrapper_contract_count
  from pg_catalog.pg_proc as procedure
  where procedure.oid =
      'worker_api.inspect_kr_calendar_collection_job_v1(text)'::regprocedure
    and not procedure.prosecdef
    and procedure.provolatile = 's'
    and procedure.proretset
    and procedure.proconfig = array['search_path=""']::text[];

  select count(distinct procedure.proowner), count(*)
  into function_owner_count, function_count
  from pg_catalog.pg_proc as procedure
  where procedure.oid in (
    'private.inspect_kr_calendar_collection_job_v1_impl(text)'::regprocedure,
    'worker_api.inspect_kr_calendar_collection_job_v1(text)'::regprocedure
  );

  select count(*) into service_function_acl_count
  from pg_catalog.pg_proc as procedure
  cross join lateral pg_catalog.aclexplode(
    coalesce(procedure.proacl, pg_catalog.acldefault('f', procedure.proowner))
  ) as acl
  join pg_catalog.pg_roles as grantee on grantee.oid = acl.grantee
  where procedure.oid in (
      'private.inspect_kr_calendar_collection_job_v1_impl(text)'::regprocedure,
      'worker_api.inspect_kr_calendar_collection_job_v1(text)'::regprocedure
    )
    and acl.privilege_type = 'EXECUTE'
    and not acl.is_grantable
    and grantee.rolname = 'service_role';

  select count(*) into forbidden_function_acl_count
  from pg_catalog.pg_proc as procedure
  cross join lateral pg_catalog.aclexplode(
    coalesce(procedure.proacl, pg_catalog.acldefault('f', procedure.proowner))
  ) as acl
  left join pg_catalog.pg_roles as grantee on grantee.oid = acl.grantee
  where procedure.oid in (
      'private.inspect_kr_calendar_collection_job_v1_impl(text)'::regprocedure,
      'worker_api.inspect_kr_calendar_collection_job_v1(text)'::regprocedure
    )
    and acl.privilege_type = 'EXECUTE'
    and acl.grantee <> procedure.proowner
    and (
      acl.grantee = 0
      or grantee.rolname is null
      or grantee.rolname <> 'service_role'
    );

  select count(*) into forbidden_effective_privilege_count
  from pg_catalog.pg_proc as procedure
  where procedure.oid in (
      'private.inspect_kr_calendar_collection_job_v1_impl(text)'::regprocedure,
      'worker_api.inspect_kr_calendar_collection_job_v1(text)'::regprocedure
    )
    and (
      pg_catalog.has_function_privilege(
        'public', procedure.oid, 'EXECUTE'
      )
      or pg_catalog.has_function_privilege(
        'anon', procedure.oid, 'EXECUTE'
      )
      or pg_catalog.has_function_privilege(
        'authenticated', procedure.oid, 'EXECUTE'
      )
      or pg_catalog.has_function_privilege(
        'authenticator', procedure.oid, 'EXECUTE'
      )
    );

  if impl_contract_count <> 1
     or wrapper_contract_count <> 1
     or function_owner_count <> 1
     or function_count <> 2
     or service_function_acl_count <> 2
     or forbidden_function_acl_count <> 0
     or forbidden_effective_privilege_count <> 0
     or exists (
       select 1
       from pg_catalog.pg_proc as procedure
       join pg_catalog.pg_roles as role on role.oid = procedure.proowner
       where procedure.oid in (
         'private.inspect_kr_calendar_collection_job_v1_impl(text)'::regprocedure,
         'worker_api.inspect_kr_calendar_collection_job_v1(text)'::regprocedure
       )
       and role.rolname in (
         'anon', 'authenticated', 'authenticator', 'service_role'
       )
     ) then
    raise exception 'kr_calendar_collection_job_inspection_security_contract_failed'
      using errcode = '55000';
  end if;
end;
$$;

commit;
