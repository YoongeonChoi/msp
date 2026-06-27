create extension if not exists pgcrypto;

create table public.user_roles (
  user_id uuid primary key references auth.users(id) on delete cascade,
  role text not null check (role in ('admin', 'viewer')),
  created_at timestamptz not null default now()
);

create table public.bot_settings (
  id text primary key default 'singleton' check (id = 'singleton'),
  enabled boolean not null default false,
  mode text not null default 'paper' check (mode in ('paper', 'live')),
  live_order_allowed boolean not null default false,
  max_order_amount_krw integer not null default 100000 check (max_order_amount_krw > 0),
  max_daily_loss_pct numeric(8,6) not null default 0.02 check (max_daily_loss_pct > 0 and max_daily_loss_pct <= 0.20),
  max_daily_order_count integer not null default 10 check (max_daily_order_count > 0),
  max_position_pct numeric(8,6) not null default 0.10 check (max_position_pct > 0 and max_position_pct <= 1),
  max_sector_pct numeric(8,6) not null default 0.30 check (max_sector_pct > 0 and max_sector_pct <= 1),
  loop_interval_sec integer not null default 30 check (loop_interval_sec between 5 and 3600),
  updated_by uuid references auth.users(id),
  updated_at timestamptz not null default now(),
  created_at timestamptz not null default now()
);

create table public.sector_rules (
  id uuid primary key default gen_random_uuid(),
  sector text not null,
  max_sector_pct numeric(8,6) not null default 0.30,
  enabled boolean not null default true,
  notes text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (sector)
);

create table public.watchlist (
  id uuid primary key default gen_random_uuid(),
  symbol text not null check (symbol ~ '^[0-9]{6}$'),
  name text,
  market text not null default 'KR' check (market in ('KR')),
  sector text not null default 'unknown',
  enabled boolean not null default true,
  target_buy_krw integer,
  target_sell_krw integer,
  stop_loss_pct numeric(8,6),
  max_position_pct numeric(8,6),
  notes text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (symbol)
);

create table public.positions (
  id uuid primary key default gen_random_uuid(),
  symbol text not null check (symbol ~ '^[0-9]{6}$'),
  quantity integer not null default 0,
  avg_price_krw integer not null default 0,
  current_price_krw integer not null default 0,
  market_value_krw integer not null default 0,
  unrealized_pnl_krw integer not null default 0,
  unrealized_pnl_pct numeric(12,6) not null default 0,
  sector text not null default 'unknown',
  synced_at timestamptz not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (symbol)
);

create table public.strategy_versions (
  id uuid primary key default gen_random_uuid(),
  version_name text not null,
  status text not null check (status in ('draft', 'paper', 'active', 'retired')),
  strategy_type text not null,
  weights jsonb not null default '{}'::jsonb,
  params jsonb not null default '{}'::jsonb,
  created_by uuid references auth.users(id),
  approved_by uuid references auth.users(id),
  approved_at timestamptz,
  created_at timestamptz not null default now(),
  unique (version_name)
);

create table public.decision_snapshots (
  id uuid primary key default gen_random_uuid(),
  cycle_id uuid not null,
  symbol text not null check (symbol ~ '^[0-9]{6}$'),
  action text not null check (action in ('hold', 'buy', 'sell')),
  final_score numeric(10,6) not null,
  confidence numeric(10,6) not null,
  strategy_version_id uuid not null references public.strategy_versions(id),
  feature_snapshot jsonb not null default '{}'::jsonb,
  risk_snapshot jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create table public.orders (
  id uuid primary key default gen_random_uuid(),
  decision_id uuid not null references public.decision_snapshots(id),
  symbol text not null check (symbol ~ '^[0-9]{6}$'),
  side text not null check (side in ('buy', 'sell')),
  mode text not null check (mode in ('paper', 'live')),
  status text not null check (status in ('proposed', 'paper', 'blocked', 'sent', 'filled', 'rejected', 'failed', 'unknown_requires_manual_check')),
  amount_krw integer not null check (amount_krw > 0),
  idempotency_key text not null unique,
  provider_order_id text,
  reason text,
  risk_result jsonb,
  provider_payload_summary jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table public.outcomes (
  id uuid primary key default gen_random_uuid(),
  decision_id uuid not null references public.decision_snapshots(id),
  order_id uuid references public.orders(id),
  symbol text not null check (symbol ~ '^[0-9]{6}$'),
  horizon_days integer not null,
  return_pct numeric(12,6),
  pnl_krw integer,
  calculated_at timestamptz not null default now(),
  created_at timestamptz not null default now()
);

create table public.features_daily (
  id uuid primary key default gen_random_uuid(),
  symbol text not null check (symbol ~ '^[0-9]{6}$'),
  trade_date date not null,
  r_1d numeric(12,6),
  r_5d numeric(12,6),
  r_20d numeric(12,6),
  ma_gap_20 numeric(12,6),
  ma_gap_60 numeric(12,6),
  volatility_20 numeric(12,6),
  rsi_14 numeric(12,6),
  atr_14 numeric(12,6),
  turnover_krw bigint,
  liquidity_filter boolean,
  recent_high_distance numeric(12,6),
  recent_low_distance numeric(12,6),
  raw_snapshot jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  unique (symbol, trade_date)
);

create table public.fundamentals_quarterly (
  id uuid primary key default gen_random_uuid(),
  symbol text not null check (symbol ~ '^[0-9]{6}$'),
  fiscal_year integer not null,
  fiscal_quarter integer not null check (fiscal_quarter between 1 and 4),
  per numeric(12,6),
  pbr numeric(12,6),
  roe numeric(12,6),
  operating_margin numeric(12,6),
  debt_ratio numeric(12,6),
  revenue_growth_yoy numeric(12,6),
  operating_income_growth_yoy numeric(12,6),
  net_income_growth_yoy numeric(12,6),
  source text not null default 'opendart',
  raw_snapshot jsonb not null default '{}'::jsonb,
  updated_at timestamptz not null default now(),
  created_at timestamptz not null default now(),
  unique (symbol, fiscal_year, fiscal_quarter)
);

create table public.account_mapping (
  id uuid primary key default gen_random_uuid(),
  provider text not null default 'opendart',
  provider_account_name text not null,
  canonical_field text not null,
  confidence numeric(8,6) not null default 0,
  verified boolean not null default false,
  created_at timestamptz not null default now(),
  unique (provider, provider_account_name, canonical_field)
);

create table public.news_events (
  id uuid primary key default gen_random_uuid(),
  symbol text not null check (symbol ~ '^[0-9]{6}$'),
  title text not null,
  source text not null,
  published_at timestamptz,
  title_hash text not null,
  url_hash text,
  raw_url text,
  relevance_score numeric(8,6),
  sentiment text check (sentiment in ('positive', 'neutral', 'negative', 'unknown')),
  event_type text,
  risk_level text check (risk_level in ('low', 'medium', 'high', 'critical', 'unknown')),
  summary_short text,
  trading_relevance numeric(8,6),
  confidence numeric(8,6),
  linked_decision_id uuid references public.decision_snapshots(id),
  created_at timestamptz not null default now(),
  unique (symbol, title_hash)
);

create table public.ai_upgrade_candidates (
  id uuid primary key default gen_random_uuid(),
  base_strategy_version_id uuid references public.strategy_versions(id),
  candidate_name text not null,
  candidate_weights jsonb not null,
  candidate_params jsonb not null default '{}'::jsonb,
  rationale text not null,
  expected_improvement text not null,
  risk_notes text not null,
  required_backtests jsonb not null default '[]'::jsonb,
  status text not null default 'proposed' check (status in ('proposed', 'backtesting', 'approved_for_paper', 'rejected', 'retired')),
  approval_required boolean not null default true,
  created_at timestamptz not null default now(),
  reviewed_by uuid references auth.users(id),
  reviewed_at timestamptz
);

create table public.api_health (
  id uuid primary key default gen_random_uuid(),
  provider text not null,
  healthy boolean not null,
  status text not null default 'unknown',
  latency_ms integer,
  details jsonb not null default '{}'::jsonb,
  checked_at timestamptz not null default now()
);

create table public.worker_heartbeats (
  id uuid primary key default gen_random_uuid(),
  worker_name text not null default 'kr-trading-worker',
  status text not null check (status in ('ok', 'warning', 'error', 'shutting_down')),
  details jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create table public.engine_events (
  id uuid primary key default gen_random_uuid(),
  level text not null check (level in ('debug', 'info', 'warning', 'error', 'critical')),
  component text not null,
  message text not null,
  details jsonb not null default '{}'::jsonb,
  correlation_id uuid,
  created_at timestamptz not null default now()
);

create table public.audit_logs (
  id uuid primary key default gen_random_uuid(),
  actor_user_id uuid references auth.users(id),
  action text not null,
  target_table text,
  target_id text,
  before_snapshot jsonb,
  after_snapshot jsonb,
  created_at timestamptz not null default now()
);

create table public.outbox_events (
  id uuid primary key default gen_random_uuid(),
  event_type text not null,
  payload jsonb not null default '{}'::jsonb,
  status text not null default 'pending' check (status in ('pending', 'processing', 'sent', 'failed')),
  created_at timestamptz not null default now(),
  processed_at timestamptz
);

create table public.manual_commands (
  id uuid primary key default gen_random_uuid(),
  command_type text not null check (command_type in ('pause_bot', 'resume_paper', 'emergency_stop', 'approve_candidate', 'reject_candidate')),
  payload jsonb not null default '{}'::jsonb,
  status text not null default 'pending' check (status in ('pending', 'accepted', 'rejected', 'applied')),
  requested_by uuid references auth.users(id),
  created_at timestamptz not null default now(),
  applied_at timestamptz
);

create table public.provider_rate_limits (
  id uuid primary key default gen_random_uuid(),
  provider text not null,
  window_started_at timestamptz not null,
  request_count integer not null default 0,
  limit_count integer,
  reset_at timestamptz,
  created_at timestamptz not null default now(),
  unique (provider, window_started_at)
);

create table public.retention_runs (
  id uuid primary key default gen_random_uuid(),
  dry_run boolean not null default true,
  deleted_counts jsonb not null default '{}'::jsonb,
  db_size_bytes bigint,
  started_at timestamptz not null default now(),
  finished_at timestamptz
);

create table public.release_versions (
  id uuid primary key default gen_random_uuid(),
  version text not null unique,
  git_sha text,
  render_deploy_id text,
  deployed_at timestamptz,
  notes text,
  created_at timestamptz not null default now()
);

create index idx_watchlist_enabled on public.watchlist (enabled, symbol);
create index idx_positions_symbol on public.positions (symbol);
create index idx_orders_symbol_created on public.orders (symbol, created_at desc);
create index idx_orders_status_created on public.orders (status, created_at desc);
create index idx_decisions_symbol_created on public.decision_snapshots (symbol, created_at desc);
create index idx_decisions_created on public.decision_snapshots (created_at desc);
create index idx_features_symbol_date on public.features_daily (symbol, trade_date desc);
create index idx_fundamentals_symbol_period on public.fundamentals_quarterly (symbol, fiscal_year desc, fiscal_quarter desc);
create index idx_news_symbol_created on public.news_events (symbol, created_at desc);
create index idx_ai_candidates_status_created on public.ai_upgrade_candidates (status, created_at desc);
create index idx_api_health_provider_checked on public.api_health (provider, checked_at desc);
create index idx_heartbeats_created on public.worker_heartbeats (created_at desc);
create index idx_engine_events_level_created on public.engine_events (level, created_at desc);
create index idx_audit_logs_created on public.audit_logs (created_at desc);
create index idx_outbox_status_created on public.outbox_events (status, created_at);

