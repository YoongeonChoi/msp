begin;

set local lock_timeout = '5s';
set local statement_timeout = '90s';

-- Forward-only dependencies:
--   20260719020000_pit_source_observation_occurrence_store.sql
--   20260719040000_pit_calendar_observation_store.sql
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
     or to_regclass('private.pit_calendar_content_revisions') is null
     or to_regclass('private.pit_calendar_observation_occurrences') is null
     or to_regclass('private.pit_calendar_observation_quarantine') is null then
    raise exception 'pit_calendar_as_of_reader_dependency_missing'
      using errcode = '55000';
  end if;
end;
$$;

-- Preserve existing ISO hash bytes while removing session DateStyle from the
-- canonical calendar helpers. Existing rows were produced from ISO dates, so
-- this is a behavior-preserving convergence for normal sessions and prevents
-- a non-ISO reader session from silently deriving another stream identity.
create or replace function private.pit_calendar_identity_sha256_v1(
  p_provider text,
  p_market text,
  p_session_date date
)
returns text
language sql
immutable
security definer
set search_path = ''
as $$
  select private.pit_sha256_text_v1(
    '{"market":"' || p_market || '"' ||
    ',"provider":"' || p_provider || '"' ||
    ',"schema_version":1' ||
    ',"session_date":"' ||
      pg_catalog.to_char(p_session_date, 'YYYY-MM-DD') || '"}'
  );
$$;

create or replace function private.pit_calendar_canonical_evidence_sha256_v1(
  p_provider text,
  p_market text,
  p_session_date date,
  p_is_open boolean,
  p_regular_start_at timestamptz,
  p_regular_end_at timestamptz,
  p_next_business_date date,
  p_next_regular_start_at timestamptz,
  p_next_regular_end_at timestamptz,
  p_provider_contract_sha256 text
)
returns text
language sql
immutable
security definer
set search_path = ''
as $$
  select private.pit_sha256_text_v1(
    '{"is_open":' || p_is_open::text ||
    ',"market":"' || p_market || '"' ||
    ',"next_business_date":"' ||
      pg_catalog.to_char(p_next_business_date, 'YYYY-MM-DD') || '"' ||
    ',"next_regular_end_at":"' ||
      private.pit_canonical_timestamp_v1(p_next_regular_end_at) || '"' ||
    ',"next_regular_start_at":"' ||
      private.pit_canonical_timestamp_v1(p_next_regular_start_at) || '"' ||
    ',"provider":"' || p_provider || '"' ||
    ',"provider_contract_sha256":"' || p_provider_contract_sha256 || '"' ||
    ',"regular_end_at":' || case
      when p_regular_end_at is null then 'null'
      else '"' || private.pit_canonical_timestamp_v1(p_regular_end_at) || '"'
    end ||
    ',"regular_start_at":' || case
      when p_regular_start_at is null then 'null'
      else '"' || private.pit_canonical_timestamp_v1(p_regular_start_at) || '"'
    end ||
    ',"schema_version":1' ||
    ',"session_date":"' ||
      pg_catalog.to_char(p_session_date, 'YYYY-MM-DD') || '"}'
  );
$$;

create or replace function private.pit_kr_calendar_as_of_candidates_v1(
  p_provider text,
  p_market text,
  p_start_session_date date,
  p_end_session_date date,
  p_as_of timestamptz,
  p_snapshot pg_catalog.pg_snapshot
)
returns table (
  session_date text,
  calendar_revision_id uuid,
  calendar_idempotency_key text,
  calendar_revision bigint,
  calendar_canonical_evidence_sha256 text,
  calendar_revision_observed_at timestamptz,
  calendar_revision_received_at timestamptz,
  calendar_content_payload jsonb,
  calendar_occurrence_id uuid,
  calendar_occurrence_observed_at timestamptz,
  calendar_occurrence_received_at timestamptz,
  calendar_occurrence_origin text,
  calendar_payload jsonb,
  candidate_lineage_sha256 text
)
language sql
stable
security definer
set search_path = ''
as $$
  select
    pg_catalog.to_char(
      p_start_session_date + requested.offset_days,
      'YYYY-MM-DD'
    ),
    calendar_revision.id,
    calendar_occurrence.calendar_idempotency_key,
    calendar_revision.revision,
    calendar_revision.canonical_evidence_sha256,
    calendar_revision.observed_at,
    calendar_revision.received_at,
    calendar_revision.calendar_payload,
    calendar_occurrence.id,
    calendar_occurrence.observed_at,
    calendar_occurrence.received_at,
    calendar_occurrence.record_origin,
    calendar_occurrence.observation_payload,
    private.pit_sha256_text_v1(
      calendar_revision.id::text || '|' ||
      calendar_occurrence.calendar_idempotency_key || '|' ||
      calendar_revision.revision::text || '|' ||
      calendar_revision.canonical_evidence_sha256 || '|' ||
      private.pit_canonical_timestamp_v1(calendar_revision.observed_at) || '|' ||
      private.pit_canonical_timestamp_v1(calendar_revision.received_at) || '|' ||
      calendar_occurrence.id::text || '|' ||
      private.pit_canonical_timestamp_v1(calendar_occurrence.observed_at) || '|' ||
      private.pit_canonical_timestamp_v1(calendar_occurrence.received_at) || '|' ||
      calendar_occurrence.record_origin
    )
  from pg_catalog.generate_series(
         0,
         p_end_session_date - p_start_session_date
       ) as requested(offset_days)
  join private.pit_calendar_observation_occurrences as calendar_occurrence
    on calendar_occurrence.calendar_idempotency_key =
       private.pit_calendar_identity_sha256_v1(
         p_provider, p_market, p_start_session_date + requested.offset_days
       )
   and calendar_occurrence.observed_at <= p_as_of
   and pg_catalog.pg_visible_in_snapshot(
         (calendar_occurrence.xmin::text)::pg_catalog.xid8,
         p_snapshot
       )
  left join private.pit_calendar_content_revisions as calendar_revision
    on calendar_occurrence.content_revision_id = calendar_revision.id
   and calendar_revision.calendar_idempotency_key =
       calendar_occurrence.calendar_idempotency_key
   and calendar_revision.canonical_evidence_sha256 =
       calendar_occurrence.canonical_evidence_sha256
   and pg_catalog.pg_visible_in_snapshot(
         (calendar_revision.xmin::text)::pg_catalog.xid8,
         p_snapshot
       );
$$;

create or replace function private.list_pit_kr_daily_sessions_as_of_v1_impl(
  p_provider text,
  p_market text,
  p_start_session_date date,
  p_end_session_date date,
  p_as_of timestamptz,
  p_limit integer,
  p_cursor jsonb
)
returns jsonb
language plpgsql
stable
security definer
set search_path = ''
as $$
declare
  contract_version constant text := 'pit_calendar_as_of_reader.v1';
  cursor_version constant text := 'pit_calendar_as_of_cursor.v1';
  cursor_ttl constant interval := interval '15 minutes';
  query_sha256_value text;
  snapshot_token_value text;
  snapshot_value pg_catalog.pg_snapshot;
  snapshot_issued_at_value timestamptz;
  snapshot_manifest_sha256_value text;
  candidate_count_value bigint;
  timeline_count_value bigint;
  candidate record;
  items_value jsonb := '[]'::jsonb;
  next_cursor_value jsonb;
  last_session_date_value text;
  last_occurrence_observed_at_value timestamptz;
  last_calendar_revision_value bigint;
  last_calendar_revision_id_value uuid;
  last_calendar_occurrence_id_value uuid;
  session_date_value date;
  is_open_value boolean;
  regular_start_value timestamptz;
  regular_end_value timestamptz;
  next_business_date_value date;
  next_regular_start_value timestamptz;
  next_regular_end_value timestamptz;
  observed_at_value timestamptz;
  expected_identity_sha256_value text;
  expected_evidence_sha256_value text;
begin
  perform private.require_service_role();

  if p_provider is null
     or p_provider !~ '^[a-z][a-z0-9._-]{0,63}$'
     or p_market is distinct from 'KR'
     or p_start_session_date is null
     or p_end_session_date is null
     or p_start_session_date > p_end_session_date
     or p_end_session_date - p_start_session_date > 365
     or p_as_of is null
     or not pg_catalog.isfinite(p_as_of)
     or p_limit is null
     or p_limit < 25
     or p_limit > 100
     or (p_cursor is not null and (
       pg_catalog.jsonb_typeof(p_cursor) <> 'object'
       or pg_catalog.octet_length(p_cursor::text) > 4096
     )) then
    raise exception 'pit_calendar_as_of_reader_argument_invalid'
      using errcode = '22023';
  end if;

  query_sha256_value := private.pit_sha256_text_v1(
    '{"as_of":"' || private.pit_canonical_timestamp_v1(p_as_of) || '"' ||
    ',"contract_version":"' || contract_version || '"' ||
    ',"end_session_date":"' ||
      pg_catalog.to_char(p_end_session_date, 'YYYY-MM-DD') || '"' ||
    ',"limit":' || p_limit::text ||
    ',"market":"' || p_market || '"' ||
    ',"provider":"' || p_provider || '"' ||
    ',"start_session_date":"' ||
      pg_catalog.to_char(p_start_session_date, 'YYYY-MM-DD') || '"}'
  );

  if p_cursor is null then
    snapshot_token_value := pg_catalog.pg_current_snapshot()::text;
    snapshot_issued_at_value := pg_catalog.statement_timestamp();
    snapshot_value := snapshot_token_value::pg_catalog.pg_snapshot;
  else
    if private.jsonb_exact_keys_v1(
         p_cursor,
         array[
           'last_calendar_occurrence_id', 'last_calendar_revision',
           'last_calendar_revision_id', 'last_occurrence_observed_at',
           'last_session_date', 'query_sha256', 'schema_version',
           'snapshot_issued_at', 'snapshot_manifest_sha256',
           'snapshot_token'
         ]
       ) is distinct from true
       or pg_catalog.jsonb_typeof(p_cursor->'schema_version') <> 'string'
       or p_cursor->>'schema_version' <> cursor_version
       or pg_catalog.jsonb_typeof(p_cursor->'query_sha256') <> 'string'
       or p_cursor->>'query_sha256' <> query_sha256_value
       or pg_catalog.jsonb_typeof(p_cursor->'snapshot_token') <> 'string'
       or pg_catalog.jsonb_typeof(p_cursor->'snapshot_issued_at') <> 'string'
       or pg_catalog.jsonb_typeof(p_cursor->'snapshot_manifest_sha256') <>
          'string'
       or (p_cursor->>'snapshot_manifest_sha256') !~ '^[0-9a-f]{64}$'
       or pg_catalog.jsonb_typeof(p_cursor->'last_session_date') <> 'string'
       or (p_cursor->>'last_session_date') !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
       or pg_catalog.jsonb_typeof(
            p_cursor->'last_occurrence_observed_at'
          ) <> 'string'
       or pg_catalog.jsonb_typeof(
            p_cursor->'last_calendar_revision'
          ) <> 'number'
       or (p_cursor->>'last_calendar_revision') !~ '^[1-9][0-9]*$'
       or pg_catalog.jsonb_typeof(
            p_cursor->'last_calendar_revision_id'
          ) <> 'string'
       or pg_catalog.jsonb_typeof(
            p_cursor->'last_calendar_occurrence_id'
          ) <> 'string' then
      raise exception 'pit_calendar_as_of_reader_cursor_invalid'
        using errcode = '22023';
    end if;

    begin
      snapshot_token_value := p_cursor->>'snapshot_token';
      snapshot_value := snapshot_token_value::pg_catalog.pg_snapshot;
      snapshot_issued_at_value :=
        (p_cursor->>'snapshot_issued_at')::timestamptz;
      last_session_date_value := p_cursor->>'last_session_date';
      perform last_session_date_value::date;
      last_occurrence_observed_at_value :=
        (p_cursor->>'last_occurrence_observed_at')::timestamptz;
      last_calendar_revision_value :=
        (p_cursor->>'last_calendar_revision')::bigint;
      last_calendar_revision_id_value :=
        (p_cursor->>'last_calendar_revision_id')::uuid;
      last_calendar_occurrence_id_value :=
        (p_cursor->>'last_calendar_occurrence_id')::uuid;
    exception
      when others then
        raise exception 'pit_calendar_as_of_reader_cursor_invalid'
          using errcode = '22023';
    end;

    if private.pit_canonical_timestamp_v1(snapshot_issued_at_value) <>
         p_cursor->>'snapshot_issued_at'
       or private.pit_canonical_timestamp_v1(
            last_occurrence_observed_at_value
          ) <> p_cursor->>'last_occurrence_observed_at'
       or snapshot_value::text <> snapshot_token_value
       or pg_catalog.to_char(
            last_session_date_value::date,
            'YYYY-MM-DD'
          ) <> last_session_date_value
       or last_calendar_revision_id_value::text <>
          p_cursor->>'last_calendar_revision_id'
       or last_calendar_occurrence_id_value::text <>
          p_cursor->>'last_calendar_occurrence_id'
       or snapshot_issued_at_value > pg_catalog.statement_timestamp()
       or snapshot_issued_at_value <
          pg_catalog.statement_timestamp() - cursor_ttl
       or last_session_date_value <
          pg_catalog.to_char(p_start_session_date, 'YYYY-MM-DD')
       or last_session_date_value >
          pg_catalog.to_char(p_end_session_date, 'YYYY-MM-DD') then
      raise exception 'pit_calendar_as_of_reader_cursor_invalid'
        using errcode = '22023';
    end if;
  end if;

  select count(*) into candidate_count_value
  from private.pit_kr_calendar_as_of_candidates_v1(
    p_provider,
    p_market,
    p_start_session_date,
    p_end_session_date,
    p_as_of,
    snapshot_value
  );

  select count(*) into timeline_count_value
  from private.pit_kr_calendar_as_of_candidates_v1(
    p_provider,
    p_market,
    p_start_session_date,
    p_end_session_date,
    p_as_of,
    snapshot_value
  );

  if candidate_count_value > 1000 or timeline_count_value > 1000 then
    raise exception 'pit_calendar_as_of_reader_candidate_limit_exceeded'
      using errcode = '54000';
  end if;

  -- Every visible revision in the requested identity range must have an exact
  -- visible occurrence. This prevents an orphan revision from disappearing
  -- merely because the candidate helper is occurrence-driven.
  if exists (
    select 1
    from pg_catalog.generate_series(
           0,
           p_end_session_date - p_start_session_date
         ) as requested(offset_days)
    join private.pit_calendar_content_revisions as revision
      on revision.calendar_idempotency_key =
         private.pit_calendar_identity_sha256_v1(
           p_provider, p_market,
           p_start_session_date + requested.offset_days
         )
     and pg_catalog.pg_visible_in_snapshot(
           (revision.xmin::text)::pg_catalog.xid8,
           snapshot_value
         )
    where revision.observed_at <= p_as_of
      and not exists (
      select 1
      from private.pit_calendar_observation_occurrences as occurrence
      where occurrence.content_revision_id = revision.id
        and occurrence.calendar_idempotency_key =
            revision.calendar_idempotency_key
        and occurrence.canonical_evidence_sha256 =
            revision.canonical_evidence_sha256
        and occurrence.observed_at <= p_as_of
        and pg_catalog.pg_visible_in_snapshot(
              (occurrence.xmin::text)::pg_catalog.xid8,
              snapshot_value
            )
    )
  ) then
    raise exception 'pit_calendar_as_of_reader_integrity_violation'
      using errcode = '55000';
  end if;

  -- Validate the complete eligible retained timeline. No malformed row at or
  -- before the source-semantic cutoff can be silently omitted.
  begin
    for candidate in
      select *
      from private.pit_kr_calendar_as_of_candidates_v1(
        p_provider,
        p_market,
        p_start_session_date,
        p_end_session_date,
        p_as_of,
        snapshot_value
      )
    loop
      if candidate.calendar_revision_id is null
         or candidate.calendar_revision is null
         or candidate.calendar_revision <= 0
         or candidate.calendar_idempotency_key !~ '^[0-9a-f]{64}$'
         or candidate.calendar_canonical_evidence_sha256 !~
            '^[0-9a-f]{64}$'
         or candidate.calendar_occurrence_id is null
         or candidate.candidate_lineage_sha256 !~ '^[0-9a-f]{64}$'
         or candidate.calendar_occurrence_origin not in (
           'content_revision_backfill', 'stream_head_recovery', 'rpc'
         )
         or not pg_catalog.isfinite(
              candidate.calendar_revision_observed_at
            )
         or not pg_catalog.isfinite(
              candidate.calendar_revision_received_at
            )
         or not pg_catalog.isfinite(
              candidate.calendar_occurrence_observed_at
            )
         or not pg_catalog.isfinite(
              candidate.calendar_occurrence_received_at
            )
         or pg_catalog.octet_length(candidate.calendar_content_payload::text)
            > 8192
         or pg_catalog.octet_length(candidate.calendar_payload::text) > 8192
         or private.jsonb_exact_keys_v1(
           candidate.calendar_content_payload,
           array[
             'canonical_evidence_sha256', 'is_open', 'market',
             'next_business_date', 'next_regular_end_at',
             'next_regular_start_at', 'observed_at', 'provider',
             'provider_contract_sha256', 'regular_end_at',
             'regular_start_at', 'schema_version', 'session_date'
           ]
         ) is distinct from true
         or private.jsonb_exact_keys_v1(
           candidate.calendar_payload,
           array[
             'canonical_evidence_sha256', 'is_open', 'market',
             'next_business_date', 'next_regular_end_at',
             'next_regular_start_at', 'observed_at', 'provider',
             'provider_contract_sha256', 'regular_end_at',
             'regular_start_at', 'schema_version', 'session_date'
           ]
         ) is distinct from true then
        raise exception 'candidate_shape_invalid';
      end if;

      if pg_catalog.jsonb_typeof(
           candidate.calendar_content_payload->'observed_at'
         ) is distinct from 'string'
         or candidate.calendar_content_payload - 'observed_at' <>
            candidate.calendar_payload - 'observed_at'
         or candidate.calendar_content_payload->>'observed_at' is distinct from
            private.pit_canonical_timestamp_v1(
              candidate.calendar_revision_observed_at
            )
         or candidate.calendar_payload->>'observed_at' is distinct from
            private.pit_canonical_timestamp_v1(
              candidate.calendar_occurrence_observed_at
            ) then
        raise exception 'candidate_occurrence_content_mismatch';
      end if;

      if pg_catalog.jsonb_typeof(
           candidate.calendar_payload->'schema_version'
         ) <> 'number'
         or candidate.calendar_payload->>'schema_version' <> '1'
         or pg_catalog.jsonb_typeof(
              candidate.calendar_payload->'provider'
            ) <> 'string'
         or candidate.calendar_payload->>'provider' <> p_provider
         or pg_catalog.jsonb_typeof(
              candidate.calendar_payload->'market'
            ) <> 'string'
         or candidate.calendar_payload->>'market' <> p_market
         or pg_catalog.jsonb_typeof(
              candidate.calendar_payload->'session_date'
            ) <> 'string'
         or (candidate.calendar_payload->>'session_date') !~
            '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
         or candidate.calendar_payload->>'session_date' <>
            candidate.session_date
         or pg_catalog.jsonb_typeof(
              candidate.calendar_payload->'is_open'
            ) <> 'boolean'
         or pg_catalog.jsonb_typeof(
              candidate.calendar_payload->'regular_start_at'
            ) not in ('string', 'null')
         or pg_catalog.jsonb_typeof(
              candidate.calendar_payload->'regular_end_at'
            ) not in ('string', 'null')
         or pg_catalog.jsonb_typeof(
              candidate.calendar_payload->'next_business_date'
            ) <> 'string'
         or (candidate.calendar_payload->>'next_business_date') !~
            '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
         or pg_catalog.jsonb_typeof(
              candidate.calendar_payload->'next_regular_start_at'
            ) <> 'string'
         or pg_catalog.jsonb_typeof(
              candidate.calendar_payload->'next_regular_end_at'
            ) <> 'string'
         or pg_catalog.jsonb_typeof(
              candidate.calendar_payload->'observed_at'
            ) <> 'string'
         or pg_catalog.jsonb_typeof(
              candidate.calendar_payload->'provider_contract_sha256'
            ) <> 'string'
         or (candidate.calendar_payload->>'provider_contract_sha256') !~
            '^[0-9a-f]{64}$'
         or pg_catalog.jsonb_typeof(
              candidate.calendar_payload->'canonical_evidence_sha256'
            ) <> 'string'
         or candidate.calendar_payload->>'canonical_evidence_sha256' <>
            candidate.calendar_canonical_evidence_sha256 then
        raise exception 'candidate_payload_invalid';
      end if;

      session_date_value :=
        (candidate.calendar_payload->>'session_date')::date;
      is_open_value :=
        (candidate.calendar_payload->>'is_open')::boolean;
      regular_start_value := case
        when pg_catalog.jsonb_typeof(
          candidate.calendar_payload->'regular_start_at'
        ) = 'null' then null
        else (candidate.calendar_payload->>'regular_start_at')::timestamptz
      end;
      regular_end_value := case
        when pg_catalog.jsonb_typeof(
          candidate.calendar_payload->'regular_end_at'
        ) = 'null' then null
        else (candidate.calendar_payload->>'regular_end_at')::timestamptz
      end;
      next_business_date_value :=
        (candidate.calendar_payload->>'next_business_date')::date;
      next_regular_start_value :=
        (candidate.calendar_payload->>'next_regular_start_at')::timestamptz;
      next_regular_end_value :=
        (candidate.calendar_payload->>'next_regular_end_at')::timestamptz;
      observed_at_value :=
        (candidate.calendar_payload->>'observed_at')::timestamptz;

      expected_identity_sha256_value :=
        private.pit_calendar_identity_sha256_v1(
          p_provider,
          p_market,
          session_date_value
        );
      expected_evidence_sha256_value :=
        private.pit_calendar_canonical_evidence_sha256_v1(
          p_provider,
          p_market,
          session_date_value,
          is_open_value,
          regular_start_value,
          regular_end_value,
          next_business_date_value,
          next_regular_start_value,
          next_regular_end_value,
          candidate.calendar_payload->>'provider_contract_sha256'
        );

      if pg_catalog.to_char(session_date_value, 'YYYY-MM-DD') <>
           candidate.session_date
         or pg_catalog.to_char(next_business_date_value, 'YYYY-MM-DD') <>
            candidate.calendar_payload->>'next_business_date'
         or private.pit_canonical_timestamp_v1(next_regular_start_value) <>
            candidate.calendar_payload->>'next_regular_start_at'
         or private.pit_canonical_timestamp_v1(next_regular_end_value) <>
            candidate.calendar_payload->>'next_regular_end_at'
         or private.pit_canonical_timestamp_v1(observed_at_value) <>
            candidate.calendar_payload->>'observed_at'
         or (
           regular_start_value is not null
           and private.pit_canonical_timestamp_v1(regular_start_value) <>
               candidate.calendar_payload->>'regular_start_at'
         )
         or (
           regular_end_value is not null
           and private.pit_canonical_timestamp_v1(regular_end_value) <>
               candidate.calendar_payload->>'regular_end_at'
         )
         or expected_identity_sha256_value <>
            candidate.calendar_idempotency_key
         or expected_evidence_sha256_value <>
            candidate.calendar_canonical_evidence_sha256
         or candidate.calendar_content_payload->>'canonical_evidence_sha256'
            <> candidate.calendar_canonical_evidence_sha256 then
        raise exception 'candidate_hash_or_canonical_value_invalid';
      end if;

      if (
           is_open_value
           and (
             regular_start_value is null
             or regular_end_value is null
             or regular_start_value >= regular_end_value
             or (regular_start_value at time zone 'Asia/Seoul')::date <>
                session_date_value
             or (regular_end_value at time zone 'Asia/Seoul')::date <>
                session_date_value
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
         or (next_regular_start_value at time zone 'Asia/Seoul')::date <>
            next_business_date_value
         or (next_regular_end_value at time zone 'Asia/Seoul')::date <>
            next_business_date_value
         or (
           is_open_value
           and regular_end_value >= next_regular_start_value
         ) then
        raise exception 'candidate_kst_geometry_invalid';
      end if;

      if candidate.calendar_revision_observed_at >
           candidate.calendar_occurrence_observed_at
         or candidate.calendar_revision_observed_at >
            candidate.calendar_revision_received_at
         or candidate.calendar_revision_received_at >
            candidate.calendar_occurrence_received_at
         or candidate.calendar_occurrence_observed_at >
            candidate.calendar_occurrence_received_at
         or candidate.calendar_occurrence_received_at >
            snapshot_issued_at_value
         or observed_at_value <>
            candidate.calendar_occurrence_observed_at
         or private.pit_sha256_text_v1(
              candidate.calendar_revision_id::text || '|' ||
              candidate.calendar_idempotency_key || '|' ||
              candidate.calendar_revision::text || '|' ||
              candidate.calendar_canonical_evidence_sha256 || '|' ||
              private.pit_canonical_timestamp_v1(
                candidate.calendar_revision_observed_at
              ) || '|' ||
              private.pit_canonical_timestamp_v1(
                candidate.calendar_revision_received_at
              ) || '|' ||
              candidate.calendar_occurrence_id::text || '|' ||
              private.pit_canonical_timestamp_v1(
                candidate.calendar_occurrence_observed_at
              ) || '|' ||
              private.pit_canonical_timestamp_v1(
                candidate.calendar_occurrence_received_at
              ) || '|' ||
              candidate.calendar_occurrence_origin
            ) <> candidate.candidate_lineage_sha256 then
        raise exception 'candidate_lineage_invalid';
      end if;
    end loop;
  exception
    when others then
      raise exception 'pit_calendar_as_of_reader_integrity_violation'
        using errcode = '55000';
  end;

  -- Revision numbers, revision clocks, and occurrence clocks are validated
  -- over the complete eligible retained timeline.
  if exists (
    with grouped_revisions as (
      select
        rows.calendar_idempotency_key,
        rows.calendar_revision,
        count(distinct rows.calendar_revision_id) as revision_id_count,
        count(distinct rows.calendar_canonical_evidence_sha256)
          as evidence_hash_count,
        min(rows.calendar_revision_observed_at) as revision_observed_at,
        min(rows.calendar_occurrence_observed_at) as first_occurrence_at,
        max(rows.calendar_occurrence_observed_at) as last_occurrence_at
      from private.pit_kr_calendar_as_of_candidates_v1(
        p_provider,
        p_market,
        p_start_session_date,
        p_end_session_date,
        p_as_of,
        snapshot_value
      ) as rows
      group by
        rows.calendar_idempotency_key,
        rows.calendar_revision
    ), ordered_revisions as (
      select
        grouped_revisions.*,
        row_number() over (
          partition by grouped_revisions.calendar_idempotency_key
          order by grouped_revisions.calendar_revision
        ) as expected_revision,
        lag(grouped_revisions.last_occurrence_at) over (
          partition by grouped_revisions.calendar_idempotency_key
          order by grouped_revisions.calendar_revision
        ) as previous_last_occurrence_at
      from grouped_revisions
    )
    select 1
    from ordered_revisions
    where ordered_revisions.revision_id_count <> 1
       or ordered_revisions.evidence_hash_count <> 1
       or ordered_revisions.calendar_revision <>
          ordered_revisions.expected_revision
       or ordered_revisions.revision_observed_at <>
          ordered_revisions.first_occurrence_at
       or (
         ordered_revisions.previous_last_occurrence_at is not null
         and ordered_revisions.revision_observed_at <=
             ordered_revisions.previous_last_occurrence_at
       )
  ) or exists (
    select 1
    from private.pit_kr_calendar_as_of_candidates_v1(
      p_provider,
      p_market,
      p_start_session_date,
      p_end_session_date,
      p_as_of,
      snapshot_value
    ) as rows
    group by rows.calendar_idempotency_key
    having count(*) <> count(distinct rows.calendar_occurrence_id)
       or count(*) <>
          count(distinct rows.calendar_occurrence_observed_at)
       or count(distinct rows.calendar_revision) <>
          count(distinct rows.calendar_canonical_evidence_sha256)
  ) then
    raise exception 'pit_calendar_as_of_reader_integrity_violation'
      using errcode = '55000';
  end if;

  -- A durable ambiguity inside the eligible requested prefix blocks the read.
  if exists (
    select 1
    from pg_catalog.generate_series(
           0,
           p_end_session_date - p_start_session_date
         ) as requested(offset_days)
    join private.pit_calendar_observation_quarantine as quarantine
      on quarantine.calendar_idempotency_key =
         private.pit_calendar_identity_sha256_v1(
           p_provider, p_market,
           p_start_session_date + requested.offset_days
         )
     and quarantine.reason_code in (
       'pit_calendar_observation_time_regressed',
       'pit_calendar_historical_hash_recurrence_ambiguous',
       'pit_calendar_revision_time_not_increasing'
     )
     and quarantine.candidate_observed_at <= p_as_of
     and pg_catalog.pg_visible_in_snapshot(
           (quarantine.xmin::text)::pg_catalog.xid8,
           snapshot_value
         )
  ) then
    raise exception 'pit_calendar_as_of_reader_timeline_ambiguous'
      using errcode = '55000';
  end if;

  select private.pit_sha256_text_v1(
           coalesce(
             pg_catalog.string_agg(
               candidates.candidate_lineage_sha256,
               E'\n' order by
                 candidates.session_date,
                 candidates.calendar_occurrence_observed_at,
                 candidates.calendar_revision,
                 candidates.calendar_revision_id,
                 candidates.calendar_occurrence_id
             ),
             ''
           )
         )
  into snapshot_manifest_sha256_value
  from private.pit_kr_calendar_as_of_candidates_v1(
    p_provider,
    p_market,
    p_start_session_date,
    p_end_session_date,
    p_as_of,
    snapshot_value
  ) as candidates;

  if p_cursor is not null
     and p_cursor->>'snapshot_manifest_sha256' <>
         snapshot_manifest_sha256_value then
    raise exception 'pit_calendar_as_of_reader_snapshot_mismatch'
      using errcode = '40001';
  end if;

  if p_cursor is not null and not exists (
    select 1
    from private.pit_kr_calendar_as_of_candidates_v1(
      p_provider,
      p_market,
      p_start_session_date,
      p_end_session_date,
      p_as_of,
      snapshot_value
    ) as cursor_candidate
    where cursor_candidate.session_date = last_session_date_value
      and cursor_candidate.calendar_occurrence_observed_at =
          last_occurrence_observed_at_value
      and cursor_candidate.calendar_revision =
          last_calendar_revision_value
      and cursor_candidate.calendar_revision_id =
          last_calendar_revision_id_value
      and cursor_candidate.calendar_occurrence_id =
          last_calendar_occurrence_id_value
  ) then
    raise exception 'pit_calendar_as_of_reader_cursor_invalid'
      using errcode = '22023';
  end if;

  with ordered as (
    select candidates.*
    from private.pit_kr_calendar_as_of_candidates_v1(
      p_provider,
      p_market,
      p_start_session_date,
      p_end_session_date,
      p_as_of,
      snapshot_value
    ) as candidates
    where p_cursor is null
       or (
         candidates.session_date,
         candidates.calendar_occurrence_observed_at,
         candidates.calendar_revision,
         candidates.calendar_revision_id,
         candidates.calendar_occurrence_id
       ) > (
         last_session_date_value,
         last_occurrence_observed_at_value,
         last_calendar_revision_value,
         last_calendar_revision_id_value,
         last_calendar_occurrence_id_value
       )
    order by
      candidates.session_date,
      candidates.calendar_occurrence_observed_at,
      candidates.calendar_revision,
      candidates.calendar_revision_id,
      candidates.calendar_occurrence_id
    limit p_limit + 1
  ), page as (
    select *
    from ordered
    order by
      session_date,
      calendar_occurrence_observed_at,
      calendar_revision,
      calendar_revision_id,
      calendar_occurrence_id
    limit p_limit
  )
  select coalesce(
           pg_catalog.jsonb_agg(
             pg_catalog.jsonb_build_object(
               'session_date', page.session_date,
               'calendar_idempotency_key',
                 page.calendar_idempotency_key,
               'calendar_revision_id', page.calendar_revision_id::text,
               'calendar_revision', page.calendar_revision,
               'calendar_canonical_evidence_sha256',
                 page.calendar_canonical_evidence_sha256,
               'calendar_revision_observed_at',
                 private.pit_canonical_timestamp_v1(
                   page.calendar_revision_observed_at
                 ),
               'calendar_revision_received_at',
                 private.pit_canonical_timestamp_v1(
                   page.calendar_revision_received_at
                 ),
               'calendar_content_payload', page.calendar_content_payload,
               'calendar_occurrence_id', page.calendar_occurrence_id::text,
               'calendar_occurrence_observed_at',
                 private.pit_canonical_timestamp_v1(
                   page.calendar_occurrence_observed_at
                 ),
               'calendar_occurrence_received_at',
                 private.pit_canonical_timestamp_v1(
                   page.calendar_occurrence_received_at
                 ),
               'calendar_occurrence_origin',
                 page.calendar_occurrence_origin,
               'calendar_payload', page.calendar_payload,
               'candidate_lineage_sha256', page.candidate_lineage_sha256
             ) order by
               page.session_date,
               page.calendar_occurrence_observed_at,
               page.calendar_revision,
               page.calendar_revision_id,
               page.calendar_occurrence_id
           ),
           '[]'::jsonb
         )
  into items_value
  from page;

  select pg_catalog.jsonb_build_object(
           'schema_version', cursor_version,
           'query_sha256', query_sha256_value,
           'snapshot_token', snapshot_token_value,
           'snapshot_issued_at',
             private.pit_canonical_timestamp_v1(snapshot_issued_at_value),
           'snapshot_manifest_sha256', snapshot_manifest_sha256_value,
           'last_session_date', tail.session_date,
           'last_occurrence_observed_at',
             private.pit_canonical_timestamp_v1(
               tail.calendar_occurrence_observed_at
             ),
           'last_calendar_revision', tail.calendar_revision,
           'last_calendar_revision_id', tail.calendar_revision_id::text,
           'last_calendar_occurrence_id', tail.calendar_occurrence_id::text
         )
  into next_cursor_value
  from (
    select candidates.*
    from private.pit_kr_calendar_as_of_candidates_v1(
      p_provider,
      p_market,
      p_start_session_date,
      p_end_session_date,
      p_as_of,
      snapshot_value
    ) as candidates
    where p_cursor is null
       or (
         candidates.session_date,
         candidates.calendar_occurrence_observed_at,
         candidates.calendar_revision,
         candidates.calendar_revision_id,
         candidates.calendar_occurrence_id
       ) > (
         last_session_date_value,
         last_occurrence_observed_at_value,
         last_calendar_revision_value,
         last_calendar_revision_id_value,
         last_calendar_occurrence_id_value
       )
    order by
      candidates.session_date,
      candidates.calendar_occurrence_observed_at,
      candidates.calendar_revision,
      candidates.calendar_revision_id,
      candidates.calendar_occurrence_id
    offset p_limit - 1
    limit 1
  ) as tail
  where exists (
    select 1
    from private.pit_kr_calendar_as_of_candidates_v1(
      p_provider,
      p_market,
      p_start_session_date,
      p_end_session_date,
      p_as_of,
      snapshot_value
    ) as remaining
    where (
      remaining.session_date,
      remaining.calendar_occurrence_observed_at,
      remaining.calendar_revision,
      remaining.calendar_revision_id,
      remaining.calendar_occurrence_id
    ) > (
      tail.session_date,
      tail.calendar_occurrence_observed_at,
      tail.calendar_revision,
      tail.calendar_revision_id,
      tail.calendar_occurrence_id
    )
  );

  return pg_catalog.jsonb_build_object(
    'schema_version', contract_version,
    'query_sha256', query_sha256_value,
    'snapshot_token', snapshot_token_value,
    'snapshot_issued_at',
      private.pit_canonical_timestamp_v1(snapshot_issued_at_value),
    'snapshot_manifest_sha256', snapshot_manifest_sha256_value,
    'candidate_count', candidate_count_value,
    'items', items_value,
    'next_cursor', next_cursor_value
  );
end;
$$;

create or replace function worker_api.list_pit_kr_daily_sessions_as_of_v1(
  p_provider text,
  p_market text,
  p_start_session_date date,
  p_end_session_date date,
  p_as_of timestamptz,
  p_limit integer,
  p_cursor jsonb default null
)
returns jsonb
language sql
stable
security invoker
set search_path = ''
as $$
  select private.list_pit_kr_daily_sessions_as_of_v1_impl(
    p_provider,
    p_market,
    p_start_session_date,
    p_end_session_date,
    p_as_of,
    p_limit,
    p_cursor
  );
$$;

revoke all on function
  private.pit_calendar_identity_sha256_v1(text,text,date),
  private.pit_calendar_canonical_evidence_sha256_v1(
    text,text,date,boolean,timestamptz,timestamptz,date,timestamptz,
    timestamptz,text
  ),
  private.pit_kr_calendar_as_of_candidates_v1(
    text,text,date,date,timestamptz,pg_catalog.pg_snapshot
  ),
  private.list_pit_kr_daily_sessions_as_of_v1_impl(
    text,text,date,date,timestamptz,integer,jsonb
  ),
  worker_api.list_pit_kr_daily_sessions_as_of_v1(
    text,text,date,date,timestamptz,integer,jsonb
  )
from public, anon, authenticated, authenticator, service_role;

grant execute on function
  private.list_pit_kr_daily_sessions_as_of_v1_impl(
    text,text,date,date,timestamptz,integer,jsonb
  ),
  worker_api.list_pit_kr_daily_sessions_as_of_v1(
    text,text,date,date,timestamptz,integer,jsonb
  )
to service_role;

do $$
declare
  private_impl_count bigint;
  private_helper_count bigint;
  canonical_helper_count bigint;
  wrapper_count bigint;
  calendar_rls_count bigint;
  calendar_policy_count bigint;
  forbidden_table_acl_count bigint;
  forbidden_function_acl_count bigint;
begin
  select count(*) into private_impl_count
  from pg_catalog.pg_proc as procedure
  join pg_catalog.pg_roles as owner_role
    on owner_role.oid = procedure.proowner
  where procedure.oid =
      'private.list_pit_kr_daily_sessions_as_of_v1_impl(text,text,date,date,timestamptz,integer,jsonb)'::regprocedure
    and procedure.prosecdef
    and procedure.provolatile = 's'
    and procedure.proconfig = array['search_path=""']::text[]
    and owner_role.rolname not in (
      'anon', 'authenticated', 'authenticator', 'service_role'
    )
    and procedure.proowner = (
      select wrapper.proowner
      from pg_catalog.pg_proc as wrapper
      where wrapper.oid =
        'worker_api.list_pit_kr_daily_sessions_as_of_v1(text,text,date,date,timestamptz,integer,jsonb)'::regprocedure
    );

  select count(*) into private_helper_count
  from pg_catalog.pg_proc as procedure
  join pg_catalog.pg_roles as owner_role
    on owner_role.oid = procedure.proowner
  where procedure.oid =
      'private.pit_kr_calendar_as_of_candidates_v1(text,text,date,date,timestamptz,pg_snapshot)'::regprocedure
    and procedure.prosecdef
    and procedure.provolatile = 's'
    and procedure.proconfig = array['search_path=""']::text[]
    and owner_role.rolname not in (
      'anon', 'authenticated', 'authenticator', 'service_role'
    )
    and procedure.proowner = (
      select implementation.proowner
      from pg_catalog.pg_proc as implementation
      where implementation.oid =
        'private.list_pit_kr_daily_sessions_as_of_v1_impl(text,text,date,date,timestamptz,integer,jsonb)'::regprocedure
    );

  select count(*) into wrapper_count
  from pg_catalog.pg_proc as procedure
  join pg_catalog.pg_roles as owner_role
    on owner_role.oid = procedure.proowner
  where procedure.oid =
      'worker_api.list_pit_kr_daily_sessions_as_of_v1(text,text,date,date,timestamptz,integer,jsonb)'::regprocedure
    and not procedure.prosecdef
    and procedure.provolatile = 's'
    and procedure.proconfig = array['search_path=""']::text[]
    and owner_role.rolname not in (
      'anon', 'authenticated', 'authenticator', 'service_role'
    )
    and procedure.proowner = (
      select implementation.proowner
      from pg_catalog.pg_proc as implementation
      where implementation.oid =
        'private.list_pit_kr_daily_sessions_as_of_v1_impl(text,text,date,date,timestamptz,integer,jsonb)'::regprocedure
    );

  select count(*) into canonical_helper_count
  from pg_catalog.pg_proc as procedure
  join pg_catalog.pg_roles as owner_role
    on owner_role.oid = procedure.proowner
  where procedure.oid in (
      'private.pit_calendar_identity_sha256_v1(text,text,date)'::regprocedure,
      'private.pit_calendar_canonical_evidence_sha256_v1(text,text,date,boolean,timestamptz,timestamptz,date,timestamptz,timestamptz,text)'::regprocedure
    )
    and procedure.prosecdef
    and procedure.provolatile = 'i'
    and procedure.proconfig = array['search_path=""']::text[]
    and owner_role.rolname not in (
      'anon', 'authenticated', 'authenticator', 'service_role'
    )
    and procedure.proowner = (
      select implementation.proowner
      from pg_catalog.pg_proc as implementation
      where implementation.oid =
        'private.list_pit_kr_daily_sessions_as_of_v1_impl(text,text,date,date,timestamptz,integer,jsonb)'::regprocedure
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
      'private.pit_calendar_identity_sha256_v1(text,text,date)'::regprocedure,
      'private.pit_calendar_canonical_evidence_sha256_v1(text,text,date,boolean,timestamptz,timestamptz,date,timestamptz,timestamptz,text)'::regprocedure,
      'private.pit_kr_calendar_as_of_candidates_v1(text,text,date,date,timestamptz,pg_snapshot)'::regprocedure,
      'private.list_pit_kr_daily_sessions_as_of_v1_impl(text,text,date,date,timestamptz,integer,jsonb)'::regprocedure,
      'worker_api.list_pit_kr_daily_sessions_as_of_v1(text,text,date,date,timestamptz,integer,jsonb)'::regprocedure
    )
    and acl.privilege_type = 'EXECUTE'
    and (
      acl.grantee = 0
      or grantee.rolname in ('anon', 'authenticated', 'authenticator')
      or (
        procedure.oid in (
          'private.pit_calendar_identity_sha256_v1(text,text,date)'::regprocedure,
          'private.pit_calendar_canonical_evidence_sha256_v1(text,text,date,boolean,timestamptz,timestamptz,date,timestamptz,timestamptz,text)'::regprocedure,
          'private.pit_kr_calendar_as_of_candidates_v1(text,text,date,date,timestamptz,pg_snapshot)'::regprocedure
        )
        and grantee.rolname = 'service_role'
      )
    );

  if private_impl_count <> 1
     or private_helper_count <> 1
     or wrapper_count <> 1
     or canonical_helper_count <> 2
     or calendar_rls_count <> 4
     or calendar_policy_count <> 0
     or forbidden_table_acl_count <> 0
     or forbidden_function_acl_count <> 0
     or not pg_catalog.has_function_privilege(
       'service_role',
       'private.list_pit_kr_daily_sessions_as_of_v1_impl(text,text,date,date,timestamptz,integer,jsonb)',
       'EXECUTE'
     )
     or not pg_catalog.has_function_privilege(
       'service_role',
       'worker_api.list_pit_kr_daily_sessions_as_of_v1(text,text,date,date,timestamptz,integer,jsonb)',
       'EXECUTE'
     )
     or pg_catalog.has_function_privilege(
       'service_role',
       'private.pit_kr_calendar_as_of_candidates_v1(text,text,date,date,timestamptz,pg_snapshot)',
       'EXECUTE'
     ) then
    raise exception 'pit_calendar_as_of_reader_security_contract_failed'
      using errcode = '55000';
  end if;
end;
$$;

commit;
