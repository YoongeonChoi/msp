# Observability

Signals:

- structured JSON logs
- `cycle_id`
- `decision_id`
- `order_id`
- provider name
- redacted secrets
- heartbeat every 30 seconds
- `api_health`
- `engine_events`
- `audit_logs`
- circuit breaker state
- memory pressure
- loop duration

UI warnings:

- stale heartbeat older than 2 minutes
- Toss or Supabase unhealthy
- critical `engine_events`
- live mode enabled
- live order allowed enabled

