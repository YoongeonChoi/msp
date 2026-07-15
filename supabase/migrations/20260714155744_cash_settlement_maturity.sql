-- G1 cash-settlement truth.
--
-- Positions and realized PnL remain trade-date accounting.  The cash leg is
-- recognized through payable/receivable clearing accounts and reaches CASH
-- only when an immutable settlement obligation matures.

begin;

lock table private.execution_controls in share row exclusive mode;
lock table private.worker_leases in share row exclusive mode;

do $$
begin
  if exists (
    select 1
    from private.execution_controls as control
    where control.execution_enabled is true
  ) then
    raise exception 'cash_settlement_upgrade_requires_disabled_controls'
      using errcode = '55000';
  end if;
  if exists (
    select 1
    from private.worker_leases as lease
    where lease.expires_at > clock_timestamp()
  ) then
    raise exception 'cash_settlement_upgrade_requires_no_active_worker_lease'
      using errcode = '55000';
  end if;
end;
$$;

alter table private.ledger_accounts
  drop constraint if exists ledger_accounts_ledger_code_check;
alter table private.ledger_accounts
  add constraint ledger_accounts_ledger_code_check check (
    ledger_code in (
      'CASH', 'OPENING_EQUITY', 'BROKER_CLEARING', 'POSITION_COST',
      'FEES', 'TAXES', 'REALIZED_PNL',
      'CASH_SETTLEMENT_RECEIVABLE', 'CASH_SETTLEMENT_PAYABLE'
    )
  );

insert into private.ledger_accounts (account_id, ledger_code, normal_side)
select account.account_id, chart.ledger_code, chart.normal_side
from private.trading_accounts as account
cross join (
  values
    ('CASH_SETTLEMENT_RECEIVABLE', 'debit'),
    ('CASH_SETTLEMENT_PAYABLE', 'credit')
) as chart(ledger_code, normal_side)
on conflict (account_id, ledger_code) do nothing;

alter table private.accounting_transactions
  drop constraint if exists accounting_transactions_source_type_check;
alter table private.accounting_transactions
  add constraint accounting_transactions_source_type_check check (
    source_type in (
      'opening_capital', 'fill', 'fee', 'tax', 'reservation', 'release',
      'adjustment', 'correction', 'settlement_reclassification',
      'cash_settlement'
    )
  );

alter table private.cash_balance_projection
  add column pending_credit_cash_krw numeric(24,4) not null default 0
    check (pending_credit_cash_krw >= 0);
alter table private.cash_balance_projection
  add column projected_settled_cash_krw numeric(24,4)
    generated always as (
      settled_cash_krw - pending_debit_cash_krw + pending_credit_cash_krw
    ) stored;

alter table private.account_snapshots
  add column pending_debit_cash_krw numeric(24,4) not null default 0
    check (pending_debit_cash_krw >= 0),
  add column pending_credit_cash_krw numeric(24,4) not null default 0
    check (pending_credit_cash_krw >= 0);

create table private.cash_settlement_cutovers (
  id uuid primary key,
  cutover_at timestamptz not null,
  cutover_business_date date not null,
  legacy_fill_count bigint not null check (legacy_fill_count >= 0),
  legacy_pending_count bigint not null check (legacy_pending_count >= 0),
  legacy_fill_sha256 text not null check (legacy_fill_sha256 ~ '^[0-9a-f]{64}$'),
  created_at timestamptz not null default clock_timestamp()
);

create table private.cash_settlement_obligations (
  id uuid primary key,
  fill_id uuid not null unique references private.fills(id),
  trade_accounting_transaction_id uuid not null unique
    references private.accounting_transactions(id),
  settlement_reclassification_transaction_id uuid not null unique
    references private.accounting_transactions(id),
  intent_id uuid not null references private.order_intents(id),
  account_id text not null references private.trading_accounts(account_id),
  environment text not null check (environment in ('paper', 'contract_test')),
  obligation_type text not null check (
    obligation_type in ('cash_payable', 'cash_receivable')
  ),
  amount_krw bigint not null check (amount_krw > 0),
  trade_at timestamptz not null,
  settlement_date date not null,
  obligation_sha256 text not null unique check (
    obligation_sha256 ~ '^[0-9a-f]{64}$'
  ),
  source_release_sha text not null check (
    source_release_sha ~ '^[0-9a-f]{40}([0-9a-f]{24})?$'
  ),
  created_at timestamptz not null default clock_timestamp(),
  constraint cash_settlement_obligation_date_check check (
    settlement_date >= (trade_at at time zone 'Asia/Seoul')::date
  )
);

create table private.cash_settlement_state (
  obligation_id uuid primary key
    references private.cash_settlement_obligations(id),
  state text not null default 'pending' check (
    state in ('pending', 'leased', 'settled', 'dead_letter')
  ),
  revision bigint not null default 0 check (revision >= 0),
  available_at timestamptz not null,
  attempt_count integer not null default 0 check (attempt_count >= 0),
  lease_owner text,
  lease_token uuid,
  lease_expires_at timestamptz,
  claim_release_sha text check (
    claim_release_sha is null
    or claim_release_sha ~ '^[0-9a-f]{40}([0-9a-f]{24})?$'
  ),
  claim_fencing_token bigint check (
    claim_fencing_token is null or claim_fencing_token > 0
  ),
  settlement_transaction_id uuid unique
    references private.accounting_transactions(id),
  last_error_code text,
  updated_at timestamptz not null default clock_timestamp(),
  constraint cash_settlement_state_shape_check check (
    (
      state = 'pending'
      and lease_owner is null and lease_token is null
      and lease_expires_at is null and claim_release_sha is null
      and claim_fencing_token is null
      and settlement_transaction_id is null
    )
    or (
      state = 'leased'
      and nullif(btrim(lease_owner), '') is not null
      and lease_token is not null and lease_expires_at is not null
      and claim_release_sha is not null and claim_fencing_token is not null
      and settlement_transaction_id is null
    )
    or (
      state = 'settled'
      and lease_owner is null and lease_token is null
      and lease_expires_at is null and claim_release_sha is null
      and claim_fencing_token is null
      and settlement_transaction_id is not null
      and last_error_code is null
    )
    or (
      state = 'dead_letter'
      and lease_owner is null and lease_token is null
      and lease_expires_at is null and claim_release_sha is null
      and claim_fencing_token is null
      and settlement_transaction_id is null
      and nullif(btrim(last_error_code), '') is not null
    )
  )
);

create table private.cash_settlement_events (
  id uuid primary key default gen_random_uuid(),
  obligation_id uuid not null references private.cash_settlement_obligations(id),
  event_sequence bigint not null check (event_sequence > 0),
  event_type text not null check (
    event_type in (
      'recognized', 'claimed', 'retry_scheduled', 'settled', 'dead_letter'
    )
  ),
  actor_type text not null check (actor_type in ('worker', 'system')),
  claim_token uuid,
  worker_id text,
  release_sha text check (
    release_sha is null or release_sha ~ '^[0-9a-f]{40}([0-9a-f]{24})?$'
  ),
  fencing_token bigint check (fencing_token is null or fencing_token > 0),
  settlement_transaction_id uuid references private.accounting_transactions(id),
  reason_code text,
  result_summary jsonb not null default '{}'::jsonb check (
    jsonb_typeof(result_summary) = 'object'
  ),
  event_sha256 text not null unique check (event_sha256 ~ '^[0-9a-f]{64}$'),
  occurred_at timestamptz not null,
  created_at timestamptz not null default clock_timestamp(),
  constraint cash_settlement_event_actor_check check (
    (
      actor_type = 'system'
      and worker_id is null and release_sha is null and fencing_token is null
    )
    or (
      actor_type = 'worker'
      and nullif(btrim(worker_id), '') is not null
      and release_sha is not null and fencing_token is not null
    )
  ),
  constraint cash_settlement_event_transaction_check check (
    (event_type = 'settled' and settlement_transaction_id is not null)
    or (event_type <> 'settled' and settlement_transaction_id is null)
  ),
  unique (obligation_id, event_sequence)
);

create unique index cash_settlement_one_settled_event
  on private.cash_settlement_events (obligation_id)
  where event_type = 'settled';
create index cash_settlement_claim_index
  on private.cash_settlement_state (available_at, obligation_id)
  where state in ('pending', 'leased');
create index cash_settlement_account_due_index
  on private.cash_settlement_obligations (account_id, settlement_date, id);

create function private.guard_cash_settlement_obligation_scope_v1()
returns trigger
language plpgsql
security invoker
set search_path = ''
as $$
begin
  if not exists (
    select 1
    from private.fills as fill
    join private.execution_observations as observation
      on observation.id = fill.event_id
    join private.order_intents as intent
      on intent.id = fill.intent_id
    join private.accounting_transactions as trade_transaction
      on trade_transaction.id = new.trade_accounting_transaction_id
    join private.accounting_transactions as reclassification_transaction
      on reclassification_transaction.id =
         new.settlement_reclassification_transaction_id
    where fill.id = new.fill_id
      and fill.intent_id = new.intent_id
      and fill.account_id = new.account_id
      and fill.filled_at = new.trade_at
      and fill.settlement_date = new.settlement_date
      and intent.id = new.intent_id
      and intent.account_id = new.account_id
      and intent.environment = new.environment
      and intent.release_sha = new.source_release_sha
      and new.obligation_type = case
        when intent.side = 'buy' then 'cash_payable'
        else 'cash_receivable'
      end
      and new.amount_krw = case
        when intent.side = 'buy' then
          fill.quantity * fill.price_krw
            + fill.commission_krw + fill.tax_krw
        else
          fill.quantity * fill.price_krw
            - fill.commission_krw - fill.tax_krw
      end
      and trade_transaction.account_id = new.account_id
      and trade_transaction.environment = new.environment
      and trade_transaction.source_type = 'fill'
      and trade_transaction.source_id = pg_catalog.encode(
        public.digest(
          pg_catalog.convert_to(
            intent.id::text || ':fill:' || observation.sequence::text,
            'UTF8'
          ),
          'sha256'
        ),
        'hex'
      )
      and trade_transaction.correlation_id = intent.correlation_id
      and trade_transaction.occurred_at = fill.filled_at
      and trade_transaction.release_sha = intent.release_sha
      and reclassification_transaction.account_id = new.account_id
      and reclassification_transaction.environment = new.environment
      and reclassification_transaction.source_type =
          'settlement_reclassification'
      and reclassification_transaction.source_id = pg_catalog.encode(
        public.digest(
          pg_catalog.convert_to(
            'cash-settlement-reclassification-v1|' || fill.id::text,
            'UTF8'
          ),
          'sha256'
        ),
        'hex'
      )
      and reclassification_transaction.correlation_id = intent.correlation_id
      and reclassification_transaction.occurred_at = fill.filled_at
      and reclassification_transaction.release_sha = intent.release_sha
  ) then
    raise exception 'cash_settlement_obligation_scope_mismatch'
      using errcode = '23514';
  end if;
  return new;
end;
$$;

create function private.populate_account_snapshot_pending_cash_v1()
returns trigger
language plpgsql
security invoker
set search_path = ''
as $$
begin
  select
    projection.pending_debit_cash_krw,
    projection.pending_credit_cash_krw
  into
    new.pending_debit_cash_krw,
    new.pending_credit_cash_krw
  from private.cash_balance_projection as projection
  where projection.account_id = new.account_id;
  if not found then
    raise exception 'account_snapshot_cash_projection_missing'
      using errcode = '23514';
  end if;
  return new;
end;
$$;

create trigger guard_cash_settlement_obligation_scope_v1
  before insert on private.cash_settlement_obligations
  for each row execute function
    private.guard_cash_settlement_obligation_scope_v1();

create trigger populate_account_snapshot_pending_cash_v1
  before insert on private.account_snapshots
  for each row execute function
    private.populate_account_snapshot_pending_cash_v1();

create or replace function private.guard_cash_settlement_state_transition_v1()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  if new.obligation_id is distinct from old.obligation_id
     or new.revision <> old.revision + 1
     or new.updated_at <= old.updated_at then
    raise exception 'cash_settlement_state_cas_invalid' using errcode = '40001';
  end if;
  if old.state in ('settled', 'dead_letter') then
    raise exception 'cash_settlement_terminal_state_is_immutable'
      using errcode = '23514';
  end if;
  if not (
    (old.state = 'pending' and new.state = 'leased')
    or (old.state = 'leased' and new.state in (
      'pending', 'leased', 'settled', 'dead_letter'
    ))
  ) then
    raise exception 'cash_settlement_state_transition_invalid'
      using errcode = '23514';
  end if;
  return new;
end;
$$;

create trigger guard_cash_settlement_state_transition_v1
  before update on private.cash_settlement_state
  for each row execute function
    private.guard_cash_settlement_state_transition_v1();

create trigger reject_cash_settlement_cutover_mutation
  before update or delete on private.cash_settlement_cutovers
  for each row execute function private.reject_append_only_mutation();
create trigger reject_cash_settlement_obligation_mutation
  before update or delete on private.cash_settlement_obligations
  for each row execute function private.reject_append_only_mutation();
create trigger reject_cash_settlement_event_mutation
  before update or delete on private.cash_settlement_events
  for each row execute function private.reject_append_only_mutation();

alter table private.cash_settlement_cutovers enable row level security;
alter table private.cash_settlement_obligations enable row level security;
alter table private.cash_settlement_state enable row level security;
alter table private.cash_settlement_events enable row level security;

-- Preserve the proven fill/position implementation behind a compatibility
-- name.  The new canonical wrapper performs an atomic reclassification from
-- trade-date CASH to the settlement clearing account before returning.
alter function private.post_accounting_transaction_impl(
  text, uuid, integer, timestamptz, jsonb, text, bigint
) rename to post_accounting_transaction_trade_date_legacy_impl;

create function private.post_accounting_transaction_impl(
  p_transaction_key text,
  p_intent_id uuid,
  p_observation_sequence integer,
  p_posted_at timestamptz,
  p_postings jsonb,
  p_holder_id text,
  p_fencing_token bigint
)
returns table (transaction_id uuid, inserted boolean)
language plpgsql
security definer
set search_path = ''
as $$
declare
  legacy_result record;
  intent_row private.order_intents%rowtype;
  observation_row private.execution_observations%rowtype;
  fill_row private.fills%rowtype;
  obligation_id_value uuid;
  obligation_type_value text;
  amount_value bigint;
  reclassification_key text;
  reclassification_id uuid;
  obligation_hash text;
  snapshot_sequence bigint;
begin
  perform private.require_service_role();

  select * into legacy_result
  from private.post_accounting_transaction_trade_date_legacy_impl(
    p_transaction_key, p_intent_id, p_observation_sequence, p_posted_at,
    p_postings, p_holder_id, p_fencing_token
  );

  select * into intent_row
  from private.order_intents
  where id = p_intent_id;
  select * into observation_row
  from private.execution_observations
  where intent_id = p_intent_id and sequence = p_observation_sequence;
  select * into fill_row
  from private.fills
  where event_id = observation_row.id;
  if intent_row.id is null or observation_row.id is null or fill_row.id is null then
    raise exception 'cash_settlement_fill_context_missing' using errcode = 'P0002';
  end if;

  if legacy_result.inserted is false then
    if not exists (
      select 1
      from private.cash_settlement_obligations as obligation
      where obligation.fill_id = fill_row.id
        and obligation.trade_accounting_transaction_id = legacy_result.transaction_id
    ) then
      raise exception 'cash_settlement_obligation_missing_for_fill_replay'
        using errcode = '23514';
    end if;
    return query select legacy_result.transaction_id, false;
    return;
  end if;

  obligation_type_value := case
    when intent_row.side = 'buy' then 'cash_payable'
    else 'cash_receivable'
  end;
  amount_value := case
    when intent_row.side = 'buy' then
      fill_row.quantity * fill_row.price_krw
        + fill_row.commission_krw + fill_row.tax_krw
    else
      fill_row.quantity * fill_row.price_krw
        - fill_row.commission_krw - fill_row.tax_krw
  end;
  if amount_value <= 0 then
    raise exception 'cash_settlement_amount_must_be_positive'
      using errcode = '23514';
  end if;

  reclassification_key := pg_catalog.encode(
    public.digest(
      pg_catalog.convert_to(
        'cash-settlement-reclassification-v1|' || fill_row.id::text,
        'UTF8'
      ),
      'sha256'
    ),
    'hex'
  );
  insert into private.accounting_transactions (
    account_id, environment, source_type, source_id, correlation_id,
    occurred_at, posted_at, release_sha
  ) values (
    intent_row.account_id, intent_row.environment,
    'settlement_reclassification', reclassification_key,
    intent_row.correlation_id, fill_row.filled_at, p_posted_at,
    intent_row.release_sha
  ) returning id into reclassification_id;

  if obligation_type_value = 'cash_payable' then
    insert into private.accounting_postings (
      journal_entry_id, ledger_account_id, side, amount_krw
    )
    select reclassification_id, ledger.id, vector.side, amount_value
    from (
      values
        ('CASH', 'debit'),
        ('CASH_SETTLEMENT_PAYABLE', 'credit')
    ) as vector(ledger_code, side)
    join private.ledger_accounts as ledger
      on ledger.account_id = intent_row.account_id
     and ledger.ledger_code = vector.ledger_code;
    if not found then
      raise exception 'cash_settlement_reclassification_ledger_missing'
        using errcode = '23514';
    end if;

    update private.cash_balance_projection
    set settled_cash_krw = settled_cash_krw + amount_value,
        pending_debit_cash_krw = pending_debit_cash_krw + amount_value,
        last_journal_entry_id = reclassification_id,
        projection_version = projection_version + 1,
        projected_at = p_posted_at
    where account_id = intent_row.account_id;
  else
    insert into private.accounting_postings (
      journal_entry_id, ledger_account_id, side, amount_krw
    )
    select reclassification_id, ledger.id, vector.side, amount_value
    from (
      values
        ('CASH_SETTLEMENT_RECEIVABLE', 'debit'),
        ('CASH', 'credit')
    ) as vector(ledger_code, side)
    join private.ledger_accounts as ledger
      on ledger.account_id = intent_row.account_id
     and ledger.ledger_code = vector.ledger_code;
    if not found then
      raise exception 'cash_settlement_reclassification_ledger_missing'
        using errcode = '23514';
    end if;

    update private.cash_balance_projection
    set settled_cash_krw = settled_cash_krw - amount_value,
        pending_credit_cash_krw = pending_credit_cash_krw + amount_value,
        last_journal_entry_id = reclassification_id,
        projection_version = projection_version + 1,
        projected_at = p_posted_at
    where account_id = intent_row.account_id
      and settled_cash_krw >= amount_value
      and settled_cash_krw - amount_value
        >= reserved_cash_krw + pending_debit_cash_krw;
  end if;
  if not found then
    raise exception 'cash_settlement_projection_reclassification_failed'
      using errcode = '23514';
  end if;

  obligation_id_value := md5(
    'cash-settlement-obligation-v1:' || fill_row.id::text
  )::uuid;
  obligation_hash := pg_catalog.encode(
    public.digest(
      pg_catalog.convert_to(
        concat_ws(
          '|', 'cash-settlement-obligation-v1', fill_row.id::text,
          legacy_result.transaction_id::text, reclassification_id::text,
          intent_row.id::text, intent_row.account_id, intent_row.environment,
          obligation_type_value, amount_value::text,
          private.utc_iso8601(fill_row.filled_at),
          fill_row.settlement_date::text, intent_row.release_sha
        ),
        'UTF8'
      ),
      'sha256'
    ),
    'hex'
  );
  insert into private.cash_settlement_obligations (
    id, fill_id, trade_accounting_transaction_id,
    settlement_reclassification_transaction_id, intent_id, account_id,
    environment, obligation_type, amount_krw, trade_at, settlement_date,
    obligation_sha256, source_release_sha
  ) values (
    obligation_id_value, fill_row.id, legacy_result.transaction_id,
    reclassification_id, intent_row.id, intent_row.account_id,
    intent_row.environment, obligation_type_value, amount_value,
    fill_row.filled_at, fill_row.settlement_date, obligation_hash,
    intent_row.release_sha
  );
  insert into private.cash_settlement_state (
    obligation_id, state, revision, available_at, attempt_count, updated_at
  ) values (
    obligation_id_value, 'pending', 0,
    fill_row.settlement_date::timestamp at time zone 'Asia/Seoul',
    0, p_posted_at
  );
  insert into private.cash_settlement_events (
    obligation_id, event_sequence, event_type, actor_type,
    result_summary, event_sha256, occurred_at
  ) values (
    obligation_id_value, 1, 'recognized', 'system',
    jsonb_build_object(
      'schema_version', 1,
      'fill_id', fill_row.id,
      'trade_accounting_transaction_id', legacy_result.transaction_id,
      'settlement_reclassification_transaction_id', reclassification_id,
      'obligation_type', obligation_type_value,
      'amount_krw', amount_value,
      'settlement_date', fill_row.settlement_date
    ),
    pg_catalog.encode(
      public.digest(
        pg_catalog.convert_to(
          obligation_id_value::text || '|1|recognized|' || obligation_hash,
          'UTF8'
        ),
        'sha256'
      ),
      'hex'
    ),
    p_posted_at
  );

  select coalesce(max(snapshot.sequence), 0) + 1
  into snapshot_sequence
  from private.account_snapshots as snapshot
  where snapshot.account_id = intent_row.account_id
    and snapshot.environment = intent_row.environment;
  insert into private.account_snapshots (
    account_id, environment, sequence, cash_krw, reserved_cash_krw,
    pending_debit_cash_krw, pending_credit_cash_krw,
    positions_sha256, source_type, source_id, observed_at
  )
  select
    intent_row.account_id,
    intent_row.environment,
    snapshot_sequence,
    balance.settled_cash_krw,
    balance.reserved_cash_krw,
    balance.pending_debit_cash_krw,
    balance.pending_credit_cash_krw,
    pg_catalog.encode(
      public.digest(
        pg_catalog.convert_to(
          coalesce(string_agg(
            position.symbol || ':' || position.quantity::text,
            '|' order by position.symbol
          ), ''),
          'UTF8'
        ),
        'sha256'
      ),
      'hex'
    ),
    'ledger_projection',
    reclassification_id,
    p_posted_at
  from private.cash_balance_projection as balance
  left join private.position_projection as position
    on position.account_id = balance.account_id
  where balance.account_id = intent_row.account_id
  group by
    balance.settled_cash_krw, balance.reserved_cash_krw,
    balance.pending_debit_cash_krw, balance.pending_credit_cash_krw;

  perform private.write_audit_event(
    'worker', null, null, 'trading_worker', p_holder_id, intent_row.release_sha,
    'cash_settlement_obligation_recognized', 'cash_settlement_obligation',
    obligation_id_value::text, intent_row.correlation_id, null,
    'trade_date_cash_reclassified', null,
    array[
      'fill_id', 'settlement_date', 'obligation_type', 'amount_krw',
      'settlement_reclassification_transaction_id'
    ],
    null, obligation_hash, null
  );

  return query select legacy_result.transaction_id, true;
end;
$$;

create function private.list_due_cash_settlement_accounts_impl(
  p_now timestamptz,
  p_limit integer
)
returns table (
  account_id text,
  environment text,
  due_count bigint,
  oldest_settlement_date date
)
language plpgsql
security definer
set search_path = ''
as $$
declare
  authorization_time timestamptz := clock_timestamp();
begin
  perform private.require_service_role();
  if p_limit not between 1 and 100
     or p_now < authorization_time - interval '5 minutes'
     or p_now > authorization_time + interval '30 seconds' then
    raise exception 'cash_settlement_due_account_parameters_invalid'
      using errcode = '22023';
  end if;
  return query
  select
    obligation.account_id,
    obligation.environment,
    count(*)::bigint,
    min(obligation.settlement_date)
  from private.cash_settlement_obligations as obligation
  join private.cash_settlement_state as state
    on state.obligation_id = obligation.id
  where state.state in ('pending', 'leased')
    and obligation.settlement_date
      <= (authorization_time at time zone 'Asia/Seoul')::date
    and (
      (state.state = 'pending' and state.available_at <= authorization_time)
      or state.state = 'leased'
    )
  group by obligation.account_id, obligation.environment
  order by min(obligation.settlement_date), obligation.account_id
  limit p_limit;
end;
$$;

create function private.claim_cash_settlement_batch_impl(
  p_account_id text,
  p_holder_id text,
  p_release_sha text,
  p_fencing_token bigint,
  p_now timestamptz,
  p_limit integer
)
returns table (
  obligation_id uuid,
  fill_id uuid,
  intent_id uuid,
  account_id text,
  environment text,
  obligation_type text,
  amount_krw bigint,
  settlement_date date,
  obligation_sha256 text,
  revision bigint,
  claim_token uuid,
  claim_expires_at timestamptz
)
language plpgsql
security definer
set search_path = ''
as $$
declare
  authorization_time timestamptz := clock_timestamp();
  candidate record;
  claimed_state private.cash_settlement_state%rowtype;
  token_value uuid;
  next_sequence bigint;
begin
  perform private.require_service_role();
  if nullif(btrim(p_account_id), '') is null
     or p_holder_id !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
     or p_release_sha !~ '^[0-9a-f]{40}([0-9a-f]{24})?$'
     or p_fencing_token <= 0
     or p_limit not between 1 and 100
     or p_now < authorization_time - interval '5 minutes'
     or p_now > authorization_time + interval '30 seconds' then
    raise exception 'cash_settlement_claim_parameters_invalid'
      using errcode = '22023';
  end if;
  if not exists (
    select 1
    from private.worker_leases as lease
    where lease.account_id = p_account_id
      and lease.holder_id = p_holder_id
      and lease.release_sha = p_release_sha
      and lease.fencing_token = p_fencing_token
      and lease.expires_at > authorization_time
  ) then
    raise exception 'cash_settlement_worker_lease_missing_or_stale'
      using errcode = '40001';
  end if;

  for candidate in
    select obligation.*, state.*
    from private.cash_settlement_obligations as obligation
    join private.cash_settlement_state as state
      on state.obligation_id = obligation.id
    where obligation.account_id = p_account_id
      and obligation.settlement_date
        <= (authorization_time at time zone 'Asia/Seoul')::date
      and (
        (state.state = 'pending' and state.available_at <= authorization_time)
        or (
          state.state = 'leased'
          and (
            state.lease_expires_at <= authorization_time
            or state.lease_owner = p_holder_id
          )
        )
      )
    order by obligation.settlement_date, obligation.trade_at, obligation.id
    limit p_limit
    for update of state skip locked
  loop
    if candidate.state = 'leased'
       and candidate.lease_owner = p_holder_id
       and candidate.claim_release_sha = p_release_sha
       and candidate.claim_fencing_token = p_fencing_token
       and candidate.lease_expires_at > authorization_time then
      select * into claimed_state
      from private.cash_settlement_state
      where private.cash_settlement_state.obligation_id = candidate.id;
    else
      token_value := gen_random_uuid();
      update private.cash_settlement_state as state
      set state = 'leased',
          revision = state.revision + 1,
          attempt_count = state.attempt_count + 1,
          lease_owner = p_holder_id,
          lease_token = token_value,
          lease_expires_at = authorization_time + interval '30 seconds',
          claim_release_sha = p_release_sha,
          claim_fencing_token = p_fencing_token,
          last_error_code = null,
          updated_at = greatest(
            authorization_time, state.updated_at + interval '1 microsecond'
          )
      where state.obligation_id = candidate.id
      returning * into claimed_state;

      select coalesce(max(event.event_sequence), 0) + 1
      into next_sequence
      from private.cash_settlement_events as event
      where event.obligation_id = candidate.id;
      insert into private.cash_settlement_events (
        obligation_id, event_sequence, event_type, actor_type,
        claim_token, worker_id, release_sha, fencing_token,
        result_summary, event_sha256, occurred_at
      ) values (
        candidate.id, next_sequence, 'claimed', 'worker',
        claimed_state.lease_token, p_holder_id, p_release_sha,
        p_fencing_token,
        jsonb_build_object(
          'schema_version', 1,
          'revision', claimed_state.revision,
          'attempt_count', claimed_state.attempt_count,
          'claim_expires_at', claimed_state.lease_expires_at,
          'reclaimed', candidate.state = 'leased'
        ),
        pg_catalog.encode(
          public.digest(
            pg_catalog.convert_to(
              candidate.id::text || '|' || next_sequence::text
                || '|claimed|' || claimed_state.lease_token::text
                || '|' || claimed_state.revision::text,
              'UTF8'
            ),
            'sha256'
          ),
          'hex'
        ),
        authorization_time
      );
      perform private.write_audit_event(
        'worker', null, null, 'trading_worker', p_holder_id, p_release_sha,
        'cash_settlement_claimed', 'cash_settlement_obligation',
        candidate.id::text, candidate.intent_id, null,
        case when candidate.state = 'leased' then 'lease_reclaimed' else 'claimed' end,
        null,
        array['revision', 'attempt_count', 'lease_token', 'lease_expires_at'],
        null, candidate.obligation_sha256, null
      );
    end if;

    return query select
      candidate.id,
      candidate.fill_id,
      candidate.intent_id,
      candidate.account_id,
      candidate.environment,
      candidate.obligation_type,
      candidate.amount_krw,
      candidate.settlement_date,
      candidate.obligation_sha256,
      claimed_state.revision,
      claimed_state.lease_token,
      claimed_state.lease_expires_at;
  end loop;
end;
$$;

create function private.complete_cash_settlement_impl(
  p_obligation_id uuid,
  p_expected_revision bigint,
  p_claim_token uuid,
  p_holder_id text,
  p_release_sha text,
  p_fencing_token bigint,
  p_now timestamptz
)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  authorization_time timestamptz := clock_timestamp();
  state_row private.cash_settlement_state%rowtype;
  obligation_row private.cash_settlement_obligations%rowtype;
  intent_row private.order_intents%rowtype;
  settled_event private.cash_settlement_events%rowtype;
  settlement_source_id text;
  settlement_transaction_id_value uuid;
  snapshot_sequence bigint;
  next_sequence bigint;
  event_hash text;
  receipt jsonb;
begin
  perform private.require_service_role();
  if p_obligation_id is null
     or p_expected_revision < 1
     or p_claim_token is null
     or p_holder_id !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
     or p_release_sha !~ '^[0-9a-f]{40}([0-9a-f]{24})?$'
     or p_fencing_token <= 0
     or p_now < authorization_time - interval '5 minutes'
     or p_now > authorization_time + interval '30 seconds' then
    raise exception 'cash_settlement_complete_parameters_invalid'
      using errcode = '22023';
  end if;

  select state.* into state_row
  from private.cash_settlement_state as state
  where state.obligation_id = p_obligation_id
  for update;
  if not found then
    raise exception 'cash_settlement_obligation_not_found'
      using errcode = 'P0002';
  end if;
  select obligation.* into obligation_row
  from private.cash_settlement_obligations as obligation
  where obligation.id = p_obligation_id;
  select intent.* into intent_row
  from private.order_intents as intent
  where intent.id = obligation_row.intent_id;

  if state_row.state = 'settled' then
    select event.* into settled_event
    from private.cash_settlement_events as event
    where event.obligation_id = p_obligation_id
      and event.event_type = 'settled';
    if settled_event.id is null
       or settled_event.claim_token is distinct from p_claim_token
       or settled_event.worker_id is distinct from p_holder_id
       or settled_event.release_sha is distinct from p_release_sha
       or settled_event.fencing_token is distinct from p_fencing_token
       or (settled_event.result_summary->>'claim_revision')::bigint
          is distinct from p_expected_revision then
      raise exception 'cash_settlement_terminal_replay_identity_mismatch'
        using errcode = '40001';
    end if;
    return settled_event.result_summary || jsonb_build_object('replayed', true);
  end if;

  if state_row.state <> 'leased'
     or state_row.revision <> p_expected_revision
     or state_row.lease_token is distinct from p_claim_token
     or state_row.lease_owner is distinct from p_holder_id
     or state_row.claim_release_sha is distinct from p_release_sha
     or state_row.claim_fencing_token is distinct from p_fencing_token
     or state_row.lease_expires_at <= authorization_time then
    raise exception 'cash_settlement_claim_not_owned_current_or_expired'
      using errcode = '40001';
  end if;
  if obligation_row.settlement_date
       > (authorization_time at time zone 'Asia/Seoul')::date then
    raise exception 'cash_settlement_obligation_not_due'
      using errcode = '22023';
  end if;
  if not exists (
    select 1
    from private.worker_leases as lease
    where lease.account_id = obligation_row.account_id
      and lease.holder_id = p_holder_id
      and lease.release_sha = p_release_sha
      and lease.fencing_token = p_fencing_token
      and lease.expires_at > authorization_time
  ) then
    raise exception 'cash_settlement_worker_lease_missing_or_stale'
      using errcode = '40001';
  end if;

  settlement_source_id := pg_catalog.encode(
    public.digest(
      pg_catalog.convert_to(
        'cash-settlement-v1|' || obligation_row.id::text,
        'UTF8'
      ),
      'sha256'
    ),
    'hex'
  );
  insert into private.accounting_transactions (
    account_id, environment, source_type, source_id, correlation_id,
    occurred_at, posted_at, release_sha
  ) values (
    obligation_row.account_id, obligation_row.environment,
    'cash_settlement', settlement_source_id, intent_row.correlation_id,
    obligation_row.settlement_date::timestamp at time zone 'Asia/Seoul',
    authorization_time, p_release_sha
  ) returning id into settlement_transaction_id_value;

  if obligation_row.obligation_type = 'cash_payable' then
    insert into private.accounting_postings (
      journal_entry_id, ledger_account_id, side, amount_krw
    )
    select settlement_transaction_id_value, ledger.id, vector.side,
      obligation_row.amount_krw
    from (
      values
        ('CASH_SETTLEMENT_PAYABLE', 'debit'),
        ('CASH', 'credit')
    ) as vector(ledger_code, side)
    join private.ledger_accounts as ledger
      on ledger.account_id = obligation_row.account_id
     and ledger.ledger_code = vector.ledger_code;

    update private.cash_balance_projection as balance
    set settled_cash_krw = balance.settled_cash_krw
          - obligation_row.amount_krw,
        pending_debit_cash_krw = balance.pending_debit_cash_krw
          - obligation_row.amount_krw,
        last_journal_entry_id = settlement_transaction_id_value,
        projection_version = balance.projection_version + 1,
        projected_at = authorization_time
    where balance.account_id = obligation_row.account_id
      and balance.settled_cash_krw >= obligation_row.amount_krw
      and balance.pending_debit_cash_krw >= obligation_row.amount_krw
      and balance.settled_cash_krw - obligation_row.amount_krw
        >= balance.reserved_cash_krw
          + balance.pending_debit_cash_krw - obligation_row.amount_krw;
  else
    insert into private.accounting_postings (
      journal_entry_id, ledger_account_id, side, amount_krw
    )
    select settlement_transaction_id_value, ledger.id, vector.side,
      obligation_row.amount_krw
    from (
      values
        ('CASH', 'debit'),
        ('CASH_SETTLEMENT_RECEIVABLE', 'credit')
    ) as vector(ledger_code, side)
    join private.ledger_accounts as ledger
      on ledger.account_id = obligation_row.account_id
     and ledger.ledger_code = vector.ledger_code;

    update private.cash_balance_projection as balance
    set settled_cash_krw = balance.settled_cash_krw
          + obligation_row.amount_krw,
        pending_credit_cash_krw = balance.pending_credit_cash_krw
          - obligation_row.amount_krw,
        last_journal_entry_id = settlement_transaction_id_value,
        projection_version = balance.projection_version + 1,
        projected_at = authorization_time
    where balance.account_id = obligation_row.account_id
      and balance.pending_credit_cash_krw >= obligation_row.amount_krw;
  end if;
  if not found then
    raise exception 'cash_settlement_projection_maturity_failed'
      using errcode = '23514';
  end if;

  select coalesce(max(snapshot.sequence), 0) + 1
  into snapshot_sequence
  from private.account_snapshots as snapshot
  where snapshot.account_id = obligation_row.account_id
    and snapshot.environment = obligation_row.environment;
  insert into private.account_snapshots (
    account_id, environment, sequence, cash_krw, reserved_cash_krw,
    pending_debit_cash_krw, pending_credit_cash_krw,
    positions_sha256, source_type, source_id, observed_at
  )
  select
    obligation_row.account_id,
    obligation_row.environment,
    snapshot_sequence,
    balance.settled_cash_krw,
    balance.reserved_cash_krw,
    balance.pending_debit_cash_krw,
    balance.pending_credit_cash_krw,
    pg_catalog.encode(
      public.digest(
        pg_catalog.convert_to(
          coalesce(string_agg(
            position.symbol || ':' || position.quantity::text,
            '|' order by position.symbol
          ), ''),
          'UTF8'
        ),
        'sha256'
      ),
      'hex'
    ),
    'ledger_projection',
    settlement_transaction_id_value,
    authorization_time
  from private.cash_balance_projection as balance
  left join private.position_projection as position
    on position.account_id = balance.account_id
  where balance.account_id = obligation_row.account_id
  group by
    balance.settled_cash_krw, balance.reserved_cash_krw,
    balance.pending_debit_cash_krw, balance.pending_credit_cash_krw;

  update private.cash_settlement_state as state
  set state = 'settled',
      revision = state.revision + 1,
      lease_owner = null,
      lease_token = null,
      lease_expires_at = null,
      claim_release_sha = null,
      claim_fencing_token = null,
      settlement_transaction_id = settlement_transaction_id_value,
      last_error_code = null,
      updated_at = greatest(
        authorization_time, state.updated_at + interval '1 microsecond'
      )
  where state.obligation_id = p_obligation_id
    and state.revision = p_expected_revision
  returning * into state_row;
  if not found then
    raise exception 'cash_settlement_state_cas_failed'
      using errcode = '40001';
  end if;

  receipt := jsonb_build_object(
    'schema_version', 1,
    'obligation_id', p_obligation_id,
    'settlement_transaction_id', settlement_transaction_id_value,
    'claim_revision', p_expected_revision,
    'settled_revision', state_row.revision,
    'obligation_type', obligation_row.obligation_type,
    'amount_krw', obligation_row.amount_krw,
    'settlement_date', obligation_row.settlement_date,
    'settled_at', authorization_time,
    'replayed', false
  );
  select coalesce(max(event.event_sequence), 0) + 1
  into next_sequence
  from private.cash_settlement_events as event
  where event.obligation_id = p_obligation_id;
  event_hash := pg_catalog.encode(
    public.digest(
      pg_catalog.convert_to(
        p_obligation_id::text || '|' || next_sequence::text
          || '|settled|' || p_claim_token::text
          || '|' || settlement_transaction_id_value::text,
        'UTF8'
      ),
      'sha256'
    ),
    'hex'
  );
  insert into private.cash_settlement_events (
    obligation_id, event_sequence, event_type, actor_type,
    claim_token, worker_id, release_sha, fencing_token,
    settlement_transaction_id, result_summary, event_sha256, occurred_at
  ) values (
    p_obligation_id, next_sequence, 'settled', 'worker',
    p_claim_token, p_holder_id, p_release_sha, p_fencing_token,
    settlement_transaction_id_value, receipt, event_hash,
    authorization_time
  );

  perform private.write_audit_event(
    'worker', null, null, 'trading_worker', p_holder_id, p_release_sha,
    'cash_settlement_completed', 'cash_settlement_obligation',
    p_obligation_id::text, intent_row.correlation_id, null,
    'settled_on_maturity', null,
    array[
      'state', 'revision', 'settlement_transaction_id', 'settlement_date'
    ],
    obligation_row.obligation_sha256, event_hash, null
  );
  insert into private.delivery_outbox (
    event_type, aggregate_type, aggregate_id, dedupe_key, payload,
    destination_type, available_at
  ) values (
    'cash_settlement_completed', 'cash_settlement_obligation',
    p_obligation_id::text, 'cash-settlement-completed:' || p_obligation_id::text,
    receipt, 'operations_metric', authorization_time
  ) on conflict (dedupe_key) do nothing;

  return receipt;
end;
$$;

create function private.fail_cash_settlement_attempt_impl(
  p_obligation_id uuid,
  p_expected_revision bigint,
  p_claim_token uuid,
  p_holder_id text,
  p_release_sha text,
  p_fencing_token bigint,
  p_now timestamptz,
  p_error_code text
)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  authorization_time timestamptz := clock_timestamp();
  state_row private.cash_settlement_state%rowtype;
  obligation_row private.cash_settlement_obligations%rowtype;
  intent_row private.order_intents%rowtype;
  replay_event private.cash_settlement_events%rowtype;
  next_state text;
  next_available timestamptz;
  next_sequence bigint;
  event_hash text;
  result_value jsonb;
  run_id_value uuid;
  break_id_value uuid;
  incident_id_value uuid;
begin
  perform private.require_service_role();
  if p_obligation_id is null
     or p_expected_revision < 1
     or p_claim_token is null
     or p_holder_id !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
     or p_release_sha !~ '^[0-9a-f]{40}([0-9a-f]{24})?$'
     or p_fencing_token <= 0
     or p_now < authorization_time - interval '5 minutes'
     or p_now > authorization_time + interval '30 seconds'
     or p_error_code not in (
       'settlement_dependency_unavailable',
       'settlement_projection_conflict',
       'settlement_worker_error'
     ) then
    raise exception 'cash_settlement_failure_parameters_invalid'
      using errcode = '22023';
  end if;

  select state.* into state_row
  from private.cash_settlement_state as state
  where state.obligation_id = p_obligation_id
  for update;
  if not found then
    raise exception 'cash_settlement_obligation_not_found'
      using errcode = 'P0002';
  end if;
  select obligation.* into obligation_row
  from private.cash_settlement_obligations as obligation
  where obligation.id = p_obligation_id;
  select intent.* into intent_row
  from private.order_intents as intent
  where intent.id = obligation_row.intent_id;
  if state_row.state in ('pending', 'dead_letter') then
    select event.* into replay_event
    from private.cash_settlement_events as event
    where event.obligation_id = p_obligation_id
      and event.event_type in ('retry_scheduled', 'dead_letter')
      and event.claim_token = p_claim_token
      and event.worker_id = p_holder_id
      and event.release_sha = p_release_sha
      and event.fencing_token = p_fencing_token
      and (event.result_summary->>'claim_revision')::bigint
        = p_expected_revision
    order by event.event_sequence desc
    limit 1;
    if replay_event.id is not null
       and (replay_event.result_summary->>'revision')::bigint
          = state_row.revision
       and replay_event.result_summary->>'state' = state_row.state then
      return replay_event.result_summary || jsonb_build_object('replayed', true);
    end if;
  end if;
  if state_row.state <> 'leased'
     or state_row.revision <> p_expected_revision
     or state_row.lease_token is distinct from p_claim_token
     or state_row.lease_owner is distinct from p_holder_id
     or state_row.claim_release_sha is distinct from p_release_sha
     or state_row.claim_fencing_token is distinct from p_fencing_token
     or state_row.lease_expires_at <= authorization_time then
    raise exception 'cash_settlement_claim_not_owned_current_or_expired'
      using errcode = '40001';
  end if;
  if not exists (
    select 1
    from private.worker_leases as lease
    where lease.account_id = obligation_row.account_id
      and lease.holder_id = p_holder_id
      and lease.release_sha = p_release_sha
      and lease.fencing_token = p_fencing_token
      and lease.expires_at > authorization_time
  ) then
    raise exception 'cash_settlement_worker_lease_missing_or_stale'
      using errcode = '40001';
  end if;

  next_state := case
    when state_row.attempt_count >= 8 then 'dead_letter'
    else 'pending'
  end;
  next_available := case
    when next_state = 'dead_letter' then state_row.available_at
    else authorization_time + make_interval(
      secs => least(
        900,
        (5 * power(2::numeric, greatest(state_row.attempt_count - 1, 0)))::integer
      )
    )
  end;
  update private.cash_settlement_state as state
  set state = next_state,
      revision = state.revision + 1,
      available_at = next_available,
      lease_owner = null,
      lease_token = null,
      lease_expires_at = null,
      claim_release_sha = null,
      claim_fencing_token = null,
      last_error_code = p_error_code,
      updated_at = greatest(
        authorization_time, state.updated_at + interval '1 microsecond'
      )
  where state.obligation_id = p_obligation_id
    and state.revision = p_expected_revision
  returning * into state_row;
  if not found then
    raise exception 'cash_settlement_state_cas_failed'
      using errcode = '40001';
  end if;

  result_value := jsonb_build_object(
    'schema_version', 1,
    'obligation_id', p_obligation_id,
    'claim_revision', p_expected_revision,
    'revision', state_row.revision,
    'state', next_state,
    'attempt_count', state_row.attempt_count,
    'available_at', next_available,
    'error_code', p_error_code,
    'replayed', false
  );
  select coalesce(max(event.event_sequence), 0) + 1
  into next_sequence
  from private.cash_settlement_events as event
  where event.obligation_id = p_obligation_id;
  event_hash := pg_catalog.encode(
    public.digest(
      pg_catalog.convert_to(
        p_obligation_id::text || '|' || next_sequence::text || '|'
          || case when next_state = 'dead_letter'
                  then 'dead_letter' else 'retry_scheduled' end
          || '|' || p_claim_token::text || '|' || p_error_code,
        'UTF8'
      ),
      'sha256'
    ),
    'hex'
  );
  insert into private.cash_settlement_events (
    obligation_id, event_sequence, event_type, actor_type,
    claim_token, worker_id, release_sha, fencing_token, reason_code,
    result_summary, event_sha256, occurred_at
  ) values (
    p_obligation_id, next_sequence,
    case when next_state = 'dead_letter'
      then 'dead_letter' else 'retry_scheduled' end,
    'worker', p_claim_token, p_holder_id, p_release_sha,
    p_fencing_token, p_error_code, result_value, event_hash,
    authorization_time
  );

  if next_state = 'dead_letter' then
    run_id_value := md5(
      'cash-settlement-dead-letter-run:' || p_obligation_id::text
    )::uuid;
    break_id_value := md5(
      'cash-settlement-dead-letter-break:' || p_obligation_id::text
    )::uuid;
    incident_id_value := md5(
      'cash-settlement-dead-letter-incident:' || p_obligation_id::text
    )::uuid;
    insert into private.reconciliation_runs (
      id, account_id, environment, started_at, completed_at, result,
      release_sha
    ) values (
      run_id_value, obligation_row.account_id, obligation_row.environment,
      authorization_time, authorization_time, 'breaks_found', p_release_sha
    ) on conflict (id) do nothing;
    insert into private.reconciliation_breaks (
      id, run_id, account_id, break_type, state, detected_at, summary_code
    ) values (
      break_id_value, run_id_value, obligation_row.account_id,
      'cash', 'open', authorization_time, p_error_code
    ) on conflict (id) do nothing;
    insert into private.incidents (
      id, severity, incident_type, summary_code, correlation_id, opened_at
    ) values (
      incident_id_value, 'critical', 'cash_settlement_dead_letter',
      p_error_code, intent_row.correlation_id, authorization_time
    ) on conflict (id) do nothing;
    insert into private.delivery_outbox (
      event_type, aggregate_type, aggregate_id, dedupe_key, payload,
      destination_type, available_at
    ) values (
      'cash_settlement_dead_letter', 'cash_settlement_obligation',
      p_obligation_id::text,
      'cash-settlement-dead-letter:' || p_obligation_id::text,
      result_value || jsonb_build_object(
        'reconciliation_run_id', run_id_value,
        'reconciliation_break_id', break_id_value,
        'incident_id', incident_id_value,
        'severity', 'critical'
      ),
      'incident_alert', authorization_time
    ) on conflict (dedupe_key) do nothing;
  end if;

  perform private.write_audit_event(
    'worker', null, null, 'trading_worker', p_holder_id, p_release_sha,
    case when next_state = 'dead_letter'
      then 'cash_settlement_dead_lettered'
      else 'cash_settlement_retry_scheduled' end,
    'cash_settlement_obligation', p_obligation_id::text,
    intent_row.correlation_id, null, p_error_code, null,
    array['state', 'revision', 'attempt_count', 'available_at'],
    obligation_row.obligation_sha256, event_hash, null
  );
  return result_value;
end;
$$;

-- Backfill only by appending compensating journals and immutable settlement
-- evidence.  Any legacy proceeds that can no longer be separated from settled
-- cash abort the entire migration instead of manufacturing a false balance.
do $$
declare
  cutover_id_value uuid := gen_random_uuid();
  cutover_time timestamptz := clock_timestamp();
  cutover_date date;
  fill_context record;
  accounting_source_id text;
  trade_transaction_id_value uuid;
  reclassification_source_id text;
  reclassification_transaction_id_value uuid;
  settlement_source_id text;
  settlement_transaction_id_value uuid;
  obligation_id_value uuid;
  obligation_type_value text;
  amount_value bigint;
  obligation_hash text;
  cash_posting_count bigint;
  cash_debit numeric(24,4);
  cash_credit numeric(24,4);
  fill_count_value bigint;
  pending_count_value bigint;
  fill_digest text;
  snapshot_sequence bigint;
begin
  cutover_date := (cutover_time at time zone 'Asia/Seoul')::date;
  for fill_context in
    select
      fill.id as fill_id,
      fill.quantity,
      fill.price_krw,
      fill.commission_krw,
      fill.tax_krw,
      fill.filled_at,
      fill.settlement_date,
      observation.sequence as observation_sequence,
      intent.id as intent_id,
      intent.account_id,
      intent.environment,
      intent.side,
      intent.correlation_id,
      intent.release_sha
    from private.fills as fill
    join private.execution_observations as observation
      on observation.id = fill.event_id
    join private.order_intents as intent
      on intent.id = fill.intent_id
    order by fill.filled_at, fill.id
  loop
    accounting_source_id := pg_catalog.encode(
      public.digest(
        pg_catalog.convert_to(
          fill_context.intent_id::text || ':fill:'
            || fill_context.observation_sequence::text,
          'UTF8'
        ),
        'sha256'
      ),
      'hex'
    );
    select transaction.id into trade_transaction_id_value
    from private.accounting_transactions as transaction
    where transaction.account_id = fill_context.account_id
      and transaction.source_type = 'fill'
      and transaction.source_id = accounting_source_id
      and transaction.environment = fill_context.environment;
    if trade_transaction_id_value is null then
      raise exception 'cash_settlement_legacy_fill_journal_missing:%',
        fill_context.fill_id using errcode = '23514';
    end if;

    obligation_type_value := case
      when fill_context.side = 'buy' then 'cash_payable'
      else 'cash_receivable'
    end;
    amount_value := case
      when fill_context.side = 'buy' then
        fill_context.quantity * fill_context.price_krw
          + fill_context.commission_krw + fill_context.tax_krw
      else
        fill_context.quantity * fill_context.price_krw
          - fill_context.commission_krw - fill_context.tax_krw
    end;
    if amount_value <= 0 then
      raise exception 'cash_settlement_legacy_amount_invalid:%',
        fill_context.fill_id using errcode = '23514';
    end if;
    select
      count(*),
      coalesce(sum(posting.amount_krw) filter (where posting.side = 'debit'), 0),
      coalesce(sum(posting.amount_krw) filter (where posting.side = 'credit'), 0)
    into cash_posting_count, cash_debit, cash_credit
    from private.accounting_postings as posting
    join private.ledger_accounts as ledger
      on ledger.id = posting.ledger_account_id
    where posting.journal_entry_id = trade_transaction_id_value
      and ledger.ledger_code = 'CASH';
    if cash_posting_count <> 1
       or (
         fill_context.side = 'buy'
         and (cash_debit <> 0 or cash_credit <> amount_value)
       )
       or (
         fill_context.side = 'sell'
         and (cash_debit <> amount_value or cash_credit <> 0)
       ) then
      raise exception 'cash_settlement_legacy_cash_vector_invalid:%',
        fill_context.fill_id using errcode = '23514';
    end if;

    reclassification_source_id := pg_catalog.encode(
      public.digest(
        pg_catalog.convert_to(
          'cash-settlement-reclassification-v1|' || fill_context.fill_id::text,
          'UTF8'
        ),
        'sha256'
      ),
      'hex'
    );
    insert into private.accounting_transactions (
      account_id, environment, source_type, source_id, correlation_id,
      occurred_at, posted_at, release_sha
    ) values (
      fill_context.account_id, fill_context.environment,
      'settlement_reclassification', reclassification_source_id,
      fill_context.correlation_id, fill_context.filled_at, cutover_time,
      fill_context.release_sha
    ) returning id into reclassification_transaction_id_value;
    if obligation_type_value = 'cash_payable' then
      insert into private.accounting_postings (
        journal_entry_id, ledger_account_id, side, amount_krw
      )
      select reclassification_transaction_id_value, ledger.id, vector.side,
        amount_value
      from (
        values
          ('CASH', 'debit'),
          ('CASH_SETTLEMENT_PAYABLE', 'credit')
      ) as vector(ledger_code, side)
      join private.ledger_accounts as ledger
        on ledger.account_id = fill_context.account_id
       and ledger.ledger_code = vector.ledger_code;
    else
      insert into private.accounting_postings (
        journal_entry_id, ledger_account_id, side, amount_krw
      )
      select reclassification_transaction_id_value, ledger.id, vector.side,
        amount_value
      from (
        values
          ('CASH_SETTLEMENT_RECEIVABLE', 'debit'),
          ('CASH', 'credit')
      ) as vector(ledger_code, side)
      join private.ledger_accounts as ledger
        on ledger.account_id = fill_context.account_id
       and ledger.ledger_code = vector.ledger_code;
    end if;

    obligation_id_value := md5(
      'cash-settlement-obligation-v1:' || fill_context.fill_id::text
    )::uuid;
    obligation_hash := pg_catalog.encode(
      public.digest(
        pg_catalog.convert_to(
          concat_ws(
            '|', 'cash-settlement-obligation-v1', fill_context.fill_id::text,
            trade_transaction_id_value::text,
            reclassification_transaction_id_value::text,
            fill_context.intent_id::text, fill_context.account_id,
            fill_context.environment, obligation_type_value,
            amount_value::text,
            private.utc_iso8601(fill_context.filled_at),
            fill_context.settlement_date::text, fill_context.release_sha
          ),
          'UTF8'
        ),
        'sha256'
      ),
      'hex'
    );
    insert into private.cash_settlement_obligations (
      id, fill_id, trade_accounting_transaction_id,
      settlement_reclassification_transaction_id, intent_id, account_id,
      environment, obligation_type, amount_krw, trade_at, settlement_date,
      obligation_sha256, source_release_sha, created_at
    ) values (
      obligation_id_value, fill_context.fill_id, trade_transaction_id_value,
      reclassification_transaction_id_value, fill_context.intent_id,
      fill_context.account_id, fill_context.environment,
      obligation_type_value, amount_value, fill_context.filled_at,
      fill_context.settlement_date, obligation_hash,
      fill_context.release_sha, cutover_time
    );

    if fill_context.settlement_date <= cutover_date then
      settlement_source_id := pg_catalog.encode(
        public.digest(
          pg_catalog.convert_to(
            'cash-settlement-v1|' || obligation_id_value::text,
            'UTF8'
          ),
          'sha256'
        ),
        'hex'
      );
      insert into private.accounting_transactions (
        account_id, environment, source_type, source_id, correlation_id,
        occurred_at, posted_at, release_sha
      ) values (
        fill_context.account_id, fill_context.environment,
        'cash_settlement', settlement_source_id,
        fill_context.correlation_id,
        fill_context.settlement_date::timestamp at time zone 'Asia/Seoul',
        cutover_time, fill_context.release_sha
      ) returning id into settlement_transaction_id_value;
      if obligation_type_value = 'cash_payable' then
        insert into private.accounting_postings (
          journal_entry_id, ledger_account_id, side, amount_krw
        )
        select settlement_transaction_id_value, ledger.id, vector.side,
          amount_value
        from (
          values
            ('CASH_SETTLEMENT_PAYABLE', 'debit'),
            ('CASH', 'credit')
        ) as vector(ledger_code, side)
        join private.ledger_accounts as ledger
          on ledger.account_id = fill_context.account_id
         and ledger.ledger_code = vector.ledger_code;
      else
        insert into private.accounting_postings (
          journal_entry_id, ledger_account_id, side, amount_krw
        )
        select settlement_transaction_id_value, ledger.id, vector.side,
          amount_value
        from (
          values
            ('CASH', 'debit'),
            ('CASH_SETTLEMENT_RECEIVABLE', 'credit')
        ) as vector(ledger_code, side)
        join private.ledger_accounts as ledger
          on ledger.account_id = fill_context.account_id
         and ledger.ledger_code = vector.ledger_code;
      end if;
      insert into private.cash_settlement_state (
        obligation_id, state, revision, available_at, attempt_count,
        settlement_transaction_id, updated_at
      ) values (
        obligation_id_value, 'settled', 0,
        fill_context.settlement_date::timestamp at time zone 'Asia/Seoul',
        0, settlement_transaction_id_value, cutover_time
      );
    else
      settlement_transaction_id_value := null;
      if obligation_type_value = 'cash_payable' then
        update private.cash_balance_projection as balance
        set settled_cash_krw = balance.settled_cash_krw + amount_value,
            pending_debit_cash_krw = balance.pending_debit_cash_krw
              + amount_value,
            last_journal_entry_id = reclassification_transaction_id_value,
            projection_version = balance.projection_version + 1,
            projected_at = cutover_time
        where balance.account_id = fill_context.account_id;
      else
        update private.cash_balance_projection as balance
        set settled_cash_krw = balance.settled_cash_krw - amount_value,
            pending_credit_cash_krw = balance.pending_credit_cash_krw
              + amount_value,
            last_journal_entry_id = reclassification_transaction_id_value,
            projection_version = balance.projection_version + 1,
            projected_at = cutover_time
        where balance.account_id = fill_context.account_id
          and balance.settled_cash_krw >= amount_value
          and balance.settled_cash_krw - amount_value
            >= balance.reserved_cash_krw + balance.pending_debit_cash_krw;
      end if;
      if not found then
        raise exception 'cash_settlement_legacy_proceeds_not_separable:%',
          fill_context.fill_id using errcode = '23514';
      end if;
      insert into private.cash_settlement_state (
        obligation_id, state, revision, available_at, attempt_count,
        updated_at
      ) values (
        obligation_id_value, 'pending', 0,
        fill_context.settlement_date::timestamp at time zone 'Asia/Seoul',
        0, cutover_time
      );
    end if;

    insert into private.cash_settlement_events (
      obligation_id, event_sequence, event_type, actor_type,
      settlement_transaction_id, result_summary, event_sha256,
      occurred_at, created_at
    ) values (
      obligation_id_value, 1, 'recognized', 'system', null,
      jsonb_build_object(
        'schema_version', 1,
        'fill_id', fill_context.fill_id,
        'trade_accounting_transaction_id', trade_transaction_id_value,
        'settlement_reclassification_transaction_id',
          reclassification_transaction_id_value,
        'obligation_type', obligation_type_value,
        'amount_krw', amount_value,
        'settlement_date', fill_context.settlement_date,
        'cutover', true
      ),
      pg_catalog.encode(
        public.digest(
          pg_catalog.convert_to(
            obligation_id_value::text || '|1|recognized|' || obligation_hash,
            'UTF8'
          ),
          'sha256'
        ),
        'hex'
      ),
      cutover_time, cutover_time
    );
    if settlement_transaction_id_value is not null then
      insert into private.cash_settlement_events (
        obligation_id, event_sequence, event_type, actor_type,
        settlement_transaction_id, result_summary, event_sha256,
        occurred_at, created_at
      ) values (
        obligation_id_value, 2, 'settled', 'system',
        settlement_transaction_id_value,
        jsonb_build_object(
          'schema_version', 1,
          'obligation_id', obligation_id_value,
          'settlement_transaction_id', settlement_transaction_id_value,
          'settled_revision', 0,
          'obligation_type', obligation_type_value,
          'amount_krw', amount_value,
          'settlement_date', fill_context.settlement_date,
          'settled_at', cutover_time,
          'cutover', true,
          'replayed', false
        ),
        pg_catalog.encode(
          public.digest(
            pg_catalog.convert_to(
              obligation_id_value::text || '|2|settled|cutover|'
                || settlement_transaction_id_value::text,
              'UTF8'
            ),
            'sha256'
          ),
          'hex'
        ),
        cutover_time, cutover_time
      );
    end if;
  end loop;

  for fill_context in
    select account.account_id, account.environment
    from private.trading_accounts as account
    order by account.account_id
  loop
    select coalesce(max(snapshot.sequence), 0) + 1
    into snapshot_sequence
    from private.account_snapshots as snapshot
    where snapshot.account_id = fill_context.account_id
      and snapshot.environment = fill_context.environment;
    insert into private.account_snapshots (
      account_id, environment, sequence, cash_krw, reserved_cash_krw,
      pending_debit_cash_krw, pending_credit_cash_krw,
      positions_sha256, source_type, source_id, observed_at
    )
    select
      fill_context.account_id,
      fill_context.environment,
      snapshot_sequence,
      balance.settled_cash_krw,
      balance.reserved_cash_krw,
      balance.pending_debit_cash_krw,
      balance.pending_credit_cash_krw,
      pg_catalog.encode(
        public.digest(
          pg_catalog.convert_to(
            coalesce(string_agg(
              position.symbol || ':' || position.quantity::text,
              '|' order by position.symbol
            ), ''),
            'UTF8'
          ),
          'sha256'
        ),
        'hex'
      ),
      'ledger_projection', cutover_id_value, cutover_time
    from private.cash_balance_projection as balance
    left join private.position_projection as position
      on position.account_id = balance.account_id
    where balance.account_id = fill_context.account_id
    group by
      balance.settled_cash_krw, balance.reserved_cash_krw,
      balance.pending_debit_cash_krw, balance.pending_credit_cash_krw;
  end loop;

  select count(*) into fill_count_value from private.fills;
  select count(*) into pending_count_value
  from private.cash_settlement_state
  where state <> 'settled';
  select pg_catalog.encode(
    public.digest(
      pg_catalog.convert_to(
        coalesce(string_agg(
          obligation.fill_id::text || ':' || obligation.obligation_sha256,
          '|' order by obligation.fill_id
        ), ''),
        'UTF8'
      ),
      'sha256'
    ),
    'hex'
  ) into fill_digest
  from private.cash_settlement_obligations as obligation;
  insert into private.cash_settlement_cutovers (
    id, cutover_at, cutover_business_date, legacy_fill_count,
    legacy_pending_count, legacy_fill_sha256, created_at
  ) values (
    cutover_id_value, cutover_time, cutover_date, fill_count_value,
    pending_count_value, fill_digest, cutover_time
  );
end;
$$;

-- Qualification checkpoints defined by the preceding control-plane migration
-- are extended here so every settlement-state mutation invalidates stale
-- evidence even when no position changes.
create or replace function private.account_ledger_payload_v1(
  p_account_id text,
  p_environment text
)
returns jsonb
language sql
stable
security definer
set search_path = ''
as $$
  select jsonb_build_object(
    'schema_version', 1,
    'account_id', p_account_id,
    'environment', p_environment,
    'cash', coalesce((
      select jsonb_build_object(
        'settled_cash_krw',
          private.canonical_numeric_v1(cash.settled_cash_krw),
        'reserved_cash_krw',
          private.canonical_numeric_v1(cash.reserved_cash_krw),
        'pending_debit_cash_krw',
          private.canonical_numeric_v1(cash.pending_debit_cash_krw),
        'pending_credit_cash_krw',
          private.canonical_numeric_v1(cash.pending_credit_cash_krw),
        'available_cash_krw',
          private.canonical_numeric_v1(cash.available_cash_krw),
        'projected_settled_cash_krw',
          private.canonical_numeric_v1(cash.projected_settled_cash_krw),
        'projection_version', cash.projection_version,
        'last_journal_entry_id', cash.last_journal_entry_id
      )
      from private.cash_balance_projection as cash
      where cash.account_id = p_account_id
    ), 'null'::jsonb),
    'clearing_balances', jsonb_build_object(
      'cash_settlement_payable_krw', private.canonical_numeric_v1(coalesce((
        select sum(case
          when posting.side = ledger.normal_side then posting.amount_krw
          else -posting.amount_krw
        end)
        from private.ledger_accounts as ledger
        left join private.accounting_postings as posting
          on posting.ledger_account_id = ledger.id
        where ledger.account_id = p_account_id
          and ledger.ledger_code = 'CASH_SETTLEMENT_PAYABLE'
      ), 0)),
      'cash_settlement_receivable_krw', private.canonical_numeric_v1(coalesce((
        select sum(case
          when posting.side = ledger.normal_side then posting.amount_krw
          else -posting.amount_krw
        end)
        from private.ledger_accounts as ledger
        left join private.accounting_postings as posting
          on posting.ledger_account_id = ledger.id
        where ledger.account_id = p_account_id
          and ledger.ledger_code = 'CASH_SETTLEMENT_RECEIVABLE'
      ), 0))
    ),
    'cash_settlements', coalesce((
      select jsonb_agg(jsonb_build_object(
        'obligation_id', obligation.id,
        'fill_id', obligation.fill_id,
        'trade_accounting_transaction_id',
          obligation.trade_accounting_transaction_id,
        'settlement_reclassification_transaction_id',
          obligation.settlement_reclassification_transaction_id,
        'obligation_type', obligation.obligation_type,
        'amount_krw', obligation.amount_krw,
        'trade_at', private.utc_iso8601(obligation.trade_at),
        'settlement_date', obligation.settlement_date,
        'obligation_sha256', obligation.obligation_sha256,
        'state', state.state,
        'revision', state.revision,
        'available_at', private.utc_iso8601(state.available_at),
        'attempt_count', state.attempt_count,
        'lease_owner', state.lease_owner,
        'lease_token', state.lease_token,
        'lease_expires_at', case
          when state.lease_expires_at is null then null
          else private.utc_iso8601(state.lease_expires_at)
        end,
        'claim_release_sha', state.claim_release_sha,
        'claim_fencing_token', state.claim_fencing_token,
        'settlement_transaction_id', state.settlement_transaction_id,
        'last_error_code', state.last_error_code,
        'updated_at', private.utc_iso8601(state.updated_at),
        'event_chain_sha256', private.sha256_jsonb_v1(coalesce((
          select jsonb_agg(event.event_sha256 order by event.event_sequence)
          from private.cash_settlement_events as event
          where event.obligation_id = obligation.id
        ), '[]'::jsonb))
      ) order by obligation.settlement_date, obligation.trade_at, obligation.id)
      from private.cash_settlement_obligations as obligation
      join private.cash_settlement_state as state
        on state.obligation_id = obligation.id
      where obligation.account_id = p_account_id
        and obligation.environment = p_environment
    ), '[]'::jsonb),
    'positions', coalesce((
      select jsonb_agg(jsonb_build_object(
        'symbol', position.symbol,
        'quantity', position.quantity,
        'reserved_quantity', position.reserved_quantity,
        'pending_sell_quantity', position.pending_sell_quantity,
        'average_cost_krw',
          private.canonical_numeric_v1(position.average_cost_krw),
        'projection_version', position.projection_version
      ) order by position.symbol)
      from private.position_projection as position
      where position.account_id = p_account_id
    ), '[]'::jsonb),
    'journal', coalesce((
      select jsonb_agg(jsonb_build_object(
        'id', transaction.id,
        'source_type', transaction.source_type,
        'source_id', transaction.source_id,
        'correlation_id', transaction.correlation_id,
        'occurred_at', private.utc_iso8601(transaction.occurred_at),
        'posted_at', private.utc_iso8601(transaction.posted_at),
        'release_sha', transaction.release_sha,
        'postings', coalesce((
          select jsonb_agg(jsonb_build_object(
            'id', posting.id,
            'ledger_code', ledger.ledger_code,
            'side', posting.side,
            'amount_krw', private.canonical_numeric_v1(posting.amount_krw)
          ) order by ledger.ledger_code, posting.side, posting.id)
          from private.accounting_postings as posting
          join private.ledger_accounts as ledger
            on ledger.id = posting.ledger_account_id
          where posting.journal_entry_id = transaction.id
        ), '[]'::jsonb)
      ) order by transaction.occurred_at, transaction.id)
      from private.accounting_transactions as transaction
      where transaction.account_id = p_account_id
        and transaction.environment = p_environment
    ), '[]'::jsonb)
  );
$$;

create or replace function private.compute_ledger_checkpoint_sha256_v1(
  p_account_id text,
  p_environment text
)
returns text
language sql
stable
security definer
set search_path = ''
as $$
  select private.sha256_jsonb_v1(
    private.account_ledger_payload_v1(p_account_id, p_environment)
  );
$$;

create or replace function private.capture_qualification_snapshot_v1_impl(
  p_account_id text,
  p_environment text,
  p_release_sha text,
  p_observed_at timestamptz
)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  account_row private.trading_accounts%rowtype;
  cash_row private.cash_balance_projection%rowtype;
  snapshot_row private.account_snapshots%rowtype;
  next_sequence bigint;
  checkpoint_sha text;
  position_sha text;
begin
  perform private.require_service_role();
  if p_environment not in ('paper', 'contract_test')
     or p_release_sha !~ '^[0-9a-f]{40}([0-9a-f]{24})?$'
     or p_observed_at < clock_timestamp() - interval '5 minutes'
     or p_observed_at > clock_timestamp() + interval '30 seconds' then
    raise exception 'qualification_snapshot_values_invalid'
      using errcode = '22023';
  end if;
  perform pg_catalog.pg_advisory_xact_lock(
    pg_catalog.hashtextextended('qualification-snapshot:' || p_account_id, 0)
  );
  select * into account_row
  from private.trading_accounts
  where account_id = p_account_id
  for update;
  if not found or account_row.environment <> p_environment
     or account_row.state <> 'open' then
    raise exception 'qualification_snapshot_account_not_open'
      using errcode = '23514';
  end if;
  if exists (
    select 1 from private.execution_controls
    where account_id = p_account_id and execution_enabled is true
  ) then
    raise exception 'qualification_snapshot_requires_disabled_execution'
      using errcode = '23514';
  end if;
  if exists (
    select 1
    from private.execution_reconciliation_state as state
    join private.order_intents as intent on intent.id = state.intent_id
    where intent.account_id = p_account_id
      and intent.environment = p_environment
      and state.state <> 'complete'
  ) then
    raise exception 'qualification_snapshot_nonterminal_intent_present'
      using errcode = '23514';
  end if;
  if exists (
    select 1
    from private.reconciliation_breaks as break_row
    join private.reconciliation_runs as run_row on run_row.id = break_row.run_id
    where run_row.account_id = p_account_id
      and run_row.environment = p_environment
      and break_row.state <> 'resolved'
  ) then
    raise exception 'qualification_snapshot_unresolved_break_present'
      using errcode = '23514';
  end if;
  if exists (
    select 1
    from private.order_reservations as reservation
    join lateral (
      select event.remaining_cash_krw, event.remaining_quantity
      from private.reservation_events as event
      where event.reservation_id = reservation.id
      order by event.event_sequence desc
      limit 1
    ) as latest on true
    where reservation.account_id = p_account_id
      and reservation.environment = p_environment
      and (latest.remaining_cash_krw > 0 or latest.remaining_quantity > 0)
  ) then
    raise exception 'qualification_snapshot_active_reservation_present'
      using errcode = '23514';
  end if;
  lock table private.accounting_transactions in share mode;
  lock table private.accounting_postings in share mode;
  lock table private.cash_balance_projection in share mode;
  lock table private.position_projection in share mode;
  lock table private.cash_settlement_obligations in share mode;
  lock table private.cash_settlement_state in share mode;
  lock table private.cash_settlement_events in share mode;
  select * into cash_row
  from private.cash_balance_projection
  where account_id = p_account_id;
  if not found or cash_row.last_journal_entry_id is null then
    raise exception 'qualification_snapshot_opening_journal_required'
      using errcode = '23514';
  end if;
  checkpoint_sha := private.compute_ledger_checkpoint_sha256_v1(
    p_account_id, p_environment
  );
  position_sha := private.compute_position_projection_sha256_v1(p_account_id);
  select * into snapshot_row
  from private.account_snapshots
  where account_id = p_account_id
    and environment = p_environment
    and checkpoint_schema_version = 1
    and ledger_checkpoint_sha256 = checkpoint_sha
  order by sequence desc
  limit 1;
  if found then
    return jsonb_build_object(
      'schema_version', 1,
      'account_snapshot_id', snapshot_row.id,
      'account_snapshot_sequence', snapshot_row.sequence,
      'ledger_checkpoint_sha256', snapshot_row.ledger_checkpoint_sha256,
      'inserted', false
    );
  end if;
  select coalesce(max(sequence), 0) + 1 into next_sequence
  from private.account_snapshots
  where account_id = p_account_id and environment = p_environment;
  insert into private.account_snapshots (
    account_id, environment, sequence, cash_krw, reserved_cash_krw,
    pending_debit_cash_krw, pending_credit_cash_krw,
    positions_sha256, source_type, source_id, observed_at,
    checkpoint_schema_version, ledger_checkpoint_sha256
  ) values (
    p_account_id, p_environment, next_sequence, cash_row.settled_cash_krw,
    cash_row.reserved_cash_krw, cash_row.pending_debit_cash_krw,
    cash_row.pending_credit_cash_krw, position_sha, 'ledger_projection',
    cash_row.last_journal_entry_id, p_observed_at, 1, checkpoint_sha
  ) returning * into snapshot_row;
  return jsonb_build_object(
    'schema_version', 1,
    'account_snapshot_id', snapshot_row.id,
    'account_snapshot_sequence', snapshot_row.sequence,
    'ledger_checkpoint_sha256', snapshot_row.ledger_checkpoint_sha256,
    'inserted', true
  );
end;
$$;

create function private.get_accounting_summary_v2()
returns table (
  account_id text,
  environment text,
  state text,
  settled_cash_krw numeric,
  reserved_cash_krw numeric,
  pending_debit_cash_krw numeric,
  pending_credit_cash_krw numeric,
  available_cash_krw numeric,
  projected_settled_cash_krw numeric,
  projection_version bigint,
  pending_settlement_count bigint,
  due_settlement_count bigint,
  oldest_settlement_date date,
  position_count bigint,
  open_reconciliation_breaks bigint
)
language plpgsql
stable
security definer
set search_path = ''
as $$
begin
  perform private.require_human_roles(
    array[
      'platform_admin', 'operator', 'risk_approver', 'strategy_reviewer',
      'auditor', 'release_manager', 'viewer'
    ],
    true
  );
  return query
  select
    account.account_id,
    account.environment,
    account.state,
    balance.settled_cash_krw,
    balance.reserved_cash_krw,
    balance.pending_debit_cash_krw,
    balance.pending_credit_cash_krw,
    balance.available_cash_krw,
    balance.projected_settled_cash_krw,
    balance.projection_version,
    (
      select count(*)
      from private.cash_settlement_obligations as obligation
      join private.cash_settlement_state as settlement_state
        on settlement_state.obligation_id = obligation.id
      where obligation.account_id = account.account_id
        and settlement_state.state <> 'settled'
    ),
    (
      select count(*)
      from private.cash_settlement_obligations as obligation
      join private.cash_settlement_state as settlement_state
        on settlement_state.obligation_id = obligation.id
      where obligation.account_id = account.account_id
        and settlement_state.state in ('pending', 'leased')
        and obligation.settlement_date
          <= (clock_timestamp() at time zone 'Asia/Seoul')::date
    ),
    (
      select min(obligation.settlement_date)
      from private.cash_settlement_obligations as obligation
      join private.cash_settlement_state as settlement_state
        on settlement_state.obligation_id = obligation.id
      where obligation.account_id = account.account_id
        and settlement_state.state <> 'settled'
    ),
    (
      select count(*)
      from private.position_projection as position
      where position.account_id = account.account_id and position.quantity > 0
    ),
    (
      select count(*)
      from private.reconciliation_breaks as break_row
      where break_row.account_id = account.account_id
        and break_row.state <> 'resolved'
    )
  from private.trading_accounts as account
  join private.cash_balance_projection as balance using (account_id)
  order by account.account_id
  limit 20;
end;
$$;

create function worker_api.list_due_cash_settlement_accounts(
  p_now timestamptz,
  p_limit integer default 20
)
returns table (
  account_id text,
  environment text,
  due_count bigint,
  oldest_settlement_date date
)
language sql
volatile
security invoker
set search_path = ''
as $$
  select * from private.list_due_cash_settlement_accounts_impl(p_now, p_limit);
$$;

create function worker_api.claim_cash_settlement_batch(
  p_account_id text,
  p_holder_id text,
  p_release_sha text,
  p_fencing_token bigint,
  p_now timestamptz,
  p_limit integer default 20
)
returns table (
  obligation_id uuid,
  fill_id uuid,
  intent_id uuid,
  account_id text,
  environment text,
  obligation_type text,
  amount_krw bigint,
  settlement_date date,
  obligation_sha256 text,
  revision bigint,
  claim_token uuid,
  claim_expires_at timestamptz
)
language sql
volatile
security invoker
set search_path = ''
as $$
  select * from private.claim_cash_settlement_batch_impl(
    p_account_id, p_holder_id, p_release_sha, p_fencing_token, p_now, p_limit
  );
$$;

create function worker_api.complete_cash_settlement(
  p_obligation_id uuid,
  p_expected_revision bigint,
  p_claim_token uuid,
  p_holder_id text,
  p_release_sha text,
  p_fencing_token bigint,
  p_now timestamptz
)
returns jsonb
language sql
volatile
security invoker
set search_path = ''
as $$
  select private.complete_cash_settlement_impl(
    p_obligation_id, p_expected_revision, p_claim_token, p_holder_id,
    p_release_sha, p_fencing_token, p_now
  );
$$;

create function worker_api.fail_cash_settlement_attempt(
  p_obligation_id uuid,
  p_expected_revision bigint,
  p_claim_token uuid,
  p_holder_id text,
  p_release_sha text,
  p_fencing_token bigint,
  p_now timestamptz,
  p_error_code text
)
returns jsonb
language sql
volatile
security invoker
set search_path = ''
as $$
  select private.fail_cash_settlement_attempt_impl(
    p_obligation_id, p_expected_revision, p_claim_token, p_holder_id,
    p_release_sha, p_fencing_token, p_now, p_error_code
  );
$$;

create function api.get_accounting_summary_v2()
returns table (
  account_id text,
  environment text,
  state text,
  settled_cash_krw numeric,
  reserved_cash_krw numeric,
  pending_debit_cash_krw numeric,
  pending_credit_cash_krw numeric,
  available_cash_krw numeric,
  projected_settled_cash_krw numeric,
  projection_version bigint,
  pending_settlement_count bigint,
  due_settlement_count bigint,
  oldest_settlement_date date,
  position_count bigint,
  open_reconciliation_breaks bigint
)
language sql
stable
security invoker
set search_path = ''
as $$ select * from private.get_accounting_summary_v2(); $$;

-- The original observation RPC selected the first settlement session from the
-- PostgreSQL session date (`p_observed_at::date`).  The trading calendar and
-- the settlement obligation contract use the Korean trade date.  Recreate the
-- already-deployed function in this forward migration so fills observed from
-- 00:00 through 08:59 KST cannot be validated against one date and rejected by
-- the obligation trigger against another date.
do $$
declare
  observation_function regprocedure :=
    'private.record_execution_observation_impl(uuid,integer,text,text,text,text,timestamptz,bigint,bigint,bigint,bigint,bigint,bigint,date,jsonb,text,text,bigint)'::regprocedure;
  function_definition text;
  patched_definition text;
  legacy_fragment constant text :=
    'session_date >= p_observed_at::date';
  kst_fragment constant text :=
    'session_date >= (p_observed_at at time zone ''Asia/Seoul'')::date';
begin
  function_definition := pg_catalog.pg_get_functiondef(observation_function);
  if pg_catalog.strpos(function_definition, legacy_fragment) = 0
     or pg_catalog.strpos(
       pg_catalog.replace(function_definition, legacy_fragment, ''),
       legacy_fragment
     ) > 0 then
    raise exception 'record_execution_observation_settlement_date_patch_target_invalid'
      using errcode = '23514';
  end if;
  patched_definition := pg_catalog.replace(
    function_definition,
    legacy_fragment,
    kst_fragment
  );
  execute patched_definition;
  function_definition := pg_catalog.pg_get_functiondef(observation_function);
  if pg_catalog.strpos(function_definition, legacy_fragment) > 0
     or pg_catalog.strpos(function_definition, kst_fragment) = 0 then
    raise exception 'record_execution_observation_settlement_date_patch_failed'
      using errcode = '23514';
  end if;
end;
$$;

-- Migration-time assertions close the upgrade only if the append-only ledger,
-- operational state and projections agree exactly.
do $$
begin
  if exists (
    select 1
    from private.fills as fill
    left join private.cash_settlement_obligations as obligation
      on obligation.fill_id = fill.id
    group by fill.id
    having count(obligation.id) <> 1
  ) then
    raise exception 'cash_settlement_fill_coverage_invariant_failed'
      using errcode = '23514';
  end if;
  if exists (
    select 1
    from private.cash_settlement_obligations as obligation
    join private.fills as fill on fill.id = obligation.fill_id
    join private.execution_observations as observation
      on observation.id = fill.event_id
    join private.order_intents as intent on intent.id = obligation.intent_id
    join private.accounting_transactions as trade_transaction
      on trade_transaction.id = obligation.trade_accounting_transaction_id
    join private.accounting_transactions as reclassification_transaction
      on reclassification_transaction.id =
         obligation.settlement_reclassification_transaction_id
    where fill.intent_id is distinct from obligation.intent_id
       or fill.account_id is distinct from obligation.account_id
       or fill.filled_at is distinct from obligation.trade_at
       or fill.settlement_date is distinct from obligation.settlement_date
       or intent.account_id is distinct from obligation.account_id
       or intent.environment is distinct from obligation.environment
       or intent.release_sha is distinct from obligation.source_release_sha
       or obligation.obligation_type is distinct from case
         when intent.side = 'buy' then 'cash_payable'
         else 'cash_receivable'
       end
       or obligation.amount_krw is distinct from case
         when intent.side = 'buy' then
           fill.quantity * fill.price_krw
             + fill.commission_krw + fill.tax_krw
         else
           fill.quantity * fill.price_krw
             - fill.commission_krw - fill.tax_krw
       end
       or trade_transaction.account_id is distinct from obligation.account_id
       or trade_transaction.environment is distinct from obligation.environment
       or trade_transaction.source_type is distinct from 'fill'
       or trade_transaction.source_id is distinct from pg_catalog.encode(
         public.digest(
           pg_catalog.convert_to(
             intent.id::text || ':fill:' || observation.sequence::text,
             'UTF8'
           ),
           'sha256'
         ),
         'hex'
       )
       or trade_transaction.correlation_id
          is distinct from intent.correlation_id
       or trade_transaction.occurred_at is distinct from fill.filled_at
       or trade_transaction.release_sha is distinct from intent.release_sha
       or reclassification_transaction.account_id
          is distinct from obligation.account_id
       or reclassification_transaction.environment
          is distinct from obligation.environment
       or reclassification_transaction.source_type
          is distinct from 'settlement_reclassification'
       or reclassification_transaction.source_id is distinct from
          pg_catalog.encode(
            public.digest(
              pg_catalog.convert_to(
                'cash-settlement-reclassification-v1|' || fill.id::text,
                'UTF8'
              ),
              'sha256'
            ),
            'hex'
          )
       or reclassification_transaction.correlation_id
          is distinct from intent.correlation_id
       or reclassification_transaction.occurred_at
          is distinct from fill.filled_at
       or reclassification_transaction.release_sha
          is distinct from intent.release_sha
  ) then
    raise exception 'cash_settlement_obligation_scope_invariant_failed'
      using errcode = '23514';
  end if;
  if exists (
    select 1
    from private.cash_balance_projection as balance
    left join lateral (
      select
        coalesce(sum(obligation.amount_krw) filter (
          where obligation.obligation_type = 'cash_payable'
            and state.state <> 'settled'
        ), 0) as pending_debit,
        coalesce(sum(obligation.amount_krw) filter (
          where obligation.obligation_type = 'cash_receivable'
            and state.state <> 'settled'
        ), 0) as pending_credit
      from private.cash_settlement_obligations as obligation
      join private.cash_settlement_state as state
        on state.obligation_id = obligation.id
      where obligation.account_id = balance.account_id
    ) as expected on true
    where balance.pending_debit_cash_krw <> expected.pending_debit
       or balance.pending_credit_cash_krw <> expected.pending_credit
  ) then
    raise exception 'cash_settlement_projection_obligation_invariant_failed'
      using errcode = '23514';
  end if;
  if exists (
    with clearing_balances as (
      select
        ledger.account_id,
        ledger.ledger_code,
        coalesce(sum(case
          when posting.side = ledger.normal_side then posting.amount_krw
          else -posting.amount_krw
        end), 0) as balance_krw
      from private.ledger_accounts as ledger
      left join private.accounting_postings as posting
        on posting.ledger_account_id = ledger.id
      where ledger.ledger_code in (
        'CASH_SETTLEMENT_PAYABLE', 'CASH_SETTLEMENT_RECEIVABLE'
      )
      group by ledger.account_id, ledger.ledger_code
    ), expected as (
      select
        account.account_id,
        coalesce(sum(obligation.amount_krw) filter (
          where obligation.obligation_type = 'cash_payable'
            and state.state <> 'settled'
        ), 0) as payable_krw,
        coalesce(sum(obligation.amount_krw) filter (
          where obligation.obligation_type = 'cash_receivable'
            and state.state <> 'settled'
        ), 0) as receivable_krw
      from private.trading_accounts as account
      left join private.cash_settlement_obligations as obligation
        on obligation.account_id = account.account_id
      left join private.cash_settlement_state as state
        on state.obligation_id = obligation.id
      group by account.account_id
    )
    select 1
    from expected
    join clearing_balances as payable
      on payable.account_id = expected.account_id
     and payable.ledger_code = 'CASH_SETTLEMENT_PAYABLE'
    join clearing_balances as receivable
      on receivable.account_id = expected.account_id
     and receivable.ledger_code = 'CASH_SETTLEMENT_RECEIVABLE'
    where payable.balance_krw <> expected.payable_krw
       or receivable.balance_krw <> expected.receivable_krw
  ) then
    raise exception 'cash_settlement_clearing_ledger_invariant_failed'
      using errcode = '23514';
  end if;
  if exists (
    select 1
    from private.accounting_transactions as transaction
    left join private.accounting_postings as posting
      on posting.journal_entry_id = transaction.id
    group by transaction.id
    having count(posting.id) < 2
      or coalesce(sum(posting.amount_krw) filter (
        where posting.side = 'debit'
      ), 0) <> coalesce(sum(posting.amount_krw) filter (
        where posting.side = 'credit'
      ), 0)
  ) then
    raise exception 'cash_settlement_journal_balance_invariant_failed'
      using errcode = '23514';
  end if;
end;
$$;

revoke all on table
  private.cash_settlement_cutovers,
  private.cash_settlement_obligations,
  private.cash_settlement_state,
  private.cash_settlement_events
from public, anon, authenticated, service_role;

revoke execute on function private.guard_cash_settlement_state_transition_v1()
  from public, anon, authenticated, service_role;
revoke execute on function private.guard_cash_settlement_obligation_scope_v1()
  from public, anon, authenticated, service_role;
revoke execute on function private.populate_account_snapshot_pending_cash_v1()
  from public, anon, authenticated, service_role;
revoke execute on function private.post_accounting_transaction_trade_date_legacy_impl(
  text, uuid, integer, timestamptz, jsonb, text, bigint
) from public, anon, authenticated, service_role;
revoke execute on function private.post_accounting_transaction_impl(
  text, uuid, integer, timestamptz, jsonb, text, bigint
) from public, anon, authenticated, service_role;

revoke execute on function private.list_due_cash_settlement_accounts_impl(
  timestamptz, integer
) from public, anon, authenticated;
revoke execute on function private.claim_cash_settlement_batch_impl(
  text, text, text, bigint, timestamptz, integer
) from public, anon, authenticated;
revoke execute on function private.complete_cash_settlement_impl(
  uuid, bigint, uuid, text, text, bigint, timestamptz
) from public, anon, authenticated;
revoke execute on function private.fail_cash_settlement_attempt_impl(
  uuid, bigint, uuid, text, text, bigint, timestamptz, text
) from public, anon, authenticated;
grant execute on function private.list_due_cash_settlement_accounts_impl(
  timestamptz, integer
) to service_role;
grant execute on function private.claim_cash_settlement_batch_impl(
  text, text, text, bigint, timestamptz, integer
) to service_role;
grant execute on function private.complete_cash_settlement_impl(
  uuid, bigint, uuid, text, text, bigint, timestamptz
) to service_role;
grant execute on function private.fail_cash_settlement_attempt_impl(
  uuid, bigint, uuid, text, text, bigint, timestamptz, text
) to service_role;

revoke execute on function worker_api.list_due_cash_settlement_accounts(
  timestamptz, integer
) from public, anon, authenticated;
revoke execute on function worker_api.claim_cash_settlement_batch(
  text, text, text, bigint, timestamptz, integer
) from public, anon, authenticated;
revoke execute on function worker_api.complete_cash_settlement(
  uuid, bigint, uuid, text, text, bigint, timestamptz
) from public, anon, authenticated;
revoke execute on function worker_api.fail_cash_settlement_attempt(
  uuid, bigint, uuid, text, text, bigint, timestamptz, text
) from public, anon, authenticated;
grant execute on function worker_api.list_due_cash_settlement_accounts(
  timestamptz, integer
) to service_role;
grant execute on function worker_api.claim_cash_settlement_batch(
  text, text, text, bigint, timestamptz, integer
) to service_role;
grant execute on function worker_api.complete_cash_settlement(
  uuid, bigint, uuid, text, text, bigint, timestamptz
) to service_role;
grant execute on function worker_api.fail_cash_settlement_attempt(
  uuid, bigint, uuid, text, text, bigint, timestamptz, text
) to service_role;

revoke execute on function private.get_accounting_summary_v2()
  from public, anon, service_role;
revoke execute on function api.get_accounting_summary_v2()
  from public, anon, service_role;
grant execute on function private.get_accounting_summary_v2()
  to authenticated;
grant execute on function api.get_accounting_summary_v2()
  to authenticated;

revoke execute on function private.account_ledger_payload_v1(text, text)
  from public, anon, authenticated, service_role;
revoke execute on function private.compute_ledger_checkpoint_sha256_v1(text, text)
  from public, anon, authenticated, service_role;
revoke execute on function private.capture_qualification_snapshot_v1_impl(
  text, text, text, timestamptz
) from public, anon, authenticated;
grant execute on function private.capture_qualification_snapshot_v1_impl(
  text, text, text, timestamptz
) to service_role;

commit;
