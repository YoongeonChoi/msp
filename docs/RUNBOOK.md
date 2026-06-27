# Runbook

Worker down:

1. Keep `live_order_allowed=false`.
2. Check Render logs.
3. Check latest `worker_heartbeats`.
4. Restart worker.
5. Verify heartbeat before paper resume.

API outage:

1. Confirm `api_health`.
2. For Toss/Supabase outage, live orders remain blocked.
3. For OpenAI outage, use cached news risk or block affected new buys.

DB full:

1. Disable bot.
2. Run retention dry-run.
3. Export long-term logs if needed.
4. Apply cleanup.

Unknown broker status:

1. Stop new orders for same symbol.
2. Check official broker order status channel.
3. Mark manually reconciled only after evidence.

Secret leak:

1. Disable bot.
2. Rotate leaked key.
3. Review logs and Git history.
4. Re-enable paper only after verification.

