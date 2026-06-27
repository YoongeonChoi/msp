create table if not exists public.backtest_runs (
  id uuid primary key default gen_random_uuid(),
  strategy text not null,
  strategy_version text not null,
  period_start date not null,
  period_end date not null,
  total_return numeric(14,8) not null default 0,
  cagr numeric(14,8),
  max_drawdown numeric(14,8) not null default 0,
  sharpe_like numeric(14,8),
  win_rate numeric(14,8),
  average_win numeric(20,4) not null default 0,
  average_loss numeric(20,4) not null default 0,
  turnover numeric(14,8) not null default 0,
  number_of_trades integer not null default 0,
  transaction_cost_krw integer not null default 0,
  blocked_reason_counts jsonb not null default '{}'::jsonb,
  assumptions jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

alter table public.backtest_runs
  add column if not exists strategy text,
  add column if not exists strategy_version text,
  add column if not exists period_start date,
  add column if not exists period_end date,
  add column if not exists total_return numeric(14,8) not null default 0,
  add column if not exists cagr numeric(14,8),
  add column if not exists max_drawdown numeric(14,8) not null default 0,
  add column if not exists sharpe_like numeric(14,8),
  add column if not exists win_rate numeric(14,8),
  add column if not exists average_win numeric(20,4) not null default 0,
  add column if not exists average_loss numeric(20,4) not null default 0,
  add column if not exists turnover numeric(14,8) not null default 0,
  add column if not exists number_of_trades integer not null default 0,
  add column if not exists transaction_cost_krw integer not null default 0,
  add column if not exists blocked_reason_counts jsonb not null default '{}'::jsonb,
  add column if not exists assumptions jsonb not null default '{}'::jsonb,
  add column if not exists created_at timestamptz not null default now();

alter table public.backtest_runs enable row level security;

create index if not exists idx_backtest_runs_created_at
  on public.backtest_runs(created_at desc);

create index if not exists idx_backtest_runs_strategy_period
  on public.backtest_runs(strategy, period_start desc, period_end desc);
