# ADR-0004 Paper First Trading Policy

Status: accepted

All strategies start in paper mode. `bot_settings.enabled=false`, `mode=paper`, and `live_order_allowed=false` are hard defaults.

Live mode requires explicit user action, final risk check, audit log, idempotency key, fresh quote, account sync, and official broker contract implementation.

