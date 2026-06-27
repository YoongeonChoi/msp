# Context Map

Trading owns cycles, decisions, strategy version use, and paper/live distinction.

MarketData owns quotes, candles, KRX calendar/statistics placeholders, and quote freshness.

Fundamentals owns OpenDART financial statement normalization and account mapping.

NewsIntel owns Naver news ingestion, deduplication, OpenAI classification, and critical news risk.

Portfolio owns positions, sector exposure, PnL, and account sync state.

Risk owns all live-order gates and fail-closed aggregation.

Execution owns order proposal, idempotency, broker call sequencing, unknown status handling, and audit events.

StrategyResearch owns monthly AI candidates, backtest requirements, and approval workflow.

Operations owns heartbeat, api_health, engine_events, outbox, retention, releases, and runbooks.

IdentityAccess owns Supabase Auth, `user_roles`, and RLS.

