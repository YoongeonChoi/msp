-- Bind every committed sell-fill POSITION_COST posting to the immutable cost
-- basis checkpoint captured when the intent reserved its shares.  The existing
-- accounting functions still preview the live projection; this trigger makes
-- any divergence from the reservation checkpoint fail atomically before a
-- posting or projection change can commit.

create or replace function private.guard_sell_fill_cost_basis_checkpoint_v1()
returns trigger
language plpgsql
security invoker
set search_path = ''
as $$
declare
  ledger_code_value text;
  intent_id_value uuid;
  checkpoint_method text;
  checkpoint_quantity bigint;
  checkpoint_average numeric(24,4);
  checkpoint_total_cost bigint;
  checkpoint_projection_version bigint;
  checkpoint_sha256 text;
  expected_checkpoint_sha256 text;
  cumulative_quantity_value bigint;
  fill_quantity_value bigint;
  previous_cumulative_quantity bigint;
  cumulative_cost_relief bigint;
  previous_cost_relief bigint;
  expected_cost_relief bigint;
begin
  select ledger.ledger_code
  into ledger_code_value
  from private.ledger_accounts as ledger
  where ledger.id = new.ledger_account_id;

  if ledger_code_value <> 'POSITION_COST' then
    return new;
  end if;

  select
    intent.id,
    intent.position_cost_basis_method,
    intent.position_quantity_snapshot,
    intent.position_average_cost_krw,
    intent.position_total_cost_krw,
    intent.position_projection_version,
    intent.position_cost_basis_sha256,
    observation.cumulative_quantity,
    fill.quantity
  into
    intent_id_value,
    checkpoint_method,
    checkpoint_quantity,
    checkpoint_average,
    checkpoint_total_cost,
    checkpoint_projection_version,
    checkpoint_sha256,
    cumulative_quantity_value,
    fill_quantity_value
  from private.accounting_transactions as transaction
  join private.execution_observations as observation
    on pg_catalog.encode(
      public.digest(
        pg_catalog.convert_to(
          observation.intent_id::text || ':fill:' || observation.sequence::text,
          'UTF8'
        ),
        'sha256'
      ),
      'hex'
    ) = transaction.source_id
  join private.order_intents as intent
    on intent.id = observation.intent_id
   and intent.correlation_id = transaction.correlation_id
   and intent.side = 'sell'
  join private.fills as fill
    on fill.event_id = observation.id
  where transaction.id = new.journal_entry_id
    and transaction.source_type = 'fill';

  if not found then
    return new;
  end if;

  expected_checkpoint_sha256 := pg_catalog.encode(
    public.digest(
      pg_catalog.convert_to(
        jsonb_build_object(
          'method', 'moving_weighted_average_v1',
          'account_id', (
            select intent.account_id
            from private.order_intents as intent
            where intent.id = intent_id_value
          ),
          'symbol', (
            select intent.symbol
            from private.order_intents as intent
            where intent.id = intent_id_value
          ),
          'quantity', checkpoint_quantity,
          'average_cost_krw_4dp', to_char(
            checkpoint_average,
            'FM99999999999999999999.0000'
          ),
          'total_cost_krw', checkpoint_total_cost,
          'projection_version', checkpoint_projection_version
        )::text,
        'UTF8'
      ),
      'sha256'
    ),
    'hex'
  );
  if checkpoint_method <> 'moving_weighted_average_v1'
     or checkpoint_quantity is null
     or checkpoint_quantity <= 0
     or checkpoint_total_cost is null
     or checkpoint_total_cost < 0
     or checkpoint_sha256 is null
     or checkpoint_sha256 <> expected_checkpoint_sha256 then
    raise exception 'sell_fill_cost_basis_checkpoint_invalid'
      using errcode = '23514';
  end if;

  previous_cumulative_quantity := cumulative_quantity_value - fill_quantity_value;
  if previous_cumulative_quantity < 0
     or cumulative_quantity_value > checkpoint_quantity then
    raise exception 'sell_fill_cost_basis_checkpoint_quantity_invalid'
      using errcode = '23514';
  end if;
  cumulative_cost_relief := case
    when cumulative_quantity_value = checkpoint_quantity
      then checkpoint_total_cost
    else floor(
      checkpoint_total_cost::numeric
        * cumulative_quantity_value
        / checkpoint_quantity
    )::bigint
  end;
  previous_cost_relief := case
    when previous_cumulative_quantity = checkpoint_quantity
      then checkpoint_total_cost
    else floor(
      checkpoint_total_cost::numeric
        * previous_cumulative_quantity
        / checkpoint_quantity
    )::bigint
  end;
  expected_cost_relief := cumulative_cost_relief - previous_cost_relief;

  if new.side <> 'credit' or new.amount_krw <> expected_cost_relief then
    raise exception 'sell_fill_position_cost_not_pinned'
      using errcode = '23514';
  end if;
  return new;
end;
$$;

revoke all on function private.guard_sell_fill_cost_basis_checkpoint_v1()
from public, anon, authenticated, service_role;

drop trigger if exists guard_sell_fill_cost_basis_checkpoint_v1
on private.accounting_postings;

create trigger guard_sell_fill_cost_basis_checkpoint_v1
before insert on private.accounting_postings
for each row execute function private.guard_sell_fill_cost_basis_checkpoint_v1();

do $$
begin
  if not exists (
    select 1
    from pg_catalog.pg_trigger as trigger
    join pg_catalog.pg_class as relation on relation.oid = trigger.tgrelid
    join pg_catalog.pg_namespace as namespace on namespace.oid = relation.relnamespace
    where namespace.nspname = 'private'
      and relation.relname = 'accounting_postings'
      and trigger.tgname = 'guard_sell_fill_cost_basis_checkpoint_v1'
      and trigger.tgenabled = 'O'
      and not trigger.tgisinternal
  ) then
    raise exception 'sell_fill_cost_basis_checkpoint_trigger_missing'
      using errcode = '23514';
  end if;
end;
$$;
