## Summary

## Risk Impact

## Trading Behavior Changed? yes/no

## Risk Engine Changed? yes/no

## Execution Engine Changed? yes/no

## DB Migration Changed? yes/no

## Security Impact

## Tests Run

## Manual Verification SQL

```sql
-- Include SQL used to verify Supabase state, or write N/A with reason.
```

## Rollback Plan

## Live Trading Safety Checklist

- [ ] `bot_settings.enabled=false` before deploy
- [ ] `live_order_allowed=false` before deploy
- [ ] `mode='paper'` verified before any worker resume
- [ ] no live Toss order endpoint added or called
- [ ] OpenAI output cannot create orders or deploy strategies
- [ ] Supabase RLS was not weakened
- [ ] Render deployment remains manual; no auto deploy workflow added
- [ ] worker heartbeat verified
- [ ] paper mode verified first
- [ ] rollback target known
