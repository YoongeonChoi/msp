begin;

set local lock_timeout = '5s';
set local statement_timeout = '90s';

-- Forward-only dependency:
--   20260719020000_pit_source_observation_occurrence_store.sql
do $$
begin
  if to_regprocedure('private.require_service_role()') is null
     or to_regprocedure('private.jsonb_exact_keys_v1(jsonb,text[])') is null
     or to_regprocedure('private.pit_canonical_timestamp_v1(timestamptz)') is null
     or to_regprocedure('private.pit_sha256_text_v1(text)') is null
     or to_regprocedure(
       'private.pit_candle_identity_sha256_v1(text,text,text,text,boolean,timestamptz)'
     ) is null
     or to_regprocedure(
       'private.pit_candle_observation_sha256_v1(text,text,text,text,boolean,timestamptz,text,numeric,numeric,numeric,numeric,numeric,text)'
     ) is null
     or to_regprocedure(
       'private.pit_calendar_identity_sha256_v1(text,text,date)'
     ) is null
     or to_regprocedure(
       'private.pit_calendar_canonical_evidence_sha256_v1(text,text,date,boolean,timestamptz,timestamptz,date,timestamptz,timestamptz,text)'
     ) is null
     or to_regprocedure(
       'private.pit_daily_candle_timing_identity_sha256_v1(text,text)'
     ) is null
     or to_regprocedure(
       'private.pit_daily_candle_timing_evidence_sha256_v1(text,text,text,text,boolean,date,timestamptz,timestamptz,timestamptz,date,timestamptz,timestamptz,timestamptz,timestamptz,text,text,text,text,text,text)'
     ) is null
     or to_regclass('private.pit_candle_observation_revisions') is null
     or to_regclass('private.pit_candle_observation_occurrences') is null
     or to_regclass('private.pit_candle_observation_quarantine') is null
     or to_regclass('private.pit_calendar_content_revisions') is null
     or to_regclass('private.pit_calendar_observation_occurrences') is null
     or to_regclass('private.pit_calendar_observation_quarantine') is null
     or to_regclass('private.pit_daily_candle_timing_revisions') is null
     or to_regclass('private.pit_daily_candle_timing_quarantine') is null then
    raise exception 'pit_daily_candle_as_of_reader_dependency_missing'
      using errcode = '55000';
  end if;
end;
$$;

-- The reader is bounded to one exact daily series, at most 366 calendar days,
-- 1,000 raw candidates, and page sizes 25..100. This expression index narrows
-- the immutable timing ledger before snapshot visibility and integrity checks
-- are applied.
create index pit_daily_candle_timing_revisions_reader_scope_idx
  on private.pit_daily_candle_timing_revisions (
    (timing_payload->>'provider'),
    (timing_payload->>'market'),
    (timing_payload->>'symbol'),
    (timing_payload->>'interval'),
    (timing_payload->>'adjusted'),
    (timing_payload->>'session_date'),
    evidence_available_at,
    revision,
    id
  );

create or replace function private.pit_daily_candle_as_of_candidates_v1(
  p_provider text,
  p_market text,
  p_symbol text,
  p_interval text,
  p_adjusted boolean,
  p_start_session_date date,
  p_end_session_date date,
  p_as_of timestamptz,
  p_snapshot pg_catalog.pg_snapshot
)
returns table (
  session_date text,
  timing_revision_id uuid,
  timing_idempotency_key text,
  timing_revision bigint,
  timing_canonical_evidence_sha256 text,
  timing_evidence_available_at timestamptz,
  timing_received_at timestamptz,
  timing_payload jsonb,
  candle_revision_id uuid,
  candle_revision bigint,
  candle_canonical_observation_sha256 text,
  candle_revision_observed_at timestamptz,
  candle_revision_received_at timestamptz,
  candle_content_payload jsonb,
  candle_occurrence_id uuid,
  candle_occurrence_observed_at timestamptz,
  candle_occurrence_received_at timestamptz,
  candle_occurrence_origin text,
  candle_payload jsonb,
  calendar_revision_id uuid,
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
    timing.timing_payload->>'session_date',
    timing.id,
    timing.timing_idempotency_key,
    timing.revision,
    timing.canonical_timing_evidence_sha256,
    timing.evidence_available_at,
    timing.received_at,
    timing.timing_payload,
    candle_revision.id,
    candle_revision.revision,
    candle_revision.canonical_observation_sha256,
    candle_revision.observed_at,
    candle_revision.received_at,
    candle_revision.candle_payload,
    candle_occurrence.id,
    candle_occurrence.observed_at,
    candle_occurrence.received_at,
    candle_occurrence.record_origin,
    candle_occurrence.observation_payload,
    calendar_revision.id,
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
      timing.id::text || '|' ||
      timing.timing_idempotency_key || '|' ||
      timing.revision::text || '|' ||
      timing.canonical_timing_evidence_sha256 || '|' ||
      private.pit_canonical_timestamp_v1(timing.evidence_available_at) || '|' ||
      private.pit_canonical_timestamp_v1(timing.received_at) || '|' ||
      candle_revision.id::text || '|' ||
      candle_revision.revision::text || '|' ||
      candle_revision.canonical_observation_sha256 || '|' ||
      private.pit_canonical_timestamp_v1(candle_revision.received_at) || '|' ||
      candle_occurrence.id::text || '|' ||
      private.pit_canonical_timestamp_v1(candle_occurrence.observed_at) || '|' ||
      private.pit_canonical_timestamp_v1(candle_occurrence.received_at) || '|' ||
      candle_occurrence.record_origin || '|' ||
      calendar_revision.id::text || '|' ||
      calendar_revision.revision::text || '|' ||
      calendar_revision.canonical_evidence_sha256 || '|' ||
      private.pit_canonical_timestamp_v1(calendar_revision.received_at) || '|' ||
      calendar_occurrence.id::text || '|' ||
      private.pit_canonical_timestamp_v1(calendar_occurrence.observed_at) || '|' ||
      private.pit_canonical_timestamp_v1(calendar_occurrence.received_at) || '|' ||
      calendar_occurrence.record_origin
    )
  from private.pit_daily_candle_timing_revisions as timing
  left join private.pit_candle_observation_occurrences as candle_occurrence
    on candle_occurrence.id = timing.candle_occurrence_id
   and candle_occurrence.content_revision_id = timing.candle_revision_id
   and pg_catalog.pg_visible_in_snapshot(
         (candle_occurrence.xmin::text)::pg_catalog.xid8,
         p_snapshot
       )
  left join private.pit_candle_observation_revisions as candle_revision
    on candle_revision.id = candle_occurrence.content_revision_id
   and candle_revision.idempotency_key = candle_occurrence.idempotency_key
   and candle_revision.canonical_observation_sha256 =
       candle_occurrence.canonical_observation_sha256
   and pg_catalog.pg_visible_in_snapshot(
         (candle_revision.xmin::text)::pg_catalog.xid8,
         p_snapshot
       )
  left join private.pit_calendar_observation_occurrences as calendar_occurrence
    on calendar_occurrence.id = timing.calendar_occurrence_id
   and calendar_occurrence.content_revision_id = timing.calendar_revision_id
   and pg_catalog.pg_visible_in_snapshot(
         (calendar_occurrence.xmin::text)::pg_catalog.xid8,
         p_snapshot
       )
  left join private.pit_calendar_content_revisions as calendar_revision
    on calendar_revision.id = calendar_occurrence.content_revision_id
   and calendar_revision.calendar_idempotency_key =
       calendar_occurrence.calendar_idempotency_key
   and calendar_revision.canonical_evidence_sha256 =
       calendar_occurrence.canonical_evidence_sha256
   and pg_catalog.pg_visible_in_snapshot(
         (calendar_revision.xmin::text)::pg_catalog.xid8,
         p_snapshot
       )
  where timing.timing_payload->>'provider' = p_provider
    and timing.timing_payload->>'market' = p_market
    and timing.timing_payload->>'symbol' = p_symbol
    and timing.timing_payload->>'interval' = p_interval
    and timing.timing_payload->>'adjusted' = p_adjusted::text
    and timing.timing_payload->>'session_date' >=
        pg_catalog.to_char(p_start_session_date, 'YYYY-MM-DD')
    and timing.timing_payload->>'session_date' <=
        pg_catalog.to_char(p_end_session_date, 'YYYY-MM-DD')
    and timing.evidence_available_at <= p_as_of
    and pg_catalog.pg_visible_in_snapshot(
          (timing.xmin::text)::pg_catalog.xid8,
          p_snapshot
        );
$$;

create or replace function private.list_pit_daily_candles_as_of_v1_impl(
  p_provider text,
  p_market text,
  p_symbol text,
  p_interval text,
  p_adjusted boolean,
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
  contract_version constant text := 'pit_daily_candle_as_of_reader.v1';
  cursor_version constant text := 'pit_daily_candle_as_of_cursor.v1';
  cursor_ttl constant interval := interval '15 minutes';
  query_sha256_value text;
  snapshot_token_value text;
  snapshot_value pg_catalog.pg_snapshot;
  snapshot_issued_at_value timestamptz;
  snapshot_manifest_sha256_value text;
  candidate_count_value bigint;
  candidate record;
  items_value jsonb := '[]'::jsonb;
  next_cursor_value jsonb;
  page_count integer;
  last_session_date_value text;
  last_candle_observed_at_value timestamptz;
  last_evidence_available_at_value timestamptz;
  last_timing_revision_value bigint;
  last_timing_revision_id_value uuid;
  candle_provider_event_at_value timestamptz;
  candle_observed_at_value timestamptz;
  candle_open_value numeric;
  candle_high_value numeric;
  candle_low_value numeric;
  candle_close_value numeric;
  candle_volume_value numeric;
  calendar_session_date_value date;
  calendar_regular_start_value timestamptz;
  calendar_regular_end_value timestamptz;
  calendar_next_date_value date;
  calendar_next_start_value timestamptz;
  calendar_next_end_value timestamptz;
  calendar_observed_at_value timestamptz;
  timing_session_date_value date;
  timing_candle_provider_event_at_value timestamptz;
  timing_regular_start_value timestamptz;
  timing_regular_end_value timestamptz;
  timing_next_date_value date;
  timing_cutoff_value timestamptz;
  timing_candle_observed_at_value timestamptz;
  timing_calendar_observed_at_value timestamptz;
  timing_available_at_value timestamptz;
begin
  perform private.require_service_role();

  if p_provider is null
     or p_provider !~ '^[a-z][a-z0-9._-]{0,63}$'
     or p_market is distinct from 'KR'
     or p_symbol is null
     or p_symbol !~ '^[0-9]{6}$'
     or p_interval is distinct from '1d'
     or p_adjusted is null
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
    raise exception 'pit_daily_candle_as_of_reader_argument_invalid'
      using errcode = '22023';
  end if;

  query_sha256_value := private.pit_sha256_text_v1(
    '{"adjusted":' || p_adjusted::text ||
    ',"as_of":"' || private.pit_canonical_timestamp_v1(p_as_of) || '"' ||
    ',"contract_version":"' || contract_version || '"' ||
    ',"end_session_date":"' ||
      pg_catalog.to_char(p_end_session_date, 'YYYY-MM-DD') || '"' ||
    ',"interval":"' || p_interval || '"' ||
    ',"limit":' || p_limit::text ||
    ',"market":"' || p_market || '"' ||
    ',"provider":"' || p_provider || '"' ||
    ',"start_session_date":"' ||
      pg_catalog.to_char(p_start_session_date, 'YYYY-MM-DD') || '"' ||
    ',"symbol":"' || p_symbol || '"}'
  );

  if p_cursor is null then
    snapshot_token_value := pg_catalog.pg_current_snapshot()::text;
    snapshot_issued_at_value := pg_catalog.statement_timestamp();
    snapshot_value := snapshot_token_value::pg_catalog.pg_snapshot;
  else
    if private.jsonb_exact_keys_v1(
         p_cursor,
         array[
           'last_candle_observed_at', 'last_evidence_available_at',
           'last_session_date', 'last_timing_revision',
           'last_timing_revision_id', 'query_sha256', 'schema_version',
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
       or pg_catalog.jsonb_typeof(p_cursor->'last_candle_observed_at') <>
          'string'
       or pg_catalog.jsonb_typeof(p_cursor->'last_evidence_available_at') <>
          'string'
       or pg_catalog.jsonb_typeof(p_cursor->'last_timing_revision') <> 'number'
       or (p_cursor->>'last_timing_revision') !~ '^[1-9][0-9]*$'
       or pg_catalog.jsonb_typeof(p_cursor->'last_timing_revision_id') <>
          'string' then
      raise exception 'pit_daily_candle_as_of_reader_cursor_invalid'
        using errcode = '22023';
    end if;

    begin
      snapshot_token_value := p_cursor->>'snapshot_token';
      snapshot_value := snapshot_token_value::pg_catalog.pg_snapshot;
      snapshot_issued_at_value :=
        (p_cursor->>'snapshot_issued_at')::timestamptz;
      last_session_date_value := p_cursor->>'last_session_date';
      perform last_session_date_value::date;
      last_candle_observed_at_value :=
        (p_cursor->>'last_candle_observed_at')::timestamptz;
      last_evidence_available_at_value :=
        (p_cursor->>'last_evidence_available_at')::timestamptz;
      last_timing_revision_value :=
        (p_cursor->>'last_timing_revision')::bigint;
      last_timing_revision_id_value :=
        (p_cursor->>'last_timing_revision_id')::uuid;
    exception
      when others then
        raise exception 'pit_daily_candle_as_of_reader_cursor_invalid'
          using errcode = '22023';
    end;

    if private.pit_canonical_timestamp_v1(snapshot_issued_at_value) <>
         p_cursor->>'snapshot_issued_at'
       or private.pit_canonical_timestamp_v1(last_candle_observed_at_value) <>
          p_cursor->>'last_candle_observed_at'
       or private.pit_canonical_timestamp_v1(last_evidence_available_at_value) <>
          p_cursor->>'last_evidence_available_at'
       or snapshot_value::text <> snapshot_token_value
       or pg_catalog.to_char(
            last_session_date_value::date,
            'YYYY-MM-DD'
          ) <> last_session_date_value
       or last_timing_revision_id_value::text <>
          p_cursor->>'last_timing_revision_id'
       or snapshot_issued_at_value > pg_catalog.statement_timestamp()
       or snapshot_issued_at_value <
          pg_catalog.statement_timestamp() - cursor_ttl
       or last_session_date_value <
          pg_catalog.to_char(p_start_session_date, 'YYYY-MM-DD')
       or last_session_date_value >
          pg_catalog.to_char(p_end_session_date, 'YYYY-MM-DD') then
      raise exception 'pit_daily_candle_as_of_reader_cursor_invalid'
        using errcode = '22023';
    end if;
  end if;

  select count(*) into candidate_count_value
  from private.pit_daily_candle_as_of_candidates_v1(
    p_provider, p_market, p_symbol, p_interval, p_adjusted,
    p_start_session_date, p_end_session_date, p_as_of, snapshot_value
  );

  if candidate_count_value > 1000 then
    raise exception 'pit_daily_candle_as_of_reader_candidate_limit_exceeded'
      using errcode = '54000';
  end if;

  -- Revalidate every eligible immutable row before any result is constructed.
  -- A malformed or ambiguously bound row fails the whole request; it is never
  -- skipped by a join or hidden by a UUID/revision tie-breaker.
  begin
    for candidate in
      select *
      from private.pit_daily_candle_as_of_candidates_v1(
        p_provider, p_market, p_symbol, p_interval, p_adjusted,
        p_start_session_date, p_end_session_date, p_as_of, snapshot_value
      )
    loop
      if candidate.timing_revision_id is null
         or candidate.timing_revision is null
         or candidate.timing_revision <= 0
         or candidate.timing_idempotency_key !~ '^[0-9a-f]{64}$'
         or candidate.timing_canonical_evidence_sha256 !~ '^[0-9a-f]{64}$'
         or candidate.candle_revision_id is null
         or candidate.candle_revision is null
         or candidate.candle_revision <= 0
         or candidate.candle_occurrence_id is null
         or candidate.candle_canonical_observation_sha256 !~ '^[0-9a-f]{64}$'
         or candidate.calendar_revision_id is null
         or candidate.calendar_revision is null
         or candidate.calendar_revision <= 0
         or candidate.calendar_occurrence_id is null
         or candidate.calendar_canonical_evidence_sha256 !~ '^[0-9a-f]{64}$'
         or candidate.candidate_lineage_sha256 !~ '^[0-9a-f]{64}$'
         or candidate.candle_occurrence_origin not in (
           'content_revision_backfill', 'stream_head_recovery', 'rpc'
         )
         or candidate.calendar_occurrence_origin not in (
           'content_revision_backfill', 'stream_head_recovery', 'rpc'
         )
         or not pg_catalog.isfinite(candidate.timing_evidence_available_at)
         or not pg_catalog.isfinite(candidate.timing_received_at)
         or not pg_catalog.isfinite(candidate.candle_revision_observed_at)
         or not pg_catalog.isfinite(candidate.candle_revision_received_at)
         or not pg_catalog.isfinite(candidate.candle_occurrence_observed_at)
         or not pg_catalog.isfinite(candidate.candle_occurrence_received_at)
         or not pg_catalog.isfinite(candidate.calendar_revision_observed_at)
         or not pg_catalog.isfinite(candidate.calendar_revision_received_at)
         or not pg_catalog.isfinite(candidate.calendar_occurrence_observed_at)
         or not pg_catalog.isfinite(candidate.calendar_occurrence_received_at)
         or pg_catalog.octet_length(candidate.candle_payload::text) > 4096
         or pg_catalog.octet_length(candidate.calendar_payload::text) > 8192
         or pg_catalog.octet_length(candidate.timing_payload::text) > 12288
         or private.jsonb_exact_keys_v1(
           candidate.candle_payload,
           array[
             'adjusted', 'canonical_observation_sha256', 'close_krw',
             'currency', 'high_krw', 'interval', 'low_krw', 'market',
             'observed_at', 'open_krw', 'provider',
             'provider_contract_sha256', 'provider_event_at',
             'schema_version', 'symbol', 'volume'
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
         ) is distinct from true
         or private.jsonb_exact_keys_v1(
           candidate.timing_payload,
           array[
             'adjusted', 'calendar_canonical_evidence_sha256',
             'calendar_idempotency_key', 'calendar_observed_at',
             'calendar_provider_contract_sha256',
             'candle_canonical_observation_sha256',
             'candle_idempotency_key', 'candle_observed_at',
             'candle_provider_contract_sha256',
             'candle_provider_event_at',
             'canonical_timing_evidence_sha256', 'check_kind', 'cutoff_at',
             'evidence_available_at', 'interval', 'market',
             'next_business_date', 'provider', 'regular_end_at',
             'regular_start_at', 'schema_version', 'session_date', 'symbol'
           ]
         ) is distinct from true then
        raise exception 'candidate_shape_invalid';
      end if;

      if candidate.candle_content_payload - 'observed_at' <>
           candidate.candle_payload - 'observed_at'
         or candidate.calendar_content_payload - 'observed_at' <>
            candidate.calendar_payload - 'observed_at'
         or candidate.candle_content_payload->>'observed_at' <>
            private.pit_canonical_timestamp_v1(
              candidate.candle_revision_observed_at
            )
         or candidate.calendar_content_payload->>'observed_at' <>
            private.pit_canonical_timestamp_v1(
              candidate.calendar_revision_observed_at
            )
         or candidate.candle_payload->>'observed_at' <>
            private.pit_canonical_timestamp_v1(
              candidate.candle_occurrence_observed_at
            )
         or candidate.calendar_payload->>'observed_at' <>
            private.pit_canonical_timestamp_v1(
              candidate.calendar_occurrence_observed_at
            ) then
        raise exception 'candidate_occurrence_content_mismatch';
      end if;

      if pg_catalog.jsonb_typeof(candidate.candle_payload->'schema_version') <>
           'number'
         or candidate.candle_payload->>'schema_version' <> '1'
         or pg_catalog.jsonb_typeof(candidate.candle_payload->'provider') <>
            'string'
         or pg_catalog.jsonb_typeof(candidate.candle_payload->'symbol') <>
            'string'
         or pg_catalog.jsonb_typeof(candidate.candle_payload->'market') <>
            'string'
         or pg_catalog.jsonb_typeof(candidate.candle_payload->'interval') <>
            'string'
         or pg_catalog.jsonb_typeof(candidate.candle_payload->'adjusted') <>
            'boolean'
         or pg_catalog.jsonb_typeof(
              candidate.candle_payload->'provider_event_at'
            ) <> 'string'
         or pg_catalog.jsonb_typeof(candidate.candle_payload->'observed_at') <>
            'string'
         or candidate.candle_payload->>'currency' <> 'KRW'
         or candidate.candle_payload->>'provider' <> p_provider
         or candidate.candle_payload->>'symbol' <> p_symbol
         or candidate.candle_payload->>'market' <> p_market
         or candidate.candle_payload->>'interval' <> p_interval
         or candidate.candle_payload->>'adjusted' <> p_adjusted::text
         or (candidate.candle_payload->>'provider_contract_sha256') !~
            '^[0-9a-f]{64}$'
         or (candidate.candle_payload->>'canonical_observation_sha256') !~
            '^[0-9a-f]{64}$'
         or pg_catalog.jsonb_typeof(candidate.candle_payload->'open_krw') <>
            'number'
         or (candidate.candle_payload->>'open_krw') !~ '^[1-9][0-9]*$'
         or pg_catalog.jsonb_typeof(candidate.candle_payload->'high_krw') <>
            'number'
         or (candidate.candle_payload->>'high_krw') !~ '^[1-9][0-9]*$'
         or pg_catalog.jsonb_typeof(candidate.candle_payload->'low_krw') <>
            'number'
         or (candidate.candle_payload->>'low_krw') !~ '^[1-9][0-9]*$'
         or pg_catalog.jsonb_typeof(candidate.candle_payload->'close_krw') <>
            'number'
         or (candidate.candle_payload->>'close_krw') !~ '^[1-9][0-9]*$'
         or pg_catalog.jsonb_typeof(candidate.candle_payload->'volume') <>
            'number'
         or (candidate.candle_payload->>'volume') !~ '^(0|[1-9][0-9]*)$'
         or candidate.candle_payload->>'canonical_observation_sha256' <>
            candidate.candle_canonical_observation_sha256 then
        raise exception 'candidate_candle_invalid';
      end if;

      candle_provider_event_at_value :=
        (candidate.candle_payload->>'provider_event_at')::timestamptz;
      candle_observed_at_value :=
        (candidate.candle_payload->>'observed_at')::timestamptz;
      candle_open_value := (candidate.candle_payload->>'open_krw')::numeric;
      candle_high_value := (candidate.candle_payload->>'high_krw')::numeric;
      candle_low_value := (candidate.candle_payload->>'low_krw')::numeric;
      candle_close_value := (candidate.candle_payload->>'close_krw')::numeric;
      candle_volume_value := (candidate.candle_payload->>'volume')::numeric;

      if private.pit_canonical_timestamp_v1(candle_provider_event_at_value) <>
           candidate.candle_payload->>'provider_event_at'
         or private.pit_canonical_timestamp_v1(candle_observed_at_value) <>
            candidate.candle_payload->>'observed_at'
         or candle_observed_at_value < candle_provider_event_at_value
         or candle_high_value <
            greatest(candle_open_value, candle_close_value)
         or candle_low_value >
            least(candle_open_value, candle_close_value)
         or private.pit_candle_identity_sha256_v1(
              p_provider, p_symbol, p_market, p_interval, p_adjusted,
              candle_provider_event_at_value
            ) <> candidate.timing_payload->>'candle_idempotency_key'
         or private.pit_candle_observation_sha256_v1(
              p_provider, p_symbol, p_market, p_interval, p_adjusted,
              candle_provider_event_at_value,
              candidate.candle_payload->>'currency',
              candle_open_value, candle_high_value, candle_low_value,
              candle_close_value, candle_volume_value,
              candidate.candle_payload->>'provider_contract_sha256'
            ) <> candidate.candle_canonical_observation_sha256 then
        raise exception 'candidate_candle_integrity_invalid';
      end if;

      if pg_catalog.jsonb_typeof(candidate.calendar_payload->'schema_version')
           <> 'number'
         or candidate.calendar_payload->>'schema_version' <> '1'
         or candidate.calendar_payload->>'provider' <> p_provider
         or candidate.calendar_payload->>'market' <> p_market
         or candidate.calendar_payload->>'session_date' <> candidate.session_date
         or candidate.calendar_payload->>'is_open' <> 'true'
         or pg_catalog.jsonb_typeof(
              candidate.calendar_payload->'regular_start_at'
            ) <> 'string'
         or pg_catalog.jsonb_typeof(
              candidate.calendar_payload->'regular_end_at'
            ) <> 'string'
         or (candidate.calendar_payload->>'provider_contract_sha256') !~
            '^[0-9a-f]{64}$'
         or candidate.calendar_payload->>'canonical_evidence_sha256' <>
            candidate.calendar_canonical_evidence_sha256 then
        raise exception 'candidate_calendar_invalid';
      end if;

      calendar_session_date_value :=
        (candidate.calendar_payload->>'session_date')::date;
      calendar_regular_start_value :=
        (candidate.calendar_payload->>'regular_start_at')::timestamptz;
      calendar_regular_end_value :=
        (candidate.calendar_payload->>'regular_end_at')::timestamptz;
      calendar_next_date_value :=
        (candidate.calendar_payload->>'next_business_date')::date;
      calendar_next_start_value :=
        (candidate.calendar_payload->>'next_regular_start_at')::timestamptz;
      calendar_next_end_value :=
        (candidate.calendar_payload->>'next_regular_end_at')::timestamptz;
      calendar_observed_at_value :=
        (candidate.calendar_payload->>'observed_at')::timestamptz;

      if pg_catalog.to_char(calendar_session_date_value, 'YYYY-MM-DD') <>
           candidate.session_date
         or private.pit_canonical_timestamp_v1(calendar_regular_start_value) <>
            candidate.calendar_payload->>'regular_start_at'
         or private.pit_canonical_timestamp_v1(calendar_regular_end_value) <>
            candidate.calendar_payload->>'regular_end_at'
         or private.pit_canonical_timestamp_v1(calendar_next_start_value) <>
            candidate.calendar_payload->>'next_regular_start_at'
         or private.pit_canonical_timestamp_v1(calendar_next_end_value) <>
            candidate.calendar_payload->>'next_regular_end_at'
         or private.pit_canonical_timestamp_v1(calendar_observed_at_value) <>
            candidate.calendar_payload->>'observed_at'
         or calendar_next_date_value <= calendar_session_date_value
         or (calendar_regular_start_value at time zone 'Asia/Seoul')::date <>
            calendar_session_date_value
         or (calendar_regular_end_value at time zone 'Asia/Seoul')::date <>
            calendar_session_date_value
         or (calendar_next_start_value at time zone 'Asia/Seoul')::date <>
            calendar_next_date_value
         or (calendar_next_end_value at time zone 'Asia/Seoul')::date <>
            calendar_next_date_value
         or calendar_regular_start_value >= calendar_regular_end_value
         or calendar_next_start_value >= calendar_next_end_value
         or private.pit_calendar_identity_sha256_v1(
              p_provider, p_market, calendar_session_date_value
            ) <> candidate.timing_payload->>'calendar_idempotency_key'
         or private.pit_calendar_canonical_evidence_sha256_v1(
              p_provider, p_market, calendar_session_date_value, true,
              calendar_regular_start_value, calendar_regular_end_value,
              calendar_next_date_value, calendar_next_start_value,
              calendar_next_end_value,
              candidate.calendar_payload->>'provider_contract_sha256'
            ) <> candidate.calendar_canonical_evidence_sha256 then
        raise exception 'candidate_calendar_integrity_invalid';
      end if;

      if candidate.timing_payload->>'schema_version' <> '1'
         or candidate.timing_payload->>'check_kind' <>
            'kr_daily_candle_observed_at_not_before_next_regular_start'
         or candidate.timing_payload->>'provider' <> p_provider
         or candidate.timing_payload->>'market' <> p_market
         or candidate.timing_payload->>'symbol' <> p_symbol
         or candidate.timing_payload->>'interval' <> p_interval
         or candidate.timing_payload->>'adjusted' <> p_adjusted::text
         or candidate.timing_payload->>'session_date' <> candidate.session_date
         or candidate.timing_payload->>'canonical_timing_evidence_sha256' <>
            candidate.timing_canonical_evidence_sha256 then
        raise exception 'candidate_timing_invalid';
      end if;

      timing_session_date_value :=
        (candidate.timing_payload->>'session_date')::date;
      timing_candle_provider_event_at_value :=
        (candidate.timing_payload->>'candle_provider_event_at')::timestamptz;
      timing_regular_start_value :=
        (candidate.timing_payload->>'regular_start_at')::timestamptz;
      timing_regular_end_value :=
        (candidate.timing_payload->>'regular_end_at')::timestamptz;
      timing_next_date_value :=
        (candidate.timing_payload->>'next_business_date')::date;
      timing_cutoff_value :=
        (candidate.timing_payload->>'cutoff_at')::timestamptz;
      timing_candle_observed_at_value :=
        (candidate.timing_payload->>'candle_observed_at')::timestamptz;
      timing_calendar_observed_at_value :=
        (candidate.timing_payload->>'calendar_observed_at')::timestamptz;
      timing_available_at_value :=
        (candidate.timing_payload->>'evidence_available_at')::timestamptz;

      if timing_session_date_value <> calendar_session_date_value
         or timing_candle_provider_event_at_value <> candle_provider_event_at_value
         or timing_regular_start_value <> calendar_regular_start_value
         or timing_regular_end_value <> calendar_regular_end_value
         or timing_next_date_value <> calendar_next_date_value
         or timing_cutoff_value <> calendar_next_start_value
         or timing_candle_observed_at_value <>
            candidate.candle_occurrence_observed_at
         or timing_calendar_observed_at_value <>
            candidate.calendar_occurrence_observed_at
         or timing_available_at_value <>
            greatest(
              candidate.candle_occurrence_observed_at,
              candidate.calendar_occurrence_observed_at
            )
         or timing_available_at_value <>
            candidate.timing_evidence_available_at
         or timing_candle_observed_at_value < timing_cutoff_value
         or timing_calendar_observed_at_value < timing_cutoff_value
         or candidate.timing_payload->>'candle_provider_contract_sha256' <>
            candidate.candle_payload->>'provider_contract_sha256'
         or candidate.timing_payload->>'calendar_provider_contract_sha256' <>
            candidate.calendar_payload->>'provider_contract_sha256'
         or candidate.timing_payload->>'candle_canonical_observation_sha256' <>
            candidate.candle_canonical_observation_sha256
         or candidate.timing_payload->>'calendar_canonical_evidence_sha256' <>
            candidate.calendar_canonical_evidence_sha256
         or private.pit_daily_candle_timing_identity_sha256_v1(
              candidate.timing_payload->>'candle_idempotency_key',
              candidate.timing_payload->>'calendar_idempotency_key'
            ) <> candidate.timing_idempotency_key
         or private.pit_daily_candle_timing_evidence_sha256_v1(
              p_provider, p_market, p_symbol, p_interval, p_adjusted,
              timing_session_date_value, timing_candle_provider_event_at_value,
              timing_regular_start_value, timing_regular_end_value,
              timing_next_date_value, timing_cutoff_value,
              timing_candle_observed_at_value,
              timing_calendar_observed_at_value, timing_available_at_value,
              candidate.timing_payload->>'candle_idempotency_key',
              candidate.timing_payload->>'calendar_idempotency_key',
              candidate.timing_payload->>'candle_provider_contract_sha256',
              candidate.timing_payload->>'calendar_provider_contract_sha256',
              candidate.timing_payload->>'candle_canonical_observation_sha256',
              candidate.timing_payload->>'calendar_canonical_evidence_sha256'
            ) <> candidate.timing_canonical_evidence_sha256 then
        raise exception 'candidate_timing_integrity_invalid';
      end if;
    end loop;
  exception
    when others then
      raise exception 'pit_daily_candle_as_of_reader_integrity_violation'
        using errcode = '55000';
  end;

  -- Only timeline ambiguity can poison a canonical series. Request-level
  -- protocol errors are not promoted into a durable data ambiguity.
  if exists (
    select 1
    from private.pit_candle_observation_quarantine as quarantine
    where quarantine.reason_code in (
        'candle_observation_store_observation_time_regressed',
        'candle_observation_store_historical_hash_recurrence_ambiguous',
        'candle_observation_store_revision_time_not_increasing'
      )
      and quarantine.candidate_observed_at <= p_as_of
      and pg_catalog.pg_visible_in_snapshot(
            (quarantine.xmin::text)::pg_catalog.xid8,
            snapshot_value
          )
      and exists (
        select 1
        from private.pit_daily_candle_as_of_candidates_v1(
          p_provider, p_market, p_symbol, p_interval, p_adjusted,
          p_start_session_date, p_end_session_date, p_as_of, snapshot_value
        ) as candidate_row
        where candidate_row.timing_payload->>'candle_idempotency_key' =
              quarantine.idempotency_key
      )
  ) or exists (
    select 1
    from private.pit_calendar_observation_quarantine as quarantine
    where quarantine.reason_code in (
        'pit_calendar_observation_time_regressed',
        'pit_calendar_historical_hash_recurrence_ambiguous',
        'pit_calendar_revision_time_not_increasing'
      )
      and quarantine.candidate_observed_at <= p_as_of
      and pg_catalog.pg_visible_in_snapshot(
            (quarantine.xmin::text)::pg_catalog.xid8,
            snapshot_value
          )
      and exists (
        select 1
        from private.pit_daily_candle_as_of_candidates_v1(
          p_provider, p_market, p_symbol, p_interval, p_adjusted,
          p_start_session_date, p_end_session_date, p_as_of, snapshot_value
        ) as candidate_row
        where candidate_row.timing_payload->>'calendar_idempotency_key' =
              quarantine.calendar_idempotency_key
      )
  ) or exists (
    select 1
    from private.pit_daily_candle_timing_quarantine as quarantine
    where quarantine.reason_code in (
        'pit_timing_observation_time_regressed',
        'pit_timing_historical_hash_recurrence_ambiguous',
        'pit_timing_revision_time_not_increasing'
      )
      and quarantine.last_seen_evidence_available_at <= p_as_of
      and pg_catalog.pg_visible_in_snapshot(
            (quarantine.xmin::text)::pg_catalog.xid8,
            snapshot_value
          )
      and exists (
        select 1
        from private.pit_daily_candle_as_of_candidates_v1(
          p_provider, p_market, p_symbol, p_interval, p_adjusted,
          p_start_session_date, p_end_session_date, p_as_of, snapshot_value
        ) as candidate_row
        where candidate_row.timing_idempotency_key =
              quarantine.timing_idempotency_key
      )
  ) then
    raise exception 'pit_daily_candle_as_of_reader_timeline_ambiguous'
      using errcode = '55000';
  end if;

  select private.pit_sha256_text_v1(
           coalesce(
             pg_catalog.string_agg(
               candidates.candidate_lineage_sha256,
               E'\n' order by
                 candidates.session_date,
                 candidates.candle_occurrence_observed_at,
                 candidates.timing_evidence_available_at,
                 candidates.timing_revision,
                 candidates.timing_revision_id
             ),
             ''
           )
         )
  into snapshot_manifest_sha256_value
  from private.pit_daily_candle_as_of_candidates_v1(
    p_provider, p_market, p_symbol, p_interval, p_adjusted,
    p_start_session_date, p_end_session_date, p_as_of, snapshot_value
  ) as candidates;

  if p_cursor is not null and
     p_cursor->>'snapshot_manifest_sha256' <>
       snapshot_manifest_sha256_value then
    raise exception 'pit_daily_candle_as_of_reader_snapshot_mismatch'
      using errcode = '40001';
  end if;

  if p_cursor is not null and not exists (
    select 1
    from private.pit_daily_candle_as_of_candidates_v1(
      p_provider, p_market, p_symbol, p_interval, p_adjusted,
      p_start_session_date, p_end_session_date, p_as_of, snapshot_value
    ) as cursor_candidate
    where cursor_candidate.session_date = last_session_date_value
      and cursor_candidate.candle_occurrence_observed_at =
          last_candle_observed_at_value
      and cursor_candidate.timing_evidence_available_at =
          last_evidence_available_at_value
      and cursor_candidate.timing_revision = last_timing_revision_value
      and cursor_candidate.timing_revision_id = last_timing_revision_id_value
  ) then
    raise exception 'pit_daily_candle_as_of_reader_cursor_invalid'
      using errcode = '22023';
  end if;

  with ordered as (
    select candidates.*
    from private.pit_daily_candle_as_of_candidates_v1(
      p_provider, p_market, p_symbol, p_interval, p_adjusted,
      p_start_session_date, p_end_session_date, p_as_of, snapshot_value
    ) as candidates
    where p_cursor is null
       or (
         candidates.session_date,
         candidates.candle_occurrence_observed_at,
         candidates.timing_evidence_available_at,
         candidates.timing_revision,
         candidates.timing_revision_id
       ) > (
         last_session_date_value,
         last_candle_observed_at_value,
         last_evidence_available_at_value,
         last_timing_revision_value,
         last_timing_revision_id_value
       )
    order by
      candidates.session_date,
      candidates.candle_occurrence_observed_at,
      candidates.timing_evidence_available_at,
      candidates.timing_revision,
      candidates.timing_revision_id
    limit p_limit + 1
  ), page as (
    select *
    from ordered
    order by
      session_date,
      candle_occurrence_observed_at,
      timing_evidence_available_at,
      timing_revision,
      timing_revision_id
    limit p_limit
  )
  select
    coalesce(
      pg_catalog.jsonb_agg(
        pg_catalog.jsonb_build_object(
          'timing_revision_id', page.timing_revision_id::text,
          'timing_idempotency_key', page.timing_idempotency_key,
          'timing_revision', page.timing_revision,
          'timing_canonical_evidence_sha256',
            page.timing_canonical_evidence_sha256,
          'timing_evidence_available_at',
            private.pit_canonical_timestamp_v1(
              page.timing_evidence_available_at
            ),
          'timing_received_at',
            private.pit_canonical_timestamp_v1(page.timing_received_at),
          'timing_payload', page.timing_payload,
          'candle_revision_id', page.candle_revision_id::text,
          'candle_revision', page.candle_revision,
          'candle_canonical_observation_sha256',
            page.candle_canonical_observation_sha256,
          'candle_revision_received_at',
            private.pit_canonical_timestamp_v1(
              page.candle_revision_received_at
            ),
          'candle_occurrence_id', page.candle_occurrence_id::text,
          'candle_occurrence_observed_at',
            private.pit_canonical_timestamp_v1(
              page.candle_occurrence_observed_at
            ),
          'candle_occurrence_received_at',
            private.pit_canonical_timestamp_v1(
              page.candle_occurrence_received_at
            ),
          'candle_occurrence_origin', page.candle_occurrence_origin,
          'candle_payload', page.candle_payload,
          'calendar_revision_id', page.calendar_revision_id::text,
          'calendar_revision', page.calendar_revision,
          'calendar_canonical_evidence_sha256',
            page.calendar_canonical_evidence_sha256,
          'calendar_revision_received_at',
            private.pit_canonical_timestamp_v1(
              page.calendar_revision_received_at
            ),
          'calendar_occurrence_id', page.calendar_occurrence_id::text,
          'calendar_occurrence_observed_at',
            private.pit_canonical_timestamp_v1(
              page.calendar_occurrence_observed_at
            ),
          'calendar_occurrence_received_at',
            private.pit_canonical_timestamp_v1(
              page.calendar_occurrence_received_at
            ),
          'calendar_occurrence_origin', page.calendar_occurrence_origin,
          'calendar_payload', page.calendar_payload,
          'candidate_lineage_sha256', page.candidate_lineage_sha256
        ) order by
          page.session_date,
          page.candle_occurrence_observed_at,
          page.timing_evidence_available_at,
          page.timing_revision,
          page.timing_revision_id
      ),
      '[]'::jsonb
    ),
    count(*)::integer
  into items_value, page_count
  from page;

  select pg_catalog.jsonb_build_object(
           'schema_version', cursor_version,
           'query_sha256', query_sha256_value,
           'snapshot_token', snapshot_token_value,
           'snapshot_issued_at',
             private.pit_canonical_timestamp_v1(snapshot_issued_at_value),
           'snapshot_manifest_sha256', snapshot_manifest_sha256_value,
           'last_session_date', tail.session_date,
           'last_candle_observed_at',
             private.pit_canonical_timestamp_v1(
               tail.candle_occurrence_observed_at
             ),
           'last_evidence_available_at',
             private.pit_canonical_timestamp_v1(
               tail.timing_evidence_available_at
             ),
           'last_timing_revision', tail.timing_revision,
           'last_timing_revision_id', tail.timing_revision_id::text
         )
  into next_cursor_value
  from (
    select candidates.*
    from private.pit_daily_candle_as_of_candidates_v1(
      p_provider, p_market, p_symbol, p_interval, p_adjusted,
      p_start_session_date, p_end_session_date, p_as_of, snapshot_value
    ) as candidates
    where p_cursor is null
       or (
         candidates.session_date,
         candidates.candle_occurrence_observed_at,
         candidates.timing_evidence_available_at,
         candidates.timing_revision,
         candidates.timing_revision_id
       ) > (
         last_session_date_value,
         last_candle_observed_at_value,
         last_evidence_available_at_value,
         last_timing_revision_value,
         last_timing_revision_id_value
       )
    order by
      candidates.session_date,
      candidates.candle_occurrence_observed_at,
      candidates.timing_evidence_available_at,
      candidates.timing_revision,
      candidates.timing_revision_id
    offset p_limit - 1
    limit 1
  ) as tail
  where exists (
    select 1
    from private.pit_daily_candle_as_of_candidates_v1(
      p_provider, p_market, p_symbol, p_interval, p_adjusted,
      p_start_session_date, p_end_session_date, p_as_of, snapshot_value
    ) as remaining
    where (
      remaining.session_date,
      remaining.candle_occurrence_observed_at,
      remaining.timing_evidence_available_at,
      remaining.timing_revision,
      remaining.timing_revision_id
    ) > (
      tail.session_date,
      tail.candle_occurrence_observed_at,
      tail.timing_evidence_available_at,
      tail.timing_revision,
      tail.timing_revision_id
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

create or replace function worker_api.list_pit_daily_candles_as_of_v1(
  p_provider text,
  p_market text,
  p_symbol text,
  p_interval text,
  p_adjusted boolean,
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
  select private.list_pit_daily_candles_as_of_v1_impl(
    p_provider,
    p_market,
    p_symbol,
    p_interval,
    p_adjusted,
    p_start_session_date,
    p_end_session_date,
    p_as_of,
    p_limit,
    p_cursor
  );
$$;

revoke all on function
  private.pit_daily_candle_as_of_candidates_v1(
    text,text,text,text,boolean,date,date,timestamptz,pg_catalog.pg_snapshot
  ),
  private.list_pit_daily_candles_as_of_v1_impl(
    text,text,text,text,boolean,date,date,timestamptz,integer,jsonb
  ),
  worker_api.list_pit_daily_candles_as_of_v1(
    text,text,text,text,boolean,date,date,timestamptz,integer,jsonb
  )
from public, anon, authenticated, authenticator, service_role;

grant execute on function
  private.list_pit_daily_candles_as_of_v1_impl(
    text,text,text,text,boolean,date,date,timestamptz,integer,jsonb
  ),
  worker_api.list_pit_daily_candles_as_of_v1(
    text,text,text,text,boolean,date,date,timestamptz,integer,jsonb
  )
to service_role;

do $$
declare
  private_impl_count bigint;
  private_helper_count bigint;
  wrapper_count bigint;
  forbidden_acl_count bigint;
begin
  select count(*) into private_impl_count
  from pg_catalog.pg_proc as procedure
  where procedure.oid =
      'private.list_pit_daily_candles_as_of_v1_impl(text,text,text,text,boolean,date,date,timestamptz,integer,jsonb)'::regprocedure
    and procedure.prosecdef
    and procedure.provolatile = 's'
    and procedure.proconfig = array['search_path=""']::text[];

  select count(*) into private_helper_count
  from pg_catalog.pg_proc as procedure
  where procedure.oid =
      'private.pit_daily_candle_as_of_candidates_v1(text,text,text,text,boolean,date,date,timestamptz,pg_snapshot)'::regprocedure
    and procedure.prosecdef
    and procedure.provolatile = 's'
    and procedure.proconfig = array['search_path=""']::text[];

  select count(*) into wrapper_count
  from pg_catalog.pg_proc as procedure
  where procedure.oid =
      'worker_api.list_pit_daily_candles_as_of_v1(text,text,text,text,boolean,date,date,timestamptz,integer,jsonb)'::regprocedure
    and not procedure.prosecdef
    and procedure.provolatile = 's'
    and procedure.proconfig = array['search_path=""']::text[];

  select count(*) into forbidden_acl_count
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
      'private.pit_daily_candle_as_of_candidates_v1(text,text,text,text,boolean,date,date,timestamptz,pg_snapshot)'::regprocedure,
      'private.list_pit_daily_candles_as_of_v1_impl(text,text,text,text,boolean,date,date,timestamptz,integer,jsonb)'::regprocedure,
      'worker_api.list_pit_daily_candles_as_of_v1(text,text,text,text,boolean,date,date,timestamptz,integer,jsonb)'::regprocedure
    )
    and acl.privilege_type = 'EXECUTE'
    and (
      acl.grantee = 0
      or grantee.rolname in ('anon', 'authenticated', 'authenticator')
      or (
        procedure.oid =
          'private.pit_daily_candle_as_of_candidates_v1(text,text,text,text,boolean,date,date,timestamptz,pg_snapshot)'::regprocedure
        and grantee.rolname = 'service_role'
      )
    );

  if private_impl_count <> 1
     or private_helper_count <> 1
     or wrapper_count <> 1
     or forbidden_acl_count <> 0
     or not pg_catalog.has_function_privilege(
       'service_role',
       'private.list_pit_daily_candles_as_of_v1_impl(text,text,text,text,boolean,date,date,timestamptz,integer,jsonb)',
       'EXECUTE'
     )
     or not pg_catalog.has_function_privilege(
       'service_role',
       'worker_api.list_pit_daily_candles_as_of_v1(text,text,text,text,boolean,date,date,timestamptz,integer,jsonb)',
       'EXECUTE'
     )
     or pg_catalog.has_function_privilege(
       'service_role',
       'private.pit_daily_candle_as_of_candidates_v1(text,text,text,text,boolean,date,date,timestamptz,pg_snapshot)',
       'EXECUTE'
     ) then
    raise exception 'pit_daily_candle_as_of_reader_security_contract_failed'
      using errcode = '55000';
  end if;
end;
$$;

commit;
