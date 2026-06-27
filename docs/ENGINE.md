# Engine

Startup:

1. Load settings.
2. Configure JSON logging.
3. Install SIGTERM/SIGINT handlers.
4. Build provider clients and circuit breaker skeletons.
5. Record heartbeat.

Cycle:

1. Load `bot_settings`.
2. If disabled, record event and create no decisions or orders.
3. Check Toss and Supabase health before any live path.
4. If market closed or unknown, run off-market jobs only.
5. Load watchlist and active strategy.
6. Fetch quotes and validate freshness.
7. Build features.
8. Score with `WeightedFactorStrategyV1`.
9. Persist decision snapshot.
10. In paper mode, create paper order only.
11. In live mode, run final risk gates and block unless all pass.

Initial scoring:

```text
final_score =
  0.35 * technical_score
+ 0.25 * fundamental_score
+ 0.15 * market_sector_score
+ 0.15 * news_event_score
+ 0.10 * portfolio_score
```

