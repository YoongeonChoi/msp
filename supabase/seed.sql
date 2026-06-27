insert into public.bot_settings (
  id,
  enabled,
  mode,
  live_order_allowed,
  max_order_amount_krw,
  max_daily_loss_pct,
  max_daily_order_count,
  max_position_pct,
  max_sector_pct,
  loop_interval_sec
)
values (
  'singleton',
  false,
  'paper',
  false,
  100000,
  0.02,
  10,
  0.10,
  0.30,
  30
)
on conflict (id) do update set
  enabled = false,
  mode = 'paper',
  live_order_allowed = false;

insert into public.strategy_versions (
  version_name,
  status,
  strategy_type,
  weights,
  params
)
values (
  'weighted_factor_v1_seed',
  'active',
  'WeightedFactorStrategyV1',
  '{"technical":0.35,"fundamental":0.25,"market_sector":0.15,"news_event":0.15,"portfolio":0.10}'::jsonb,
  '{"buy_threshold":0.68,"sell_threshold":0.25}'::jsonb
)
on conflict (version_name) do nothing;

insert into public.watchlist (symbol, name, sector, enabled)
values ('005930', '삼성전자', '반도체', true)
on conflict (symbol) do nothing;

