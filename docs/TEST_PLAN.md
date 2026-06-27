# Test Plan

Unit:

- signal scoring
- each risk policy
- risk aggregation
- idempotency
- stale quote detection
- provider error mapping
- redaction
- config validation
- circuit breaker transitions

Integration with mocks:

- disabled bot creates no decisions
- paper mode creates paper orders
- live mode blocked by default
- Supabase/Toss health failure blocks live order
- OpenAI invalid JSON rejected
- retention dry-run

Contract:

- provider fixture schema validation
- no live broker calls in CI
- uncertain contracts tracked in `API_GAPS.md`

E2E smoke:

- worker starts with mocks
- heartbeat persists
- desktop builds
- dangerous controls require confirmation

