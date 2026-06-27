# Cost Limits

Render:

- Background Worker Starter target: `$7/month`.
- 512MB RAM and 0.5 CPU require small batches and no heavy ML training.

Supabase:

- Free plan assumed internal hard budget: 500MB.
- Monitor with `select pg_database_size(current_database());`.
- Retain high-value records long term and clean heartbeat/status feeds.

API budgets:

- Prefer batch symbol calls where official APIs allow.
- Keep Naver daily budget.
- Use OpenAI only for compact classification and monthly offline research.

Retention:

- `worker_heartbeats`: 7 days
- `api_health`: 30 days
- debug/info `engine_events`: 30 days
- warning/error/critical `engine_events`: 365 days
- unlinked `news_events`: 180 days
- orders/outcomes/strategy versions: long term

