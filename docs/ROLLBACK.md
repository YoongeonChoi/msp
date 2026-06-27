# Rollback

Render:

1. Disable live order permission.
2. Use previous deploy rollback.
3. Verify heartbeat.
4. Resume paper only.

Strategy:

1. Mark current `strategy_versions.status='retired'` if bad.
2. Restore previous paper or active version.
3. Record audit log.

Database:

- Prefer forward fixes.
- Migration PRs must include rollback notes.
- Never auto-rollback into live trading.

