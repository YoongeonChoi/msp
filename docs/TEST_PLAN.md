# Test Plan

Unit:

- signal scoring
- each risk policy
- risk aggregation
- paper risk evaluation excludes live permission but keeps safety gates
- idempotency
- stale quote detection
- missing quote blocks order
- provider health failure blocks order
- max position and max sector blocks
- max daily order count blocks
- max daily loss blocks
- max order amount blocks
- critical negative news blocks paper buys
- missing strategy version blocks order
- invalid settings block order
- log redaction removes API keys and authorization values
- paper health report PASS/WARN/FAIL aggregation
- paper health report duplicate idempotency detection
- paper health report stale heartbeat detection
- paper health report output does not print secrets
- paper health report ignores its own `paper_ops/paper_health_report` critical
  events for repeated-critical counts while still failing on worker criticals
- provider health failure details persist only safe bounded fields and redact
  secret-looking keys or values
- provider error mapping
- redaction
- config validation
- circuit breaker transitions

Integration with mocks:

- disabled bot creates no decisions or orders and calls no broker order API
- disabled bot records heartbeat and safe health checks only
- paper mode creates only `paper`, `proposed`, or `blocked` orders
- paper mode never calls broker order API
- duplicate paper signal in cooldown window is blocked
- stale quote creates a blocked paper order
- decision snapshot includes component scores and final score
- strategy DB params can change paper action without code changes
- missing strategy creates no decision or order
- live mode blocked by default
- `live_order_allowed=false` prevents broker calls
- Supabase/Toss health failure blocks order creation
- OpenAI invalid JSON rejected
- retention dry-run

Contract:

- provider fixture schema validation
- no live broker calls in CI
- uncertain contracts tracked in `API_GAPS.md`
- safe one-off tools do not print secrets and do not call live broker order APIs
- `paper_health_report` reads Supabase only and creates no orders or decisions

E2E smoke:

- worker starts with mocks
- heartbeat persists
- bot disabled creates no decisions/orders
- paper enabled creates paper/blocked orders only
- no `sent`, `filled`, or `partial_filled` order appears in paper mode
- daily `paper_health_report` exits non-zero for critical consistency failures only
- desktop builds
- dangerous controls require confirmation
