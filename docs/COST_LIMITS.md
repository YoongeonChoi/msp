# Cost Limits

Render:

- Background Worker Starter target: `$7/month`.
- 512MB RAM and 0.5 CPU require small batches and no heavy ML training.

Supabase:

- Free plan DB budget: 500MB, matching the current official Free project database space.
- Paper health warns at 450MB before the Free cap.
- Monitor with `select pg_database_size(current_database());`.
- Retain high-value records long term and clean heartbeat/status feeds.

API budgets:

- Prefer batch symbol calls where official APIs allow.
- Keep Naver Search News calls below the official daily quota of 25,000 calls.
- Use OpenAI only for compact classification and monthly offline research.

Retention:

- `worker_heartbeats`: 7 days
- `api_health`: 30 days
- debug/info `engine_events`: 30 days
- warning/error/critical `engine_events`: 365 days
- unlinked `news_events`: 180 days
- orders/outcomes/strategy versions: long term
