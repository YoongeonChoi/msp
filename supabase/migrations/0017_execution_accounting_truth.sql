-- G1 paper truth and G2 execution-safety source of truth.
--
-- The Supabase CLI is unavailable in the approved local workspace. This file
-- follows the repository's sequential SQL migration convention and must be
-- applied as one reviewed migration. Production broker writes are deliberately
-- absent: only paper and a local contract-test simulator are valid environments.

create table private.trading_accounts (
  account_id text primary key check (account_id ~ '^[a-z0-9][a-z0-9_-]{2,63}$'),
  environment text not null check (environment in ('paper', 'contract_test')),
  broker text not null check (broker in ('internal_paper', 'local_contract_simulator')),
  legal_owner_scope text not null default 'single_entity_proprietary'
    check (legal_owner_scope = 'single_entity_proprietary'),
  currency text not null default 'KRW' check (currency = 'KRW'),
  state text not null default 'pending_open'
    check (state in ('pending_open', 'open', 'closed')),
  cost_basis_method text not null default 'moving_weighted_average_v1'
    check (cost_basis_method = 'moving_weighted_average_v1'),
  opening_capital_krw bigint not null check (opening_capital_krw >= 0),
  opening_journal_entry_id uuid,
  opened_at timestamptz,
  closed_at timestamptz,
  created_at timestamptz not null default now(),
  constraint accounts_environment_broker_check check (
    (environment = 'paper' and broker = 'internal_paper')
    or (
      environment = 'contract_test'
      and broker = 'local_contract_simulator'
    )
  ),
  constraint accounts_state_shape_check check (
    (state = 'pending_open' and opened_at is null and closed_at is null)
    or (state = 'open' and opened_at is not null and closed_at is null)
    or (
      state = 'closed'
      and opened_at is not null
      and closed_at is not null
      and closed_at >= opened_at
    )
  )
);

insert into private.trading_accounts (
  account_id,
  environment,
  broker,
  opening_capital_krw
)
values
  ('paper-primary', 'paper', 'internal_paper', 10000000),
  ('contract-test-primary', 'contract_test', 'local_contract_simulator', 10000000)
on conflict (account_id) do nothing;

create table private.execution_controls (
  account_id text primary key references private.trading_accounts(account_id),
  environment text not null check (environment in ('paper', 'contract_test')),
  execution_enabled boolean not null default false,
  control_epoch bigint not null default 1 check (control_epoch > 0),
  active_strategy_version_id text not null default 'unapproved',
  active_risk_policy_version_id uuid,
  execution_policy_version text not null,
  execution_policy_sha256 text not null check (
    execution_policy_sha256 ~ '^[0-9a-f]{64}$'
  ),
  risk_policy_sha256 text not null check (risk_policy_sha256 ~ '^[0-9a-f]{64}$'),
  provider_contract_version text,
  provider_openapi_sha256 text check (
    provider_openapi_sha256 is null or provider_openapi_sha256 ~ '^[0-9a-f]{64}$'
  ),
  effective_at timestamptz not null default now(),
  expires_at timestamptz not null default (now() + interval '1 day'),
  last_command_id uuid references private.operation_commands(id),
  updated_reason_code text not null default 'initial_fail_closed',
  updated_at timestamptz not null default now(),
  constraint execution_controls_window_check check (expires_at > effective_at),
  constraint execution_controls_provider_pin_check check (
    (environment = 'paper' and provider_contract_version is null and provider_openapi_sha256 is null)
    or environment = 'contract_test'
  )
);

insert into private.execution_controls (
  account_id,
  environment,
  execution_policy_version,
  execution_policy_sha256,
  risk_policy_sha256
)
select
  account_id,
  environment,
  'unapproved',
  encode(digest('unapproved-execution-policy', 'sha256'), 'hex'),
  encode(digest('unapproved-risk-policy', 'sha256'), 'hex')
from private.trading_accounts
on conflict (account_id) do nothing;

create or replace function private.guard_execution_control_transition()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  if new.environment is distinct from old.environment then
    raise exception 'execution_environment_is_immutable' using errcode = '23514';
  end if;
  if new.control_epoch <= old.control_epoch then
    raise exception 'control_epoch_must_increase' using errcode = '23514';
  end if;
  if new.updated_at <= old.updated_at then
    raise exception 'execution_control_time_must_increase' using errcode = '23514';
  end if;
  return new;
end;
$$;

create trigger guard_execution_control_transition
  before update on private.execution_controls
  for each row execute function private.guard_execution_control_transition();

create table private.worker_leases (
  account_id text primary key references private.trading_accounts(account_id),
  holder_id text not null check (nullif(btrim(holder_id), '') is not null),
  fencing_token bigint not null check (fencing_token > 0),
  acquired_at timestamptz not null,
  renewed_at timestamptz not null,
  expires_at timestamptz not null,
  release_sha text not null check (release_sha ~ '^[0-9a-f]{40}([0-9a-f]{24})?$'),
  constraint worker_lease_window_check check (
    acquired_at <= renewed_at and renewed_at < expires_at
  )
);

create table private.ledger_accounts (
  id uuid primary key default gen_random_uuid(),
  account_id text not null references private.trading_accounts(account_id),
  ledger_code text not null check (
    ledger_code in (
      'CASH',
      'OPENING_EQUITY',
      'BROKER_CLEARING',
      'POSITION_COST',
      'FEES',
      'TAXES',
      'REALIZED_PNL'
    )
  ),
  normal_side text not null check (normal_side in ('debit', 'credit')),
  created_at timestamptz not null default now(),
  unique (account_id, ledger_code)
);

insert into private.ledger_accounts (account_id, ledger_code, normal_side)
select account_id, ledger_code, normal_side
from private.trading_accounts
cross join (
  values
    ('CASH', 'debit'),
    ('OPENING_EQUITY', 'credit'),
    ('BROKER_CLEARING', 'debit'),
    ('POSITION_COST', 'debit'),
    ('FEES', 'debit'),
    ('TAXES', 'debit'),
    ('REALIZED_PNL', 'credit')
) as chart(ledger_code, normal_side)
on conflict (account_id, ledger_code) do nothing;

create table private.accounting_transactions (
  id uuid primary key default gen_random_uuid(),
  account_id text not null references private.trading_accounts(account_id),
  environment text not null check (environment in ('paper', 'contract_test')),
  source_type text not null check (
    source_type in (
      'opening_capital',
      'fill',
      'fee',
      'tax',
      'reservation',
      'release',
      'adjustment',
      'correction'
    )
  ),
  source_id text not null,
  control_command_id uuid references private.operation_commands(id),
  correlation_id uuid not null,
  occurred_at timestamptz not null,
  posted_at timestamptz not null default now(),
  release_sha text check (release_sha is null or release_sha ~ '^[0-9a-f]{40}([0-9a-f]{24})?$'),
  unique (account_id, source_type, source_id)
);

create table private.accounting_postings (
  id uuid primary key default gen_random_uuid(),
  journal_entry_id uuid not null references private.accounting_transactions(id),
  ledger_account_id uuid not null references private.ledger_accounts(id),
  side text not null check (side in ('debit', 'credit')),
  amount_krw numeric(24,4) not null check (amount_krw > 0),
  created_at timestamptz not null default now()
);

create index idx_accounting_postings_entry
  on private.accounting_postings (journal_entry_id);

alter table private.trading_accounts
  add constraint accounts_opening_journal_entry_fk
  foreign key (opening_journal_entry_id) references private.accounting_transactions(id);

create or replace function private.assert_journal_entry_balanced()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
declare
  target_entry_id uuid;
  posting_count bigint;
  debit_total numeric(24,4);
  credit_total numeric(24,4);
begin
  if tg_table_name = 'accounting_transactions' then
    target_entry_id := new.id;
  else
    target_entry_id := coalesce(new.journal_entry_id, old.journal_entry_id);
  end if;

  select
    count(*),
    coalesce(sum(amount_krw) filter (where side = 'debit'), 0),
    coalesce(sum(amount_krw) filter (where side = 'credit'), 0)
  into posting_count, debit_total, credit_total
  from private.accounting_postings
  where journal_entry_id = target_entry_id;

  if posting_count < 2 or debit_total <> credit_total then
    raise exception 'journal_entry_is_not_balanced' using errcode = '23514';
  end if;
  return null;
end;
$$;

create constraint trigger assert_journal_entry_balanced_from_entry
  after insert or update on private.accounting_transactions
  deferrable initially deferred
  for each row execute function private.assert_journal_entry_balanced();

create constraint trigger assert_journal_entry_balanced_from_posting
  after insert or update or delete on private.accounting_postings
  deferrable initially deferred
  for each row execute function private.assert_journal_entry_balanced();

create table private.cash_balance_projection (
  account_id text primary key references private.trading_accounts(account_id),
  settled_cash_krw numeric(24,4) not null default 0 check (settled_cash_krw >= 0),
  reserved_cash_krw numeric(24,4) not null default 0 check (reserved_cash_krw >= 0),
  pending_debit_cash_krw numeric(24,4) not null default 0 check (pending_debit_cash_krw >= 0),
  available_cash_krw numeric(24,4) generated always as (
    settled_cash_krw - reserved_cash_krw - pending_debit_cash_krw
  ) stored,
  last_journal_entry_id uuid references private.accounting_transactions(id),
  projection_version bigint not null default 0 check (projection_version >= 0),
  projected_at timestamptz not null default now(),
  constraint cash_projection_available_check check (
    settled_cash_krw >= reserved_cash_krw + pending_debit_cash_krw
  )
);

insert into private.cash_balance_projection (account_id)
select account_id from private.trading_accounts
on conflict (account_id) do nothing;

create table private.position_projection (
  account_id text not null references private.trading_accounts(account_id),
  symbol text not null check (symbol ~ '^[0-9]{6}$'),
  quantity bigint not null default 0 check (quantity >= 0),
  reserved_quantity bigint not null default 0 check (reserved_quantity >= 0),
  pending_sell_quantity bigint not null default 0 check (pending_sell_quantity >= 0),
  available_quantity bigint generated always as (
    quantity - reserved_quantity - pending_sell_quantity
  ) stored,
  average_cost_krw numeric(24,4) not null default 0 check (average_cost_krw >= 0),
  projection_version bigint not null default 0 check (projection_version >= 0),
  projected_at timestamptz not null default now(),
  primary key (account_id, symbol),
  constraint position_projection_available_check check (
    quantity >= reserved_quantity + pending_sell_quantity
  )
);

create table private.execution_decisions (
  id uuid primary key,
  account_id text not null references private.trading_accounts(account_id),
  environment text not null check (environment in ('paper', 'contract_test')),
  strategy_version_id text not null,
  symbol text not null check (symbol ~ '^[0-9]{6}$'),
  action text not null check (action in ('buy', 'sell')),
  decision_at timestamptz not null,
  signal_valid_from timestamptz not null,
  signal_valid_until timestamptz not null,
  feature_snapshot_sha256 text not null check (
    feature_snapshot_sha256 ~ '^[0-9a-f]{64}$'
  ),
  decision_sha256 text not null unique check (decision_sha256 ~ '^[0-9a-f]{64}$'),
  release_sha text not null check (release_sha ~ '^[0-9a-f]{40}([0-9a-f]{24})?$'),
  created_at timestamptz not null default clock_timestamp(),
  constraint execution_decision_window_check check (
    signal_valid_from <= decision_at and decision_at <= signal_valid_until
  )
);

create table private.risk_results (
  id uuid primary key,
  decision_id uuid not null references private.execution_decisions(id),
  account_id text not null references private.trading_accounts(account_id),
  environment text not null check (environment in ('paper', 'contract_test')),
  strategy_version_id text not null,
  risk_policy_sha256 text not null check (risk_policy_sha256 ~ '^[0-9a-f]{64}$'),
  control_epoch bigint not null check (control_epoch > 0),
  allowed boolean not null,
  reason_codes text[] not null default array[]::text[],
  result_sha256 text not null unique check (result_sha256 ~ '^[0-9a-f]{64}$'),
  evaluated_at timestamptz not null,
  expires_at timestamptz not null,
  release_sha text not null check (release_sha ~ '^[0-9a-f]{40}([0-9a-f]{24})?$'),
  created_at timestamptz not null default now(),
  constraint risk_result_window_check check (expires_at > evaluated_at),
  unique (decision_id, account_id, risk_policy_sha256)
);

create table private.order_intents (
  id uuid primary key,
  semantic_key_sha256 text not null check (semantic_key_sha256 ~ '^[0-9a-f]{64}$'),
  account_id text not null references private.trading_accounts(account_id),
  environment text not null check (environment in ('paper', 'contract_test')),
  strategy_version_id text not null,
  decision_id uuid not null references private.execution_decisions(id),
  risk_result_id uuid not null references private.risk_results(id),
  correlation_id uuid not null,
  symbol text not null check (symbol ~ '^[0-9]{6}$'),
  side text not null check (side in ('buy', 'sell')),
  order_type text not null default 'limit' check (order_type = 'limit'),
  time_in_force text not null default 'DAY' check (time_in_force = 'DAY'),
  quantity bigint not null check (quantity > 0),
  limit_price_krw bigint not null check (limit_price_krw > 0),
  decision_at timestamptz not null,
  signal_valid_from timestamptz not null,
  signal_valid_until timestamptz not null,
  eligible_at timestamptz not null,
  expires_at timestamptz not null,
  execution_policy_version text not null,
  execution_policy_sha256 text not null check (
    execution_policy_sha256 ~ '^[0-9a-f]{64}$'
  ),
  cost_schedule_version text not null,
  cost_schedule_evidence_sha256 text not null check (
    cost_schedule_evidence_sha256 ~ '^[0-9a-f]{64}$'
  ),
  cash_commitment_krw bigint not null check (cash_commitment_krw >= 0),
  position_cost_basis_method text,
  position_quantity_snapshot bigint,
  position_average_cost_krw numeric(24,4),
  position_total_cost_krw bigint,
  position_projection_version bigint,
  position_cost_basis_sha256 text check (
    position_cost_basis_sha256 is null
    or position_cost_basis_sha256 ~ '^[0-9a-f]{64}$'
  ),
  risk_policy_sha256 text not null check (risk_policy_sha256 ~ '^[0-9a-f]{64}$'),
  provider_contract_version text,
  provider_openapi_sha256 text check (
    provider_openapi_sha256 is null or provider_openapi_sha256 ~ '^[0-9a-f]{64}$'
  ),
  control_epoch bigint not null check (control_epoch > 0),
  release_sha text not null check (release_sha ~ '^[0-9a-f]{40}([0-9a-f]{24})?$'),
  created_at timestamptz not null default now(),
  constraint order_intent_window_check check (
    signal_valid_from <= decision_at
    and decision_at <= signal_valid_until
    and signal_valid_until >= eligible_at
    and expires_at >= eligible_at
  ),
  constraint order_intent_provider_pin_check check (
    (environment = 'paper' and provider_contract_version is null and provider_openapi_sha256 is null)
    or (
      environment = 'contract_test'
      and nullif(btrim(provider_contract_version), '') is not null
      and provider_openapi_sha256 is not null
    )
  ),
  constraint order_intent_position_cost_basis_shape_check check (
    (
      side = 'buy'
      and position_cost_basis_method is null
      and position_quantity_snapshot is null
      and position_average_cost_krw is null
      and position_total_cost_krw is null
      and position_projection_version is null
      and position_cost_basis_sha256 is null
    )
    or (
      side = 'sell'
      and position_cost_basis_method = 'moving_weighted_average_v1'
      and position_quantity_snapshot >= quantity
      and position_average_cost_krw >= 0
      and position_total_cost_krw >= 0
      and position_total_cost_krw = round(
        position_quantity_snapshot * position_average_cost_krw
      )::bigint
      and position_projection_version >= 0
      and position_cost_basis_sha256 is not null
    )
  ),
  unique (account_id, environment, semantic_key_sha256)
);

create table private.execution_reconciliation_state (
  intent_id uuid primary key references private.order_intents(id),
  priority integer not null default 30 check (priority between 0 and 1000),
  state text not null default 'pending' check (
    state in ('pending', 'leased', 'complete', 'manual')
  ),
  next_reconcile_at timestamptz not null,
  lease_owner text,
  lease_expires_at timestamptz,
  attempt_count integer not null default 0 check (attempt_count >= 0),
  last_reason_code text,
  updated_at timestamptz not null default clock_timestamp(),
  constraint execution_reconciliation_lease_shape_check check (
    (state = 'leased' and lease_owner is not null and lease_expires_at is not null)
    or (state <> 'leased' and lease_owner is null and lease_expires_at is null)
  )
);

create index execution_reconciliation_claim_index
  on private.execution_reconciliation_state (
    priority, intent_id, next_reconcile_at
  ) where state in ('pending', 'leased');

create table private.order_reservations (
  id uuid primary key default gen_random_uuid(),
  intent_id uuid not null unique references private.order_intents(id),
  account_id text not null references private.trading_accounts(account_id),
  environment text not null check (environment in ('paper', 'contract_test')),
  lease_holder_id text not null,
  fencing_token bigint not null check (fencing_token > 0),
  control_epoch bigint not null check (control_epoch > 0),
  reserved_cash_krw bigint not null default 0 check (reserved_cash_krw >= 0),
  reserved_quantity bigint not null default 0 check (reserved_quantity >= 0),
  reserved_at timestamptz not null,
  expires_at timestamptz not null,
  reservation_sha256 text not null unique check (
    reservation_sha256 ~ '^[0-9a-f]{64}$'
  ),
  constraint order_reservation_window_check check (expires_at >= reserved_at),
  constraint order_reservation_resource_check check (
    (reserved_cash_krw > 0 and reserved_quantity = 0)
    or (reserved_cash_krw = 0 and reserved_quantity > 0)
  )
);

create table private.reservation_events (
  id uuid primary key default gen_random_uuid(),
  reservation_id uuid not null references private.order_reservations(id),
  intent_id uuid not null references private.order_intents(id),
  event_sequence integer not null check (event_sequence > 0),
  event_type text not null check (
    event_type in ('reserved', 'partially_consumed', 'fully_consumed', 'released')
  ),
  cash_delta_krw bigint not null default 0,
  quantity_delta bigint not null default 0,
  remaining_cash_krw bigint not null check (remaining_cash_krw >= 0),
  remaining_quantity bigint not null check (remaining_quantity >= 0),
  source_observation_id uuid,
  occurred_at timestamptz not null default clock_timestamp(),
  constraint reservation_event_resource_check check (
    (cash_delta_krw <> 0 and quantity_delta = 0 and remaining_quantity = 0)
    or (cash_delta_krw = 0 and quantity_delta <> 0 and remaining_cash_krw = 0)
  ),
  unique (reservation_id, event_sequence),
  unique (source_observation_id)
);

create table private.order_attempts (
  id uuid primary key default gen_random_uuid(),
  reservation_id uuid not null unique references private.order_reservations(id),
  intent_id uuid not null unique references private.order_intents(id),
  account_id text not null references private.trading_accounts(account_id),
  environment text not null check (environment in ('paper', 'contract_test')),
  broker text not null check (broker in ('internal_paper', 'local_contract_simulator')),
  lease_holder_id text not null,
  fencing_token bigint not null check (fencing_token > 0),
  control_epoch bigint not null check (control_epoch > 0),
  client_order_key text not null unique,
  request_sha256 text not null check (request_sha256 ~ '^[0-9a-f]{64}$'),
  prepared_at timestamptz not null,
  constraint order_attempt_environment_broker_check check (
    (environment = 'paper' and broker = 'internal_paper')
    or (
      environment = 'contract_test'
      and broker = 'local_contract_simulator'
    )
  )
);

create table private.provider_order_bindings (
  attempt_id uuid primary key references private.order_attempts(id),
  intent_id uuid not null unique references private.order_intents(id),
  provider_order_id text not null check (nullif(btrim(provider_order_id), '') is not null),
  binding_sha256 text not null unique check (binding_sha256 ~ '^[0-9a-f]{64}$'),
  bound_at timestamptz not null default clock_timestamp()
);

create table private.execution_observations (
  id uuid primary key default gen_random_uuid(),
  intent_id uuid not null references private.order_intents(id),
  attempt_id uuid references private.order_attempts(id),
  sequence integer not null check (sequence > 0),
  event_type text not null check (
    event_type in (
      'open',
      'partial_filled',
      'filled',
      'canceled',
      'expired',
      'rejected',
      'failed_pre_dispatch',
      'unknown_requires_manual_check'
    )
  ),
  observed_at timestamptz not null,
  cumulative_quantity bigint not null check (cumulative_quantity >= 0),
  cumulative_gross_krw bigint not null check (cumulative_gross_krw >= 0),
  cumulative_commission_krw bigint not null check (cumulative_commission_krw >= 0),
  cumulative_tax_krw bigint not null check (cumulative_tax_krw >= 0),
  reason_code text,
  observation_sha256 text not null check (observation_sha256 ~ '^[0-9a-f]{64}$'),
  provider_order_id text,
  provider_execution_id text,
  provider_observation_sha256 text not null unique check (
    provider_observation_sha256 ~ '^[0-9a-f]{64}$'
  ),
  created_at timestamptz not null default now(),
  constraint order_event_filled_quantity_check check (
    event_type <> 'filled' or cumulative_quantity > 0
  ),
  constraint execution_observation_provider_identity_shape_check check (
    (
      event_type = 'failed_pre_dispatch'
      and attempt_id is null
      and provider_order_id is null
      and provider_execution_id is null
    )
    or (
      event_type <> 'failed_pre_dispatch'
      and attempt_id is not null
      and nullif(btrim(provider_order_id), '') is not null
    )
  ),
  unique (intent_id, sequence),
  unique (intent_id, observation_sha256)
);

create index idx_execution_observations_trace
  on private.execution_observations (intent_id, sequence);

create table private.quarantined_execution_observations (
  id uuid primary key default gen_random_uuid(),
  intent_id uuid not null references private.order_intents(id),
  attempt_id uuid references private.order_attempts(id),
  sequence integer not null check (sequence > 0),
  provider_order_id text,
  provider_execution_id text,
  provider_observation_sha256 text not null check (
    provider_observation_sha256 ~ '^[0-9a-f]{64}$'
  ),
  payload_sha256 text not null check (payload_sha256 ~ '^[0-9a-f]{64}$'),
  reason_code text not null,
  observed_at timestamptz not null,
  created_at timestamptz not null default clock_timestamp(),
  unique (intent_id, sequence, provider_observation_sha256)
);

alter table private.reservation_events
  add constraint reservation_events_source_observation_fk
  foreign key (source_observation_id) references private.execution_observations(id);

create table private.order_events (
  id uuid primary key default gen_random_uuid(),
  intent_id uuid not null references private.order_intents(id),
  attempt_id uuid references private.order_attempts(id),
  observation_id uuid references private.execution_observations(id),
  event_key text not null,
  event_type text not null check (
    event_type in (
      'intent_reserved', 'dispatch_prepared', 'observation_recorded',
      'terminal_confirmed', 'manual_check_quarantined'
    )
  ),
  correlation_id uuid not null,
  event_summary jsonb not null default '{}'::jsonb check (jsonb_typeof(event_summary) = 'object'),
  occurred_at timestamptz not null,
  created_at timestamptz not null default clock_timestamp(),
  unique (intent_id, event_key)
);

create table private.fills (
  id uuid primary key default gen_random_uuid(),
  event_id uuid not null unique references private.execution_observations(id),
  intent_id uuid not null references private.order_intents(id),
  attempt_id uuid not null references private.order_attempts(id),
  account_id text not null references private.trading_accounts(account_id),
  broker text not null check (broker in ('internal_paper', 'local_contract_simulator')),
  provider_execution_id text not null,
  quantity bigint not null check (quantity > 0),
  price_krw bigint not null check (price_krw > 0),
  commission_krw bigint not null default 0 check (commission_krw >= 0),
  tax_krw bigint not null default 0 check (tax_krw >= 0),
  filled_at timestamptz not null,
  settlement_date date not null,
  created_at timestamptz not null default now(),
  unique (account_id, broker, provider_execution_id)
);

create table private.position_lots (
  id uuid primary key default gen_random_uuid(),
  account_id text not null references private.trading_accounts(account_id),
  symbol text not null check (symbol ~ '^[0-9]{6}$'),
  source_fill_id uuid not null unique references private.fills(id),
  acquired_quantity bigint not null check (acquired_quantity > 0),
  remaining_quantity bigint not null check (
    remaining_quantity >= 0 and remaining_quantity <= acquired_quantity
  ),
  unit_cost_krw numeric(24,4) not null check (unit_cost_krw > 0),
  acquired_at timestamptz not null,
  created_at timestamptz not null default now()
);

comment on table private.position_lots is
  'Immutable acquisition evidence only; moving_weighted_average_v1 cost basis is owned by private.position_projection and position_movements.';

create table private.broker_observations (
  id uuid primary key default gen_random_uuid(),
  account_id text not null references private.trading_accounts(account_id),
  environment text not null check (environment in ('paper', 'contract_test')),
  observation_type text not null check (
    observation_type in ('account', 'position', 'order', 'execution')
  ),
  intent_id uuid references private.order_intents(id),
  observed_at timestamptz not null,
  ingested_at timestamptz not null default now(),
  source_sha256 text not null check (source_sha256 ~ '^[0-9a-f]{64}$'),
  summary jsonb not null default '{}'::jsonb,
  release_sha text not null check (release_sha ~ '^[0-9a-f]{40}([0-9a-f]{24})?$'),
  constraint broker_observation_time_check check (
    observed_at <= ingested_at + interval '5 minutes'
  ),
  unique (account_id, environment, source_sha256)
);

create table private.reconciliation_runs (
  id uuid primary key default gen_random_uuid(),
  account_id text not null references private.trading_accounts(account_id),
  environment text not null check (environment in ('paper', 'contract_test')),
  started_at timestamptz not null,
  completed_at timestamptz,
  result text check (result is null or result in ('matched', 'breaks_found', 'failed')),
  release_sha text not null check (release_sha ~ '^[0-9a-f]{40}([0-9a-f]{24})?$'),
  constraint reconciliation_run_state_check check (
    (completed_at is null and result is null)
    or (completed_at is not null and completed_at >= started_at and result is not null)
  )
);

create table private.reconciliation_breaks (
  id uuid primary key default gen_random_uuid(),
  run_id uuid not null references private.reconciliation_runs(id),
  account_id text not null references private.trading_accounts(account_id),
  break_type text not null check (
    break_type in ('cash', 'position', 'order', 'execution', 'legacy_unreconciled')
  ),
  state text not null default 'open' check (
    state in ('open', 'resolution_requested', 'resolved')
  ),
  evidence_id uuid references private.control_evidence(id),
  resolution_command_id uuid references private.operation_commands(id),
  detected_at timestamptz not null,
  resolved_at timestamptz,
  summary_code text not null,
  constraint reconciliation_break_resolution_check check (
    (state <> 'resolved' and resolved_at is null)
    or (
      state = 'resolved'
      and resolved_at is not null
      and evidence_id is not null
      and resolution_command_id is not null
    )
  )
);

create table private.paper_execution_policies (
  id uuid primary key default gen_random_uuid(),
  account_id text not null references private.trading_accounts(account_id),
  policy_version text not null,
  policy_sha256 text not null check (policy_sha256 ~ '^[0-9a-f]{64}$'),
  status text not null default 'draft' check (status in ('draft', 'approved', 'retired')),
  price_model text not null check (price_model = 'next_executable_minute_v1'),
  fill_model text not null check (fill_model = 'whole_share_volume_bounded_v1'),
  parameters jsonb not null check (jsonb_typeof(parameters) = 'object'),
  evidence_id uuid not null references private.control_evidence(id),
  requested_by uuid not null references auth.users(id),
  reviewed_by uuid references auth.users(id),
  effective_from timestamptz not null,
  effective_until timestamptz not null,
  created_at timestamptz not null default now(),
  constraint paper_policy_window_check check (effective_until > effective_from),
  constraint paper_policy_review_check check (
    (status = 'draft' and reviewed_by is null)
    or (status in ('approved', 'retired') and reviewed_by is not null and reviewed_by <> requested_by)
  ),
  unique (account_id, policy_version),
  unique (account_id, policy_sha256)
);

create table private.execution_cost_schedules (
  id uuid primary key default gen_random_uuid(),
  account_id text not null references private.trading_accounts(account_id),
  schedule_version text not null,
  schedule_sha256 text not null check (schedule_sha256 ~ '^[0-9a-f]{64}$'),
  buy_commission_rate numeric(18,12) not null check (
    buy_commission_rate >= 0 and buy_commission_rate < 1
  ),
  sell_commission_rate numeric(18,12) not null check (
    sell_commission_rate >= 0 and sell_commission_rate < 1
  ),
  sell_tax_rate numeric(18,12) not null check (sell_tax_rate >= 0 and sell_tax_rate < 1),
  settlement_days integer not null check (settlement_days between 0 and 10),
  status text not null default 'draft' check (status in ('draft', 'approved', 'retired')),
  evidence_id uuid not null references private.control_evidence(id),
  requested_by uuid not null references auth.users(id),
  reviewed_by uuid references auth.users(id),
  effective_from timestamptz not null,
  effective_until timestamptz not null,
  created_at timestamptz not null default now(),
  constraint execution_cost_window_check check (effective_until > effective_from),
  constraint execution_cost_review_check check (
    (status = 'draft' and reviewed_by is null)
    or (status in ('approved', 'retired') and reviewed_by is not null and reviewed_by <> requested_by)
  ),
  unique (account_id, schedule_version),
  unique (account_id, schedule_sha256)
);

create table private.provider_contract_registry (
  id uuid primary key default gen_random_uuid(),
  provider text not null check (provider = 'toss'),
  qualification_environment text not null check (qualification_environment = 'contract_test'),
  execution_transport text not null check (execution_transport = 'local_contract_simulator'),
  contract_version text not null,
  openapi_sha256 text not null check (openapi_sha256 ~ '^[0-9a-f]{64}$'),
  official_artifact_uri text not null check (
    official_artifact_uri ~ '^https://[^/?#[:space:]]+/'
    and official_artifact_uri !~ '[?#]'
  ),
  retrieved_at timestamptz not null,
  evidence_id uuid not null references private.control_evidence(id),
  release_sha text not null check (release_sha ~ '^[0-9a-f]{40}([0-9a-f]{24})?$'),
  status text not null default 'pending' check (status in ('pending', 'approved', 'retired')),
  requested_by uuid not null references auth.users(id),
  reviewed_by uuid references auth.users(id),
  effective_from timestamptz not null,
  effective_until timestamptz not null,
  created_at timestamptz not null default now(),
  constraint provider_contract_window_check check (effective_until > effective_from),
  constraint provider_contract_review_check check (
    (status = 'pending' and reviewed_by is null)
    or (status in ('approved', 'retired') and reviewed_by is not null and reviewed_by <> requested_by)
  ),
  unique (provider, qualification_environment, contract_version),
  unique (provider, qualification_environment, openapi_sha256)
);

create table private.market_calendars (
  id uuid primary key default gen_random_uuid(),
  environment text not null check (environment in ('paper', 'contract_test')),
  calendar_version text not null,
  calendar_sha256 text not null unique check (calendar_sha256 ~ '^[0-9a-f]{64}$'),
  timezone_name text not null check (timezone_name = 'Asia/Seoul'),
  valid_from date not null,
  valid_until date not null,
  status text not null default 'draft' check (status in ('draft', 'approved', 'retired')),
  evidence_id uuid not null references private.control_evidence(id),
  requested_by uuid not null references auth.users(id),
  reviewed_by uuid references auth.users(id),
  created_at timestamptz not null default clock_timestamp(),
  constraint market_calendar_window_check check (valid_until >= valid_from),
  constraint market_calendar_review_check check (
    (status = 'draft' and reviewed_by is null)
    or (status in ('approved', 'retired') and reviewed_by is not null and reviewed_by <> requested_by)
  ),
  unique (environment, calendar_version)
);

create table private.market_calendar_sessions (
  calendar_id uuid not null references private.market_calendars(id),
  session_date date not null,
  is_open boolean not null,
  session_sha256 text not null check (session_sha256 ~ '^[0-9a-f]{64}$'),
  primary key (calendar_id, session_date),
  unique (calendar_id, session_sha256)
);

create table private.paper_execution_model_registry (
  id uuid primary key default gen_random_uuid(),
  environment text not null check (environment in ('paper', 'contract_test')),
  model_version text not null,
  tick_size_evidence_sha256 text not null check (tick_size_evidence_sha256 ~ '^[0-9a-f]{64}$'),
  volume_model_evidence_sha256 text not null check (volume_model_evidence_sha256 ~ '^[0-9a-f]{64}$'),
  corporate_action_evidence_sha256 text not null check (corporate_action_evidence_sha256 ~ '^[0-9a-f]{64}$'),
  market_calendar_id uuid not null references private.market_calendars(id),
  status text not null default 'draft' check (status in ('draft', 'approved', 'retired')),
  evidence_id uuid not null references private.control_evidence(id),
  requested_by uuid not null references auth.users(id),
  reviewed_by uuid references auth.users(id),
  effective_from timestamptz not null,
  effective_until timestamptz not null,
  created_at timestamptz not null default clock_timestamp(),
  constraint paper_execution_model_window_check check (effective_until > effective_from),
  constraint paper_execution_model_review_check check (
    (status = 'draft' and reviewed_by is null)
    or (status in ('approved', 'retired') and reviewed_by is not null and reviewed_by <> requested_by)
  ),
  unique (environment, model_version)
);

create table private.account_snapshots (
  id uuid primary key default gen_random_uuid(),
  account_id text not null references private.trading_accounts(account_id),
  environment text not null check (environment in ('paper', 'contract_test')),
  sequence bigint not null check (sequence > 0),
  cash_krw numeric(24,4) not null check (cash_krw >= 0),
  reserved_cash_krw numeric(24,4) not null check (
    reserved_cash_krw >= 0 and reserved_cash_krw <= cash_krw
  ),
  positions_sha256 text not null check (positions_sha256 ~ '^[0-9a-f]{64}$'),
  source_type text not null check (source_type in ('ledger_projection', 'provider_observation')),
  source_id uuid not null,
  observed_at timestamptz not null,
  created_at timestamptz not null default now(),
  unique (account_id, environment, sequence),
  unique (account_id, environment, source_type, source_id)
);

create table private.position_movements (
  id uuid primary key default gen_random_uuid(),
  account_id text not null references private.trading_accounts(account_id),
  symbol text not null check (symbol ~ '^[0-9]{6}$'),
  source_fill_id uuid references private.fills(id),
  source_transaction_id uuid not null references private.accounting_transactions(id),
  movement_type text not null check (
    movement_type in ('buy_fill', 'sell_fill', 'correction')
  ),
  quantity_delta bigint not null check (quantity_delta <> 0),
  resulting_quantity bigint not null check (resulting_quantity >= 0),
  unit_cost_krw numeric(24,4) not null check (unit_cost_krw >= 0),
  occurred_at timestamptz not null,
  created_at timestamptz not null default now(),
  unique (source_transaction_id, symbol)
);

create table private.step_up_grants (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id),
  command_sha256 text not null check (command_sha256 ~ '^[0-9a-f]{64}$'),
  bound_action text not null check (bound_action in ('request', 'review', 'access_change')),
  bound_command_type text not null,
  session_binding_sha256 text not null check (session_binding_sha256 ~ '^[0-9a-f]{64}$'),
  issued_at timestamptz not null default clock_timestamp(),
  expires_at timestamptz not null,
  consumed_at timestamptz,
  consumed_for text,
  constraint step_up_grant_window_check check (
    expires_at > issued_at and expires_at <= issued_at + interval '5 minutes'
  ),
  constraint step_up_grant_consumption_check check (
    (consumed_at is null and consumed_for is null)
    or (consumed_at is not null and consumed_at >= issued_at and nullif(btrim(consumed_for), '') is not null)
  )
);

create index idx_step_up_grants_consume
  on private.step_up_grants (user_id, expires_at)
  where consumed_at is null;

create table private.operation_command_reviews (
  id uuid primary key default gen_random_uuid(),
  command_id uuid not null unique references private.operation_commands(id),
  reviewer_user_id uuid not null references auth.users(id),
  reviewer_role text not null check (
    reviewer_role in ('risk_approver', 'strategy_reviewer', 'release_manager')
  ),
  decision text not null check (decision in ('approved', 'rejected')),
  reason_code text,
  step_up_grant_id uuid not null unique references private.step_up_grants(id),
  reviewed_at timestamptz not null default clock_timestamp(),
  constraint operation_review_rejection_reason check (
    decision = 'approved' or nullif(btrim(reason_code), '') is not null
  )
);

create table private.operation_command_events (
  id uuid primary key default gen_random_uuid(),
  command_id uuid not null references private.operation_commands(id),
  event_type text not null check (
    event_type in (
      'requested', 'approved', 'claimed', 'applied', 'rejected',
      'failed', 'expired', 'canceled'
    )
  ),
  actor_type text not null check (actor_type in ('human', 'worker', 'system')),
  actor_user_id uuid references auth.users(id),
  service_principal text,
  event_summary jsonb not null default '{}'::jsonb check (jsonb_typeof(event_summary) = 'object'),
  occurred_at timestamptz not null default clock_timestamp(),
  constraint operation_event_actor_check check (
    (actor_type = 'human' and actor_user_id is not null and service_principal is null)
    or (actor_type = 'worker' and actor_user_id is null and service_principal is not null)
    or (actor_type = 'system' and actor_user_id is null)
  )
);

create index idx_operation_command_events_trace
  on private.operation_command_events (command_id, occurred_at, id);

create table private.access_change_requests (
  id uuid primary key default gen_random_uuid(),
  subject_user_id uuid not null references auth.users(id),
  requested_role text not null check (
    requested_role in (
      'platform_admin', 'operator', 'risk_approver', 'strategy_reviewer',
      'auditor', 'release_manager', 'viewer'
    )
  ),
  change_type text not null check (change_type in ('grant', 'revoke')),
  state text not null default 'requested' check (
    state in ('requested', 'approved', 'applied', 'rejected', 'expired', 'canceled')
  ),
  requester_user_id uuid not null references auth.users(id),
  reviewer_user_id uuid references auth.users(id),
  evidence_id uuid not null references private.control_evidence(id),
  request_step_up_grant_id uuid not null unique references private.step_up_grants(id),
  review_step_up_grant_id uuid unique references private.step_up_grants(id),
  requested_at timestamptz not null default clock_timestamp(),
  reviewed_at timestamptz,
  applied_at timestamptz,
  expires_at timestamptz not null,
  reason_code text not null,
  constraint access_change_separation_check check (
    reviewer_user_id is null or reviewer_user_id <> requester_user_id
  ),
  constraint access_change_window_check check (
    expires_at > requested_at and expires_at <= requested_at + interval '24 hours'
  ),
  constraint access_change_state_check check (
    (state = 'requested' and reviewer_user_id is null and reviewed_at is null and applied_at is null)
    or (state in ('approved', 'rejected') and reviewer_user_id is not null and reviewed_at is not null and applied_at is null)
    or (state = 'applied' and reviewer_user_id is not null and reviewed_at is not null and applied_at is not null)
    or (state in ('expired', 'canceled') and applied_at is null)
  )
);

create table private.qualifications (
  id uuid primary key,
  environment text not null check (environment in ('paper', 'contract_test')),
  status text not null check (status in ('qualified', 'blocked', 'expired')),
  release_sha text not null check (release_sha ~ '^[0-9a-f]{40}([0-9a-f]{24})?$'),
  ledger_checkpoint text not null,
  dataset_version text not null,
  execution_policy_version text not null,
  execution_policy_sha256 text not null check (execution_policy_sha256 ~ '^[0-9a-f]{64}$'),
  risk_policy_sha256 text not null check (risk_policy_sha256 ~ '^[0-9a-f]{64}$'),
  strategy_version_id uuid not null references public.strategy_versions(id),
  risk_policy_version_id uuid not null,
  provider_contract_version text,
  provider_openapi_sha256 text check (
    provider_openapi_sha256 is null or provider_openapi_sha256 ~ '^[0-9a-f]{64}$'
  ),
  valid_from timestamptz not null,
  valid_until timestamptz not null,
  g1_status text not null check (g1_status in ('pass', 'fail', 'expired', 'not_evaluated')),
  g1_checked_at timestamptz not null,
  g1_evidence_id uuid references private.control_evidence(id),
  g2_status text not null check (g2_status in ('pass', 'fail', 'expired', 'not_evaluated')),
  g2_checked_at timestamptz not null,
  g2_evidence_id uuid references private.control_evidence(id),
  created_at timestamptz not null default clock_timestamp(),
  constraint qualification_window_check check (valid_until > valid_from),
  constraint qualification_gate_check check (
    status <> 'qualified'
    or (g1_status = 'pass' and g2_status = 'pass' and g1_evidence_id is not null and g2_evidence_id is not null)
  ),
  constraint qualification_provider_pin_check check (
    (environment = 'paper' and provider_contract_version is null and provider_openapi_sha256 is null)
    or (
      environment = 'contract_test'
      and nullif(btrim(provider_contract_version), '') is not null
      and provider_openapi_sha256 is not null
    )
  )
);

create or replace function private.reject_immutable_execution_mutation()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  raise exception 'immutable_execution_record_mutation_forbidden'
    using errcode = '42501';
end;
$$;

create trigger reject_ledger_account_mutation
  before update or delete on private.ledger_accounts
  for each row execute function private.reject_immutable_execution_mutation();
create trigger reject_journal_entry_mutation
  before update or delete on private.accounting_transactions
  for each row execute function private.reject_immutable_execution_mutation();
create trigger reject_journal_posting_mutation
  before update or delete on private.accounting_postings
  for each row execute function private.reject_immutable_execution_mutation();
create trigger reject_execution_decision_mutation
  before update or delete on private.execution_decisions
  for each row execute function private.reject_immutable_execution_mutation();
create trigger reject_order_intent_mutation
  before update or delete on private.order_intents
  for each row execute function private.reject_immutable_execution_mutation();
create trigger reject_risk_result_mutation
  before update or delete on private.risk_results
  for each row execute function private.reject_immutable_execution_mutation();
create trigger reject_order_reservation_mutation
  before update or delete on private.order_reservations
  for each row execute function private.reject_immutable_execution_mutation();
create trigger reject_reservation_event_mutation
  before update or delete on private.reservation_events
  for each row execute function private.reject_immutable_execution_mutation();
create trigger reject_order_attempt_mutation
  before update or delete on private.order_attempts
  for each row execute function private.reject_immutable_execution_mutation();
create trigger reject_provider_order_binding_mutation
  before update or delete on private.provider_order_bindings
  for each row execute function private.reject_immutable_execution_mutation();
create trigger reject_quarantined_observation_mutation
  before update or delete on private.quarantined_execution_observations
  for each row execute function private.reject_immutable_execution_mutation();
create trigger reject_order_event_mutation
  before update or delete on private.execution_observations
  for each row execute function private.reject_immutable_execution_mutation();
create trigger reject_order_lifecycle_event_mutation
  before update or delete on private.order_events
  for each row execute function private.reject_immutable_execution_mutation();
create trigger reject_fill_mutation
  before update or delete on private.fills
  for each row execute function private.reject_immutable_execution_mutation();
create trigger reject_position_lot_mutation
  before update or delete on private.position_lots
  for each row execute function private.reject_immutable_execution_mutation();
create trigger reject_broker_observation_mutation
  before update or delete on private.broker_observations
  for each row execute function private.reject_immutable_execution_mutation();
create trigger reject_paper_execution_policy_mutation
  before update or delete on private.paper_execution_policies
  for each row execute function private.reject_immutable_execution_mutation();
create trigger reject_execution_cost_schedule_mutation
  before update or delete on private.execution_cost_schedules
  for each row execute function private.reject_immutable_execution_mutation();
create trigger reject_provider_contract_mutation
  before update or delete on private.provider_contract_registry
  for each row execute function private.reject_immutable_execution_mutation();
create trigger reject_market_calendar_mutation
  before update or delete on private.market_calendars
  for each row execute function private.reject_immutable_execution_mutation();
create trigger reject_market_calendar_session_mutation
  before update or delete on private.market_calendar_sessions
  for each row execute function private.reject_immutable_execution_mutation();
create trigger reject_paper_execution_model_mutation
  before update or delete on private.paper_execution_model_registry
  for each row execute function private.reject_immutable_execution_mutation();
create trigger reject_account_snapshot_mutation
  before update or delete on private.account_snapshots
  for each row execute function private.reject_immutable_execution_mutation();
create trigger reject_position_movement_mutation
  before update or delete on private.position_movements
  for each row execute function private.reject_immutable_execution_mutation();
create trigger reject_operation_command_review_mutation
  before update or delete on private.operation_command_reviews
  for each row execute function private.reject_immutable_execution_mutation();
create trigger reject_operation_command_event_mutation
  before update or delete on private.operation_command_events
  for each row execute function private.reject_immutable_execution_mutation();
create trigger reject_qualification_mutation
  before update or delete on private.qualifications
  for each row execute function private.reject_immutable_execution_mutation();

-- Existing execution rows remain as explicitly unreconciled historical evidence.
-- They are neither deleted nor converted into synthetic fills.
alter table public.orders
  add column if not exists record_class text not null default 'legacy_unreconciled'
    check (record_class = 'legacy_unreconciled');
alter table public.positions
  add column if not exists record_class text not null default 'legacy_unreconciled'
    check (record_class = 'legacy_unreconciled');
alter table public.manual_commands
  add column if not exists record_class text not null default 'legacy_unreconciled'
    check (record_class = 'legacy_unreconciled');
alter table public.audit_logs
  add column if not exists record_class text not null default 'legacy_unreconciled'
    check (record_class = 'legacy_unreconciled');

create or replace function private.reject_legacy_execution_write()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  raise exception 'legacy_unreconciled_table_is_read_only'
    using errcode = '42501';
end;
$$;

create trigger reject_legacy_order_write
  before insert or update or delete on public.orders
  for each row execute function private.reject_legacy_execution_write();
create trigger reject_legacy_position_write
  before insert or update or delete on public.positions
  for each row execute function private.reject_legacy_execution_write();
create trigger reject_legacy_manual_command_write
  before insert or update or delete on public.manual_commands
  for each row execute function private.reject_legacy_execution_write();
create trigger reject_legacy_audit_mutation
  before update or delete on public.audit_logs
  for each row execute function private.reject_legacy_execution_write();

-- A live state is impossible even for the table owner. Contract-test is modeled
-- in private.execution_controls and never by reusing legacy mode='live'.
update public.bot_settings
set enabled = false,
    mode = 'paper',
    live_order_allowed = false,
    updated_at = now();

alter table public.bot_settings
  add constraint bot_settings_production_live_permanently_disabled
  check (mode = 'paper' and live_order_allowed is false);

revoke all on table public.orders, public.positions, public.manual_commands, public.audit_logs
  from anon, authenticated;
revoke insert, update, delete on table
  public.orders,
  public.positions,
  public.manual_commands
from service_role;
revoke update, delete on table public.audit_logs from service_role;
grant select on table
  public.orders,
  public.positions,
  public.manual_commands,
  public.audit_logs
to service_role;

alter publication supabase_realtime drop table public.positions;
alter publication supabase_realtime drop table public.orders;

alter table private.trading_accounts enable row level security;
alter table private.execution_controls enable row level security;
alter table private.worker_leases enable row level security;
alter table private.ledger_accounts enable row level security;
alter table private.accounting_transactions enable row level security;
alter table private.accounting_postings enable row level security;
alter table private.cash_balance_projection enable row level security;
alter table private.position_projection enable row level security;
alter table private.execution_decisions enable row level security;
alter table private.risk_results enable row level security;
alter table private.order_intents enable row level security;
alter table private.execution_reconciliation_state enable row level security;
alter table private.order_reservations enable row level security;
alter table private.reservation_events enable row level security;
alter table private.order_attempts enable row level security;
alter table private.provider_order_bindings enable row level security;
alter table private.execution_observations enable row level security;
alter table private.quarantined_execution_observations enable row level security;
alter table private.order_events enable row level security;
alter table private.fills enable row level security;
alter table private.position_lots enable row level security;
alter table private.broker_observations enable row level security;
alter table private.reconciliation_runs enable row level security;
alter table private.reconciliation_breaks enable row level security;
alter table private.paper_execution_policies enable row level security;
alter table private.execution_cost_schedules enable row level security;
alter table private.provider_contract_registry enable row level security;
alter table private.market_calendars enable row level security;
alter table private.market_calendar_sessions enable row level security;
alter table private.paper_execution_model_registry enable row level security;
alter table private.account_snapshots enable row level security;
alter table private.position_movements enable row level security;
alter table private.step_up_grants enable row level security;
alter table private.operation_command_reviews enable row level security;
alter table private.operation_command_events enable row level security;
alter table private.access_change_requests enable row level security;
alter table private.qualifications enable row level security;

revoke all on all tables in schema private
  from public, anon, authenticated, service_role;
revoke all on all sequences in schema private
  from public, anon, authenticated, service_role;
revoke execute on all functions in schema private
  from public, anon, authenticated, service_role;
