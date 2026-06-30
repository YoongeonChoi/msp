# Paper Trading Operations

Paper Trading 운영 점검은 저장된 Supabase control-plane 데이터만 읽는다. 이 경로는 Toss 주문 생성, live broker execution, `decision_snapshots` 생성, `orders` 생성을 수행하지 않는다.

## Command

Run from `apps/worker` with server-side Supabase env vars:

```bash
python -m app.tools.paper_health_report
```

On Windows:

```bash
py -m app.tools.paper_health_report
```

Required env:

```text
SUPABASE_URL=...
SUPABASE_SECRET_KEY=...
PAPER_HEALTH_DB_WARNING_BYTES=450000000
```

The command prints a safe report and writes one `engine_events` summary row with `component='paper_ops'` and `message='paper_health_report'`. It does not print API keys, provider secrets, account identifiers, or raw provider payload details. Its own `paper_ops` summary events are excluded from the repeated-critical-event count so the report cannot keep itself failing, but operational critical events from worker/provider components still count.

## Report Sections

- `bot_settings`: `enabled`, `mode`, `live_order_allowed`
- latest worker heartbeat age in seconds
- latest provider health by provider, including safe failure details when present
- decisions in the last 24h grouped by `action`
- orders in the last 24h grouped by `status`
- live-like order count for `sent`, `filled`, `partial_filled`
- duplicate `idempotency_key` count
- missing `reason_json` metrics when the column is present
- missing feature JSON metrics when `feature_json`, `feature_snapshot_json`, or `feature_snapshot` exists
- orders missing `idempotency_key`
- recent `error` and `critical` `engine_events`
- DB size from the latest `retention_runs.db_size_bytes`, if queryable
- final `PASS`, `WARN`, or `FAIL`

Provider health detail output is intentionally narrow. When an `api_health`
row has safe diagnostic fields, the report may print values such as
`error_type=ProviderAuthError reason=toss_access_denied` on the provider line.
Only `error_type`, `reason`, `status`, and `code` are summarized, string values
are single-line normalized and bounded, and the final line still passes through
secret redaction before it is printed. Raw provider payloads, credentials,
tokens, account identifiers, and unknown detail keys must remain out of the
report.

## Exit Codes

- `0`: final result is `PASS` or `WARN`
- `1`: final result is `FAIL`, Supabase env is missing, or a Supabase query fails

Warnings do not stop the command because they are operational follow-up items, not critical consistency failures.

## Critical Failures

The command returns `FAIL` when any of these are true:

- `bot_settings.live_order_allowed=true`
- `bot_settings.mode='live'`
- any `sent`, `filled`, or `partial_filled` order exists
- duplicate `idempotency_key` exists
- latest heartbeat is missing or older than 5 minutes
- two or more recent operational critical `engine_events` exist, excluding
  `component='paper_ops'` and `message='paper_health_report'`
- Supabase query fails

## Warnings

The command returns `WARN` when no critical failure exists but one of these conditions is found:

- latest provider health is degraded
- no decisions were generated during Korean market hours while bot is enabled
- blocked paper orders are high
- DB size is above `PAPER_HEALTH_DB_WARNING_BYTES`
- orders older than the outcome grace window are missing optional outcome rows
- orders are missing `idempotency_key`

## Manual Verification SQL

Keep Paper Trading fail-closed before running the report:

```sql
update public.bot_settings
set mode = 'paper',
    live_order_allowed = false,
    updated_at = now()
where id = 'singleton'
returning id, enabled, mode, live_order_allowed;
```

Check the command summary event:

```sql
select level, component, message, details, created_at
from public.engine_events
where component = 'paper_ops'
  and message = 'paper_health_report'
order by created_at desc
limit 5;
```

Check no live-like order exists:

```sql
select *
from public.orders
where status in ('sent', 'filled', 'partial_filled')
order by created_at desc
limit 20;
```

Check duplicate idempotency keys:

```sql
select idempotency_key, count(*) as count
from public.orders
where idempotency_key is not null
group by idempotency_key
having count(*) > 1;
```

Check latest heartbeat:

```sql
select *
from public.worker_heartbeats
order by created_at desc
limit 1;
```

After a manual Render deploy, confirm the report's `[worker]` section shows a
`release_sha` or `release_source` for the expected commit. The underlying row is:

```sql
select
  details->>'release_sha' as release_sha,
  details->>'release_source' as release_source,
  created_at
from public.worker_heartbeats
order by created_at desc
limit 1;
```

## Safety Notes

- Do not run this command from Desktop.
- Do not copy Supabase secret keys into Vite/Tauri env vars.
- Do not use this report as approval for live trading.
- If the report returns `FAIL`, keep `live_order_allowed=false` and inspect the listed finding codes first.
