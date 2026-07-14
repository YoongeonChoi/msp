# Engine

Startup:

1. Load settings.
2. Configure JSON logging.
3. Install SIGTERM/SIGINT handlers.
4. Build provider clients and circuit breaker skeletons.
5. Record heartbeat.

Cycle:

1. Load `bot_settings`.
2. If disabled, keep heartbeat, provider health, feature collection, and decision snapshots running,
   but create no paper or live orders.
3. Validate settings. Invalid settings stop the cycle before decisions or orders.
4. If market closed or unknown, run off-market jobs only.
5. Load watchlist and active strategy.
6. Prefer `strategy_versions.version='strategy_v1_weighted_factor'` when it is `paper` or `active`; otherwise fall back to another `paper`/`active` strategy.
7. If no active paper strategy exists, stop the cycle and record `missing_strategy_version`.
8. Fetch quotes and validate freshness through `RiskService`.
9. Build features.
10. In live mode, refresh time, settings, provider/calendar state, account,
    positions, strategy approval, and quote evidence after feature collection.
11. Score with `WeightedFactorStrategyV1` using DB `weights` and `params`.
12. Evaluate paper or live risk gates before order creation.
13. Persist decision snapshot with component scores, `price_at_decision`, feature snapshot, and risk snapshot.
14. In paper mode, create only `paper` or `blocked` orders.
15. In live mode, run final risk gates and block unless all pass.

Paper trading:

- Disabled bot mode keeps observation and decision snapshots available while preventing order creation.
- Paper mode never calls `BrokerPort.place_order`.
- Paper orders require an idempotency key, strategy explanation, feature snapshot, and risk snapshot.
- Paper and live buys require enough mode-specific account cash for the calculated whole-share
  limit-order notional. Live sells require enough synchronized quantity for the calculated order
  quantity; paper sells do not reuse live holdings while a separate paper position ledger is absent.
- Allowed paper orders persist the shared whole-share `quantity` and decision `price_krw`. Outcome
  tracking prioritizes those execution details over legacy snapshot/order price fields.
- Duplicate paper signals for the same symbol/action/strategy in the same hourly cooldown bucket are blocked, not sent again.
- Paper order statuses must stay within `paper`, `proposed`, or `blocked`; the current worker creates `paper` and `blocked` only.

Safe one-off tools:

```bash
python -m app.tools.seed_watchlist_demo
python -m app.tools.seed_strategy_v1
python -m app.tools.run_paper_cycle_once
python -m app.tools.run_backtest --strategy strategy_v1_weighted_factor --start YYYY-MM-DD --end YYYY-MM-DD
python -m app.tools.reconcile_live_orders_once --limit 50
python -m app.tools.run_live_execution_safety_drill_once
python -m app.tools.run_live_recovery_drill_once
python -m app.tools.verify_live_readiness_scorecard --scorecard docs/LIVE_READINESS_SCORECARD.md --security-evidence path/to/security_scan_summary.json --repo-root .
python -m app.tools.cancel_live_order_once --order-id ORDER_UUID
```

`run_paper_cycle_once` forces `enabled=true`, `mode='paper'`, and `live_order_allowed=false` for that one cycle only. It uses mock broker execution and does not print secrets.

Backtesting:

- Uses cached `features_daily`, `fundamentals_quarterly`, `news_events`, `watchlist`, and `strategy_versions`.
- Replays `WeightedFactorStrategyV1` scoring with stored strategy weights and params.
- Simulates paper-only positions in memory and writes `backtest_runs`.
- Does not call `BrokerPort.place_order`, create `orders`, or change `strategy_versions.status`.

Live trading:

- Toss live order creation is implemented only as a guarded worker-owned KRX `LIMIT` order path.
- The scheduled worker cycle never uses simulated paper account data in live mode.
- Live cycles read Toss cash buying power and holdings through official read-only endpoints.
- Live buy risk uses synchronized holdings and account equity to check the projected symbol and sector
  exposure after the proposed order. Missing position sync, unknown held-position sectors, or unverified
  target-sector evidence blocks before any broker call.
- The same pure whole-share quantity calculation is used by risk evaluation and broker request creation;
  insufficient cash, unknown sell inventory, or insufficient sell quantity blocks before broker dispatch.
- Critical-news, liquidity, and volatility gates accept only explicit feature evidence in live mode;
  missing evidence remains fail-closed.
- Toss broker-wide or externally placed daily order history remains unverified because
  `GET /api/v1/orders status=CLOSED` is documented as `400 closed-not-supported`.
- System-created live order count is verified from local `orders` rows for the current KST trading day
  before risk evaluation; repository count failure blocks with `daily_order_count_unverified` before
  any broker call.
- Live market-open state uses Toss KR `regularMarket` calendar and stops before the documented
  `singlePriceAuctionStartTime` when present.
- Worker cycles reconcile existing live `sent`, `partial_filled`, or `unknown_requires_manual_check`
  orders through Toss order reads before new decisions.
- A local `unknown_requires_manual_check` order is never auto-cleared by reconciliation. A later provider
  status observation records `live_order_manual_check_provider_status_observed` and leaves the order in
  manual recovery until operator review.
- If any of those live orders remains pending after reconciliation, the cycle records
  `live_pending_reconciliation_blocks_new_live_orders` and stops before new live order
  proposals or broker calls. Decision snapshots may still be written so provider health,
  features, and signals remain observable while execution is gated.
- Manual live cancellation is available only through the worker CLI for one local open live order at a time.
  The worker confirms the original provider order is `CANCELED` before marking local `canceled`; timeout,
  unknown provider results, or non-`CANCELED` confirmation require manual review. Modify remains disabled
  until provider contracts, price/quantity policy, and rollback workflows exist.
- UI and OpenAI output cannot call broker execution.
- `live_order_allowed=false` blocks live proposals before any broker call.
- `provider_live_v1` features are not live-ready until they include non-mock
  quote, provider fundamentals, provider news, positive PER/PBR valuation
  inputs, and verified market/sector evidence. Missing valuation or
  market/sector evidence records `live_feature_snapshot_not_ready` and stops
  before live order proposal creation.

Initial scoring:

```text
final_score =
  0.35 * technical_score
+ 0.25 * fundamental_score
+ 0.15 * market_sector_score
+ 0.15 * news_event_score
+ 0.10 * portfolio_score
```
