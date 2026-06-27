# Supabase Instructions

## Overview

Supabase is the control plane for commands, state, logs, RLS, and lightweight Realtime.

## Where To Look

| Task | Location |
| --- | --- |
| Schema | `migrations/0001_schema.sql` |
| RLS | `migrations/0002_rls.sql` |
| Realtime | `migrations/0003_realtime.sql` |
| Retention | `migrations/0004_retention.sql` |
| Safety seed | `seed.sql` |

## Rules

- Every public table must have RLS enabled.
- No anon write policies.
- Desktop authenticated admin may update settings/watchlist/manual commands, not insert orders or decisions.
- Worker writes privileged tables with server-side secret key only.
- Never store raw API secrets.
- Keep `bot_settings.enabled=false`, `mode='paper'`, `live_order_allowed=false` in seed.
- Realtime only for lightweight control/status tables.
- Destructive migrations need explicit review and rollback notes.

## Checks

```bash
rg "enable row level security" supabase/migrations/0002_rls.sql
rg "live_order_allowed" supabase/seed.sql
```

