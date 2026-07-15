# Supabase Instructions

## Overview

Supabase is the control plane for commands, state, logs, RLS, and lightweight Realtime.

## Where To Look

| Task | Location |
| --- | --- |
| Legacy baseline | `migrations/0001_schema.sql` through `0015_*` |
| G1/G2 source of truth | `migrations/0016_private_foundation.sql`, `0017_execution_accounting_truth.sql` |
| RPC and access boundary | `migrations/0018_*` through `0024_*` |
| Disposable integration verifier | `verify_g1_g2_migration.py` |
| Safety seed | `seed.sql` |

## Rules

- `private` is the V2 source of truth; it is not an exposed Data API schema.
- Desktop uses only strict `api` views/RPCs; Worker uses only the fixed `worker_api` RPC surface.
- Every exposed table/view must have RLS and explicit grants.
- No anon write policies.
- Desktop and Worker must not directly CRUD `public.*` or `private.*` V2 state.
- `SECURITY DEFINER` implementations belong in `private`, use `search_path=''`, and have narrow execute grants.
- Never store raw API secrets.
- Keep `bot_settings.enabled=false`, `mode='paper'`, `live_order_allowed=false` in seed.
- Realtime publishes only `api.control_plane_signal`; raw order, audit, command, and decision payloads stay out.
- `public.orders`, `positions`, `manual_commands`, and `audit_logs` are frozen legacy evidence and never feed V2 balances.
- Destructive migrations need explicit review and rollback notes.

## Checks

```bash
python .github/scripts/repository_safety.py migrations
python supabase/verify_g1_g2_migration.py
```

