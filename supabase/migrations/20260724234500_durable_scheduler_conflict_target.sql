begin;

set local lock_timeout = '5s';
set local statement_timeout = '90s';

-- The initial durable scheduler migration used a column-list conflict target
-- inside the ensure routine, which also exposes account_id and job_key as
-- output variables. PostgreSQL correctly treats those identifiers as ambiguous
-- when the INSERT is first planned. Preserve the immutable migration history
-- and normalize that routine plus the converge routine's matching upsert shape
-- to the table constraint identity.
do $$
declare
  target_functions constant regprocedure[] := array[
    'private.ensure_scheduler_job_definition_impl(text,text,bigint,text,text,text,integer,integer,integer,integer,integer,integer,boolean)'::regprocedure,
    'private.converge_scheduler_job_definition_impl(text,text,bigint,text,text,text,integer,integer,integer,integer,integer,integer,boolean)'::regprocedure
  ];
  target_function regprocedure;
  function_definition text;
  patched_definition text;
  original_owner oid;
  patched_owner oid;
  patched_security_definer boolean;
  patched_volatility "char";
  patched_config text[];
  legacy_occurrences integer;
  constraint_occurrences integer;
  legacy_fragment constant text :=
    'on conflict (account_id, job_key) do nothing';
  constraint_fragment constant text :=
    'on conflict on constraint scheduler_job_definitions_account_id_job_key_key do nothing';
begin
  if not exists (
    select 1
    from pg_catalog.pg_constraint as constraint_record
    where constraint_record.conrelid =
          'private.scheduler_job_definitions'::regclass
      and constraint_record.conname =
          'scheduler_job_definitions_account_id_job_key_key'
      and constraint_record.contype = 'u'
      and pg_catalog.pg_get_constraintdef(constraint_record.oid, false) =
          'UNIQUE (account_id, job_key)'
  ) then
    raise exception 'durable_scheduler_conflict_constraint_missing'
      using errcode = '55000';
  end if;

  foreach target_function in array target_functions loop
    select procedure.proowner
    into original_owner
    from pg_catalog.pg_proc as procedure
    where procedure.oid = target_function;

    function_definition := pg_catalog.pg_get_functiondef(target_function);
    legacy_occurrences := (
      pg_catalog.length(function_definition)
      - pg_catalog.length(
        pg_catalog.replace(function_definition, legacy_fragment, '')
      )
    ) / pg_catalog.length(legacy_fragment);
    if legacy_occurrences <> 1
       or pg_catalog.strpos(function_definition, constraint_fragment) > 0 then
      raise exception 'durable_scheduler_conflict_patch_target_invalid'
        using errcode = '23514';
    end if;

    patched_definition := pg_catalog.replace(
      function_definition,
      legacy_fragment,
      constraint_fragment
    );
    execute patched_definition;

    function_definition := pg_catalog.pg_get_functiondef(target_function);
    legacy_occurrences := (
      pg_catalog.length(function_definition)
      - pg_catalog.length(
        pg_catalog.replace(function_definition, legacy_fragment, '')
      )
    ) / pg_catalog.length(legacy_fragment);
    constraint_occurrences := (
      pg_catalog.length(function_definition)
      - pg_catalog.length(
        pg_catalog.replace(function_definition, constraint_fragment, '')
      )
    ) / pg_catalog.length(constraint_fragment);
    if legacy_occurrences <> 0
       or constraint_occurrences <> 1 then
      raise exception 'durable_scheduler_conflict_patch_failed'
        using errcode = '23514';
    end if;

    select
      procedure.proowner,
      procedure.prosecdef,
      procedure.provolatile,
      procedure.proconfig
    into
      patched_owner,
      patched_security_definer,
      patched_volatility,
      patched_config
    from pg_catalog.pg_proc as procedure
    where procedure.oid = target_function;

    if patched_owner is distinct from original_owner
       or patched_security_definer is distinct from true
       or patched_volatility is distinct from 'v'
       or patched_config is distinct from array['search_path=""']::text[] then
      raise exception 'durable_scheduler_conflict_patch_metadata_changed'
        using errcode = '23514';
    end if;
  end loop;
end;
$$;

commit;
