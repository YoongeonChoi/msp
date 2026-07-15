-- Durable, replayable Paper execution inputs.
--
-- This source accepts only immutable local fixture or explicitly allowed
-- read-evidence batches.  It contains no provider URL, credential, or order
-- transport.  Order reservation and accounting remain owned by the existing
-- execution kernel RPCs.

create table private.paper_bar_series (
  id uuid primary key,
  environment text not null default 'paper' check (environment = 'paper'),
  source_kind text not null check (
    source_kind in ('local_fixture', 'allowed_read_evidence')
  ),
  dataset_version text not null check (nullif(btrim(dataset_version), '') is not null),
  symbol text not null check (symbol ~ '^[0-9]{6}$'),
  model_version text not null check (nullif(btrim(model_version), '') is not null),
  execution_policy_version text not null check (
    nullif(btrim(execution_policy_version), '') is not null
  ),
  tick_rule_version text not null check (nullif(btrim(tick_rule_version), '') is not null),
  tick_size_krw bigint not null check (tick_size_krw > 0),
  tick_rule_evidence_sha256 text not null check (
    tick_rule_evidence_sha256 ~ '^[0-9a-f]{64}$'
  ),
  volume_source text not null check (nullif(btrim(volume_source), '') is not null),
  volume_evidence_sha256 text not null check (
    volume_evidence_sha256 ~ '^[0-9a-f]{64}$'
  ),
  corporate_action_status text not null check (
    corporate_action_status in ('not_required', 'adjusted')
  ),
  corporate_action_evidence_sha256 text not null check (
    corporate_action_evidence_sha256 ~ '^[0-9a-f]{64}$'
  ),
  market_calendar_version text not null check (
    nullif(btrim(market_calendar_version), '') is not null
  ),
  market_calendar_evidence_sha256 text not null check (
    market_calendar_evidence_sha256 ~ '^[0-9a-f]{64}$'
  ),
  effective_from timestamptz not null,
  effective_until timestamptz not null,
  created_release_sha text not null check (
    created_release_sha ~ '^[0-9a-f]{40}([0-9a-f]{24})?$'
  ),
  created_at timestamptz not null default clock_timestamp(),
  constraint paper_bar_series_window_check check (effective_until > effective_from),
  unique (dataset_version, symbol, model_version, execution_policy_version)
);

create table private.paper_bar_fixture_sets (
  id uuid primary key,
  series_id uuid not null references private.paper_bar_series(id),
  batch_sequence integer not null check (batch_sequence > 0),
  first_minute timestamptz not null,
  last_minute timestamptz not null,
  bar_count integer not null check (bar_count > 0),
  observed_through timestamptz not null,
  fixture_sha256 text not null unique check (fixture_sha256 ~ '^[0-9a-f]{64}$'),
  evidence_urn text not null unique check (
    evidence_urn = 'urn:sha256:' || fixture_sha256
  ),
  release_sha text not null check (
    release_sha ~ '^[0-9a-f]{40}([0-9a-f]{24})?$'
  ),
  ingested_at timestamptz not null,
  constraint paper_bar_fixture_window_check check (
    date_trunc('minute', first_minute) = first_minute
    and date_trunc('minute', last_minute) = last_minute
    and last_minute = first_minute + make_interval(mins => bar_count - 1)
    and observed_through >= last_minute + interval '1 minute'
  ),
  unique (id, series_id),
  unique (series_id, batch_sequence),
  unique (series_id, first_minute),
  unique (series_id, last_minute)
);

create table private.paper_minute_bars (
  fixture_set_id uuid not null,
  series_id uuid not null,
  sequence integer not null check (sequence > 0),
  minute timestamptz not null,
  completed_at timestamptz not null,
  as_of timestamptz not null,
  source_sha256 text not null check (source_sha256 ~ '^[0-9a-f]{64}$'),
  is_complete boolean not null check (is_complete is true),
  open_krw bigint not null check (open_krw > 0),
  high_krw bigint not null check (high_krw > 0),
  low_krw bigint not null check (low_krw > 0),
  close_krw bigint not null check (close_krw > 0),
  volume bigint not null check (volume >= 0),
  bar_sha256 text not null check (bar_sha256 ~ '^[0-9a-f]{64}$'),
  primary key (fixture_set_id, sequence),
  foreign key (fixture_set_id, series_id)
    references private.paper_bar_fixture_sets(id, series_id),
  constraint paper_minute_bar_time_check check (
    date_trunc('minute', minute) = minute
    and completed_at = minute + interval '1 minute'
    and as_of >= completed_at
  ),
  constraint paper_minute_bar_ohlc_check check (
    high_krw >= greatest(open_krw, low_krw, close_krw)
    and low_krw <= least(open_krw, high_krw, close_krw)
  ),
  unique (series_id, minute),
  unique (series_id, bar_sha256)
);

create table private.paper_execution_candidates (
  intent_id uuid primary key,
  account_id text not null references private.trading_accounts(account_id),
  environment text not null default 'paper' check (environment = 'paper'),
  semantic_key_sha256 text not null check (
    semantic_key_sha256 ~ '^[0-9a-f]{64}$'
  ),
  decision_id uuid not null,
  risk_result_id uuid not null,
  decision_feature_sha256 text not null check (
    decision_feature_sha256 ~ '^[0-9a-f]{64}$'
  ),
  risk_evaluated_at timestamptz not null,
  risk_expires_at timestamptz not null,
  strategy_version_id uuid not null,
  symbol text not null check (symbol ~ '^[0-9]{6}$'),
  side text not null check (side in ('buy', 'sell')),
  quantity bigint not null check (quantity > 0),
  limit_price_krw bigint not null check (limit_price_krw > 0),
  cash_commitment_krw bigint not null check (cash_commitment_krw >= 0),
  decision_at timestamptz not null,
  signal_valid_from timestamptz not null,
  signal_valid_until timestamptz not null,
  eligible_at timestamptz not null,
  expires_at timestamptz not null,
  execution_policy_version text not null,
  cost_schedule_version text not null,
  cost_schedule_evidence_sha256 text not null check (
    cost_schedule_evidence_sha256 ~ '^[0-9a-f]{64}$'
  ),
  fixture_series_id uuid not null references private.paper_bar_series(id),
  risk_input jsonb not null check (jsonb_typeof(risk_input) = 'object'),
  candidate_sha256 text not null unique check (candidate_sha256 ~ '^[0-9a-f]{64}$'),
  source_release_sha text not null check (
    source_release_sha ~ '^[0-9a-f]{40}([0-9a-f]{24})?$'
  ),
  created_at timestamptz not null,
  constraint paper_candidate_window_check check (
    risk_evaluated_at <= decision_at
    and decision_at <= created_at + interval '30 seconds'
    and risk_expires_at > created_at
    and signal_valid_from <= decision_at
    and decision_at <= signal_valid_until
    and eligible_at = date_trunc('minute', decision_at) + interval '1 minute'
    and date_trunc('minute', expires_at) = expires_at
    and expires_at >= eligible_at
    and expires_at <= signal_valid_until
  ),
  constraint paper_candidate_cash_shape_check check (
    (side = 'buy' and cash_commitment_krw >= quantity * limit_price_krw)
    or (side = 'sell' and cash_commitment_krw = 0)
  ),
  unique (account_id, environment, semantic_key_sha256)
);

create table private.paper_execution_work_items (
  id uuid primary key default gen_random_uuid(),
  intent_id uuid not null unique references private.paper_execution_candidates(intent_id),
  account_id text not null references private.trading_accounts(account_id),
  kind text not null check (kind in ('new_candidate', 'resume_existing')),
  state text not null check (state in ('pending', 'claimed', 'complete', 'manual')),
  revision bigint not null check (revision > 0),
  available_at timestamptz not null,
  claim_token uuid,
  claim_worker_id uuid,
  claim_release_sha text check (
    claim_release_sha is null
    or claim_release_sha ~ '^[0-9a-f]{40}([0-9a-f]{24})?$'
  ),
  claim_fencing_token bigint,
  claim_control_epoch bigint,
  claimed_at timestamptz,
  claim_expires_at timestamptz,
  attempt_count integer not null default 0 check (attempt_count >= 0),
  last_reason_code text,
  completed_at timestamptz,
  created_at timestamptz not null,
  updated_at timestamptz not null,
  constraint paper_work_claim_shape_check check (
    (
      state = 'claimed'
      and claim_token is not null
      and claim_worker_id is not null
      and claim_release_sha is not null
      and claim_fencing_token > 0
      and claim_control_epoch > 0
      and claimed_at is not null
      and claim_expires_at > claimed_at
      and completed_at is null
    )
    or (
      state <> 'claimed'
      and claim_token is null
      and claim_worker_id is null
      and claim_release_sha is null
      and claim_fencing_token is null
      and claim_control_epoch is null
      and claimed_at is null
      and claim_expires_at is null
    )
  ),
  constraint paper_work_terminal_shape_check check (
    (state in ('complete', 'manual') and completed_at is not null)
    or (state in ('pending', 'claimed') and completed_at is null)
  )
);

create index paper_execution_work_claim_index
  on private.paper_execution_work_items (available_at, created_at, id)
  where state in ('pending', 'claimed');

create table private.paper_execution_work_events (
  id uuid primary key default gen_random_uuid(),
  work_item_id uuid not null references private.paper_execution_work_items(id),
  revision bigint not null check (revision > 0),
  event_type text not null check (
    event_type in ('enqueued', 'claimed', 'reclaimed', 'rescheduled', 'completed', 'manual')
  ),
  worker_id uuid,
  claim_token uuid,
  reason_code text not null,
  occurred_at timestamptz not null,
  unique (work_item_id, revision)
);

create trigger reject_paper_bar_series_mutation
  before update or delete on private.paper_bar_series
  for each row execute function private.reject_append_only_mutation();
create trigger reject_paper_bar_fixture_set_mutation
  before update or delete on private.paper_bar_fixture_sets
  for each row execute function private.reject_append_only_mutation();
create trigger reject_paper_minute_bar_mutation
  before update or delete on private.paper_minute_bars
  for each row execute function private.reject_append_only_mutation();
create trigger reject_paper_execution_candidate_mutation
  before update or delete on private.paper_execution_candidates
  for each row execute function private.reject_append_only_mutation();
create trigger reject_paper_execution_work_event_mutation
  before update or delete on private.paper_execution_work_events
  for each row execute function private.reject_append_only_mutation();

alter table private.paper_bar_series enable row level security;
alter table private.paper_bar_fixture_sets enable row level security;
alter table private.paper_minute_bars enable row level security;
alter table private.paper_execution_candidates enable row level security;
alter table private.paper_execution_work_items enable row level security;
alter table private.paper_execution_work_events enable row level security;

revoke all on table
  private.paper_bar_series,
  private.paper_bar_fixture_sets,
  private.paper_minute_bars,
  private.paper_execution_candidates,
  private.paper_execution_work_items,
  private.paper_execution_work_events
from public, anon, authenticated, service_role;

create or replace function private.jsonb_exact_keys_v1(
  p_value jsonb,
  p_expected text[]
)
returns boolean
language sql
immutable
security invoker
set search_path = ''
as $$
  select jsonb_typeof(p_value) = 'object'
    and coalesce(
      (
        select array_agg(key order by key)
        from jsonb_object_keys(p_value) as item(key)
      ),
      array[]::text[]
    ) = (
      select array_agg(value order by value)
      from unnest(p_expected) as item(value)
    );
$$;

create or replace function private.paper_source_clock_v1(p_now timestamptz)
returns timestamptz
language plpgsql
security invoker
set search_path = ''
as $$
declare
  authorization_time timestamptz := clock_timestamp();
begin
  if p_now is null
     or p_now < authorization_time - interval '5 minutes'
     or p_now > authorization_time + interval '30 seconds' then
    raise exception 'paper_source_clock_out_of_range' using errcode = '22023';
  end if;
  return authorization_time;
end;
$$;

create or replace function private.paper_source_authorization_v1(
  p_account_id text,
  p_worker_id uuid,
  p_release_sha text,
  p_fencing_token bigint,
  p_control_epoch bigint,
  p_now timestamptz
)
returns void
language plpgsql
security definer
set search_path = ''
as $$
begin
  if p_release_sha !~ '^[0-9a-f]{40}([0-9a-f]{24})?$'
     or p_fencing_token is null or p_fencing_token <= 0
     or p_control_epoch is null or p_control_epoch <= 0 then
    raise exception 'paper_source_authorization_values_invalid' using errcode = '22023';
  end if;
  if not exists (
    select 1
    from private.trading_accounts as account
    join private.execution_controls as control
      on control.account_id = account.account_id
     and control.environment = account.environment
    join private.worker_leases as lease
      on lease.account_id = account.account_id
    join private.operation_commands as command
      on command.id = control.last_command_id
    join private.qualifications as qualification
      on qualification.id::text = command.requested_change->>'qualification_id'
    where account.account_id = p_account_id
      and account.environment = 'paper'
      and account.broker = 'internal_paper'
      and account.state = 'open'
      and control.execution_enabled is true
      and control.control_epoch = p_control_epoch
      and control.effective_at <= p_now
      and control.expires_at > p_now
      and control.provider_contract_version is null
      and control.provider_openapi_sha256 is null
      and lease.holder_id = p_worker_id::text
      and lease.fencing_token = p_fencing_token
      and lease.release_sha = p_release_sha
      and lease.expires_at > p_now
      and command.state = 'applied'
      and command.target_release_sha = p_release_sha
      and qualification.checkpoint_schema_version = 1
      and qualification.account_id = account.account_id
      and qualification.environment = account.environment
      and qualification.status = 'qualified'
      and qualification.g1_status = 'pass'
      and qualification.g2_status = 'pass'
      and qualification.release_sha = p_release_sha
      and qualification.valid_from <= p_now
      and qualification.valid_until > p_now
      and qualification.strategy_version_id::text = control.active_strategy_version_id
      and qualification.risk_policy_version_id = control.active_risk_policy_version_id
      and qualification.execution_policy_version = control.execution_policy_version
      and qualification.execution_policy_sha256 = control.execution_policy_sha256
      and qualification.risk_policy_sha256 = control.risk_policy_sha256
      and qualification.provider_contract_version is null
      and qualification.provider_openapi_sha256 is null
      and qualification.ledger_checkpoint = 'snapshot-v1:'
        || qualification.account_snapshot_id::text || ':'
        || qualification.account_snapshot_sequence::text || ':'
        || qualification.ledger_checkpoint_sha256
      and not exists (
        select 1
        from private.reconciliation_breaks as reconciliation_break
        where reconciliation_break.account_id = account.account_id
          and reconciliation_break.break_type <> 'legacy_unreconciled'
          and reconciliation_break.state <> 'resolved'
      )
      and exists (
        select 1
        from private.environment_policy as policy
        where policy.id = 'singleton'
          and policy.environment = 'paper'
          and policy.production_live_enabled is false
          and policy.production_order_credentials_present is false
      )
  ) then
    raise exception 'paper_source_gate_lease_or_qualification_stale'
      using errcode = '40001';
  end if;
end;
$$;

create or replace function private.validate_paper_risk_input_v1(
  p_risk_input jsonb,
  p_candidate jsonb
)
returns void
language plpgsql
security invoker
set search_path = ''
as $$
declare
  settings_value jsonb := p_risk_input->'settings';
  signal_value jsonb := p_risk_input->'signal';
  account_value jsonb := p_risk_input->'account_state';
  quote_value jsonb := p_risk_input->'quote';
begin
  if not private.jsonb_exact_keys_v1(
    p_risk_input,
    array[
      'account_state', 'available_position_quantity', 'cooldown_active',
      'critical_news_risk', 'duplicate_order', 'existing_position_pct',
      'liquidity_ok', 'market_open', 'provider_health', 'quote',
      'schema_version', 'sector_position_pct', 'settings',
      'shutdown_requested', 'signal', 'strategy_approved',
      'strategy_status', 'strategy_version_id', 'volatility_ok'
    ]
  ) or jsonb_typeof(p_risk_input->'schema_version') <> 'number'
     or (p_risk_input->>'schema_version')::integer <> 1 then
    raise exception 'paper_risk_input_schema_invalid' using errcode = '22023';
  end if;
  if not private.jsonb_exact_keys_v1(
    settings_value,
    array[
      'deployment_lock', 'deployment_target_sha', 'enabled',
      'live_order_allowed', 'loop_interval_sec', 'max_daily_loss_pct',
      'max_daily_order_count', 'max_order_amount_krw', 'max_position_pct',
      'max_sector_pct', 'mode', 'quote_freshness_sec'
    ]
  )
     or settings_value->>'mode' <> 'paper'
     or jsonb_typeof(settings_value->'enabled') <> 'boolean'
     or (settings_value->>'enabled')::boolean is not true
     or jsonb_typeof(settings_value->'live_order_allowed') <> 'boolean'
     or (settings_value->>'live_order_allowed')::boolean is not false
     or jsonb_typeof(settings_value->'deployment_lock') <> 'boolean'
     or (
       jsonb_typeof(settings_value->'deployment_target_sha') <> 'null'
       and (
         jsonb_typeof(settings_value->'deployment_target_sha') <> 'string'
         or settings_value->>'deployment_target_sha'
           !~ '^[0-9a-f]{40}([0-9a-f]{24})?$'
       )
     )
     or jsonb_typeof(settings_value->'max_order_amount_krw') <> 'number'
     or (settings_value->>'max_order_amount_krw')::bigint <= 0
     or jsonb_typeof(settings_value->'max_daily_loss_pct') <> 'number'
     or (settings_value->>'max_daily_loss_pct')::numeric <= 0
     or (settings_value->>'max_daily_loss_pct')::numeric >= 1
     or jsonb_typeof(settings_value->'max_daily_order_count') <> 'number'
     or (settings_value->>'max_daily_order_count')::integer <= 0
     or jsonb_typeof(settings_value->'max_position_pct') <> 'number'
     or (settings_value->>'max_position_pct')::numeric <= 0
     or (settings_value->>'max_position_pct')::numeric > 1
     or jsonb_typeof(settings_value->'max_sector_pct') <> 'number'
     or (settings_value->>'max_sector_pct')::numeric <= 0
     or (settings_value->>'max_sector_pct')::numeric > 1
     or jsonb_typeof(settings_value->'loop_interval_sec') <> 'number'
     or (settings_value->>'loop_interval_sec')::integer <= 0
     or jsonb_typeof(settings_value->'quote_freshness_sec') <> 'number'
     or (settings_value->>'quote_freshness_sec')::integer <= 0 then
    raise exception 'paper_risk_settings_invalid' using errcode = '22023';
  end if;
  if not private.jsonb_exact_keys_v1(
    signal_value,
    array[
      'action', 'confidence', 'final_score', 'order_amount_krw',
      'reason_json', 'sector', 'symbol'
    ]
  )
     or signal_value->>'symbol' <> p_candidate->>'symbol'
     or signal_value->>'action' <> p_candidate->>'side'
     or jsonb_typeof(signal_value->'final_score') <> 'number'
     or jsonb_typeof(signal_value->'confidence') <> 'number'
     or jsonb_typeof(signal_value->'order_amount_krw') <> 'number'
     or (signal_value->>'order_amount_krw')::bigint
       < (p_candidate->>'quantity')::bigint
         * (p_candidate->>'limit_price_krw')::bigint
     or nullif(btrim(signal_value->>'sector'), '') is null
     or signal_value->'reason_json' <> '{}'::jsonb then
    raise exception 'paper_risk_signal_invalid' using errcode = '22023';
  end if;
  if jsonb_typeof(p_risk_input->'provider_health') <> 'object'
     or exists (
       select 1 from jsonb_each(p_risk_input->'provider_health') as item
       where jsonb_typeof(item.value) <> 'boolean'
     )
     or jsonb_typeof(p_risk_input->'cooldown_active') <> 'boolean'
     or jsonb_typeof(p_risk_input->'duplicate_order') <> 'boolean'
     or jsonb_typeof(p_risk_input->'strategy_approved') <> 'boolean'
     or jsonb_typeof(p_risk_input->'shutdown_requested') <> 'boolean'
     or nullif(btrim(p_risk_input->>'strategy_status'), '') is null
     or p_risk_input->>'strategy_version_id'
       <> p_candidate->>'strategy_version_id' then
    raise exception 'paper_risk_flags_invalid' using errcode = '22023';
  end if;
  perform (p_risk_input->>'strategy_version_id')::uuid;
  if exists (
    select 1
    from (values
      (p_risk_input->'market_open'),
      (p_risk_input->'critical_news_risk'),
      (p_risk_input->'liquidity_ok'),
      (p_risk_input->'volatility_ok')
    ) as optional_boolean(value)
    where jsonb_typeof(value) not in ('null', 'boolean')
  ) or exists (
    select 1
    from (values
      (p_risk_input->'existing_position_pct'),
      (p_risk_input->'sector_position_pct'),
      (p_risk_input->'available_position_quantity')
    ) as optional_number(value)
    where jsonb_typeof(value) not in ('null', 'number')
  ) then
    raise exception 'paper_risk_optional_value_invalid' using errcode = '22023';
  end if;
  if jsonb_typeof(account_value) <> 'null' then
    if not private.jsonb_exact_keys_v1(
      account_value,
      array[
        'cash_krw', 'daily_loss_pct', 'daily_order_count',
        'daily_order_count_verified', 'equity_krw', 'synced', 'synced_at'
      ]
    )
       or jsonb_typeof(account_value->'synced') <> 'boolean'
       or jsonb_typeof(account_value->'cash_krw') <> 'number'
       or jsonb_typeof(account_value->'equity_krw') <> 'number'
       or jsonb_typeof(account_value->'daily_loss_pct') <> 'number'
       or jsonb_typeof(account_value->'daily_order_count') <> 'number'
       or jsonb_typeof(account_value->'daily_order_count_verified') <> 'boolean'
       or (account_value->>'synced_at')::timestamptz
         > (p_candidate->>'risk_evaluated_at')::timestamptz then
      raise exception 'paper_risk_account_state_invalid' using errcode = '22023';
    end if;
  end if;
  if jsonb_typeof(quote_value) <> 'null' then
    if not private.jsonb_exact_keys_v1(
      quote_value,
      array['as_of', 'price_krw', 'source', 'symbol']
    )
       or quote_value->>'symbol' <> p_candidate->>'symbol'
       or jsonb_typeof(quote_value->'price_krw') <> 'number'
       or (quote_value->>'price_krw')::bigint <= 0
       or nullif(btrim(quote_value->>'source'), '') is null
       or (quote_value->>'as_of')::timestamptz
         > (p_candidate->>'risk_evaluated_at')::timestamptz then
      raise exception 'paper_risk_quote_invalid' using errcode = '22023';
    end if;
  end if;
exception
  when invalid_text_representation or numeric_value_out_of_range then
    raise exception 'paper_risk_input_value_invalid' using errcode = '22023';
end;
$$;

create or replace function private.ingest_paper_bar_fixture_v1_impl(
  p_fixture jsonb,
  p_worker_id uuid,
  p_release_sha text,
  p_now timestamptz
)
returns table (
  series_id uuid,
  fixture_set_id uuid,
  batch_sequence integer,
  bar_count integer,
  fixture_sha256 text,
  idempotent boolean
)
language plpgsql
security definer
set search_path = ''
as $$
declare
  authorization_time timestamptz;
  series_id_value uuid;
  fixture_id_value uuid;
  fixture_digest text;
  existing_series private.paper_bar_series%rowtype;
  existing_fixture private.paper_bar_fixture_sets%rowtype;
  prior_fixture private.paper_bar_fixture_sets%rowtype;
  bar_value jsonb;
  bar_index integer := 0;
  first_minute_value timestamptz;
  last_minute_value timestamptz;
  observed_through_value timestamptz;
  expected_minute timestamptz;
  bar_digest text;
begin
  perform private.require_service_role();
  authorization_time := private.paper_source_clock_v1(p_now);
  if p_release_sha !~ '^[0-9a-f]{40}([0-9a-f]{24})?$'
     or not private.jsonb_exact_keys_v1(
       p_fixture,
       array[
         'bars', 'corporate_action_evidence_sha256',
         'corporate_action_status', 'dataset_version', 'effective_from',
         'effective_until', 'execution_policy_version', 'fixture_set_id',
         'fixture_sha256', 'market_calendar_evidence_sha256',
         'market_calendar_version', 'model_version', 'schema_version',
         'series_id', 'source_kind', 'symbol',
         'tick_rule_evidence_sha256', 'tick_rule_version', 'tick_size_krw',
         'volume_evidence_sha256', 'volume_source'
       ]
     )
     or jsonb_typeof(p_fixture->'schema_version') <> 'number'
     or (p_fixture->>'schema_version')::integer <> 1
     or p_fixture->>'source_kind'
       not in ('local_fixture', 'allowed_read_evidence')
     or p_fixture->>'symbol' !~ '^[0-9]{6}$'
     or p_fixture->>'fixture_sha256' !~ '^[0-9a-f]{64}$'
     or p_fixture->>'tick_rule_evidence_sha256' !~ '^[0-9a-f]{64}$'
     or p_fixture->>'volume_evidence_sha256' !~ '^[0-9a-f]{64}$'
     or p_fixture->>'corporate_action_evidence_sha256' !~ '^[0-9a-f]{64}$'
     or p_fixture->>'market_calendar_evidence_sha256' !~ '^[0-9a-f]{64}$'
     or p_fixture->>'corporate_action_status' not in ('not_required', 'adjusted')
     or jsonb_typeof(p_fixture->'tick_size_krw') <> 'number'
     or (p_fixture->>'tick_size_krw')::bigint <= 0
     or jsonb_typeof(p_fixture->'bars') <> 'array'
     or jsonb_array_length(p_fixture->'bars') = 0 then
    raise exception 'paper_fixture_schema_invalid' using errcode = '22023';
  end if;
  series_id_value := (p_fixture->>'series_id')::uuid;
  fixture_id_value := (p_fixture->>'fixture_set_id')::uuid;
  if p_worker_id::text !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$' then
    raise exception 'paper_fixture_worker_id_invalid' using errcode = '22023';
  end if;
  fixture_digest := private.sha256_jsonb_v1(p_fixture - 'fixture_sha256');
  if fixture_digest <> p_fixture->>'fixture_sha256' then
    raise exception 'paper_fixture_digest_mismatch' using errcode = '23514';
  end if;
  select * into existing_fixture
  from private.paper_bar_fixture_sets where id = fixture_id_value;
  if found then
    if existing_fixture.series_id <> series_id_value
       or existing_fixture.fixture_sha256 <> fixture_digest
       or existing_fixture.release_sha <> p_release_sha then
      raise exception 'paper_fixture_idempotency_conflict' using errcode = '23505';
    end if;
    return query select
      existing_fixture.series_id, existing_fixture.id,
      existing_fixture.batch_sequence, existing_fixture.bar_count,
      existing_fixture.fixture_sha256, true;
    return;
  end if;
  if not exists (
    select 1
    from private.paper_execution_model_registry as model
    join private.market_calendars as calendar
      on calendar.id = model.market_calendar_id
    where model.environment = 'paper'
      and model.model_version = p_fixture->>'model_version'
      and model.tick_size_evidence_sha256
        = p_fixture->>'tick_rule_evidence_sha256'
      and model.volume_model_evidence_sha256
        = p_fixture->>'volume_evidence_sha256'
      and model.corporate_action_evidence_sha256
        = p_fixture->>'corporate_action_evidence_sha256'
      and model.status = 'approved'
      and model.effective_from <= (p_fixture->>'effective_from')::timestamptz
      and model.effective_until >= (p_fixture->>'effective_until')::timestamptz
      and calendar.environment = 'paper'
      and calendar.calendar_version = p_fixture->>'market_calendar_version'
      and calendar.calendar_sha256
        = p_fixture->>'market_calendar_evidence_sha256'
      and calendar.status = 'approved'
  ) then
    raise exception 'approved_paper_fixture_model_required' using errcode = '23514';
  end if;
  select * into existing_series
  from private.paper_bar_series where id = series_id_value
  for share;
  if not found then
    insert into private.paper_bar_series (
      id, source_kind, dataset_version, symbol, model_version,
      execution_policy_version, tick_rule_version, tick_size_krw,
      tick_rule_evidence_sha256, volume_source, volume_evidence_sha256,
      corporate_action_status, corporate_action_evidence_sha256,
      market_calendar_version, market_calendar_evidence_sha256,
      effective_from, effective_until, created_release_sha, created_at
    ) values (
      series_id_value, p_fixture->>'source_kind', p_fixture->>'dataset_version',
      p_fixture->>'symbol', p_fixture->>'model_version',
      p_fixture->>'execution_policy_version', p_fixture->>'tick_rule_version',
      (p_fixture->>'tick_size_krw')::bigint,
      p_fixture->>'tick_rule_evidence_sha256', p_fixture->>'volume_source',
      p_fixture->>'volume_evidence_sha256',
      p_fixture->>'corporate_action_status',
      p_fixture->>'corporate_action_evidence_sha256',
      p_fixture->>'market_calendar_version',
      p_fixture->>'market_calendar_evidence_sha256',
      (p_fixture->>'effective_from')::timestamptz,
      (p_fixture->>'effective_until')::timestamptz,
      p_release_sha, authorization_time
    );
  elsif existing_series.source_kind <> p_fixture->>'source_kind'
     or existing_series.dataset_version <> p_fixture->>'dataset_version'
     or existing_series.symbol <> p_fixture->>'symbol'
     or existing_series.model_version <> p_fixture->>'model_version'
     or existing_series.execution_policy_version
       <> p_fixture->>'execution_policy_version'
     or existing_series.tick_rule_version <> p_fixture->>'tick_rule_version'
     or existing_series.tick_size_krw <> (p_fixture->>'tick_size_krw')::bigint
     or existing_series.tick_rule_evidence_sha256
       <> p_fixture->>'tick_rule_evidence_sha256'
     or existing_series.volume_source <> p_fixture->>'volume_source'
     or existing_series.volume_evidence_sha256
       <> p_fixture->>'volume_evidence_sha256'
     or existing_series.corporate_action_status
       <> p_fixture->>'corporate_action_status'
     or existing_series.corporate_action_evidence_sha256
       <> p_fixture->>'corporate_action_evidence_sha256'
     or existing_series.market_calendar_version
       <> p_fixture->>'market_calendar_version'
     or existing_series.market_calendar_evidence_sha256
       <> p_fixture->>'market_calendar_evidence_sha256'
     or existing_series.effective_from
       <> (p_fixture->>'effective_from')::timestamptz
     or existing_series.effective_until
       <> (p_fixture->>'effective_until')::timestamptz
     or existing_series.created_release_sha <> p_release_sha then
    raise exception 'paper_bar_series_identity_conflict' using errcode = '23505';
  end if;
  select fixture.* into prior_fixture
  from private.paper_bar_fixture_sets as fixture
  where fixture.series_id = series_id_value
  order by fixture.batch_sequence desc
  limit 1
  for update;
  for bar_value in
    select value from jsonb_array_elements(p_fixture->'bars')
  loop
    bar_index := bar_index + 1;
    if not private.jsonb_exact_keys_v1(
      bar_value,
      array[
        'as_of', 'close_krw', 'completed_at', 'high_krw', 'is_complete',
        'low_krw', 'minute', 'open_krw', 'sequence', 'source_sha256',
        'volume'
      ]
    )
       or jsonb_typeof(bar_value->'sequence') <> 'number'
       or (bar_value->>'sequence')::integer <> bar_index
       or jsonb_typeof(bar_value->'is_complete') <> 'boolean'
       or (bar_value->>'is_complete')::boolean is not true
       or bar_value->>'source_sha256'
         <> p_fixture->>'volume_evidence_sha256'
       or jsonb_typeof(bar_value->'open_krw') <> 'number'
       or jsonb_typeof(bar_value->'high_krw') <> 'number'
       or jsonb_typeof(bar_value->'low_krw') <> 'number'
       or jsonb_typeof(bar_value->'close_krw') <> 'number'
       or jsonb_typeof(bar_value->'volume') <> 'number'
       or (bar_value->>'open_krw')::bigint <= 0
       or (bar_value->>'high_krw')::bigint <= 0
       or (bar_value->>'low_krw')::bigint <= 0
       or (bar_value->>'close_krw')::bigint <= 0
       or (bar_value->>'open_krw')::bigint
         % (p_fixture->>'tick_size_krw')::bigint <> 0
       or (bar_value->>'high_krw')::bigint
         % (p_fixture->>'tick_size_krw')::bigint <> 0
       or (bar_value->>'low_krw')::bigint
         % (p_fixture->>'tick_size_krw')::bigint <> 0
       or (bar_value->>'close_krw')::bigint
         % (p_fixture->>'tick_size_krw')::bigint <> 0
       or (bar_value->>'volume')::bigint < 0 then
      raise exception 'paper_fixture_bar_invalid' using errcode = '22023';
    end if;
    if bar_index = 1 then
      first_minute_value := (bar_value->>'minute')::timestamptz;
      expected_minute := first_minute_value;
    end if;
    if (bar_value->>'minute')::timestamptz <> expected_minute
       or date_trunc('minute', (bar_value->>'minute')::timestamptz)
         <> (bar_value->>'minute')::timestamptz
       or (bar_value->>'completed_at')::timestamptz
         <> (bar_value->>'minute')::timestamptz + interval '1 minute'
       or (bar_value->>'as_of')::timestamptz
         < (bar_value->>'completed_at')::timestamptz
       or (bar_value->>'as_of')::timestamptz > authorization_time
       or (bar_value->>'high_krw')::bigint < greatest(
         (bar_value->>'open_krw')::bigint,
         (bar_value->>'low_krw')::bigint,
         (bar_value->>'close_krw')::bigint
       )
       or (bar_value->>'low_krw')::bigint > least(
         (bar_value->>'open_krw')::bigint,
         (bar_value->>'high_krw')::bigint,
         (bar_value->>'close_krw')::bigint
       ) then
      raise exception 'paper_fixture_bar_timeline_invalid' using errcode = '23514';
    end if;
    last_minute_value := (bar_value->>'minute')::timestamptz;
    observed_through_value := greatest(
      coalesce(observed_through_value, '-infinity'::timestamptz),
      (bar_value->>'as_of')::timestamptz
    );
    expected_minute := expected_minute + interval '1 minute';
  end loop;
  if first_minute_value < (p_fixture->>'effective_from')::timestamptz
     or last_minute_value + interval '1 minute'
       > (p_fixture->>'effective_until')::timestamptz
     or (
       prior_fixture.id is not null
       and first_minute_value <> prior_fixture.last_minute + interval '1 minute'
     ) then
    raise exception 'paper_fixture_series_gap_or_window_invalid' using errcode = '23514';
  end if;
  insert into private.paper_bar_fixture_sets (
    id, series_id, batch_sequence, first_minute, last_minute, bar_count,
    observed_through, fixture_sha256, evidence_urn, release_sha, ingested_at
  ) values (
    fixture_id_value, series_id_value,
    coalesce(prior_fixture.batch_sequence + 1, 1), first_minute_value,
    last_minute_value, bar_index, observed_through_value, fixture_digest,
    'urn:sha256:' || fixture_digest, p_release_sha, authorization_time
  );
  bar_index := 0;
  for bar_value in
    select value from jsonb_array_elements(p_fixture->'bars')
  loop
    bar_index := bar_index + 1;
    bar_digest := private.sha256_jsonb_v1(
      jsonb_build_object(
        'series_id', series_id_value,
        'symbol', p_fixture->>'symbol',
        'bar', bar_value
      )
    );
    insert into private.paper_minute_bars (
      fixture_set_id, series_id, sequence, minute, completed_at, as_of,
      source_sha256, is_complete, open_krw, high_krw, low_krw,
      close_krw, volume, bar_sha256
    ) values (
      fixture_id_value, series_id_value, bar_index,
      (bar_value->>'minute')::timestamptz,
      (bar_value->>'completed_at')::timestamptz,
      (bar_value->>'as_of')::timestamptz,
      bar_value->>'source_sha256', true,
      (bar_value->>'open_krw')::bigint,
      (bar_value->>'high_krw')::bigint,
      (bar_value->>'low_krw')::bigint,
      (bar_value->>'close_krw')::bigint,
      (bar_value->>'volume')::bigint,
      bar_digest
    );
  end loop;
  perform private.write_audit_event(
    'worker', null, null, 'trading_worker', p_worker_id::text, p_release_sha,
    'paper_bar_fixture_ingested', 'paper_bar_fixture_set', fixture_id_value::text,
    fixture_id_value, null, 'immutable_gapless_fixture_ingested', null,
    array['series_id', 'batch_sequence', 'bar_count', 'fixture_sha256'],
    null, fixture_digest, null
  );
  return query select
    series_id_value, fixture_id_value,
    coalesce(prior_fixture.batch_sequence + 1, 1), bar_index,
    fixture_digest, false;
exception
  when invalid_text_representation or numeric_value_out_of_range then
    raise exception 'paper_fixture_value_invalid' using errcode = '22023';
end;
$$;

create or replace function private.enqueue_paper_execution_candidate_v1_impl(
  p_candidate jsonb,
  p_worker_id uuid,
  p_fencing_token bigint,
  p_control_epoch bigint,
  p_release_sha text,
  p_now timestamptz
)
returns table (
  command_id uuid,
  intent_id uuid,
  state text,
  source_revision bigint,
  semantic_key_sha256 text,
  idempotent boolean
)
language plpgsql
security definer
set search_path = ''
as $$
declare
  authorization_time timestamptz;
  intent_id_value uuid;
  account_id_value text;
  series_id_value uuid;
  eligible_at_value timestamptz;
  semantic_key_value text;
  candidate_digest text;
  existing_candidate private.paper_execution_candidates%rowtype;
  existing_work private.paper_execution_work_items%rowtype;
  work_id_value uuid := gen_random_uuid();
  schedule_rate numeric(18,12);
  expected_cash bigint;
begin
  perform private.require_service_role();
  authorization_time := private.paper_source_clock_v1(p_now);
  if not private.jsonb_exact_keys_v1(
    p_candidate,
    array[
      'account_id', 'cash_commitment_krw', 'cost_schedule_evidence_sha256',
      'cost_schedule_version', 'decision_at', 'decision_feature_sha256',
      'decision_id', 'execution_policy_version', 'expires_at',
      'fixture_series_id', 'intent_id', 'limit_price_krw', 'quantity',
      'risk_evaluated_at', 'risk_expires_at', 'risk_input',
      'risk_result_id', 'schema_version', 'semantic_key_sha256', 'side',
      'signal_valid_from', 'signal_valid_until', 'strategy_version_id',
      'symbol'
    ]
  )
     or jsonb_typeof(p_candidate->'schema_version') <> 'number'
     or (p_candidate->>'schema_version')::integer <> 1
     or p_candidate->>'symbol' !~ '^[0-9]{6}$'
     or p_candidate->>'side' not in ('buy', 'sell')
     or p_candidate->>'decision_feature_sha256' !~ '^[0-9a-f]{64}$'
     or p_candidate->>'cost_schedule_evidence_sha256' !~ '^[0-9a-f]{64}$'
     or p_candidate->>'semantic_key_sha256' !~ '^[0-9a-f]{64}$'
     or p_release_sha !~ '^[0-9a-f]{40}([0-9a-f]{24})?$'
     or jsonb_typeof(p_candidate->'quantity') <> 'number'
     or jsonb_typeof(p_candidate->'limit_price_krw') <> 'number'
     or jsonb_typeof(p_candidate->'cash_commitment_krw') <> 'number'
     or (p_candidate->>'quantity')::bigint <= 0
     or (p_candidate->>'limit_price_krw')::bigint <= 0
     or (p_candidate->>'cash_commitment_krw')::bigint < 0 then
    raise exception 'paper_candidate_schema_invalid' using errcode = '22023';
  end if;
  intent_id_value := (p_candidate->>'intent_id')::uuid;
  perform (p_candidate->>'decision_id')::uuid;
  perform (p_candidate->>'risk_result_id')::uuid;
  perform (p_candidate->>'strategy_version_id')::uuid;
  series_id_value := (p_candidate->>'fixture_series_id')::uuid;
  account_id_value := p_candidate->>'account_id';
  eligible_at_value := date_trunc(
    'minute', (p_candidate->>'decision_at')::timestamptz
  ) + interval '1 minute';
  if (p_candidate->>'risk_evaluated_at')::timestamptz
       > (p_candidate->>'decision_at')::timestamptz
     or (p_candidate->>'decision_at')::timestamptz
       > authorization_time + interval '30 seconds'
     or (p_candidate->>'risk_expires_at')::timestamptz <= authorization_time
     or (p_candidate->>'signal_valid_from')::timestamptz
       > (p_candidate->>'decision_at')::timestamptz
     or (p_candidate->>'signal_valid_until')::timestamptz
       < (p_candidate->>'decision_at')::timestamptz
     or date_trunc('minute', (p_candidate->>'expires_at')::timestamptz)
       <> (p_candidate->>'expires_at')::timestamptz
     or (p_candidate->>'expires_at')::timestamptz < eligible_at_value
     or (p_candidate->>'expires_at')::timestamptz
       > (p_candidate->>'signal_valid_until')::timestamptz then
    raise exception 'paper_candidate_window_invalid' using errcode = '23514';
  end if;
  perform private.paper_source_authorization_v1(
    account_id_value, p_worker_id, p_release_sha,
    p_fencing_token, p_control_epoch, authorization_time
  );
  semantic_key_value := private.compute_order_semantic_key(
    account_id_value,
    'paper',
    p_candidate->>'strategy_version_id',
    p_candidate->>'symbol',
    p_candidate->>'side',
    (p_candidate->>'signal_valid_from')::timestamptz,
    (p_candidate->>'signal_valid_until')::timestamptz,
    p_candidate->>'execution_policy_version'
  );
  if semantic_key_value <> p_candidate->>'semantic_key_sha256' then
    raise exception 'paper_candidate_semantic_key_mismatch' using errcode = '23514';
  end if;
  perform private.validate_paper_risk_input_v1(
    p_candidate->'risk_input', p_candidate
  );
  if not exists (
    select 1
    from private.paper_bar_series as series
    join private.paper_execution_policies as policy
      on policy.account_id = account_id_value
     and policy.policy_version = p_candidate->>'execution_policy_version'
    join private.execution_controls as control
      on control.account_id = account_id_value
     and control.environment = 'paper'
    join private.paper_execution_model_registry as model
      on model.environment = 'paper'
     and model.model_version = series.model_version
    join private.market_calendars as calendar
      on calendar.id = model.market_calendar_id
    where series.id = series_id_value
      and series.environment = 'paper'
      and series.symbol = p_candidate->>'symbol'
      and series.execution_policy_version
        = p_candidate->>'execution_policy_version'
      and series.effective_from <= eligible_at_value
      and series.effective_until >= (p_candidate->>'expires_at')::timestamptz
      and series.created_release_sha = p_release_sha
      and policy.status = 'approved'
      and policy.policy_sha256 = control.execution_policy_sha256
      and policy.effective_from <= authorization_time
      and policy.effective_until > authorization_time
      and policy.parameters->>'execution_model_version' = series.model_version
      and policy.parameters->>'tick_size_evidence_sha256'
        = series.tick_rule_evidence_sha256
      and policy.parameters->>'volume_model_evidence_sha256'
        = series.volume_evidence_sha256
      and policy.parameters->>'corporate_action_evidence_sha256'
        = series.corporate_action_evidence_sha256
      and policy.parameters->>'market_calendar_version'
        = series.market_calendar_version
      and policy.parameters->>'market_calendar_sha256'
        = series.market_calendar_evidence_sha256
      and model.status = 'approved'
      and model.tick_size_evidence_sha256 = series.tick_rule_evidence_sha256
      and model.volume_model_evidence_sha256 = series.volume_evidence_sha256
      and model.corporate_action_evidence_sha256
        = series.corporate_action_evidence_sha256
      and model.effective_from <= eligible_at_value
      and model.effective_until >= (p_candidate->>'expires_at')::timestamptz
      and calendar.environment = 'paper'
      and calendar.status = 'approved'
      and calendar.calendar_version = series.market_calendar_version
      and calendar.calendar_sha256 = series.market_calendar_evidence_sha256
      and calendar.valid_from <= eligible_at_value::date
      and calendar.valid_until >= (p_candidate->>'expires_at')::date
      and exists (
        select 1
        from private.paper_minute_bars as first_bar
        where first_bar.series_id = series.id
          and first_bar.minute <= eligible_at_value
      )
  ) then
    raise exception 'paper_candidate_approved_evidence_bundle_required'
      using errcode = '23514';
  end if;
  select schedule.buy_commission_rate
  into schedule_rate
  from private.execution_cost_schedules as schedule
  join private.control_evidence as evidence on evidence.id = schedule.evidence_id
  where schedule.account_id = account_id_value
    and schedule.schedule_version = p_candidate->>'cost_schedule_version'
    and evidence.artifact_sha256
      = p_candidate->>'cost_schedule_evidence_sha256'
    and schedule.status = 'approved'
    and schedule.effective_from <= eligible_at_value
    and schedule.effective_until >= (p_candidate->>'expires_at')::timestamptz
  order by schedule.effective_from desc
  limit 1;
  if schedule_rate is null then
    raise exception 'paper_candidate_approved_cost_schedule_required'
      using errcode = '23514';
  end if;
  expected_cash := case
    when p_candidate->>'side' = 'buy' then
      (p_candidate->>'quantity')::bigint
        * (p_candidate->>'limit_price_krw')::bigint
      + ceil(
        (p_candidate->>'quantity')::bigint
          * (p_candidate->>'limit_price_krw')::bigint
          * schedule_rate
      )::bigint
    else 0
  end;
  if expected_cash <> (p_candidate->>'cash_commitment_krw')::bigint then
    raise exception 'paper_candidate_cash_commitment_mismatch'
      using errcode = '23514';
  end if;
  candidate_digest := private.sha256_jsonb_v1(
    jsonb_build_object('candidate', p_candidate, 'release_sha', p_release_sha)
  );
  perform pg_catalog.pg_advisory_xact_lock(
    pg_catalog.hashtextextended(semantic_key_value, 921374611::bigint)
  );
  select candidate.* into existing_candidate
  from private.paper_execution_candidates as candidate
  where candidate.account_id = account_id_value
    and candidate.environment = 'paper'
    and candidate.semantic_key_sha256 = semantic_key_value;
  if found then
    select * into existing_work
    from private.paper_execution_work_items
    where private.paper_execution_work_items.intent_id = existing_candidate.intent_id;
    if existing_candidate.intent_id <> intent_id_value
       or existing_candidate.candidate_sha256 <> candidate_digest
       or existing_candidate.source_release_sha <> p_release_sha
       or existing_work.id is null then
      raise exception 'paper_candidate_idempotency_conflict' using errcode = '23505';
    end if;
    return query select
      existing_work.id, existing_candidate.intent_id, existing_work.state,
      existing_work.revision, existing_candidate.semantic_key_sha256, true;
    return;
  end if;
  insert into private.paper_execution_candidates (
    intent_id, account_id, semantic_key_sha256, decision_id, risk_result_id,
    decision_feature_sha256, risk_evaluated_at, risk_expires_at,
    strategy_version_id, symbol, side, quantity, limit_price_krw,
    cash_commitment_krw, decision_at, signal_valid_from, signal_valid_until,
    eligible_at, expires_at, execution_policy_version,
    cost_schedule_version, cost_schedule_evidence_sha256,
    fixture_series_id, risk_input, candidate_sha256, source_release_sha,
    created_at
  ) values (
    intent_id_value, account_id_value, semantic_key_value,
    (p_candidate->>'decision_id')::uuid,
    (p_candidate->>'risk_result_id')::uuid,
    p_candidate->>'decision_feature_sha256',
    (p_candidate->>'risk_evaluated_at')::timestamptz,
    (p_candidate->>'risk_expires_at')::timestamptz,
    (p_candidate->>'strategy_version_id')::uuid,
    p_candidate->>'symbol', p_candidate->>'side',
    (p_candidate->>'quantity')::bigint,
    (p_candidate->>'limit_price_krw')::bigint,
    (p_candidate->>'cash_commitment_krw')::bigint,
    (p_candidate->>'decision_at')::timestamptz,
    (p_candidate->>'signal_valid_from')::timestamptz,
    (p_candidate->>'signal_valid_until')::timestamptz,
    eligible_at_value, (p_candidate->>'expires_at')::timestamptz,
    p_candidate->>'execution_policy_version',
    p_candidate->>'cost_schedule_version',
    p_candidate->>'cost_schedule_evidence_sha256', series_id_value,
    p_candidate->'risk_input', candidate_digest, p_release_sha,
    authorization_time
  );
  insert into private.paper_execution_work_items (
    id, intent_id, account_id, kind, state, revision, available_at,
    last_reason_code, created_at, updated_at
  ) values (
    work_id_value, intent_id_value, account_id_value, 'new_candidate',
    'pending', 1, eligible_at_value, 'paper_candidate_enqueued',
    authorization_time, authorization_time
  );
  insert into private.paper_execution_work_events (
    work_item_id, revision, event_type, worker_id, reason_code, occurred_at
  ) values (
    work_id_value, 1, 'enqueued', p_worker_id,
    'paper_candidate_enqueued', authorization_time
  );
  perform private.write_audit_event(
    'worker', null, null, 'trading_worker', p_worker_id::text, p_release_sha,
    'paper_execution_candidate_enqueued', 'paper_execution_work_item',
    work_id_value::text, intent_id_value, null, 'paper_candidate_enqueued', null,
    array['semantic_key_sha256', 'fixture_series_id', 'available_at'],
    null, candidate_digest, null
  );
  return query select
    work_id_value, intent_id_value, 'pending'::text, 1::bigint,
    semantic_key_value, false;
exception
  when invalid_text_representation or numeric_value_out_of_range then
    raise exception 'paper_candidate_value_invalid' using errcode = '22023';
end;
$$;

create or replace function private.claim_paper_execution_v1_impl(
  p_account_id text,
  p_worker_id uuid,
  p_release_sha text,
  p_now timestamptz,
  p_lease_seconds integer
)
returns table (
  command_id uuid,
  intent_id uuid,
  kind text,
  claim_token uuid,
  source_revision bigint,
  worker_id uuid,
  release_sha text,
  available_at timestamptz,
  claimed_at timestamptz,
  claim_expires_at timestamptz
)
language plpgsql
security definer
set search_path = ''
as $$
declare
  authorization_time timestamptz;
  control_epoch_value bigint;
  fencing_token_value bigint;
  authorization_expires_at timestamptz;
  work_row private.paper_execution_work_items%rowtype;
  candidate_row private.paper_execution_candidates%rowtype;
  expired_work private.paper_execution_work_items%rowtype;
  terminal_work private.paper_execution_work_items%rowtype;
  expired_reason text;
  token_value uuid := gen_random_uuid();
  new_revision bigint;
  was_reclaim boolean;
begin
  perform private.require_service_role();
  authorization_time := private.paper_source_clock_v1(p_now);
  if p_release_sha !~ '^[0-9a-f]{40}([0-9a-f]{24})?$'
     or p_lease_seconds < 5 or p_lease_seconds > 300 then
    raise exception 'paper_execution_claim_values_invalid' using errcode = '22023';
  end if;
  select
    control.control_epoch,
    lease.fencing_token,
    least(control.expires_at, lease.expires_at, qualification.valid_until)
  into control_epoch_value, fencing_token_value, authorization_expires_at
  from private.execution_controls as control
  join private.worker_leases as lease on lease.account_id = control.account_id
  join private.operation_commands as command on command.id = control.last_command_id
  join private.qualifications as qualification
    on qualification.id::text = command.requested_change->>'qualification_id'
  where control.account_id = p_account_id
    and control.environment = 'paper'
    and control.execution_enabled is true
    and control.effective_at <= authorization_time
    and control.expires_at > authorization_time
    and lease.holder_id = p_worker_id::text
    and lease.release_sha = p_release_sha
    and lease.expires_at > authorization_time
    and command.state = 'applied'
    and command.target_release_sha = p_release_sha
    and qualification.checkpoint_schema_version = 1
    and qualification.account_id = p_account_id
    and qualification.environment = 'paper'
    and qualification.status = 'qualified'
    and qualification.release_sha = p_release_sha
    and qualification.valid_from <= authorization_time
    and qualification.valid_until > authorization_time;
  if control_epoch_value is null or fencing_token_value is null then
    raise exception 'paper_source_gate_lease_or_qualification_stale'
      using errcode = '40001';
  end if;
  perform private.paper_source_authorization_v1(
    p_account_id, p_worker_id, p_release_sha, fencing_token_value,
    control_epoch_value, authorization_time
  );

  -- A terminal ledger observation is authoritative.  A pending simulator
  -- resume must converge instead of being dispatched again after recovery.
  select work.* into terminal_work
  from private.paper_execution_work_items as work
  join private.paper_execution_candidates as candidate
    on candidate.intent_id = work.intent_id
  join lateral (
    select observation.event_type
    from private.execution_observations as observation
    where observation.intent_id = work.intent_id
    order by observation.sequence desc
    limit 1
  ) as latest_observation on true
  where work.account_id = p_account_id
    and work.state = 'pending'
    and work.kind = 'resume_existing'
    and candidate.source_release_sha = p_release_sha
    and latest_observation.event_type in (
      'filled', 'canceled', 'expired', 'rejected', 'failed_pre_dispatch'
    )
  order by work.created_at, work.id
  for update of work skip locked
  limit 1;
  if found then
    update private.paper_execution_work_items
    set state = 'complete', revision = revision + 1,
        last_reason_code = 'paper_execution_terminal_observation_converged',
        completed_at = authorization_time, updated_at = authorization_time
    where id = terminal_work.id
    returning * into terminal_work;
    insert into private.paper_execution_work_events (
      work_item_id, revision, event_type, reason_code, occurred_at
    ) values (
      terminal_work.id, terminal_work.revision, 'completed',
      'paper_execution_terminal_observation_converged', authorization_time
    );
    perform private.write_audit_event(
      'system', null, null, 'paper_execution_source', null, p_release_sha,
      'paper_execution_source_completed', 'paper_execution_work_item',
      terminal_work.id::text, terminal_work.intent_id, null,
      'paper_execution_terminal_observation_converged', null,
      array['state', 'revision', 'completed_at'], null, null, null
    );
  end if;

  -- A never-reserved candidate whose risk evidence or execution window
  -- expired is terminal for this source.  It is never silently retried with
  -- refreshed defaults.
  select work.* into expired_work
  from private.paper_execution_work_items as work
  join private.paper_execution_candidates as candidate
    on candidate.intent_id = work.intent_id
  where work.account_id = p_account_id
    and work.state = 'pending'
    and work.kind = 'new_candidate'
    and candidate.source_release_sha = p_release_sha
    and (
      candidate.risk_expires_at <= authorization_time
      or candidate.expires_at < authorization_time
    )
  order by work.created_at, work.id
  for update of work skip locked
  limit 1;
  if found then
    select case
      when candidate.risk_expires_at <= authorization_time
        then 'paper_candidate_risk_evidence_expired'
      else 'paper_candidate_execution_window_expired'
    end into expired_reason
    from private.paper_execution_candidates as candidate
    where candidate.intent_id = expired_work.intent_id;
    update private.paper_execution_work_items
    set state = 'manual', revision = revision + 1,
        last_reason_code = expired_reason,
        completed_at = authorization_time, updated_at = authorization_time
    where id = expired_work.id
    returning * into expired_work;
    insert into private.paper_execution_work_events (
      work_item_id, revision, event_type, reason_code, occurred_at
    ) values (
      expired_work.id, expired_work.revision, 'manual',
      expired_reason, authorization_time
    );
    insert into private.incidents (
      severity, incident_type, summary_code, correlation_id, opened_at
    ) values (
      'high', 'paper_execution_source_manual_required',
      expired_reason, expired_work.intent_id,
      authorization_time
    );
    update private.execution_controls
    set execution_enabled = false,
        control_epoch = control_epoch + 1,
        effective_at = greatest(
          authorization_time, updated_at + interval '1 microsecond'
        ),
        expires_at = greatest(
          expires_at,
          greatest(authorization_time, updated_at + interval '1 microsecond')
            + interval '1 microsecond'
        ),
        updated_reason_code = 'paper_execution_source_manual_required',
        updated_at = greatest(
          authorization_time, updated_at + interval '1 microsecond'
        )
    where account_id = expired_work.account_id
      and execution_enabled is true
      and control_epoch = control_epoch_value;
    perform private.write_audit_event(
      'system', null, null, 'paper_execution_source', null, p_release_sha,
      'paper_execution_source_manual_required', 'paper_execution_work_item',
      expired_work.id::text, expired_work.intent_id, null,
      expired_reason, null,
      array['state', 'revision', 'completed_at'], null, null, null
    );
    return;
  end if;

  select work.* into work_row
  from private.paper_execution_work_items as work
  join private.paper_execution_candidates as candidate
    on candidate.intent_id = work.intent_id
  where work.account_id = p_account_id
    and candidate.source_release_sha = p_release_sha
    and work.available_at <= authorization_time
    and (
      work.state = 'pending'
      or (
        work.state = 'claimed'
        and work.claim_expires_at <= authorization_time
      )
    )
    and (
      work.kind = 'resume_existing'
      or candidate.risk_expires_at > authorization_time
    )
    and candidate.expires_at >= authorization_time
  order by work.available_at, work.created_at, work.id
  for update of work skip locked
  limit 1;
  if not found then
    return;
  end if;
  select * into candidate_row
  from private.paper_execution_candidates
  where private.paper_execution_candidates.intent_id = work_row.intent_id;
  was_reclaim := work_row.state = 'claimed';
  new_revision := work_row.revision + 1;
  authorization_expires_at := least(
    authorization_expires_at,
    authorization_time + make_interval(secs => p_lease_seconds)
  );
  if authorization_expires_at <= authorization_time then
    raise exception 'paper_execution_claim_window_expired' using errcode = '40001';
  end if;
  update private.paper_execution_work_items
  set state = 'claimed', revision = new_revision,
      claim_token = token_value, claim_worker_id = p_worker_id,
      claim_release_sha = p_release_sha,
      claim_fencing_token = fencing_token_value,
      claim_control_epoch = control_epoch_value,
      claimed_at = authorization_time,
      claim_expires_at = authorization_expires_at,
      attempt_count = attempt_count + 1,
      last_reason_code = case when was_reclaim
        then 'paper_execution_claim_reclaimed'
        else 'paper_execution_claimed' end,
      updated_at = authorization_time
  where id = work_row.id
  returning * into work_row;
  insert into private.paper_execution_work_events (
    work_item_id, revision, event_type, worker_id, claim_token,
    reason_code, occurred_at
  ) values (
    work_row.id, work_row.revision,
    case when was_reclaim then 'reclaimed' else 'claimed' end,
    p_worker_id, token_value,
    case when was_reclaim
      then 'paper_execution_claim_reclaimed'
      else 'paper_execution_claimed' end,
    authorization_time
  );
  return query select
    work_row.id, work_row.intent_id, work_row.kind, token_value,
    work_row.revision, p_worker_id, p_release_sha, work_row.available_at,
    authorization_time, authorization_expires_at;
end;
$$;

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
  authorization_time timestamptz;
  work_row private.paper_execution_work_items%rowtype;
  candidate_row private.paper_execution_candidates%rowtype;
  intent_row private.order_intents%rowtype;
  series_row private.paper_bar_series%rowtype;
  schedule_row private.execution_cost_schedules%rowtype;
  schedule_id_value uuid;
  schedule_evidence_sha text;
  calendar_row private.market_calendars%rowtype;
  bars_value jsonb;
  sessions_value jsonb;
  position_value jsonb;
  intent_value jsonb;
  command_value jsonb;
  dispatch_time timestamptz;
  intent_gate_epoch bigint;
  intent_cash_commitment bigint;
begin
  perform private.require_service_role();
  authorization_time := private.paper_source_clock_v1(p_now);
  if p_expected_revision is null or p_expected_revision <= 0
     or p_release_sha !~ '^[0-9a-f]{40}([0-9a-f]{24})?$' then
    raise exception 'paper_execution_load_values_invalid' using errcode = '22023';
  end if;
  select * into work_row
  from private.paper_execution_work_items
  where id = p_command_id
  for share;
  if not found
     or work_row.intent_id <> p_intent_id
     or work_row.state <> 'claimed'
     or work_row.revision <> p_expected_revision
     or work_row.claim_token <> p_claim_token
     or work_row.claim_worker_id <> p_worker_id
     or work_row.claim_release_sha <> p_release_sha
     or work_row.claim_expires_at <= authorization_time then
    raise exception 'paper_execution_claim_stale_or_mismatched'
      using errcode = '40001';
  end if;
  perform private.paper_source_authorization_v1(
    work_row.account_id, p_worker_id, p_release_sha,
    work_row.claim_fencing_token, work_row.claim_control_epoch,
    authorization_time
  );
  select * into candidate_row
  from private.paper_execution_candidates
  where private.paper_execution_candidates.intent_id = work_row.intent_id;
  select * into series_row
  from private.paper_bar_series where id = candidate_row.fixture_series_id;
  if candidate_row.source_release_sha <> p_release_sha
     or series_row.created_release_sha <> p_release_sha
     or series_row.symbol <> candidate_row.symbol
     or series_row.execution_policy_version
       <> candidate_row.execution_policy_version
     or series_row.effective_from > candidate_row.eligible_at
     or series_row.effective_until < candidate_row.expires_at then
    raise exception 'paper_execution_source_evidence_identity_mismatch'
      using errcode = '23514';
  end if;
  select * into intent_row
  from private.order_intents
  where id = candidate_row.intent_id;
  if work_row.kind = 'resume_existing' then
    if intent_row.id is null
       or intent_row.account_id <> candidate_row.account_id
       or intent_row.environment <> 'paper'
       or intent_row.semantic_key_sha256 <> candidate_row.semantic_key_sha256
       or intent_row.release_sha <> p_release_sha
       or intent_row.control_epoch <> work_row.claim_control_epoch then
      raise exception 'paper_execution_resume_intent_mismatch'
        using errcode = '40001';
    end if;
    intent_gate_epoch := intent_row.control_epoch;
    intent_cash_commitment := intent_row.cash_commitment_krw;
    dispatch_time := authorization_time;
  else
    if intent_row.id is not null and (
      intent_row.account_id <> candidate_row.account_id
      or intent_row.environment <> 'paper'
      or intent_row.semantic_key_sha256 <> candidate_row.semantic_key_sha256
      or intent_row.release_sha <> p_release_sha
      or intent_row.control_epoch <> work_row.claim_control_epoch
    ) then
      raise exception 'paper_execution_replay_intent_mismatch'
        using errcode = '40001';
    end if;
    if candidate_row.risk_expires_at <= authorization_time then
      raise exception 'paper_execution_candidate_risk_expired'
        using errcode = '40001';
    end if;
    intent_gate_epoch := work_row.claim_control_epoch;
    intent_cash_commitment := candidate_row.cash_commitment_krw;
    dispatch_time := case
      when intent_row.id is null then candidate_row.eligible_at
      else authorization_time
    end;
  end if;
  if dispatch_time < candidate_row.eligible_at
     or dispatch_time > candidate_row.expires_at then
    raise exception 'paper_execution_dispatch_window_invalid'
      using errcode = '23514';
  end if;
  select schedule.id, evidence.artifact_sha256
  into schedule_id_value, schedule_evidence_sha
  from private.execution_cost_schedules as schedule
  join private.control_evidence as evidence on evidence.id = schedule.evidence_id
  where schedule.account_id = candidate_row.account_id
    and schedule.schedule_version = candidate_row.cost_schedule_version
    and evidence.artifact_sha256 = candidate_row.cost_schedule_evidence_sha256
    and schedule.effective_from <= candidate_row.eligible_at
    and schedule.effective_until >= candidate_row.expires_at
  order by schedule.effective_from desc
  limit 1;
  if schedule_id_value is null then
    raise exception 'paper_execution_cost_schedule_missing'
      using errcode = '23514';
  end if;
  select * into schedule_row
  from private.execution_cost_schedules where id = schedule_id_value;
  select calendar.* into calendar_row
  from private.paper_execution_model_registry as model
  join private.market_calendars as calendar on calendar.id = model.market_calendar_id
  where model.environment = 'paper'
    and model.model_version = series_row.model_version
    and model.tick_size_evidence_sha256 = series_row.tick_rule_evidence_sha256
    and model.volume_model_evidence_sha256 = series_row.volume_evidence_sha256
    and model.corporate_action_evidence_sha256
      = series_row.corporate_action_evidence_sha256
    and calendar.environment = 'paper'
    and calendar.calendar_version = series_row.market_calendar_version
    and calendar.calendar_sha256 = series_row.market_calendar_evidence_sha256
  limit 1;
  if calendar_row.id is null then
    raise exception 'paper_execution_calendar_evidence_missing'
      using errcode = '23514';
  end if;
  select coalesce(
    jsonb_agg(
      jsonb_build_object(
        'symbol', candidate_row.symbol,
        'minute', bar.minute,
        'completed_at', bar.completed_at,
        'as_of', bar.as_of,
        'source_sha256', bar.source_sha256,
        'is_complete', bar.is_complete,
        'open_krw', bar.open_krw,
        'high_krw', bar.high_krw,
        'low_krw', bar.low_krw,
        'close_krw', bar.close_krw,
        'volume', bar.volume
      ) order by bar.minute
    ),
    '[]'::jsonb
  ) into bars_value
  from private.paper_minute_bars as bar
  where bar.series_id = series_row.id
    and bar.minute >= candidate_row.eligible_at
    and bar.minute <= candidate_row.expires_at
    and bar.completed_at <= authorization_time
    and bar.as_of <= authorization_time;
  if exists (
    select 1
    from (
      select
        bar.minute,
        lag(bar.minute) over (order by bar.minute) as prior_minute
      from private.paper_minute_bars as bar
      where bar.series_id = series_row.id
        and bar.minute >= candidate_row.eligible_at
        and bar.minute <= candidate_row.expires_at
        and bar.completed_at <= authorization_time
        and bar.as_of <= authorization_time
    ) as ordered_bar
    where prior_minute is not null
      and minute <> prior_minute + interval '1 minute'
  ) then
    raise exception 'paper_execution_bar_history_has_gap' using errcode = '23514';
  end if;
  select coalesce(
    jsonb_agg(to_char(session.session_date, 'YYYY-MM-DD') order by session.session_date),
    '[]'::jsonb
  ) into sessions_value
  from private.market_calendar_sessions as session
  where session.calendar_id = calendar_row.id
    and session.is_open is true;
  if jsonb_array_length(sessions_value) = 0 then
    raise exception 'paper_execution_open_sessions_missing' using errcode = '23514';
  end if;
  if candidate_row.side = 'sell' then
    if work_row.kind = 'resume_existing' or intent_row.id is not null then
      if intent_row.position_cost_basis_method
           <> 'moving_weighted_average_v1'
         or intent_row.position_quantity_snapshot < candidate_row.quantity
         or intent_row.position_total_cost_krw <= 0
         or intent_row.position_cost_basis_sha256 is null then
        raise exception 'paper_execution_sell_pinned_cost_basis_missing'
          using errcode = '23514';
      end if;
      position_value := jsonb_build_object(
        'symbol', candidate_row.symbol,
        'quantity', intent_row.position_quantity_snapshot,
        'total_cost_krw', intent_row.position_total_cost_krw,
        'accounting_method', intent_row.position_cost_basis_method
      );
    else
      select jsonb_build_object(
        'symbol', position.symbol,
        'quantity', position.quantity,
        'total_cost_krw', round(
          position.quantity * position.average_cost_krw
        )::bigint,
        'accounting_method', 'moving_weighted_average_v1'
      ) into position_value
      from private.position_projection as position
      where position.account_id = candidate_row.account_id
        and position.symbol = candidate_row.symbol
        and position.available_quantity >= candidate_row.quantity
        and position.quantity > 0
        and position.average_cost_krw > 0;
      if position_value is null then
        raise exception 'paper_execution_sell_cost_basis_unavailable'
          using errcode = '23514';
      end if;
    end if;
  else
    position_value := 'null'::jsonb;
  end if;
  intent_value := jsonb_build_object(
    'id', candidate_row.intent_id,
    'decision_id', candidate_row.decision_id,
    'risk_result_id', candidate_row.risk_result_id,
    'decision_feature_sha256', candidate_row.decision_feature_sha256,
    'risk_allowed', true,
    'risk_reason_codes', '[]'::jsonb,
    'risk_evaluated_at', candidate_row.risk_evaluated_at,
    'risk_expires_at', candidate_row.risk_expires_at,
    'semantic_key', candidate_row.semantic_key_sha256,
    'account_id', candidate_row.account_id,
    'environment', 'paper',
    'strategy_version_id', candidate_row.strategy_version_id::text,
    'symbol', candidate_row.symbol,
    'side', candidate_row.side,
    'quantity', candidate_row.quantity,
    'limit_price_krw', candidate_row.limit_price_krw,
    'decision_at', candidate_row.decision_at,
    'signal_valid_from', candidate_row.signal_valid_from,
    'signal_valid_until', candidate_row.signal_valid_until,
    'execution_policy_version', candidate_row.execution_policy_version,
    'cost_schedule_version', candidate_row.cost_schedule_version,
    'cost_schedule_evidence_sha256', candidate_row.cost_schedule_evidence_sha256,
    'cash_commitment_krw', intent_cash_commitment,
    'eligible_at', candidate_row.eligible_at,
    'expires_at', candidate_row.expires_at,
    'gate_epoch', intent_gate_epoch,
    'lease_holder_id', p_worker_id::text,
    'lease_fencing_token', work_row.claim_fencing_token,
    'time_in_force', 'DAY'
  );
  command_value := jsonb_build_object(
    'intent', intent_value,
    'bars', bars_value,
    'cost_schedule', jsonb_build_object(
      'version', schedule_row.schedule_version,
      'effective_from', schedule_row.effective_from,
      'effective_until', schedule_row.effective_until,
      'evidence_sha256', schedule_evidence_sha,
      'settlement_days', schedule_row.settlement_days,
      'settlement_evidence_sha256', schedule_evidence_sha,
      'buy_commission_rate', schedule_row.buy_commission_rate::text,
      'sell_commission_rate', schedule_row.sell_commission_rate::text,
      'sell_tax_rate', schedule_row.sell_tax_rate::text
    ),
    'execution_evidence', jsonb_build_object(
      'version', series_row.model_version,
      'execution_policy_version', series_row.execution_policy_version,
      'effective_from', series_row.effective_from,
      'effective_until', series_row.effective_until,
      'tick_rule_version', series_row.tick_rule_version,
      'tick_size_krw', series_row.tick_size_krw,
      'tick_rule_evidence_sha256', series_row.tick_rule_evidence_sha256,
      'volume_source', series_row.volume_source,
      'volume_unit', 'shares',
      'volume_evidence_sha256', series_row.volume_evidence_sha256,
      'corporate_action_status', series_row.corporate_action_status,
      'corporate_action_evidence_sha256',
        series_row.corporate_action_evidence_sha256,
      'market_calendar_version', series_row.market_calendar_version,
      'market_calendar_status', 'open_sessions_verified',
      'market_calendar_evidence_sha256',
        series_row.market_calendar_evidence_sha256,
      'open_session_dates', sessions_value
    ),
    'position_cost_basis', position_value,
    'dispatch_at', dispatch_time,
    'evaluated_at', authorization_time
  );
  return query select jsonb_build_object(
    'schema_version', 1,
    'command', command_value,
    'risk_input', case
      when work_row.kind = 'new_candidate' then candidate_row.risk_input
      else 'null'::jsonb
    end
  );
end;
$$;

create or replace function private.complete_paper_execution_source_v1_impl(
  p_command_id uuid,
  p_claim_token uuid,
  p_expected_revision bigint,
  p_worker_id uuid,
  p_release_sha text,
  p_now timestamptz,
  p_outcome text,
  p_next_available_at timestamptz,
  p_reason_code text
)
returns table (
  command_id uuid,
  state text,
  source_revision bigint,
  next_available_at timestamptz
)
language plpgsql
security definer
set search_path = ''
as $$
declare
  authorization_time timestamptz;
  work_row private.paper_execution_work_items%rowtype;
  candidate_row private.paper_execution_candidates%rowtype;
  next_state text;
  next_kind text;
  new_revision bigint;
  claimed_control_epoch bigint;
begin
  perform private.require_service_role();
  authorization_time := private.paper_source_clock_v1(p_now);
  if p_expected_revision is null or p_expected_revision <= 0
     or p_release_sha !~ '^[0-9a-f]{40}([0-9a-f]{24})?$'
     or p_outcome not in ('complete', 'reschedule', 'manual')
     or p_reason_code !~ '^[a-z0-9][a-z0-9_]{2,119}$'
     or (p_outcome = 'reschedule') <> (p_next_available_at is not null) then
    raise exception 'paper_execution_completion_values_invalid'
      using errcode = '22023';
  end if;
  select * into work_row
  from private.paper_execution_work_items
  where id = p_command_id
  for update;
  if not found
     or work_row.state <> 'claimed'
     or work_row.revision <> p_expected_revision
     or work_row.claim_token <> p_claim_token
     or work_row.claim_worker_id <> p_worker_id
     or work_row.claim_release_sha <> p_release_sha
     or work_row.claim_expires_at <= authorization_time then
    raise exception 'paper_execution_completion_claim_stale_or_mismatched'
      using errcode = '40001';
  end if;
  claimed_control_epoch := work_row.claim_control_epoch;
  perform private.paper_source_authorization_v1(
    work_row.account_id, p_worker_id, p_release_sha,
    work_row.claim_fencing_token, claimed_control_epoch,
    authorization_time
  );
  select * into candidate_row
  from private.paper_execution_candidates
  where private.paper_execution_candidates.intent_id = work_row.intent_id;
  if p_outcome = 'reschedule' then
    if p_next_available_at <= authorization_time
       or date_trunc('minute', p_next_available_at) <> p_next_available_at
       or p_next_available_at > candidate_row.expires_at
       or not exists (
         select 1
         from private.order_intents as intent
         join private.order_attempts as attempt on attempt.intent_id = intent.id
         where intent.id = work_row.intent_id
           and intent.account_id = work_row.account_id
           and intent.environment = 'paper'
           and intent.control_epoch = claimed_control_epoch
           and intent.release_sha = p_release_sha
       ) then
      raise exception 'paper_execution_reschedule_requires_reserved_attempt'
        using errcode = '23514';
    end if;
    next_state := 'pending';
    next_kind := 'resume_existing';
  elsif p_outcome = 'complete' then
    next_state := 'complete';
    next_kind := work_row.kind;
  else
    next_state := 'manual';
    next_kind := work_row.kind;
  end if;
  new_revision := work_row.revision + 1;
  update private.paper_execution_work_items
  set state = next_state,
      kind = next_kind,
      revision = new_revision,
      available_at = coalesce(p_next_available_at, available_at),
      claim_token = null,
      claim_worker_id = null,
      claim_release_sha = null,
      claim_fencing_token = null,
      claim_control_epoch = null,
      claimed_at = null,
      claim_expires_at = null,
      last_reason_code = p_reason_code,
      completed_at = case
        when next_state in ('complete', 'manual') then authorization_time
        else null end,
      updated_at = authorization_time
  where id = work_row.id
  returning * into work_row;
  insert into private.paper_execution_work_events (
    work_item_id, revision, event_type, worker_id, claim_token,
    reason_code, occurred_at
  ) values (
    work_row.id, work_row.revision,
    case p_outcome
      when 'complete' then 'completed'
      when 'reschedule' then 'rescheduled'
      else 'manual'
    end,
    p_worker_id, p_claim_token, p_reason_code, authorization_time
  );
  if p_outcome = 'manual' then
    update private.execution_controls
    set execution_enabled = false,
        control_epoch = control_epoch + 1,
        effective_at = greatest(
          authorization_time, updated_at + interval '1 microsecond'
        ),
        expires_at = greatest(
          expires_at,
          greatest(authorization_time, updated_at + interval '1 microsecond')
            + interval '1 microsecond'
        ),
        updated_reason_code = 'paper_execution_source_manual_required',
        updated_at = greatest(
          authorization_time, updated_at + interval '1 microsecond'
        )
    where account_id = work_row.account_id
      and execution_enabled is true
      and control_epoch = claimed_control_epoch;
    insert into private.incidents (
      severity, incident_type, summary_code, correlation_id, opened_at
    ) values (
      'high', 'paper_execution_source_manual_required', p_reason_code,
      work_row.intent_id, authorization_time
    );
  end if;
  perform private.write_audit_event(
    'worker', null, null, 'trading_worker', p_worker_id::text, p_release_sha,
    case p_outcome
      when 'complete' then 'paper_execution_source_completed'
      when 'reschedule' then 'paper_execution_source_rescheduled'
      else 'paper_execution_source_manual_required'
    end,
    'paper_execution_work_item', work_row.id::text, work_row.intent_id,
    null, p_reason_code, null,
    array['state', 'kind', 'revision', 'available_at'], null, null, null
  );
  return query select
    work_row.id, work_row.state, work_row.revision,
    case when work_row.state = 'pending' then work_row.available_at else null end;
end;
$$;

create or replace function worker_api.ingest_paper_bar_fixture_v1(
  p_fixture jsonb,
  p_worker_id uuid,
  p_release_sha text,
  p_now timestamptz
)
returns table (
  series_id uuid,
  fixture_set_id uuid,
  batch_sequence integer,
  bar_count integer,
  fixture_sha256 text,
  idempotent boolean
)
language sql
security invoker
set search_path = ''
as $$
  select * from private.ingest_paper_bar_fixture_v1_impl(
    p_fixture, p_worker_id, p_release_sha, p_now
  );
$$;

create or replace function worker_api.enqueue_paper_execution_candidate_v1(
  p_candidate jsonb,
  p_worker_id uuid,
  p_fencing_token bigint,
  p_control_epoch bigint,
  p_release_sha text,
  p_now timestamptz
)
returns table (
  command_id uuid,
  intent_id uuid,
  state text,
  source_revision bigint,
  semantic_key_sha256 text,
  idempotent boolean
)
language sql
security invoker
set search_path = ''
as $$
  select * from private.enqueue_paper_execution_candidate_v1_impl(
    p_candidate, p_worker_id, p_fencing_token, p_control_epoch,
    p_release_sha, p_now
  );
$$;

create or replace function worker_api.claim_paper_execution_v1(
  p_account_id text,
  p_worker_id uuid,
  p_release_sha text,
  p_now timestamptz,
  p_lease_seconds integer
)
returns table (
  command_id uuid,
  intent_id uuid,
  kind text,
  claim_token uuid,
  source_revision bigint,
  worker_id uuid,
  release_sha text,
  available_at timestamptz,
  claimed_at timestamptz,
  claim_expires_at timestamptz
)
language sql
security invoker
set search_path = ''
as $$
  select * from private.claim_paper_execution_v1_impl(
    p_account_id, p_worker_id, p_release_sha, p_now, p_lease_seconds
  );
$$;

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
    p_command_id, p_intent_id, p_claim_token, p_expected_revision,
    p_worker_id, p_release_sha, p_now
  );
$$;

create or replace function worker_api.complete_paper_execution_source_v1(
  p_command_id uuid,
  p_claim_token uuid,
  p_expected_revision bigint,
  p_worker_id uuid,
  p_release_sha text,
  p_now timestamptz,
  p_outcome text,
  p_next_available_at timestamptz,
  p_reason_code text
)
returns table (
  command_id uuid,
  state text,
  source_revision bigint,
  next_available_at timestamptz
)
language sql
security invoker
set search_path = ''
as $$
  select * from private.complete_paper_execution_source_v1_impl(
    p_command_id, p_claim_token, p_expected_revision, p_worker_id,
    p_release_sha, p_now, p_outcome, p_next_available_at, p_reason_code
  );
$$;

revoke execute on function
  private.jsonb_exact_keys_v1(jsonb, text[]),
  private.paper_source_clock_v1(timestamptz),
  private.paper_source_authorization_v1(text, uuid, text, bigint, bigint, timestamptz),
  private.validate_paper_risk_input_v1(jsonb, jsonb),
  private.ingest_paper_bar_fixture_v1_impl(jsonb, uuid, text, timestamptz),
  private.enqueue_paper_execution_candidate_v1_impl(
    jsonb, uuid, bigint, bigint, text, timestamptz
  ),
  private.claim_paper_execution_v1_impl(text, uuid, text, timestamptz, integer),
  private.load_claimed_paper_execution_bundle_v1_impl(
    uuid, uuid, uuid, bigint, uuid, text, timestamptz
  ),
  private.complete_paper_execution_source_v1_impl(
    uuid, uuid, bigint, uuid, text, timestamptz, text, timestamptz, text
  )
from public, anon, authenticated, service_role;

grant execute on function
  private.ingest_paper_bar_fixture_v1_impl(jsonb, uuid, text, timestamptz),
  private.enqueue_paper_execution_candidate_v1_impl(
    jsonb, uuid, bigint, bigint, text, timestamptz
  ),
  private.claim_paper_execution_v1_impl(text, uuid, text, timestamptz, integer),
  private.load_claimed_paper_execution_bundle_v1_impl(
    uuid, uuid, uuid, bigint, uuid, text, timestamptz
  ),
  private.complete_paper_execution_source_v1_impl(
    uuid, uuid, bigint, uuid, text, timestamptz, text, timestamptz, text
  )
to service_role;

revoke execute on function
  worker_api.ingest_paper_bar_fixture_v1(jsonb, uuid, text, timestamptz),
  worker_api.enqueue_paper_execution_candidate_v1(
    jsonb, uuid, bigint, bigint, text, timestamptz
  ),
  worker_api.claim_paper_execution_v1(text, uuid, text, timestamptz, integer),
  worker_api.load_claimed_paper_execution_bundle_v1(
    uuid, uuid, uuid, bigint, uuid, text, timestamptz
  ),
  worker_api.complete_paper_execution_source_v1(
    uuid, uuid, bigint, uuid, text, timestamptz, text, timestamptz, text
  )
from public, anon, authenticated, service_role;

grant execute on function
  worker_api.ingest_paper_bar_fixture_v1(jsonb, uuid, text, timestamptz),
  worker_api.enqueue_paper_execution_candidate_v1(
    jsonb, uuid, bigint, bigint, text, timestamptz
  ),
  worker_api.claim_paper_execution_v1(text, uuid, text, timestamptz, integer),
  worker_api.load_claimed_paper_execution_bundle_v1(
    uuid, uuid, uuid, bigint, uuid, text, timestamptz
  ),
  worker_api.complete_paper_execution_source_v1(
    uuid, uuid, bigint, uuid, text, timestamptz, text, timestamptz, text
  )
to service_role;
