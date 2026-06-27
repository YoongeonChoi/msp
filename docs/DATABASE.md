# Database

Core tables:

- `bot_settings`
- `watchlist`
- `positions`
- `orders`
- `decision_snapshots`
- `features_daily`
- `fundamentals_quarterly`
- `news_events`
- `strategy_versions`
- `ai_upgrade_candidates`
- `api_health`
- `worker_heartbeats`
- `engine_events`
- `audit_logs`
- `outbox_events`

Rules:

- Store UTC `timestamptz`; display KST in UI.
- Keep queryable fields typed.
- Use JSONB for snapshots and extensible strategy params.
- Never store raw API secrets.
- Never store full OpenAI prompts by default.
- Never store full news article bodies.
- Unique `orders.idempotency_key`.
- RLS enabled for all exposed public tables.

DB size query:

```sql
select pg_database_size(current_database());
```

