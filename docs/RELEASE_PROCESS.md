# Release Process

Versioning:

- `v0.x.y` until live trading is implemented and tested.

Pre-release checklist:

- Worker tests pass.
- Desktop typecheck/build pass.
- Migration check pass.
- `bot_settings.enabled=false`.
- `live_order_allowed=false`.
- Rollback target identified.

Deploy:

1. Disable live.
2. Deploy Render manually.
3. Verify heartbeat.
4. Run worker with paper settings.
5. Check engine events.
6. Re-enable only paper.

