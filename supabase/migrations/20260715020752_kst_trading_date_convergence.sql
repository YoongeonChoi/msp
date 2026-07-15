-- Converge every execution/calendar comparison on the Korean market date.
--
-- PostgreSQL casts timestamptz::date in the database session timezone.  That
-- made 00:00-08:59 KST orders use the prior UTC date in reservation and
-- Unknown-resolution checks while cash settlement correctly used KST.  Patch
-- the existing function definitions forward so fresh and upgraded databases
-- share one explicit Asia/Seoul trading-date contract.

do $migration$
declare
  function_oid oid;
  function_count integer;
  definition text;
  original_definition text;
begin
  select min(proc.oid), count(*)::integer
  into function_oid, function_count
  from pg_catalog.pg_proc as proc
  join pg_catalog.pg_namespace as namespace on namespace.oid = proc.pronamespace
  where namespace.nspname = 'private'
    and proc.proname = 'reserve_order_intent_impl';
  if function_count <> 1 then
    raise exception 'reserve_order_intent_kst_patch_function_count_invalid'
      using errcode = '23514';
  end if;
  definition := pg_catalog.pg_get_functiondef(function_oid);
  original_definition := definition;
  if pg_catalog.strpos(
    definition, 'calendar.valid_from <= p_signal_valid_from::date'
  ) = 0
     or pg_catalog.strpos(
       definition, 'calendar.valid_until >= p_expires_at::date'
     ) = 0
     or pg_catalog.strpos(
       definition, 'session.session_date = p_eligible_at::date'
     ) = 0 then
    raise exception 'reserve_order_intent_kst_patch_target_invalid'
      using errcode = '23514';
  end if;
  definition := pg_catalog.replace(
    definition,
    'calendar.valid_from <= p_signal_valid_from::date',
    'calendar.valid_from <= (p_signal_valid_from at time zone ''Asia/Seoul'')::date'
  );
  definition := pg_catalog.replace(
    definition,
    'calendar.valid_until >= p_expires_at::date',
    'calendar.valid_until >= (p_expires_at at time zone ''Asia/Seoul'')::date'
  );
  definition := pg_catalog.replace(
    definition,
    'session.session_date = p_eligible_at::date',
    'session.session_date = (p_eligible_at at time zone ''Asia/Seoul'')::date'
  );
  if definition = original_definition
     or pg_catalog.strpos(definition, 'p_signal_valid_from::date') > 0
     or pg_catalog.strpos(definition, 'p_expires_at::date') > 0
     or pg_catalog.strpos(definition, 'p_eligible_at::date') > 0 then
    raise exception 'reserve_order_intent_kst_patch_result_invalid'
      using errcode = '23514';
  end if;
  execute definition;
end;
$migration$;

do $migration$
declare
  function_oid oid;
  function_count integer;
  definition text;
begin
  select min(proc.oid), count(*)::integer
  into function_oid, function_count
  from pg_catalog.pg_proc as proc
  join pg_catalog.pg_namespace as namespace on namespace.oid = proc.pronamespace
  where namespace.nspname = 'private'
    and proc.proname = 'enqueue_paper_execution_candidate_v1_impl';
  if function_count <> 1 then
    raise exception 'paper_candidate_kst_patch_function_count_invalid'
      using errcode = '23514';
  end if;
  definition := pg_catalog.pg_get_functiondef(function_oid);
  if pg_catalog.strpos(
    definition, 'calendar.valid_from <= eligible_at_value::date'
  ) = 0
     or pg_catalog.strpos(
       definition, 'calendar.valid_until >= (p_candidate->>''expires_at'')::date'
     ) = 0 then
    raise exception 'paper_candidate_kst_patch_target_invalid'
      using errcode = '23514';
  end if;
  definition := pg_catalog.replace(
    definition,
    'calendar.valid_from <= eligible_at_value::date',
    'calendar.valid_from <= (eligible_at_value at time zone ''Asia/Seoul'')::date'
  );
  definition := pg_catalog.replace(
    definition,
    'calendar.valid_until >= (p_candidate->>''expires_at'')::date',
    'calendar.valid_until >= (((p_candidate->>''expires_at'')::timestamptz at time zone ''Asia/Seoul'')::date)'
  );
  if pg_catalog.strpos(definition, 'eligible_at_value::date') > 0
     or pg_catalog.strpos(
       definition, '(p_candidate->>''expires_at'')::date'
     ) > 0 then
    raise exception 'paper_candidate_kst_patch_result_invalid'
      using errcode = '23514';
  end if;
  execute definition;
end;
$migration$;

do $migration$
declare
  function_oid oid;
  function_count integer;
  definition text;
begin
  select min(proc.oid), count(*)::integer
  into function_oid, function_count
  from pg_catalog.pg_proc as proc
  join pg_catalog.pg_namespace as namespace on namespace.oid = proc.pronamespace
  where namespace.nspname = 'private'
    and proc.proname = 'assert_unknown_resolution_fill_manifest_v2';
  if function_count <> 1 then
    raise exception 'unknown_resolution_kst_patch_function_count_invalid'
      using errcode = '23514';
  end if;
  definition := pg_catalog.pg_get_functiondef(function_oid);
  if pg_catalog.strpos(
    definition, 'session.session_date >= filled_time::date'
  ) = 0 then
    raise exception 'unknown_resolution_kst_patch_target_invalid'
      using errcode = '23514';
  end if;
  definition := pg_catalog.replace(
    definition,
    'session.session_date >= filled_time::date',
    'session.session_date >= (filled_time at time zone ''Asia/Seoul'')::date'
  );
  if pg_catalog.strpos(definition, 'filled_time::date') > 0 then
    raise exception 'unknown_resolution_kst_patch_result_invalid'
      using errcode = '23514';
  end if;
  execute definition;
end;
$migration$;

do $migration$
declare
  function_oid oid;
  function_count integer;
  definition text;
begin
  select min(proc.oid), count(*)::integer
  into function_oid, function_count
  from pg_catalog.pg_proc as proc
  join pg_catalog.pg_namespace as namespace on namespace.oid = proc.pronamespace
  where namespace.nspname = 'private'
    and proc.proname = 'assert_qualification_candidate_v1';
  if function_count <> 1 then
    raise exception 'qualification_kst_patch_function_count_invalid'
      using errcode = '23514';
  end if;
  definition := pg_catalog.pg_get_functiondef(function_oid);
  if pg_catalog.strpos(
    definition, 'valid_from_value::date < calendar_row.valid_from'
  ) = 0
     or pg_catalog.strpos(
       definition, 'valid_until_value::date > calendar_row.valid_until'
     ) = 0 then
    raise exception 'qualification_kst_patch_target_invalid'
      using errcode = '23514';
  end if;
  definition := pg_catalog.replace(
    definition,
    'valid_from_value::date < calendar_row.valid_from',
    '(valid_from_value at time zone ''Asia/Seoul'')::date < calendar_row.valid_from'
  );
  definition := pg_catalog.replace(
    definition,
    'valid_until_value::date > calendar_row.valid_until',
    '(valid_until_value at time zone ''Asia/Seoul'')::date > calendar_row.valid_until'
  );
  if pg_catalog.strpos(definition, 'valid_from_value::date') > 0
     or pg_catalog.strpos(definition, 'valid_until_value::date') > 0 then
    raise exception 'qualification_kst_patch_result_invalid'
      using errcode = '23514';
  end if;
  execute definition;
end;
$migration$;

do $migration$
declare
  definition text;
begin
  select pg_catalog.pg_get_functiondef(proc.oid)
  into definition
  from pg_catalog.pg_proc as proc
  join pg_catalog.pg_namespace as namespace on namespace.oid = proc.pronamespace
  where namespace.nspname = 'private'
    and proc.proname = 'record_execution_observation_impl';
  if definition is null
     or pg_catalog.strpos(
       definition,
       'session_date >= (p_observed_at at time zone ''Asia/Seoul'')::date'
     ) = 0 then
    raise exception 'record_execution_observation_kst_contract_missing'
      using errcode = '23514';
  end if;
end;
$migration$;
