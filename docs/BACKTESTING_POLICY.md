# Backtesting Policy

Required before promotion:

- out-of-sample validation
- walk-forward validation when enough data exists
- transaction costs
- slippage
- max drawdown
- turnover
- hit rate
- average win/loss
- daily loss limit simulation
- sector exposure
- liquidity filter
- baseline comparison

Do not promote based on one month alone. Track number of candidates tried. Add Deflated Sharpe Ratio or equivalent later.

## Paper Outcome Inputs

Run this before monthly review or backtest data export:

```bash
cd apps/worker
python -m app.tools.update_outcomes_once
```

The command calculates `return_1d`, `return_5d`, `return_20d`, `max_drawdown_20d`, target/stop hits, and paper `realized_pnl_krw` from `decision_snapshots`, `features_daily`, and linked paper orders. It does not call live broker APIs and does not create orders.

Backtests may use `outcome_status='complete'` rows for 20-trading-day metrics. `partial` rows may be used only for shorter horizons that are present. `pending` and `skipped` rows must be excluded from return averages and reviewed separately for data-quality issues.

## Lightweight Strategy V1 Backtest

Run small cached-data backtests locally or from a trusted worker shell:

```bash
cd apps/worker
python -m app.tools.run_backtest --strategy strategy_v1_weighted_factor --start YYYY-MM-DD --end YYYY-MM-DD
```

The runner reads cached `features_daily`, `fundamentals_quarterly`, `news_events`, `watchlist`, and `strategy_versions`. It uses `strategy_versions.weights` or `weights_json` plus `params` or `params_json` to simulate `WeightedFactorStrategyV1` scoring.

The runner includes:

- transaction fee and slippage assumptions
- `max_position_pct`
- `max_sector_pct`
- `max_daily_order_count`
- `max_order_amount_krw`
- optional stop loss and target return parameters

The runner writes only `backtest_runs`. It must not create `orders`, must not call Toss, and must not mutate `strategy_versions.status`.

## AI Candidate Backtest Gate

Monthly research generation reads recent `backtest_runs` as a compact summary only. It does not replay a backtest, does not deploy a strategy, and does not treat a passing backtest as approval.

Monthly AI candidates are hypotheses, not strategies. A row in `ai_upgrade_candidates` with `status='proposed'` only means:

- OpenAI returned valid structured JSON.
- The worker stored the candidate for review.
- No approval, paper promotion, live promotion, or broker action occurred.

Before promoting any AI candidate to paper:

- replay the month used to generate the candidate
- run out-of-sample periods not used in the prompt
- include transaction costs and slippage
- compare with the previous active strategy
- record max drawdown, turnover, hit rate, average win/loss, and sector exposure
- record the number of candidate variants tried
- reject candidates that only improve one month or one symbol cluster

Before live promotion:

- require paper validation first
- keep `live_order_allowed=false` during deployment
- verify rollback target
- require manual confirmation

Batch-generated candidates follow the same backtest gate.
