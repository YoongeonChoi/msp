# Data Pipeline

Toss: account, position, quote, candle, order contracts are represented as ports and placeholders until official endpoint details are verified.

KRX: market calendar/listing/statistics are adapter placeholders and mock data in local mode.

OpenDART: financial statement ingestion requires corp code and account-name mapping verification. Canonical fields are defined in `domain/fundamentals/value_objects.py`.

Naver: news search stores title, source, published time, compact summary/classification, and hashes for deduplication. Full article scraping is out of MVP scope.

OpenAI: structured outputs classify news/disclosures and propose monthly strategy candidates. Inputs must be sanitized and data-minimized.

Feature storage uses typed columns for queryable fields and JSONB only for snapshots.

## Outcome Tracking

`python -m app.tools.update_outcomes_once` builds paper-trading outcomes from stored database facts only:

- `decision_snapshots` provides `symbol`, `action`, decision time, and `feature_snapshot.price_at_decision`.
- `features_daily` provides verified cached future close prices by trading date.
- `orders` provides linked paper order amount, price, and quantity when available.
- `outcomes` is upserted by `decision_id`, so reruns update the same row instead of creating duplicates.

The command does not call Toss order APIs, does not create orders, does not create decision snapshots, and does not send account data to OpenAI. Non-trading days are handled by using the next available `features_daily.trade_date` rows rather than calendar-day interpolation.

Outcome fields:

- `return_1d`, `return_5d`, `return_20d`
- `max_drawdown_20d`
- `hit_target`, `hit_stop`
- `realized_pnl_krw`
- `outcome_status`: `pending`, `partial`, `complete`, or `skipped`

`return_pct` and `pnl_krw` remain populated from the 20-day outcome for compatibility with monthly research summaries.
