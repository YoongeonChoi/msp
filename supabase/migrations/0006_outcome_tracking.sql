alter table public.outcomes
  add column if not exists return_1d numeric(12,6),
  add column if not exists return_5d numeric(12,6),
  add column if not exists return_20d numeric(12,6),
  add column if not exists max_drawdown_20d numeric(12,6),
  add column if not exists hit_target boolean,
  add column if not exists hit_stop boolean,
  add column if not exists realized_pnl_krw integer,
  add column if not exists price_at_decision numeric(20,6),
  add column if not exists outcome_status text not null default 'pending',
  add column if not exists reason text,
  add column if not exists updated_at timestamptz not null default now();

alter table public.outcomes
  alter column horizon_days set default 20,
  alter column updated_at set default now(),
  alter column outcome_status set default 'pending';

do $$
begin
  if not exists (
    select 1
    from pg_constraint
    where conrelid = 'public.outcomes'::regclass
      and conname = 'outcomes_outcome_status_check'
  ) then
    alter table public.outcomes
      add constraint outcomes_outcome_status_check
      check (outcome_status in ('pending', 'partial', 'complete', 'skipped'));
  end if;
end $$;

with outcome_rollup as (
  select
    decision_id,
    max(return_pct) filter (where horizon_days = 1) as legacy_return_1d,
    max(return_pct) filter (where horizon_days = 5) as legacy_return_5d,
    max(return_pct) filter (where horizon_days = 20) as legacy_return_20d,
    max(pnl_krw) filter (where horizon_days = 20) as legacy_pnl_krw
  from public.outcomes
  group by decision_id
),
ranked_outcomes as (
  select
    id,
    decision_id,
    row_number() over (
      partition by decision_id
      order by updated_at desc nulls last, calculated_at desc nulls last, created_at desc nulls last, id desc
    ) as duplicate_rank
  from public.outcomes
)
update public.outcomes o
set
  return_1d = coalesce(o.return_1d, r.legacy_return_1d),
  return_5d = coalesce(o.return_5d, r.legacy_return_5d),
  return_20d = coalesce(o.return_20d, r.legacy_return_20d),
  return_pct = coalesce(o.return_pct, r.legacy_return_20d),
  realized_pnl_krw = coalesce(o.realized_pnl_krw, r.legacy_pnl_krw),
  pnl_krw = coalesce(o.pnl_krw, r.legacy_pnl_krw),
  horizon_days = coalesce(o.horizon_days, 20),
  updated_at = now()
from ranked_outcomes k
join outcome_rollup r on r.decision_id = k.decision_id
where o.id = k.id
  and k.duplicate_rank = 1;

with ranked_outcomes as (
  select
    id,
    row_number() over (
      partition by decision_id
      order by updated_at desc nulls last, calculated_at desc nulls last, created_at desc nulls last, id desc
    ) as duplicate_rank
  from public.outcomes
)
delete from public.outcomes o
using ranked_outcomes r
where o.id = r.id
  and r.duplicate_rank > 1;

create unique index if not exists idx_outcomes_decision_id_unique
  on public.outcomes(decision_id);

create index if not exists idx_outcomes_updated_at
  on public.outcomes(updated_at desc);
