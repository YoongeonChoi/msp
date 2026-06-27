# Rollback

Rollback must reduce trading risk before changing code, strategy, or database state.

## First Step

Always disable live permission first:

```sql
update public.bot_settings
set enabled = false,
    live_order_allowed = false,
    updated_at = now()
where id = 'singleton';
```

Verify:

```sql
select id, enabled, mode, live_order_allowed
from public.bot_settings
where id = 'singleton';
```

## Render

1. Confirm `live_order_allowed=false`.
2. Use Render previous deploy rollback manually.
3. Verify worker heartbeat.
4. Resume paper only.
5. Review `engine_events`.

GitHub Actions must not auto-rollback into a running live state.

CI safety guards must remain active during rollback PRs:

- no production secrets in workflows
- no automatic Render deploy or rollback command
- no tracked non-example `.env` files
- `live_order_allowed=false` verified before manual Render rollback

## Strategy

1. Keep trading disabled.
2. Restore previous paper strategy version.
3. Do not promote any AI candidate directly to live.
4. Record audit evidence.
5. Resume paper validation.

## Database

- Prefer forward fixes.
- Migration PRs must include rollback notes.
- Destructive migrations require explicit rollback note or destructive migration approval text.
- Never auto-rollback into live trading.

## Verification SQL

Check no live-like orders appeared during rollback:

```sql
select *
from public.orders
where status in ('sent', 'filled', 'partial_filled')
order by created_at desc
limit 20;
```

Check worker state:

```sql
select *
from public.worker_heartbeats
order by created_at desc
limit 10;
```
