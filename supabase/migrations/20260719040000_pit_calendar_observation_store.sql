begin;

set local lock_timeout = '5s';
set local statement_timeout = '90s';

-- Forward-only dependencies:
--   20260719010000_pit_daily_candle_timing_store.sql
--   20260719020000_pit_source_observation_occurrence_store.sql
do $$
begin
  if to_regprocedure('private.require_service_role()') is null
     or to_regprocedure('private.jsonb_exact_keys_v1(jsonb,text[])') is null
     or to_regprocedure(
       'private.pit_canonical_timestamp_v1(timestamptz)'
     ) is null
     or to_regprocedure('private.pit_sha256_text_v1(text)') is null
     or to_regprocedure(
       'private.pit_calendar_identity_sha256_v1(text,text,date)'
     ) is null
     or to_regprocedure(
       'private.pit_calendar_canonical_evidence_sha256_v1(text,text,date,boolean,timestamptz,timestamptz,date,timestamptz,timestamptz,text)'
     ) is null
     or to_regprocedure(
       'private.quarantine_pit_calendar_observation_v1(text,text,text,text,text,timestamptz,jsonb,bigint,text,timestamptz)'
     ) is null
     or to_regprocedure(
       'private.put_pit_calendar_observation_occurrence_v1(text,text,uuid,timestamptz,jsonb)'
     ) is null
     or to_regclass('private.pit_calendar_stream_heads') is null
     or to_regclass('private.pit_calendar_content_revisions') is null
     or to_regclass('private.pit_calendar_observation_occurrences') is null
     or to_regclass('private.pit_calendar_observation_quarantine') is null then
    raise exception 'pit_calendar_observation_store_dependency_missing'
      using errcode = '55000';
  end if;
end;
$$;

create or replace function private.append_pit_kr_daily_session_observation_v1_impl(
  p_session jsonb
)
returns table (
  status text,
  calendar_idempotency_key text,
  canonical_evidence_sha256 text,
  revision bigint,
  revision_inserted boolean,
  occurrence_id uuid,
  occurrence_inserted boolean,
  observed_at timestamptz,
  quarantine_id uuid,
  reason_code text
)
language plpgsql
volatile
security definer
set search_path = ''
as $$
declare
  provider_value text;
  market_value text;
  session_date_value date;
  is_open_value boolean;
  regular_start_value timestamptz;
  regular_end_value timestamptz;
  next_business_date_value date;
  next_regular_start_value timestamptz;
  next_regular_end_value timestamptz;
  observed_at_value timestamptz;
  provider_contract_sha256_value text;
  candidate_sha256_value text;
  expected_identity_sha256_value text;
  expected_evidence_sha256_value text;
  request_sha256_value text;
  head private.pit_calendar_stream_heads%rowtype;
  content_revision private.pit_calendar_content_revisions%rowtype;
  existing_occurrence private.pit_calendar_observation_occurrences%rowtype;
  next_revision_value bigint;
  occurrence_id_value uuid;
  occurrence_inserted_value boolean;
  quarantine_id_value uuid;
  quarantine_reason_value text;
begin
  perform private.require_service_role();

  if p_session is null
     or pg_catalog.octet_length(p_session::text) > 8192
     or private.jsonb_exact_keys_v1(
       p_session,
       array[
         'canonical_evidence_sha256', 'is_open', 'market',
         'next_business_date', 'next_regular_end_at',
         'next_regular_start_at', 'observed_at', 'provider',
         'provider_contract_sha256', 'regular_end_at', 'regular_start_at',
         'schema_version', 'session_date'
       ]
     ) is distinct from true then
    raise exception 'pit_calendar_observation_payload_shape_invalid'
      using errcode = '22023';
  end if;

  if pg_catalog.jsonb_typeof(p_session->'schema_version')
       is distinct from 'number'
     or p_session->>'schema_version' <> '1'
     or pg_catalog.jsonb_typeof(p_session->'provider')
       is distinct from 'string'
     or (p_session->>'provider') !~ '^[a-z][a-z0-9._-]{0,63}$'
     or pg_catalog.jsonb_typeof(p_session->'market')
       is distinct from 'string'
     or p_session->>'market' <> 'KR'
     or pg_catalog.jsonb_typeof(p_session->'session_date')
       is distinct from 'string'
     or (p_session->>'session_date') !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
     or pg_catalog.jsonb_typeof(p_session->'is_open')
       is distinct from 'boolean'
     or pg_catalog.jsonb_typeof(p_session->'regular_start_at')
       not in ('string', 'null')
     or pg_catalog.jsonb_typeof(p_session->'regular_end_at')
       not in ('string', 'null')
     or pg_catalog.jsonb_typeof(p_session->'next_business_date')
       is distinct from 'string'
     or (p_session->>'next_business_date')
       !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
     or pg_catalog.jsonb_typeof(p_session->'next_regular_start_at')
       is distinct from 'string'
     or pg_catalog.jsonb_typeof(p_session->'next_regular_end_at')
       is distinct from 'string'
     or pg_catalog.jsonb_typeof(p_session->'observed_at')
       is distinct from 'string'
     or pg_catalog.jsonb_typeof(p_session->'provider_contract_sha256')
       is distinct from 'string'
     or (p_session->>'provider_contract_sha256') !~ '^[0-9a-f]{64}$'
     or pg_catalog.jsonb_typeof(p_session->'canonical_evidence_sha256')
       is distinct from 'string'
     or (p_session->>'canonical_evidence_sha256') !~ '^[0-9a-f]{64}$' then
    raise exception 'pit_calendar_observation_payload_value_invalid'
      using errcode = '22023';
  end if;

  begin
    session_date_value := (p_session->>'session_date')::date;
    is_open_value := (p_session->>'is_open')::boolean;
    regular_start_value := case
      when pg_catalog.jsonb_typeof(p_session->'regular_start_at') = 'null'
        then null
      else (p_session->>'regular_start_at')::timestamptz
    end;
    regular_end_value := case
      when pg_catalog.jsonb_typeof(p_session->'regular_end_at') = 'null'
        then null
      else (p_session->>'regular_end_at')::timestamptz
    end;
    next_business_date_value := (p_session->>'next_business_date')::date;
    next_regular_start_value :=
      (p_session->>'next_regular_start_at')::timestamptz;
    next_regular_end_value :=
      (p_session->>'next_regular_end_at')::timestamptz;
    observed_at_value := (p_session->>'observed_at')::timestamptz;
  exception
    when others then
      raise exception 'pit_calendar_observation_typed_value_invalid'
        using errcode = '22023';
  end;

  if session_date_value::text <> p_session->>'session_date'
     or next_business_date_value::text <> p_session->>'next_business_date'
     or private.pit_canonical_timestamp_v1(next_regular_start_value)
       <> p_session->>'next_regular_start_at'
     or private.pit_canonical_timestamp_v1(next_regular_end_value)
       <> p_session->>'next_regular_end_at'
     or private.pit_canonical_timestamp_v1(observed_at_value)
       <> p_session->>'observed_at'
     or (
       regular_start_value is not null
       and private.pit_canonical_timestamp_v1(regular_start_value)
         <> p_session->>'regular_start_at'
     )
     or (
       regular_end_value is not null
       and private.pit_canonical_timestamp_v1(regular_end_value)
         <> p_session->>'regular_end_at'
     ) then
    raise exception 'pit_calendar_observation_canonical_value_invalid'
      using errcode = '22023';
  end if;

  if (
       is_open_value
       and (
         regular_start_value is null
         or regular_end_value is null
         or regular_start_value >= regular_end_value
         or (regular_start_value at time zone 'Asia/Seoul')::date
           <> session_date_value
         or (regular_end_value at time zone 'Asia/Seoul')::date
           <> session_date_value
       )
     )
     or (
       not is_open_value
       and (
         regular_start_value is not null
         or regular_end_value is not null
       )
     )
     or next_business_date_value <= session_date_value
     or next_regular_start_value >= next_regular_end_value
     or (next_regular_start_value at time zone 'Asia/Seoul')::date
       <> next_business_date_value
     or (next_regular_end_value at time zone 'Asia/Seoul')::date
       <> next_business_date_value
     or (
       is_open_value
       and regular_end_value >= next_regular_start_value
     ) then
    raise exception 'pit_calendar_observation_kst_geometry_invalid'
      using errcode = '22023';
  end if;

  provider_value := p_session->>'provider';
  market_value := p_session->>'market';
  provider_contract_sha256_value :=
    p_session->>'provider_contract_sha256';
  candidate_sha256_value := p_session->>'canonical_evidence_sha256';
  expected_identity_sha256_value := private.pit_calendar_identity_sha256_v1(
    provider_value,
    market_value,
    session_date_value
  );
  expected_evidence_sha256_value :=
    private.pit_calendar_canonical_evidence_sha256_v1(
      provider_value,
      market_value,
      session_date_value,
      is_open_value,
      regular_start_value,
      regular_end_value,
      next_business_date_value,
      next_regular_start_value,
      next_regular_end_value,
      provider_contract_sha256_value
    );

  if candidate_sha256_value <> expected_evidence_sha256_value then
    raise exception 'pit_calendar_observation_canonical_evidence_sha256_mismatch'
      using errcode = '22023';
  end if;

  request_sha256_value := private.pit_sha256_text_v1(p_session::text);

  -- This lock namespace is intentionally shared with the timing-evidence RPC.
  perform pg_catalog.pg_advisory_xact_lock(
    pg_catalog.hashtextextended(
      expected_identity_sha256_value,
      813004912::bigint
    )
  );

  select stream.* into head
  from private.pit_calendar_stream_heads as stream
  where stream.calendar_idempotency_key = expected_identity_sha256_value
  for update;

  if not found then
    insert into private.pit_calendar_stream_heads (
      calendar_idempotency_key,
      provider,
      market,
      session_date,
      latest_revision,
      latest_canonical_evidence_sha256,
      latest_observed_at,
      last_seen_observed_at
    ) values (
      expected_identity_sha256_value,
      provider_value,
      market_value,
      session_date_value,
      1,
      candidate_sha256_value,
      observed_at_value,
      observed_at_value
    );

    insert into private.pit_calendar_content_revisions (
      calendar_idempotency_key,
      revision,
      canonical_evidence_sha256,
      observed_at,
      calendar_payload
    ) values (
      expected_identity_sha256_value,
      1,
      candidate_sha256_value,
      observed_at_value,
      p_session
    )
    returning * into content_revision;

    select result.occurrence_id, result.occurrence_inserted
    into occurrence_id_value, occurrence_inserted_value
    from private.put_pit_calendar_observation_occurrence_v1(
      expected_identity_sha256_value,
      candidate_sha256_value,
      content_revision.id,
      observed_at_value,
      p_session
    ) as result;

    if occurrence_id_value is null
       or occurrence_inserted_value is distinct from true then
      raise exception 'pit_calendar_observation_initial_occurrence_failed'
        using errcode = '55000';
    end if;

    return query select
      'stored'::text,
      expected_identity_sha256_value,
      candidate_sha256_value,
      1::bigint,
      true,
      occurrence_id_value,
      true,
      observed_at_value,
      null::uuid,
      null::text;
    return;
  end if;

  if head.provider <> provider_value
     or head.market <> market_value
     or head.session_date <> session_date_value then
    raise exception 'pit_calendar_observation_identity_hash_collision'
      using errcode = '22023';
  end if;

  -- An exact occurrence is a safe retry even after a later observation or
  -- correction. Resolve it before applying monotonic delivery guards.
  select occurrence.* into existing_occurrence
  from private.pit_calendar_observation_occurrences as occurrence
  where occurrence.calendar_idempotency_key =
      expected_identity_sha256_value
    and occurrence.observed_at = observed_at_value;

  if found then
    if existing_occurrence.canonical_evidence_sha256 = candidate_sha256_value
       and existing_occurrence.observation_payload = p_session then
      select revision_row.* into strict content_revision
      from private.pit_calendar_content_revisions as revision_row
      where revision_row.id = existing_occurrence.content_revision_id;

      return query select
        'replayed'::text,
        expected_identity_sha256_value,
        candidate_sha256_value,
        content_revision.revision,
        false,
        existing_occurrence.id,
        false,
        observed_at_value,
        null::uuid,
        null::text;
      return;
    end if;

    quarantine_reason_value := 'pit_calendar_revision_time_not_increasing';
  end if;

  if quarantine_reason_value is null then
    if observed_at_value < head.last_seen_observed_at then
      quarantine_reason_value := 'pit_calendar_observation_time_regressed';
    elsif candidate_sha256_value = head.latest_canonical_evidence_sha256 then
      select revision_row.* into strict content_revision
      from private.pit_calendar_content_revisions as revision_row
      where revision_row.calendar_idempotency_key =
          expected_identity_sha256_value
        and revision_row.revision = head.latest_revision;

      select result.occurrence_id, result.occurrence_inserted
      into occurrence_id_value, occurrence_inserted_value
      from private.put_pit_calendar_observation_occurrence_v1(
        expected_identity_sha256_value,
        candidate_sha256_value,
        content_revision.id,
        observed_at_value,
        p_session
      ) as result;

      if occurrence_id_value is null then
        raise exception 'pit_calendar_observation_replay_occurrence_failed'
          using errcode = '55000';
      end if;

      if occurrence_inserted_value
         and observed_at_value > head.last_seen_observed_at then
        update private.pit_calendar_stream_heads as stream
        set last_seen_observed_at = observed_at_value,
            updated_at = pg_catalog.clock_timestamp()
        where stream.calendar_idempotency_key =
          expected_identity_sha256_value;
      end if;

      return query select
        'replayed'::text,
        expected_identity_sha256_value,
        candidate_sha256_value,
        head.latest_revision,
        false,
        occurrence_id_value,
        occurrence_inserted_value,
        observed_at_value,
        null::uuid,
        null::text;
      return;
    elsif exists (
      select 1
      from private.pit_calendar_content_revisions as historical
      where historical.calendar_idempotency_key =
          expected_identity_sha256_value
        and historical.canonical_evidence_sha256 = candidate_sha256_value
    ) then
      quarantine_reason_value :=
        'pit_calendar_historical_hash_recurrence_ambiguous';
    elsif observed_at_value <= head.last_seen_observed_at then
      quarantine_reason_value := 'pit_calendar_revision_time_not_increasing';
    end if;
  end if;

  if quarantine_reason_value is not null then
    quarantine_id_value := private.quarantine_pit_calendar_observation_v1(
      expected_identity_sha256_value,
      request_sha256_value,
      expected_identity_sha256_value,
      quarantine_reason_value,
      candidate_sha256_value,
      observed_at_value,
      p_session,
      head.latest_revision,
      head.latest_canonical_evidence_sha256,
      head.last_seen_observed_at
    );

    return query select
      'quarantined'::text,
      expected_identity_sha256_value,
      candidate_sha256_value,
      head.latest_revision,
      false,
      null::uuid,
      false,
      observed_at_value,
      quarantine_id_value,
      quarantine_reason_value;
    return;
  end if;

  next_revision_value := head.latest_revision + 1;
  insert into private.pit_calendar_content_revisions (
    calendar_idempotency_key,
    revision,
    canonical_evidence_sha256,
    observed_at,
    calendar_payload
  ) values (
    expected_identity_sha256_value,
    next_revision_value,
    candidate_sha256_value,
    observed_at_value,
    p_session
  )
  returning * into content_revision;

  select result.occurrence_id, result.occurrence_inserted
  into occurrence_id_value, occurrence_inserted_value
  from private.put_pit_calendar_observation_occurrence_v1(
    expected_identity_sha256_value,
    candidate_sha256_value,
    content_revision.id,
    observed_at_value,
    p_session
  ) as result;

  if occurrence_id_value is null
     or occurrence_inserted_value is distinct from true then
    raise exception 'pit_calendar_observation_revision_occurrence_failed'
      using errcode = '55000';
  end if;

  update private.pit_calendar_stream_heads as stream
  set latest_revision = next_revision_value,
      latest_canonical_evidence_sha256 = candidate_sha256_value,
      latest_observed_at = observed_at_value,
      last_seen_observed_at = observed_at_value,
      updated_at = pg_catalog.clock_timestamp()
  where stream.calendar_idempotency_key = expected_identity_sha256_value;

  return query select
    'stored'::text,
    expected_identity_sha256_value,
    candidate_sha256_value,
    next_revision_value,
    true,
    occurrence_id_value,
    true,
    observed_at_value,
    null::uuid,
    null::text;
end;
$$;

create or replace function worker_api.append_pit_kr_daily_session_observation_v1(
  p_session jsonb
)
returns table (
  status text,
  calendar_idempotency_key text,
  canonical_evidence_sha256 text,
  revision bigint,
  revision_inserted boolean,
  occurrence_id uuid,
  occurrence_inserted boolean,
  observed_at timestamptz,
  quarantine_id uuid,
  reason_code text
)
language sql
volatile
security invoker
set search_path = ''
as $$
  select result.*
  from private.append_pit_kr_daily_session_observation_v1_impl(
    p_session
  ) as result;
$$;

revoke all on function
  private.append_pit_kr_daily_session_observation_v1_impl(jsonb),
  worker_api.append_pit_kr_daily_session_observation_v1(jsonb)
from public, anon, authenticated, authenticator, service_role;

grant execute on function
  private.append_pit_kr_daily_session_observation_v1_impl(jsonb),
  worker_api.append_pit_kr_daily_session_observation_v1(jsonb)
to service_role;

do $$
declare
  impl_contract_count bigint;
  wrapper_contract_count bigint;
  calendar_rls_count bigint;
  calendar_policy_count bigint;
  forbidden_table_acl_count bigint;
  forbidden_function_acl_count bigint;
begin
  select count(*) into impl_contract_count
  from pg_catalog.pg_proc as procedure
  join pg_catalog.pg_roles as owner_role
    on owner_role.oid = procedure.proowner
  where procedure.oid =
      'private.append_pit_kr_daily_session_observation_v1_impl(jsonb)'::regprocedure
    and procedure.prosecdef
    and procedure.provolatile = 'v'
    and procedure.proconfig = array['search_path=""']::text[]
    and owner_role.rolname not in (
      'anon', 'authenticated', 'authenticator', 'service_role'
    )
    and procedure.proowner = (
      select wrapper.proowner
      from pg_catalog.pg_proc as wrapper
      where wrapper.oid =
        'worker_api.append_pit_kr_daily_session_observation_v1(jsonb)'::regprocedure
    );

  select count(*) into wrapper_contract_count
  from pg_catalog.pg_proc as procedure
  join pg_catalog.pg_roles as owner_role
    on owner_role.oid = procedure.proowner
  where procedure.oid =
      'worker_api.append_pit_kr_daily_session_observation_v1(jsonb)'::regprocedure
    and not procedure.prosecdef
    and procedure.provolatile = 'v'
    and procedure.proconfig = array['search_path=""']::text[]
    and owner_role.rolname not in (
      'anon', 'authenticated', 'authenticator', 'service_role'
    )
    and procedure.proowner = (
      select implementation.proowner
      from pg_catalog.pg_proc as implementation
      where implementation.oid =
        'private.append_pit_kr_daily_session_observation_v1_impl(jsonb)'::regprocedure
    );

  select count(*) into calendar_rls_count
  from pg_catalog.pg_class as relation
  where relation.oid in (
      'private.pit_calendar_stream_heads'::regclass,
      'private.pit_calendar_content_revisions'::regclass,
      'private.pit_calendar_observation_occurrences'::regclass,
      'private.pit_calendar_observation_quarantine'::regclass
    )
    and relation.relrowsecurity;

  select count(*) into calendar_policy_count
  from pg_catalog.pg_policy as policy
  where policy.polrelid in (
      'private.pit_calendar_stream_heads'::regclass,
      'private.pit_calendar_content_revisions'::regclass,
      'private.pit_calendar_observation_occurrences'::regclass,
      'private.pit_calendar_observation_quarantine'::regclass
    );

  select count(*) into forbidden_table_acl_count
  from pg_catalog.pg_class as relation
  cross join lateral pg_catalog.aclexplode(
    coalesce(
      relation.relacl,
      pg_catalog.acldefault('r', relation.relowner)
    )
  ) as acl
  left join pg_catalog.pg_roles as grantee
    on grantee.oid = acl.grantee
  where relation.oid in (
      'private.pit_calendar_stream_heads'::regclass,
      'private.pit_calendar_content_revisions'::regclass,
      'private.pit_calendar_observation_occurrences'::regclass,
      'private.pit_calendar_observation_quarantine'::regclass
    )
    and (
      acl.grantee = 0
      or grantee.rolname in (
        'anon', 'authenticated', 'authenticator', 'service_role'
      )
    );

  select count(*) into forbidden_function_acl_count
  from pg_catalog.pg_proc as procedure
  cross join lateral pg_catalog.aclexplode(
    coalesce(
      procedure.proacl,
      pg_catalog.acldefault('f', procedure.proowner)
    )
  ) as acl
  left join pg_catalog.pg_roles as grantee
    on grantee.oid = acl.grantee
  where procedure.oid in (
      'private.append_pit_kr_daily_session_observation_v1_impl(jsonb)'::regprocedure,
      'worker_api.append_pit_kr_daily_session_observation_v1(jsonb)'::regprocedure
    )
    and acl.privilege_type = 'EXECUTE'
    and (
      acl.grantee = 0
      or grantee.rolname in ('anon', 'authenticated', 'authenticator')
    );

  if impl_contract_count <> 1
     or wrapper_contract_count <> 1
     or calendar_rls_count <> 4
     or calendar_policy_count <> 0
     or forbidden_table_acl_count <> 0
     or forbidden_function_acl_count <> 0
     or not pg_catalog.has_function_privilege(
       'service_role',
       'private.append_pit_kr_daily_session_observation_v1_impl(jsonb)',
       'EXECUTE'
     )
     or not pg_catalog.has_function_privilege(
       'service_role',
       'worker_api.append_pit_kr_daily_session_observation_v1(jsonb)',
       'EXECUTE'
     ) then
    raise exception 'pit_calendar_observation_store_security_contract_failed'
      using errcode = '55000';
  end if;
end;
$$;

commit;
