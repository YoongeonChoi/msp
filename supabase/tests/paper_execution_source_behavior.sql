\set ON_ERROR_STOP on

-- This fixture is disposable and local-only.  It deliberately uses only the
-- internal Paper broker and immutable local minute-bar fixtures.

insert into auth.users (id, email) values
  ('11111111-1111-4111-8111-111111111111', 'paper-maker@example.invalid'),
  ('22222222-2222-4222-8222-222222222222', 'paper-checker@example.invalid');

insert into public.strategy_versions (
  id, version_name, status, strategy_type, weights, params,
  created_by, approved_by, approved_at, version
) values (
  '77777777-7777-4777-8777-777777777777',
  'paper-source-verifier-v1', 'paper', 'weighted_factor',
  '{"technical":0.2,"fundamental":0.2,"market_sector":0.2,"news_event":0.2,"portfolio":0.2}',
  '{"buy_threshold":0.7,"sell_threshold":0.3}',
  '11111111-1111-4111-8111-111111111111',
  '22222222-2222-4222-8222-222222222222', clock_timestamp(), 'v1'
);

update private.trading_accounts
set state = 'open', opened_at = clock_timestamp()
where account_id = 'paper-primary';

update private.cash_balance_projection
set settled_cash_krw = 10000000,
    reserved_cash_krw = 0,
    pending_debit_cash_krw = 0,
    projection_version = projection_version + 1,
    projected_at = clock_timestamp()
where account_id = 'paper-primary';

insert into private.control_evidence (
  id, evidence_type, environment, artifact_uri, artifact_sha256,
  captured_at, verified_at, verified_by, metadata_summary
) values
  (
    'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1', 'reference_bundle', 'paper',
    'urn:sha256:1111111111111111111111111111111111111111111111111111111111111111',
    repeat('1', 64), clock_timestamp() - interval '2 minutes',
    clock_timestamp() - interval '1 minute',
    '11111111-1111-4111-8111-111111111111', '{"local_fixture":true}'
  ),
  (
    'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa2', 'risk_review', 'paper',
    'urn:sha256:2222222222222222222222222222222222222222222222222222222222222222',
    repeat('2', 64), clock_timestamp() - interval '2 minutes',
    clock_timestamp() - interval '1 minute',
    '22222222-2222-4222-8222-222222222222', '{"local_fixture":true}'
  ),
  (
    'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa3', 'g1_qualification', 'paper',
    'urn:sha256:3333333333333333333333333333333333333333333333333333333333333333',
    repeat('3', 64), clock_timestamp() - interval '2 minutes',
    clock_timestamp() - interval '1 minute',
    '11111111-1111-4111-8111-111111111111', '{"local_fixture":true}'
  ),
  (
    'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa4', 'g2_qualification', 'paper',
    'urn:sha256:4444444444444444444444444444444444444444444444444444444444444444',
    repeat('4', 64), clock_timestamp() - interval '2 minutes',
    clock_timestamp() - interval '1 minute',
    '22222222-2222-4222-8222-222222222222', '{"local_fixture":true}'
  );

insert into private.market_calendars (
  id, environment, calendar_version, calendar_sha256, timezone_name,
  valid_from, valid_until, status, evidence_id, requested_by, reviewed_by
) values (
  'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb1', 'paper',
  'paper-source-calendar-v1', repeat('7', 64), 'Asia/Seoul',
  current_date - 1, current_date + 1, 'approved',
  'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1',
  '11111111-1111-4111-8111-111111111111',
  '22222222-2222-4222-8222-222222222222'
);

insert into private.market_calendar_sessions (
  calendar_id, session_date, is_open, session_sha256
) values
  ('bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb1', current_date - 1, true, repeat('6', 64)),
  ('bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb1', current_date, true, repeat('5', 64)),
  ('bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb1', current_date + 1, true, repeat('4', 64));

insert into private.paper_execution_model_registry (
  id, environment, model_version, tick_size_evidence_sha256,
  volume_model_evidence_sha256, corporate_action_evidence_sha256,
  market_calendar_id, status, evidence_id, requested_by, reviewed_by,
  effective_from, effective_until
) values (
  'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb2', 'paper',
  'paper-source-model-v1', repeat('8', 64), repeat('9', 64), repeat('a', 64),
  'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb1', 'approved',
  'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1',
  '11111111-1111-4111-8111-111111111111',
  '22222222-2222-4222-8222-222222222222',
  clock_timestamp() - interval '1 day', clock_timestamp() + interval '1 day'
);

insert into private.paper_execution_policies (
  id, account_id, policy_version, policy_sha256, status, price_model,
  fill_model, parameters, evidence_id, requested_by, reviewed_by,
  effective_from, effective_until
) values (
  'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb3', 'paper-primary',
  'paper-source-policy-v1', repeat('5', 64), 'approved',
  'next_executable_minute_v1', 'whole_share_volume_bounded_v1',
  jsonb_build_object(
    'corporate_action_evidence_sha256', repeat('a', 64),
    'execution_model_version', 'paper-source-model-v1',
    'market_calendar_sha256', repeat('7', 64),
    'market_calendar_version', 'paper-source-calendar-v1',
    'tick_size_evidence_sha256', repeat('8', 64),
    'volume_model_evidence_sha256', repeat('9', 64)
  ),
  'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1',
  '11111111-1111-4111-8111-111111111111',
  '22222222-2222-4222-8222-222222222222',
  clock_timestamp() - interval '1 day', clock_timestamp() + interval '1 day'
);

insert into private.execution_cost_schedules (
  id, account_id, schedule_version, schedule_sha256,
  buy_commission_rate, sell_commission_rate, sell_tax_rate, settlement_days,
  status, evidence_id, requested_by, reviewed_by, effective_from, effective_until
) values (
  'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb4', 'paper-primary',
  'paper-source-cost-v1', repeat('2', 64), 0.001, 0.001, 0.002, 2,
  'approved', 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa2',
  '11111111-1111-4111-8111-111111111111',
  '22222222-2222-4222-8222-222222222222',
  clock_timestamp() - interval '1 day', clock_timestamp() + interval '1 day'
);

insert into private.control_approval_requests (
  id, request_kind, account_id, environment, payload, payload_sha256,
  requester_user_id, requested_at, expires_at, idempotency_key
) values (
  'cccccccc-cccc-4ccc-8ccc-ccccccccccc1', 'reference_bundle',
  'paper-primary', 'paper', '{"schema_version":1}', repeat('0', 64),
  '11111111-1111-4111-8111-111111111111',
  clock_timestamp() - interval '2 minutes', clock_timestamp() + interval '1 hour',
  'cccccccc-cccc-4ccc-8ccc-ccccccccccc1'
);

insert into private.control_approval_reviews (
  id, request_id, decision, payload, payload_sha256,
  expected_request_payload_sha256, reviewer_user_id, reviewed_at
) values (
  'cccccccc-cccc-4ccc-8ccc-ccccccccccc2',
  'cccccccc-cccc-4ccc-8ccc-ccccccccccc1', 'approved',
  '{"schema_version":1}', repeat('b', 64), repeat('0', 64),
  '22222222-2222-4222-8222-222222222222',
  clock_timestamp() - interval '90 seconds'
);

insert into private.reference_bundle_materializations (
  request_id, review_id, evidence_id, policy_id, cost_schedule_id,
  calendar_id, execution_model_id, execution_model_sha256, bundle_sha256
) values (
  'cccccccc-cccc-4ccc-8ccc-ccccccccccc1',
  'cccccccc-cccc-4ccc-8ccc-ccccccccccc2',
  'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1',
  'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb3',
  'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb4',
  'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb1',
  'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb2', repeat('b', 64), repeat('c', 64)
);

insert into private.account_snapshots (
  id, account_id, environment, sequence, cash_krw, reserved_cash_krw,
  positions_sha256, source_type, source_id, observed_at,
  checkpoint_schema_version, ledger_checkpoint_sha256
)
select
  'dddddddd-dddd-4ddd-8ddd-ddddddddddd1', 'paper-primary', 'paper',
  coalesce((
    select max(existing.sequence)
    from private.account_snapshots as existing
    where existing.account_id = 'paper-primary'
      and existing.environment = 'paper'
  ), 0) + 1,
  settled_cash_krw, reserved_cash_krw, repeat('d', 64),
  'ledger_projection', 'dddddddd-dddd-4ddd-8ddd-ddddddddddd2',
  clock_timestamp(), 1,
  private.compute_ledger_checkpoint_sha256_v1('paper-primary', 'paper')
from private.cash_balance_projection
where account_id = 'paper-primary';

insert into private.qualification_runs (
  id, run_kind, account_id, environment, reference_bundle_request_id,
  account_snapshot_id, account_snapshot_sequence, ledger_checkpoint_sha256,
  release_sha, suite_version, result, evidence_manifest, evidence_sha256,
  started_at, completed_at, worker_id
)
select
  run_id, run_kind, 'paper-primary', 'paper',
  'cccccccc-cccc-4ccc-8ccc-ccccccccccc1', snapshot.id, snapshot.sequence,
  snapshot.ledger_checkpoint_sha256, repeat('a', 40), 'source-contract-v1',
  'pass', jsonb_build_object('local_fixture', true), evidence_sha256,
  clock_timestamp() - interval '1 minute', clock_timestamp() - interval '30 seconds',
  '88888888-8888-4888-8888-888888888888'
from private.account_snapshots as snapshot
cross join (values
  ('eeeeeeee-eeee-4eee-8eee-eeeeeeeeeee1'::uuid, 'g1'::text, repeat('e', 64)),
  ('eeeeeeee-eeee-4eee-8eee-eeeeeeeeeee2'::uuid, 'g2'::text, repeat('f', 64))
) as run(run_id, run_kind, evidence_sha256)
where snapshot.id = 'dddddddd-dddd-4ddd-8ddd-ddddddddddd1';

insert into private.control_approval_requests (
  id, request_kind, account_id, environment, payload, payload_sha256,
  requester_user_id, requested_at, expires_at, idempotency_key
) values (
  'cccccccc-cccc-4ccc-8ccc-ccccccccccc3', 'qualification_finalization',
  'paper-primary', 'paper', '{"schema_version":1}', repeat('d', 64),
  '11111111-1111-4111-8111-111111111111',
  clock_timestamp() - interval '1 minute', clock_timestamp() + interval '1 hour',
  'cccccccc-cccc-4ccc-8ccc-ccccccccccc3'
);

insert into private.control_approval_reviews (
  id, request_id, decision, payload, payload_sha256,
  expected_request_payload_sha256, reviewer_user_id, reviewed_at
) values (
  'cccccccc-cccc-4ccc-8ccc-ccccccccccc4',
  'cccccccc-cccc-4ccc-8ccc-ccccccccccc3', 'approved',
  '{"schema_version":1}', repeat('e', 64), repeat('d', 64),
  '22222222-2222-4222-8222-222222222222',
  clock_timestamp() - interval '30 seconds'
);

insert into private.qualifications (
  id, environment, status, release_sha, ledger_checkpoint, dataset_version,
  execution_policy_version, execution_policy_sha256, risk_policy_sha256,
  strategy_version_id, risk_policy_version_id, valid_from, valid_until,
  g1_status, g1_checked_at, g1_evidence_id,
  g2_status, g2_checked_at, g2_evidence_id,
  account_id, checkpoint_schema_version, account_snapshot_id,
  account_snapshot_sequence, ledger_checkpoint_sha256,
  reference_bundle_request_id, g1_run_id, g2_run_id, approval_request_id
)
select
  'ffffffff-ffff-4fff-8fff-fffffffffff1', 'paper', 'qualified', repeat('a', 40),
  'snapshot-v1:' || id::text || ':' || sequence::text || ':'
    || ledger_checkpoint_sha256,
  'paper-source-dataset-v1', 'paper-source-policy-v1', repeat('5', 64),
  repeat('6', 64), '77777777-7777-4777-8777-777777777777',
  'ffffffff-ffff-4fff-8fff-fffffffffff2',
  clock_timestamp() - interval '1 minute', clock_timestamp() + interval '1 hour',
  'pass', clock_timestamp(), 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa3',
  'pass', clock_timestamp(), 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa4',
  'paper-primary', 1, id, sequence, ledger_checkpoint_sha256,
  'cccccccc-cccc-4ccc-8ccc-ccccccccccc1',
  'eeeeeeee-eeee-4eee-8eee-eeeeeeeeeee1',
  'eeeeeeee-eeee-4eee-8eee-eeeeeeeeeee2',
  'cccccccc-cccc-4ccc-8ccc-ccccccccccc3'
from private.account_snapshots
where id = 'dddddddd-dddd-4ddd-8ddd-ddddddddddd1';

insert into private.qualification_finalizations (
  request_id, review_id, qualification_id, g1_evidence_id, g2_evidence_id
) values (
  'cccccccc-cccc-4ccc-8ccc-ccccccccccc3',
  'cccccccc-cccc-4ccc-8ccc-ccccccccccc4',
  'ffffffff-ffff-4fff-8fff-fffffffffff1',
  'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa3',
  'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa4'
);

insert into private.operation_commands (
  id, command_type, state, requested_change, revision, evidence_id,
  target_release_sha, requester_user_id, reviewer_user_id, claimed_by_service,
  requested_at, reviewed_at, claimed_at, claim_expires_at, applied_at,
  expires_at, result_summary, idempotency_key
)
select
  'ffffffff-ffff-4fff-8fff-fffffffffff3', 'paper_resume', 'applied',
  jsonb_build_object(
    'account_id', 'paper-primary', 'environment', 'paper',
    'expected_state_version', 1,
    'qualification_id', 'ffffffff-ffff-4fff-8fff-fffffffffff1',
    'strategy_version_id', '77777777-7777-4777-8777-777777777777',
    'risk_policy_version_id', 'ffffffff-ffff-4fff-8fff-fffffffffff2',
    'release_sha', repeat('a', 40), 'ledger_checkpoint', ledger_checkpoint,
    'execution_policy_version', 'paper-source-policy-v1',
    'execution_policy_sha256', repeat('5', 64),
    'risk_policy_sha256', repeat('6', 64),
    'reason_code', 'paper_source_behavior_verification'
  ),
  1, 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa4', repeat('a', 40),
  '11111111-1111-4111-8111-111111111111',
  '22222222-2222-4222-8222-222222222222',
  '88888888-8888-4888-8888-888888888888',
  clock_timestamp() - interval '50 seconds',
  clock_timestamp() - interval '40 seconds',
  clock_timestamp() - interval '30 seconds',
  clock_timestamp() + interval '10 minutes',
  clock_timestamp() - interval '20 seconds',
  clock_timestamp() + interval '30 minutes', '{}',
  'ffffffff-ffff-4fff-8fff-fffffffffff3'
from private.qualifications
where id = 'ffffffff-ffff-4fff-8fff-fffffffffff1';

update private.execution_controls as control
set execution_enabled = true,
    control_epoch = control.control_epoch + 1,
    active_strategy_version_id = '77777777-7777-4777-8777-777777777777',
    active_risk_policy_version_id = 'ffffffff-ffff-4fff-8fff-fffffffffff2',
    execution_policy_version = 'paper-source-policy-v1',
    execution_policy_sha256 = repeat('5', 64),
    risk_policy_sha256 = repeat('6', 64),
    effective_at = clock_timestamp() - interval '10 seconds',
    expires_at = clock_timestamp() + interval '30 minutes',
    last_command_id = 'ffffffff-ffff-4fff-8fff-fffffffffff3',
    updated_reason_code = 'paper_source_behavior_verification',
    updated_at = clock_timestamp()
where control.account_id = 'paper-primary';

insert into private.worker_leases (
  account_id, holder_id, fencing_token, acquired_at, renewed_at,
  expires_at, release_sha
) values (
  'paper-primary', '88888888-8888-4888-8888-888888888888', 1,
  clock_timestamp() - interval '10 seconds',
  clock_timestamp() - interval '5 seconds',
  clock_timestamp() + interval '30 minutes', repeat('a', 40)
);

create temp table paper_source_times as
select
  date_trunc('minute', clock_timestamp()) as current_minute,
  date_trunc('minute', clock_timestamp()) - interval '4 minutes' as decision_at,
  date_trunc('minute', clock_timestamp()) - interval '5 minutes' as signal_from,
  date_trunc('minute', clock_timestamp()) - interval '3 minutes' as eligible_at,
  date_trunc('minute', clock_timestamp()) + interval '5 minutes' as expires_at,
  date_trunc('minute', clock_timestamp()) + interval '6 minutes' as signal_until,
  date_trunc('minute', clock_timestamp()) - interval '4 minutes 10 seconds'
    as risk_at,
  clock_timestamp() + interval '20 minutes' as risk_expires;

create temp table paper_source_gate as
select control.control_epoch, lease.fencing_token
from private.execution_controls as control
join private.worker_leases as lease using (account_id)
where control.account_id = 'paper-primary';

create temp table paper_source_payloads (
  payload_name text primary key,
  payload jsonb not null
);

with raw_fixture(payload) as (
  select jsonb_build_object(
    'schema_version', 1,
    'series_id', '10101010-1010-4010-8010-101010101010',
    'fixture_set_id', '11111111-1010-4010-8010-101010101010',
    'source_kind', 'local_fixture',
    'dataset_version', 'paper-source-dataset-v1',
    'symbol', '005930',
    'model_version', 'paper-source-model-v1',
    'execution_policy_version', 'paper-source-policy-v1',
    'tick_rule_version', 'krx-tick-v1',
    'tick_size_krw', 10,
    'tick_rule_evidence_sha256', repeat('8', 64),
    'volume_source', 'immutable_local_fixture',
    'volume_evidence_sha256', repeat('9', 64),
    'corporate_action_status', 'not_required',
    'corporate_action_evidence_sha256', repeat('a', 64),
    'market_calendar_version', 'paper-source-calendar-v1',
    'market_calendar_evidence_sha256', repeat('7', 64),
    'effective_from', times.current_minute - interval '10 minutes',
    'effective_until', times.current_minute + interval '10 minutes',
    'bars', jsonb_build_array(jsonb_build_object(
      'sequence', 1,
      'minute', times.eligible_at,
      'completed_at', times.eligible_at + interval '1 minute',
      'as_of', times.eligible_at + interval '1 minute',
      'source_sha256', repeat('9', 64),
      'is_complete', true,
      'open_krw', 10000,
      'high_krw', 10020,
      'low_krw', 9990,
      'close_krw', 10010,
      'volume', 10000
    ))
  )
  from paper_source_times as times
)
insert into paper_source_payloads (payload_name, payload)
select
  'fixture',
  payload || jsonb_build_object(
    'fixture_sha256', private.sha256_jsonb_v1(payload)
  )
from raw_fixture;

with candidate_base as (
  select
    times.*,
    private.compute_order_semantic_key(
      'paper-primary', 'paper',
      '77777777-7777-4777-8777-777777777777',
      '005930', 'buy', times.signal_from, times.signal_until,
      'paper-source-policy-v1'
    ) as semantic_key
  from paper_source_times as times
), candidate(payload) as (
  select jsonb_build_object(
    'schema_version', 1,
    'intent_id', '20202020-2020-4020-8020-202020202020',
    'account_id', 'paper-primary',
    'semantic_key_sha256', semantic_key,
    'decision_id', '21212121-2121-4121-8121-212121212121',
    'risk_result_id', '22222222-2121-4121-8121-212121212121',
    'decision_feature_sha256', repeat('3', 64),
    'risk_evaluated_at', risk_at,
    'risk_expires_at', risk_expires,
    'strategy_version_id', '77777777-7777-4777-8777-777777777777',
    'symbol', '005930', 'side', 'buy', 'quantity', 1,
    'limit_price_krw', 10000, 'cash_commitment_krw', 10010,
    'decision_at', decision_at, 'signal_valid_from', signal_from,
    'signal_valid_until', signal_until, 'expires_at', expires_at,
    'execution_policy_version', 'paper-source-policy-v1',
    'cost_schedule_version', 'paper-source-cost-v1',
    'cost_schedule_evidence_sha256', repeat('2', 64),
    'fixture_series_id', '10101010-1010-4010-8010-101010101010',
    'risk_input', jsonb_build_object(
      'schema_version', 1,
      'settings', jsonb_build_object(
        'mode', 'paper', 'enabled', true, 'live_order_allowed', false,
        'deployment_lock', false, 'deployment_target_sha', null,
        'max_order_amount_krw', 1000000, 'max_daily_loss_pct', 0.05,
        'max_daily_order_count', 100, 'max_position_pct', 0.2,
        'max_sector_pct', 0.4, 'loop_interval_sec', 5,
        'quote_freshness_sec', 60
      ),
      'signal', jsonb_build_object(
        'symbol', '005930', 'action', 'buy', 'final_score', 0.8,
        'confidence', 0.9, 'order_amount_krw', 10000,
        'sector', 'fixture', 'reason_json', '{}'::jsonb
      ),
      'account_state', jsonb_build_object(
        'synced', true, 'cash_krw', 10000000, 'equity_krw', 10000000,
        'daily_loss_pct', 0, 'daily_order_count', 0,
        'daily_order_count_verified', true,
        'synced_at', risk_at - interval '1 second'
      ),
      'quote', jsonb_build_object(
        'symbol', '005930', 'price_krw', 10000,
        'source', 'immutable_local_fixture',
        'as_of', risk_at - interval '1 second'
      ),
      'provider_health', jsonb_build_object('local_fixture', true),
      'cooldown_active', false, 'duplicate_order', false,
      'strategy_approved', true, 'shutdown_requested', false,
      'strategy_status', 'paper',
      'strategy_version_id', '77777777-7777-4777-8777-777777777777',
      'market_open', true, 'critical_news_risk', false,
      'liquidity_ok', true, 'volatility_ok', true,
      'existing_position_pct', 0, 'sector_position_pct', 0,
      'available_position_quantity', null
    )
  ) from candidate_base
)
insert into paper_source_payloads (payload_name, payload)
select 'candidate_1', payload from candidate;

grant select on paper_source_times, paper_source_gate, paper_source_payloads
to service_role;

set role service_role;
do $$
begin
  perform set_config('request.jwt.claim.role', 'service_role', false);
end;
$$;

create temp table fixture_result_1 as
select * from worker_api.ingest_paper_bar_fixture_v1(
  (select payload from paper_source_payloads where payload_name = 'fixture'),
  '88888888-8888-4888-8888-888888888888', repeat('a', 40),
  clock_timestamp()
);
create temp table fixture_result_2 as
select * from worker_api.ingest_paper_bar_fixture_v1(
  (select payload from paper_source_payloads where payload_name = 'fixture'),
  '88888888-8888-4888-8888-888888888888', repeat('a', 40),
  clock_timestamp()
);
do $$
begin
  if (select idempotent from fixture_result_1) is not false
     or (select idempotent from fixture_result_2) is not true
     or (select bar_count from fixture_result_1) <> 1 then
    raise exception 'fixture_idempotency_assertion_failed';
  end if;
end;
$$;
\echo PASS immutable fixture ingest and duplicate no-op

reset role;
with raw_fixture(payload) as (
  select (base.payload - 'fixture_sha256' - 'fixture_set_id' - 'bars')
    || jsonb_build_object(
      'fixture_set_id', '11111111-1010-4010-8010-101010101011',
      'bars', jsonb_build_array(jsonb_build_object(
        'sequence', 1,
        'minute', times.current_minute - interval '1 minute',
        'completed_at', times.current_minute,
        'as_of', times.current_minute,
        'source_sha256', repeat('9', 64), 'is_complete', true,
        'open_krw', 10000, 'high_krw', 10020,
        'low_krw', 9990, 'close_krw', 10010, 'volume', 10000
      ))
    )
  from paper_source_payloads as base
  cross join paper_source_times as times
  where base.payload_name = 'fixture'
)
insert into paper_source_payloads (payload_name, payload)
select 'gap_fixture', payload || jsonb_build_object(
  'fixture_sha256', private.sha256_jsonb_v1(payload)
) from raw_fixture;

set role service_role;
do $$
declare
  caught boolean := false;
begin
  begin
    perform * from worker_api.ingest_paper_bar_fixture_v1(
      (select payload from paper_source_payloads where payload_name = 'gap_fixture'),
      '88888888-8888-4888-8888-888888888888', repeat('a', 40),
      clock_timestamp()
    );
  exception when check_violation then
    if position('paper_fixture_series_gap_or_window_invalid' in sqlerrm) > 0 then
      caught := true;
    else
      raise;
    end if;
  end;
  if not caught then
    raise exception 'fixture_gap_was_not_rejected';
  end if;
end;
$$;
\echo PASS gapless minute-bar boundary rejects skipped minute

create temp table enqueue_result_1 as
select * from worker_api.enqueue_paper_execution_candidate_v1(
  (select payload from paper_source_payloads where payload_name = 'candidate_1'),
  '88888888-8888-4888-8888-888888888888',
  (select fencing_token from paper_source_gate),
  (select control_epoch from paper_source_gate),
  repeat('a', 40), clock_timestamp()
);
create temp table enqueue_result_2 as
select * from worker_api.enqueue_paper_execution_candidate_v1(
  (select payload from paper_source_payloads where payload_name = 'candidate_1'),
  '88888888-8888-4888-8888-888888888888',
  (select fencing_token from paper_source_gate),
  (select control_epoch from paper_source_gate),
  repeat('a', 40), clock_timestamp()
);
do $$
begin
  if (select idempotent from enqueue_result_1) is not false
     or (select idempotent from enqueue_result_2) is not true
     or (select source_revision from enqueue_result_1) <> 1 then
    raise exception 'candidate_idempotency_assertion_failed';
  end if;
end;
$$;
\echo PASS semantic candidate enqueue and duplicate no-op

do $$
declare
  caught boolean := false;
begin
  begin
    perform * from worker_api.claim_paper_execution_v1(
      'paper-primary', '88888888-8888-4888-8888-888888888888',
      repeat('b', 40), clock_timestamp(), 30
    );
  exception when serialization_failure then
    if position('paper_source_gate_lease_or_qualification_stale' in sqlerrm) > 0 then
      caught := true;
    else
      raise;
    end if;
  end;
  if not caught then
    raise exception 'stale_release_was_not_rejected';
  end if;
end;
$$;
\echo PASS release pin rejects mismatched Worker release

create temp table claim_1 as
select * from worker_api.claim_paper_execution_v1(
  'paper-primary', '88888888-8888-4888-8888-888888888888',
  repeat('a', 40), clock_timestamp(), 30
);
create temp table bundle_1 as
select * from worker_api.load_claimed_paper_execution_bundle_v1(
  (select command_id from claim_1),
  (select intent_id from claim_1),
  (select claim_token from claim_1),
  (select source_revision from claim_1),
  '88888888-8888-4888-8888-888888888888', repeat('a', 40),
  clock_timestamp()
);
do $$
begin
  if (select kind from claim_1) <> 'new_candidate'
     or (select source_revision from claim_1) <> 2
     or (select bundle->>'schema_version' from bundle_1) <> '1'
     or (select jsonb_array_length(bundle->'command'->'bars') from bundle_1) <> 1
     or (select bundle->'risk_input' = 'null'::jsonb from bundle_1) then
    raise exception 'positive_claim_load_assertion_failed';
  end if;
end;
$$;
\echo PASS positive claim/load returns strict source bundle

reset role;
update private.worker_leases
set fencing_token = fencing_token + 1
where account_id = 'paper-primary';
set role service_role;
do $$
declare
  caught boolean := false;
begin
  begin
    perform * from worker_api.load_claimed_paper_execution_bundle_v1(
      (select command_id from claim_1), (select intent_id from claim_1),
      (select claim_token from claim_1), (select source_revision from claim_1),
      '88888888-8888-4888-8888-888888888888', repeat('a', 40),
      clock_timestamp()
    );
  exception when serialization_failure then
    if position('paper_source_gate_lease_or_qualification_stale' in sqlerrm) > 0 then
      caught := true;
    else
      raise;
    end if;
  end;
  if not caught then raise exception 'stale_fence_was_not_rejected'; end if;
end;
$$;
\echo PASS stale fencing token rejects claimed bundle

reset role;
update private.worker_leases
set fencing_token = fencing_token - 1
where account_id = 'paper-primary';
set session_replication_role = replica;
update private.execution_controls
set control_epoch = control_epoch + 1
where account_id = 'paper-primary';
set session_replication_role = origin;
set role service_role;
do $$
declare
  caught boolean := false;
begin
  begin
    perform * from worker_api.load_claimed_paper_execution_bundle_v1(
      (select command_id from claim_1), (select intent_id from claim_1),
      (select claim_token from claim_1), (select source_revision from claim_1),
      '88888888-8888-4888-8888-888888888888', repeat('a', 40),
      clock_timestamp()
    );
  exception when serialization_failure then
    if position('paper_source_gate_lease_or_qualification_stale' in sqlerrm) > 0 then
      caught := true;
    else
      raise;
    end if;
  end;
  if not caught then raise exception 'stale_epoch_was_not_rejected'; end if;
end;
$$;
\echo PASS stale control epoch rejects claimed bundle

reset role;
set session_replication_role = replica;
update private.execution_controls
set control_epoch = control_epoch - 1
where account_id = 'paper-primary';
set session_replication_role = origin;
update private.paper_execution_work_items
set claimed_at = clock_timestamp() - interval '2 minutes',
    claim_expires_at = clock_timestamp() - interval '1 minute'
where id = (select command_id from claim_1);
set role service_role;
create temp table claim_2 as
select * from worker_api.claim_paper_execution_v1(
  'paper-primary', '88888888-8888-4888-8888-888888888888',
  repeat('a', 40), clock_timestamp(), 30
);
do $$
begin
  if (select command_id from claim_2) <> (select command_id from claim_1)
     or (select claim_token from claim_2) = (select claim_token from claim_1)
     or (select source_revision from claim_2)
       <> (select source_revision from claim_1) + 1 then
    raise exception 'reclaim_cas_assertion_failed';
  end if;
end;
$$;
do $$
declare
  caught boolean := false;
begin
  begin
    perform * from worker_api.complete_paper_execution_source_v1(
      (select command_id from claim_1), (select claim_token from claim_1),
      (select source_revision from claim_1),
      '88888888-8888-4888-8888-888888888888', repeat('a', 40),
      clock_timestamp(), 'complete', null, 'stale_claim_must_fail'
    );
  exception when serialization_failure then
    if position('paper_execution_completion_claim_stale_or_mismatched' in sqlerrm) > 0 then
      caught := true;
    else
      raise;
    end if;
  end;
  if not caught then raise exception 'stale_claim_completion_succeeded'; end if;
end;
$$;
\echo PASS expired claim is reclaimed with new token/revision and old CAS is rejected

create temp table reservation_1 as
select result.*
from (
  select payload as candidate
  from paper_source_payloads where payload_name = 'candidate_1'
) as payload
cross join lateral worker_api.reserve_order_intent(
  (candidate->>'intent_id')::uuid,
  candidate->>'semantic_key_sha256', candidate->>'account_id', 'paper',
  candidate->>'strategy_version_id', (candidate->>'decision_id')::uuid,
  candidate->>'decision_feature_sha256', (candidate->>'risk_result_id')::uuid,
  true, array[]::text[], (candidate->>'risk_evaluated_at')::timestamptz,
  (candidate->>'risk_expires_at')::timestamptz, candidate->>'symbol',
  candidate->>'side', (candidate->>'quantity')::bigint,
  (candidate->>'limit_price_krw')::bigint,
  (candidate->>'decision_at')::timestamptz,
  (candidate->>'signal_valid_from')::timestamptz,
  (candidate->>'signal_valid_until')::timestamptz,
  candidate->>'execution_policy_version', candidate->>'cost_schedule_version',
  candidate->>'cost_schedule_evidence_sha256',
  (candidate->>'cash_commitment_krw')::bigint,
  date_trunc('minute', (candidate->>'decision_at')::timestamptz) + interval '1 minute',
  (candidate->>'expires_at')::timestamptz,
  (select control_epoch from paper_source_gate),
  '88888888-8888-4888-8888-888888888888',
  (select fencing_token from paper_source_gate), repeat('a', 40)
) as result;

create temp table attempt_1 as
select * from worker_api.mark_dispatch_started(
  (select intent_id from claim_2), 'paper-primary', 'paper',
  '88888888-8888-4888-8888-888888888888',
  (select fencing_token from paper_source_gate),
  (select control_epoch from paper_source_gate), clock_timestamp(),
  repeat('1', 64), 'paper-source-client-key-1'
);

create temp table reschedule_1 as
select * from worker_api.complete_paper_execution_source_v1(
  (select command_id from claim_2), (select claim_token from claim_2),
  (select source_revision from claim_2),
  '88888888-8888-4888-8888-888888888888', repeat('a', 40),
  clock_timestamp(), 'reschedule',
  date_trunc('minute', clock_timestamp()) + interval '1 minute',
  'paper_partial_fill_resume_required'
);
reset role;
do $$
begin
  if (select reserved from reservation_1) is not true
     or (select count(*) from attempt_1) <> 1
     or (select state from reschedule_1) <> 'pending'
     or (select kind from private.paper_execution_work_items
         where id = (select command_id from claim_2)) <> 'resume_existing' then
    raise exception 'reschedule_assertion_failed';
  end if;
end;
$$;
\echo PASS reserved attempt reschedules new work into resume_existing

with base as (
  select payload as first_candidate
  from paper_source_payloads where payload_name = 'candidate_1'
), changed as (
  select
    (first_candidate
      || jsonb_build_object(
        'intent_id', '30303030-3030-4030-8030-303030303030',
        'decision_id', '31313131-3131-4131-8131-313131313131',
        'risk_result_id', '32323232-3232-4232-8232-323232323232',
        'decision_feature_sha256', repeat('4', 64),
        'signal_valid_until',
          (first_candidate->>'signal_valid_until')::timestamptz + interval '1 minute'
      )) as candidate
  from base
), canonical as (
  select candidate || jsonb_build_object(
    'semantic_key_sha256', private.compute_order_semantic_key(
      candidate->>'account_id', 'paper', candidate->>'strategy_version_id',
      candidate->>'symbol', candidate->>'side',
      (candidate->>'signal_valid_from')::timestamptz,
      (candidate->>'signal_valid_until')::timestamptz,
      candidate->>'execution_policy_version'
    )
  ) as candidate
  from changed
)
insert into paper_source_payloads (payload_name, payload)
select 'candidate_2', candidate from canonical;

set role service_role;
create temp table enqueue_result_3 as
select * from worker_api.enqueue_paper_execution_candidate_v1(
  (select payload from paper_source_payloads where payload_name = 'candidate_2'),
  '88888888-8888-4888-8888-888888888888',
  (select fencing_token from paper_source_gate),
  (select control_epoch from paper_source_gate), repeat('a', 40),
  clock_timestamp()
);
create temp table claim_3 as
select * from worker_api.claim_paper_execution_v1(
  'paper-primary', '88888888-8888-4888-8888-888888888888',
  repeat('a', 40), clock_timestamp(), 30
);
create temp table manual_1 as
select * from worker_api.complete_paper_execution_source_v1(
  (select command_id from claim_3), (select claim_token from claim_3),
  (select source_revision from claim_3),
  '88888888-8888-4888-8888-888888888888', repeat('a', 40),
  clock_timestamp(), 'manual', null, 'paper_execution_evidence_requires_review'
);
reset role;
do $$
begin
  if (select state from manual_1) <> 'manual'
     or (select execution_enabled from private.execution_controls
         where account_id = 'paper-primary') is not false
     or (select control_epoch from private.execution_controls
         where account_id = 'paper-primary')
       <> (select control_epoch from paper_source_gate) + 1
     or not exists (
       select 1 from private.incidents
       where incident_type = 'paper_execution_source_manual_required'
         and summary_code = 'paper_execution_evidence_requires_review'
     )
     or not exists (
       select 1 from private.audit_events
       where action = 'paper_execution_source_manual_required'
         and resource_id = (select command_id::text from claim_3)
     ) then
    raise exception 'manual_fail_closed_assertion_failed';
  end if;
end;
$$;
\echo PASS manual outcome atomically disables execution and records incident/audit

select concat_ws('|',
  (select count(*) from private.paper_bar_series),
  (select count(*) from private.paper_bar_fixture_sets),
  (select count(*) from private.paper_minute_bars),
  (select count(*) from private.paper_execution_candidates),
  (select count(*) from private.paper_execution_work_items),
  (select count(*) from private.paper_execution_work_events),
  (select execution_enabled from private.execution_controls
    where account_id = 'paper-primary')
) as paper_source_final_state;
