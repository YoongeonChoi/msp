# Risk Policy

All live orders require every live policy to pass:

- bot enabled
- valid settings
- active strategy version present
- mode live
- live permission true
- market open known true
- fresh valid quote
- account sync successful and fresh
- Toss and Supabase healthy
- max position not exceeded
- max sector not exceeded
- daily loss below limit
- daily order count below limit and verified
- order amount within limit
- unique idempotency key
- no critical negative news risk for buys
- liquidity sufficient
- volatility acceptable
- no cooldown
- no shutdown in progress

Paper orders use a separate policy set:

- bot enabled
- valid settings
- active strategy version present
- market open known true
- fresh valid quote
- account sync successful and fresh
- critical provider health good
- max position not exceeded
- max sector not exceeded
- daily loss below limit
- daily order count below limit
- order amount within limit
- no duplicate signal in cooldown window
- no critical negative news risk for buys
- liquidity sufficient
- volatility acceptable
- no cooldown

Paper policy excludes `mode_live` and `live_order_allowed` so safe paper trading can run with `mode='paper'` and `live_order_allowed=false`. It still blocks stale quotes, missing quotes, provider health failures, critical news risk, exceeded limits, missing strategy version, duplicate signals, and invalid settings.

Paper duplicate order prevention:

- Logical idempotency is based on `paper`, hourly cooldown bucket, strategy version, symbol, action, and order amount.
- Repeated signals inside the cooldown bucket create a `blocked` order with the duplicate reason instead of another `paper` order.
- Blocked paper orders write an `engine_events` row with `message='paper_order_blocked'`.

Fail-closed matrix:

| Condition | Paper order | Live order |
| --- | --- | --- |
| Missing setting | Block | Block |
| Invalid setting | Block | Block |
| Missing strategy version | Block | Block |
| Unknown market calendar | Block | Block |
| Missing quote | Block | Block |
| Stale quote | Block | Block |
| Unverified daily order count | Block | Block |
| Supabase unavailable | Block | Block |
| Toss unavailable | Block | Block |
| Critical news risk | Block new buy | Block new buy |
| Duplicate signal | Block | Block |
| OpenAI unavailable | Use cached news risk or block affected new buys | Use cached news risk or block affected new buys |
| DB write failure | Block | Block |
| Unknown exception | Block | Block |

Every blocked order must persist a reason.

Broker call rule:

- Paper orders never call `BrokerPort.place_order`.
- Scheduled live cycles do not use simulated paper account data.
- Missing live account state blocks with `missing_account_state` before any broker call.
- Live daily order count is verified from local system-created `orders` for the current KST trading day
  only after the operator has explicitly accepted the system-originated-order scope with
  `LIVE_SYSTEM_ORDER_COUNT_SCOPE_ACCEPTED=true`. Live readiness also requires retained
  `system_order_scope_evidence.json` proving scope, Toss limitation, deployment environment, operator,
  runtime env confirmation, HTTPS evidence URI, and SHA-256 hash; the env var alone is only the runtime gate,
  not the release evidence. The final release bundle also binds the evidence to the target environment:
  `staging` requires `deployment_environment=staging`, and `production-readiness` requires
  `deployment_environment=production`. The default `false` blocks with
  `daily_order_count_unverified`, records `live_external_order_history_scope_not_accepted`, and makes no
  broker call.
- If the local repository count cannot be read after scope acceptance, risk also blocks with
  `daily_order_count_unverified` before any broker call.
- Live orders may call the broker only from `ExecutionService` after final risk passes.
- OpenAI structured output is research/classification input only and cannot trigger execution.
- Toss live order creation is limited to guarded KRX `LIMIT` orders. Manual cancel is limited to one
  existing local open live order through the worker CLI, and local `canceled` requires provider
  `CANCELED` confirmation. Modify remains disabled.
