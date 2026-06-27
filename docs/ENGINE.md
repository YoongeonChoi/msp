# Engine

Startup:

1. Load settings.
2. Configure JSON logging.
3. Install SIGTERM/SIGINT handlers.
4. Build provider clients and circuit breaker skeletons.
5. Record heartbeat.

Cycle:

1. Load `bot_settings`.
2. If disabled, record one heartbeat, run safe provider health checks, and create no decisions or orders.
3. Validate settings. Invalid settings stop the cycle before decisions or orders.
4. If market closed or unknown, run off-market jobs only.
5. Load watchlist and active strategy.
6. Prefer `strategy_versions.version='strategy_v1_weighted_factor'` when it is `paper` or `active`; otherwise fall back to another `paper`/`active` strategy.
7. If no active paper strategy exists, stop the cycle and record `missing_strategy_version`.
8. Fetch quotes and validate freshness through `RiskService`.
9. Build features.
10. Score with `WeightedFactorStrategyV1` using DB `weights` and `params`.
11. Evaluate paper or live risk gates before order creation.
12. Persist decision snapshot with component scores, feature snapshot, and risk snapshot.
13. In paper mode, create only `paper` or `blocked` orders.
14. In live mode, run final risk gates and block unless all pass.

Paper trading:

- Disabled bot mode performs heartbeat and health checks only.
- Paper mode never calls `BrokerPort.place_order`.
- Paper orders require an idempotency key, strategy explanation, feature snapshot, and risk snapshot.
- Duplicate paper signals for the same symbol/action/strategy in the same hourly cooldown bucket are blocked, not sent again.
- Paper order statuses must stay within `paper`, `proposed`, or `blocked`; the current worker creates `paper` and `blocked` only.

Safe one-off tools:

```bash
python -m app.tools.seed_watchlist_demo
python -m app.tools.seed_strategy_v1
python -m app.tools.run_paper_cycle_once
python -m app.tools.run_backtest --strategy strategy_v1_weighted_factor --start YYYY-MM-DD --end YYYY-MM-DD
```

`run_paper_cycle_once` forces `enabled=true`, `mode='paper'`, and `live_order_allowed=false` for that one cycle only. It uses mock broker execution and does not print secrets.

Backtesting:

- Uses cached `features_daily`, `fundamentals_quarterly`, `news_events`, `watchlist`, and `strategy_versions`.
- Replays `WeightedFactorStrategyV1` scoring with stored strategy weights and params.
- Simulates paper-only positions in memory and writes `backtest_runs`.
- Does not call `BrokerPort.place_order`, create `orders`, or change `strategy_versions.status`.

Live trading:

- Toss live order execution is not implemented in this MVP.
- UI and OpenAI output cannot call broker execution.
- `live_order_allowed=false` blocks live proposals before any broker call.

Initial scoring:

```text
final_score =
  0.35 * technical_score
+ 0.25 * fundamental_score
+ 0.15 * market_sector_score
+ 0.15 * news_event_score
+ 0.10 * portfolio_score
```
