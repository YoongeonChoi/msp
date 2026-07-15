-- Aggregate the Paper v1 one-percent bar participation cap across every
-- semantic intent for the same account, symbol, and completed minute bar.
--
-- The source projection excludes the currently claimed intent so deterministic
-- replay can rebuild its own prefix.  The fill trigger is the authoritative
-- commit-time guard and serializes competing inserts with an xact advisory lock.

alter function private.load_claimed_paper_execution_bundle_v1_impl(
  uuid, uuid, uuid, bigint, uuid, text, timestamptz
)
rename to load_claimed_paper_execution_bundle_without_participation_v1_impl;

revoke all on function
  private.load_claimed_paper_execution_bundle_without_participation_v1_impl(
    uuid, uuid, uuid, bigint, uuid, text, timestamptz
  )
from public, anon, authenticated, service_role;

create or replace function private.load_claimed_paper_execution_bundle_v1_impl(
  p_command_id uuid,
  p_intent_id uuid,
  p_claim_token uuid,
  p_expected_revision bigint,
  p_worker_id uuid,
  p_release_sha text,
  p_now timestamptz
)
returns table (bundle jsonb)
language plpgsql
security definer
set search_path = ''
as $$
declare
  raw_bundle jsonb;
  bars_with_participation jsonb;
begin
  select source.bundle into raw_bundle
  from private.load_claimed_paper_execution_bundle_without_participation_v1_impl(
    p_command_id,
    p_intent_id,
    p_claim_token,
    p_expected_revision,
    p_worker_id,
    p_release_sha,
    p_now
  ) as source;

  if raw_bundle is null then
    return;
  end if;
  if jsonb_typeof(raw_bundle #> '{command,bars}') <> 'array' then
    raise exception 'paper_execution_bundle_bars_invalid' using errcode = '23514';
  end if;

  select coalesce(
    jsonb_agg(
      item.value || jsonb_build_object(
        'other_intent_filled_quantity',
        coalesce((
          select sum(fill.quantity)
          from private.fills as fill
          join private.order_intents as other_intent
            on other_intent.id = fill.intent_id
          where fill.account_id = raw_bundle #>> '{command,intent,account_id}'
            and fill.broker = 'internal_paper'
            and other_intent.environment = 'paper'
            and other_intent.symbol = raw_bundle #>> '{command,intent,symbol}'
            and date_trunc('minute', fill.filled_at)
              = (item.value->>'completed_at')::timestamptz
            and fill.intent_id <> p_intent_id
        ), 0)
      )
      order by item.ordinality
    ),
    '[]'::jsonb
  ) into bars_with_participation
  from jsonb_array_elements(raw_bundle #> '{command,bars}')
    with ordinality as item(value, ordinality);

  bundle := jsonb_set(
    raw_bundle,
    '{command,bars}',
    bars_with_participation,
    false
  );
  return next;
end;
$$;

revoke all on function private.load_claimed_paper_execution_bundle_v1_impl(
  uuid, uuid, uuid, bigint, uuid, text, timestamptz
) from public, anon, authenticated, service_role;

grant execute on function private.load_claimed_paper_execution_bundle_v1_impl(
  uuid, uuid, uuid, bigint, uuid, text, timestamptz
) to service_role;

create or replace function worker_api.load_claimed_paper_execution_bundle_v1(
  p_command_id uuid,
  p_intent_id uuid,
  p_claim_token uuid,
  p_expected_revision bigint,
  p_worker_id uuid,
  p_release_sha text,
  p_now timestamptz
)
returns table (bundle jsonb)
language sql
security invoker
set search_path = ''
as $$
  select * from private.load_claimed_paper_execution_bundle_v1_impl(
    p_command_id,
    p_intent_id,
    p_claim_token,
    p_expected_revision,
    p_worker_id,
    p_release_sha,
    p_now
  );
$$;

revoke all on function worker_api.load_claimed_paper_execution_bundle_v1(
  uuid, uuid, uuid, bigint, uuid, text, timestamptz
) from public, anon, authenticated, service_role;

grant execute on function worker_api.load_claimed_paper_execution_bundle_v1(
  uuid, uuid, uuid, bigint, uuid, text, timestamptz
) to service_role;

create or replace function private.enforce_paper_bar_participation_guard()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
declare
  intent_symbol text;
  bar_minute timestamptz;
  bar_volume bigint;
  participation_capacity bigint;
  already_filled_quantity bigint;
begin
  if new.broker <> 'internal_paper' then
    return new;
  end if;

  select intent.symbol, bar.minute, bar.volume
  into intent_symbol, bar_minute, bar_volume
  from private.order_intents as intent
  join private.paper_execution_candidates as candidate
    on candidate.intent_id = intent.id
   and candidate.account_id = intent.account_id
  join private.paper_minute_bars as bar
    on bar.series_id = candidate.fixture_series_id
   and bar.completed_at = date_trunc('minute', new.filled_at)
  where intent.id = new.intent_id
    and intent.account_id = new.account_id
    and intent.environment = 'paper';

  if not found then
    raise exception 'paper_bar_participation_evidence_missing'
      using errcode = '23514';
  end if;

  perform pg_catalog.pg_advisory_xact_lock(
    pg_catalog.hashtextextended(
      new.account_id || '|' || intent_symbol || '|' ||
      private.utc_iso8601(bar_minute),
      61972411386421::bigint
    )
  );

  select coalesce(sum(fill.quantity), 0)
  into already_filled_quantity
  from private.fills as fill
  join private.order_intents as filled_intent
    on filled_intent.id = fill.intent_id
  where fill.account_id = new.account_id
    and fill.broker = 'internal_paper'
    and filled_intent.environment = 'paper'
    and filled_intent.symbol = intent_symbol
    and date_trunc('minute', fill.filled_at) = bar_minute + interval '1 minute';

  participation_capacity := bar_volume / 100;
  if already_filled_quantity + new.quantity > participation_capacity then
    raise exception 'paper_bar_participation_capacity_exceeded'
      using errcode = '23514';
  end if;
  return new;
end;
$$;

revoke all on function private.enforce_paper_bar_participation_guard()
from public, anon, authenticated, service_role;

create trigger enforce_paper_bar_participation
before insert on private.fills
for each row execute function private.enforce_paper_bar_participation_guard();

comment on function private.enforce_paper_bar_participation_guard() is
  'Serializes Paper fill commits and rejects aggregate account/symbol/bar participation above one percent.';

-- Preserve qualification v1 exactly for in-flight pre-upgrade retries.  The
-- journal-backed six-check contract is a distinct v2 suite and RPC.
create or replace function private.validate_qualification_run_v2(
  p_payload jsonb
)
returns void
language plpgsql
immutable
security definer
set search_path = ''
as $$
declare
  manifest jsonb := p_payload->'evidence_manifest';
  check_row jsonb;
  required_checks text[];
  actual_checks text[];
  all_pass boolean;
begin
  perform private.assert_exact_json_keys(p_payload, array[
    'schema_version', 'run_id', 'run_kind', 'account_id', 'environment',
    'reference_bundle_request_id', 'account_snapshot_id',
    'account_snapshot_sequence', 'ledger_checkpoint_sha256', 'release_sha',
    'suite_version', 'started_at', 'completed_at', 'result',
    'evidence_manifest', 'evidence_sha256', 'worker_id'
  ]);
  perform private.assert_exact_json_keys(manifest, array[
    'schema_version', 'checks'
  ]);
  if jsonb_typeof(p_payload->'schema_version') <> 'number'
     or jsonb_typeof(p_payload->'account_snapshot_sequence') <> 'number'
     or coalesce((p_payload->>'schema_version')::integer, -1) <> 1
     or jsonb_typeof(manifest->'schema_version') <> 'number'
     or coalesce((manifest->>'schema_version')::integer, -1) <> 1
     or jsonb_typeof(manifest->'checks') <> 'array'
     or p_payload->>'run_kind' not in ('g1', 'g2', 'contract_qualification')
     or p_payload->>'environment' not in ('paper', 'contract_test')
     or p_payload->>'result' not in ('pass', 'fail')
     or p_payload->>'ledger_checkpoint_sha256' !~ '^[0-9a-f]{64}$'
     or p_payload->>'release_sha' !~ '^[0-9a-f]{40}([0-9a-f]{24})?$'
     or p_payload->>'evidence_sha256' !~ '^[0-9a-f]{64}$'
     or nullif(btrim(p_payload->>'suite_version'), '') is null
     or (
       p_payload->>'run_kind' = 'contract_qualification'
       and p_payload->>'suite_version' <> 'contract-test-qualification-v2'
     ) then
    raise exception 'qualification_run_values_invalid' using errcode = '22023';
  end if;
  required_checks := case p_payload->>'run_kind'
    when 'g1' then array[
      'ledger_balance', 'no_fictitious_sell', 'observation_idempotency',
      'restart_recovery', 'semantic_dedupe'
    ]::text[]
    when 'g2' then array[
      'command_ack', 'fencing', 'maker_checker', 'outbox_delivery',
      'restore_drill'
    ]::text[]
    else array[
      'cancel_lifecycle', 'create_lifecycle', 'fault_injection',
      'ledger_invariants', 'production_order_network_zero',
      'status_partial_terminal'
    ]::text[]
  end;
  for check_row in select value from jsonb_array_elements(manifest->'checks') loop
    perform private.assert_exact_json_keys(check_row, array[
      'check_id', 'status', 'evidence_sha256', 'metrics'
    ]);
    if check_row->>'status' not in ('pass', 'fail')
       or check_row->>'evidence_sha256' !~ '^[0-9a-f]{64}$'
       or jsonb_typeof(check_row->'metrics') <> 'object' then
      raise exception 'qualification_run_check_invalid' using errcode = '22023';
    end if;
    if check_row->>'check_id' = 'production_order_network_zero' then
      perform private.assert_exact_json_keys(check_row->'metrics', array[
        'request_count'
      ]);
      if jsonb_typeof(check_row->'metrics'->'request_count') <> 'number'
         or (check_row->'metrics'->>'request_count')::integer <> 0 then
        raise exception 'production_order_network_zero_not_proven'
          using errcode = '23514';
      end if;
    end if;
    if check_row->>'check_id' = 'ledger_invariants'
       and check_row->>'status' = 'pass' then
      perform private.assert_exact_json_keys(check_row->'metrics', array[
        'balanced_transaction_count', 'position_quantity',
        'provider_identity_change_blocked', 'projection_backed_by_journal'
      ]);
      if jsonb_typeof(check_row->'metrics'->'balanced_transaction_count')
           <> 'number'
         or (check_row->'metrics'->>'balanced_transaction_count')::integer < 1
         or jsonb_typeof(check_row->'metrics'->'position_quantity') <> 'number'
         or (check_row->'metrics'->>'position_quantity')::bigint <= 0
         or check_row->'metrics'->'provider_identity_change_blocked'
           <> 'true'::jsonb
         or check_row->'metrics'->'projection_backed_by_journal'
           <> 'true'::jsonb then
        raise exception 'contract_qualification_ledger_invariants_not_proven'
          using errcode = '23514';
      end if;
    end if;
  end loop;
  select array_agg(value->>'check_id' order by value->>'check_id'),
         bool_and(value->>'status' = 'pass')
  into actual_checks, all_pass
  from jsonb_array_elements(manifest->'checks');
  if actual_checks is distinct from required_checks
     or (p_payload->>'result' = 'pass') is distinct from all_pass then
    raise exception 'qualification_run_check_set_invalid' using errcode = '23514';
  end if;
  if p_payload->>'run_kind' = 'contract_qualification'
     and p_payload->>'environment' <> 'contract_test' then
    raise exception 'contract_qualification_environment_invalid'
      using errcode = '23514';
  end if;
  if private.sha256_jsonb_v1(private.qualification_run_content_v1(p_payload))
       <> p_payload->>'evidence_sha256' then
    raise exception 'qualification_run_evidence_digest_mismatch'
      using errcode = '23514';
  end if;
  perform (p_payload->>'run_id')::uuid;
  perform (p_payload->>'reference_bundle_request_id')::uuid;
  perform (p_payload->>'account_snapshot_id')::uuid;
  perform (p_payload->>'worker_id')::uuid;
  if (p_payload->>'account_snapshot_sequence')::bigint <= 0
     or (p_payload->>'completed_at')::timestamptz
       < (p_payload->>'started_at')::timestamptz then
    raise exception 'qualification_run_window_invalid' using errcode = '22023';
  end if;
exception
  when invalid_text_representation or numeric_value_out_of_range then
    raise exception 'qualification_run_values_invalid' using errcode = '22023';
end;
$$;

revoke all on function private.validate_qualification_run_v2(jsonb)
from public, anon, authenticated, service_role;

comment on function private.validate_qualification_run_v2(jsonb) is
  'Requires the six-check journal/projection/provider-identity contract qualification v2 manifest.';

create or replace function private.register_qualification_run_v2_impl(
  p_run_payload jsonb
)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  run_id_value uuid;
  existing_row private.qualification_runs%rowtype;
  snapshot_row private.account_snapshots%rowtype;
  bundle_row private.reference_bundle_materializations%rowtype;
  request_row private.control_approval_requests%rowtype;
begin
  perform private.require_service_role();
  perform private.validate_qualification_run_v2(p_run_payload);
  run_id_value := (p_run_payload->>'run_id')::uuid;
  select * into existing_row
  from private.qualification_runs where id = run_id_value;
  if found then
    if existing_row.evidence_sha256 <> p_run_payload->>'evidence_sha256'
       or existing_row.evidence_manifest <> p_run_payload->'evidence_manifest' then
      raise exception 'qualification_run_idempotency_conflict'
        using errcode = '23505';
    end if;
    return jsonb_build_object(
      'schema_version', 1, 'run_id', existing_row.id,
      'run_kind', existing_row.run_kind, 'result', existing_row.result,
      'evidence_sha256', existing_row.evidence_sha256, 'inserted', false
    );
  end if;
  if not exists (
    select 1
    from private.worker_leases as lease
    where lease.account_id = p_run_payload->>'account_id'
      and lease.holder_id = p_run_payload->>'worker_id'
      and lease.release_sha = p_run_payload->>'release_sha'
      and lease.expires_at > clock_timestamp()
  ) then
    raise exception 'qualification_run_worker_lease_missing_or_stale'
      using errcode = '40001';
  end if;
  select * into bundle_row
  from private.reference_bundle_materializations
  where request_id = (p_run_payload->>'reference_bundle_request_id')::uuid;
  select * into request_row
  from private.control_approval_requests
  where id = (p_run_payload->>'reference_bundle_request_id')::uuid;
  if bundle_row.request_id is null or request_row.id is null
     or request_row.account_id <> p_run_payload->>'account_id'
     or request_row.environment <> p_run_payload->>'environment'
     or request_row.payload->>'release_sha' <> p_run_payload->>'release_sha' then
    raise exception 'qualification_run_reference_bundle_mismatch'
      using errcode = '23514';
  end if;
  select * into snapshot_row
  from private.account_snapshots
  where id = (p_run_payload->>'account_snapshot_id')::uuid;
  if not found
     or snapshot_row.account_id <> p_run_payload->>'account_id'
     or snapshot_row.environment <> p_run_payload->>'environment'
     or snapshot_row.checkpoint_schema_version <> 1
     or snapshot_row.sequence
       <> (p_run_payload->>'account_snapshot_sequence')::bigint
     or snapshot_row.ledger_checkpoint_sha256
       <> p_run_payload->>'ledger_checkpoint_sha256' then
    raise exception 'qualification_run_snapshot_mismatch'
      using errcode = '23514';
  end if;
  if exists (
    select 1 from private.execution_controls
    where account_id = p_run_payload->>'account_id'
      and execution_enabled is true
  ) then
    raise exception 'qualification_run_requires_disabled_execution'
      using errcode = '23514';
  end if;
  if (p_run_payload->>'completed_at')::timestamptz
       > clock_timestamp() + interval '30 seconds' then
    raise exception 'qualification_run_future_completion_invalid'
      using errcode = '22023';
  end if;
  insert into private.qualification_runs (
    id, run_kind, account_id, environment, reference_bundle_request_id,
    account_snapshot_id, account_snapshot_sequence,
    ledger_checkpoint_sha256, release_sha, suite_version, result,
    evidence_manifest, evidence_sha256, started_at, completed_at, worker_id
  ) values (
    run_id_value, p_run_payload->>'run_kind', p_run_payload->>'account_id',
    p_run_payload->>'environment',
    (p_run_payload->>'reference_bundle_request_id')::uuid,
    (p_run_payload->>'account_snapshot_id')::uuid,
    (p_run_payload->>'account_snapshot_sequence')::bigint,
    p_run_payload->>'ledger_checkpoint_sha256', p_run_payload->>'release_sha',
    p_run_payload->>'suite_version', p_run_payload->>'result',
    p_run_payload->'evidence_manifest', p_run_payload->>'evidence_sha256',
    (p_run_payload->>'started_at')::timestamptz,
    (p_run_payload->>'completed_at')::timestamptz,
    (p_run_payload->>'worker_id')::uuid
  );
  return jsonb_build_object(
    'schema_version', 1, 'run_id', run_id_value,
    'run_kind', p_run_payload->>'run_kind',
    'result', p_run_payload->>'result',
    'evidence_sha256', p_run_payload->>'evidence_sha256',
    'inserted', true
  );
end;
$$;

revoke all on function private.register_qualification_run_v2_impl(jsonb)
from public, anon, authenticated, service_role;
grant execute on function private.register_qualification_run_v2_impl(jsonb)
to service_role;

create or replace function worker_api.register_qualification_run_v2(
  run_payload jsonb
)
returns jsonb
language sql
security invoker
set search_path = ''
as $$
  select private.register_qualification_run_v2_impl(run_payload);
$$;

revoke all on function worker_api.register_qualification_run_v2(jsonb)
from public, anon, authenticated;
grant execute on function worker_api.register_qualification_run_v2(jsonb)
to service_role;
