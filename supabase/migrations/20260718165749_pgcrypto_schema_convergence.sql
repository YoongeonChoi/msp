-- Converge already-applied databases without rewriting historical migrations.
-- pgcrypto is moved with its object OIDs intact, then application routines are
-- recompiled to use the qualified extension schema.
--
-- Rollback procedure: disable all trading first, move pgcrypto back to public,
-- then replace extensions.digest references with public.digest in the same
-- transaction. A reviewed forward fix is preferred after later migrations
-- depend on the extensions schema.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '60s';

create schema if not exists extensions;
revoke create on schema extensions
  from public, anon, authenticated, service_role, authenticator;

do $pgcrypto_convergence$
declare
  extension_schema text;
  extension_relocatable boolean;
  extension_owner oid;
  extensions_schema_owner oid;
  digest_bytea_oid oid;
  digest_text_oid oid;
  digest_bytea_metadata jsonb;
  digest_text_metadata jsonb;
  current_digest_bytea_metadata jsonb;
  current_digest_text_metadata jsonb;
  moved_extension boolean := false;
  patched_count integer := 0;
  routine record;
  current_metadata jsonb;
  patched_definition text;
begin
  select
    n.nspname,
    e.extrelocatable,
    e.extowner,
    pg_catalog.to_regprocedure(
      pg_catalog.format('%I.digest(bytea,text)', n.nspname)
    )::oid,
    pg_catalog.to_regprocedure(
      pg_catalog.format('%I.digest(text,text)', n.nspname)
    )::oid
  into
    extension_schema,
    extension_relocatable,
    extension_owner,
    digest_bytea_oid,
    digest_text_oid
  from pg_catalog.pg_extension as e
  join pg_catalog.pg_namespace as n on n.oid = e.extnamespace
  where e.extname = 'pgcrypto';

  if extension_schema is null then
    raise exception 'pgcrypto must exist before schema convergence';
  end if;
  if extension_schema not in ('public', 'extensions') then
    raise exception 'pgcrypto is installed in unexpected schema %', extension_schema;
  end if;
  if not extension_relocatable then
    raise exception 'pgcrypto must remain relocatable for schema convergence';
  end if;
  if digest_bytea_oid is null or digest_text_oid is null then
    raise exception 'pgcrypto digest overloads are incomplete before convergence';
  end if;

  select n.nspowner
  into extensions_schema_owner
  from pg_catalog.pg_namespace as n
  where n.nspname = 'extensions';

  if not exists (
    select 1
    from pg_catalog.pg_roles as r
    where r.oid = extension_owner
      and r.rolname in ('postgres', 'supabase_admin')
  ) or not exists (
    select 1
    from pg_catalog.pg_roles as r
    where r.oid = extensions_schema_owner
      and r.rolname in ('postgres', 'supabase_admin')
  ) then
    raise exception 'pgcrypto and extensions schema require trusted owners';
  end if;

  select pg_catalog.to_jsonb(p) - 'pronamespace'
  into digest_bytea_metadata
  from pg_catalog.pg_proc as p
  where p.oid = digest_bytea_oid;

  select pg_catalog.to_jsonb(p) - 'pronamespace'
  into digest_text_metadata
  from pg_catalog.pg_proc as p
  where p.oid = digest_text_oid;

  if exists (
    select 1
    from pg_catalog.pg_proc as p
    join pg_catalog.pg_namespace as n on n.oid = p.pronamespace
    where n.nspname in ('private', 'api', 'worker_api')
      and p.prokind in ('f', 'p')
      and pg_catalog.pg_get_functiondef(p.oid)
        ~* '(^|[^a-zA-Z0-9_."])"?digest"?[[:space:]]*\('
  ) then
    raise exception 'unqualified application digest reference requires review';
  end if;

  if extension_schema = 'public' then
    execute 'alter extension pgcrypto set schema extensions';
    moved_extension := true;
  end if;

  if pg_catalog.to_regprocedure('extensions.digest(bytea,text)')::oid
      is distinct from digest_bytea_oid
    or pg_catalog.to_regprocedure('extensions.digest(text,text)')::oid
      is distinct from digest_text_oid then
    raise exception 'pgcrypto digest OIDs changed during schema convergence';
  end if;

  select pg_catalog.to_jsonb(p) - 'pronamespace'
  into current_digest_bytea_metadata
  from pg_catalog.pg_proc as p
  where p.oid = digest_bytea_oid;

  select pg_catalog.to_jsonb(p) - 'pronamespace'
  into current_digest_text_metadata
  from pg_catalog.pg_proc as p
  where p.oid = digest_text_oid;

  if current_digest_bytea_metadata is distinct from digest_bytea_metadata
    or current_digest_text_metadata is distinct from digest_text_metadata then
    raise exception 'pgcrypto digest metadata changed during schema convergence';
  end if;

  for routine in
    select
      p.oid,
      pg_catalog.to_jsonb(p) - 'prosrc' - 'prosqlbody' as metadata,
      pg_catalog.pg_get_functiondef(p.oid) as definition
    from pg_catalog.pg_proc as p
    join pg_catalog.pg_namespace as n on n.oid = p.pronamespace
    where n.nspname in ('private', 'api', 'worker_api')
      and p.prokind in ('f', 'p')
      and pg_catalog.pg_get_functiondef(p.oid)
        ~* '"?public"?[[:space:]]*\.[[:space:]]*"?digest"?[[:space:]]*\('
    order by n.nspname, p.proname, p.oid
  loop
    patched_definition := pg_catalog.replace(
      routine.definition,
      'public.digest',
      'extensions.digest'
    );
    if patched_definition = routine.definition
      or patched_definition
        ~* '"?public"?[[:space:]]*\.[[:space:]]*"?digest"?[[:space:]]*\('
      or patched_definition
        ~* '(^|[^a-zA-Z0-9_."])"?digest"?[[:space:]]*\(' then
      raise exception 'routine % uses a noncanonical digest reference', routine.oid;
    end if;

    execute patched_definition;

    select pg_catalog.to_jsonb(p) - 'prosrc' - 'prosqlbody'
    into current_metadata
    from pg_catalog.pg_proc as p
    where p.oid = routine.oid;

    if not found then
      raise exception 'routine % disappeared during convergence', routine.oid;
    end if;

    if current_metadata is distinct from routine.metadata then
      raise exception 'routine % metadata changed during convergence', routine.oid;
    end if;

    patched_count := patched_count + 1;
  end loop;

  if moved_extension and patched_count = 0 then
    raise exception 'no application routines were patched during pgcrypto move';
  end if;

  if pg_catalog.to_regprocedure('extensions.digest(bytea,text)') is null
    or pg_catalog.to_regprocedure('extensions.digest(text,text)') is null
    or pg_catalog.to_regprocedure('public.digest(bytea,text)') is not null
    or pg_catalog.to_regprocedure('public.digest(text,text)') is not null then
    raise exception 'pgcrypto digest schema boundary did not converge';
  end if;

  if exists (
    select 1
    from pg_catalog.pg_proc as p
    join pg_catalog.pg_namespace as n on n.oid = p.pronamespace
    where n.nspname in ('private', 'api', 'worker_api')
      and p.prokind in ('f', 'p')
      and (
        pg_catalog.pg_get_functiondef(p.oid)
          ~* '"?public"?[[:space:]]*\.[[:space:]]*"?digest"?[[:space:]]*\('
        or pg_catalog.pg_get_functiondef(p.oid)
          ~* '(^|[^a-zA-Z0-9_."])"?digest"?[[:space:]]*\('
      )
  ) then
    raise exception 'legacy digest reference remains after convergence';
  end if;

  if exists (
    select 1
    from pg_catalog.pg_namespace as n
    cross join lateral pg_catalog.aclexplode(
      coalesce(
        n.nspacl,
        pg_catalog.acldefault('n', n.nspowner)
      )
    ) as acl
    where n.nspname = 'extensions'
      and pg_catalog.lower(acl.privilege_type) = 'create'
      and acl.grantee <> n.nspowner
  )
    or pg_catalog.has_schema_privilege('anon', 'extensions', 'CREATE')
    or pg_catalog.has_schema_privilege('authenticated', 'extensions', 'CREATE')
    or pg_catalog.has_schema_privilege('service_role', 'extensions', 'CREATE')
    or pg_catalog.has_schema_privilege('authenticator', 'extensions', 'CREATE') then
    raise exception 'untrusted roles retain CREATE on extensions schema';
  end if;

  if exists (
    select 1
    from pg_catalog.pg_proc as p
    join pg_catalog.pg_namespace as n on n.oid = p.pronamespace
    cross join pg_catalog.pg_roles as r
    where n.nspname in ('private', 'api', 'worker_api')
      and p.prokind in ('f', 'p')
      and not p.prosecdef
      and pg_catalog.pg_get_functiondef(p.oid)
        ~* '"?extensions"?[[:space:]]*\.[[:space:]]*"?digest"?[[:space:]]*\('
      and r.rolname in ('anon', 'authenticated', 'service_role', 'authenticator')
      and pg_catalog.has_function_privilege(r.oid, p.oid, 'EXECUTE')
      and not pg_catalog.has_schema_privilege(r.oid, 'extensions', 'USAGE')
  ) then
    raise exception 'digest caller lacks USAGE on extensions schema';
  end if;
end;
$pgcrypto_convergence$;

commit;
