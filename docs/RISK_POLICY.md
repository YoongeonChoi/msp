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
- projected position after a buy does not exceed the maximum
- projected sector exposure after a buy does not exceed the maximum
- daily loss below limit
- daily order count below limit and verified
- order amount within limit
- cash buying power covers the calculated whole-share buy notional
- synchronized position quantity covers the calculated sell quantity
- unique idempotency key
- no critical negative news risk for buys
- liquidity sufficient
- volatility acceptable
- no cooldown
- no shutdown in progress

For live cycles, time-sensitive settings, provider/calendar state, account,
positions, active strategy approval, and quote inputs are refreshed after
feature collection. The final `RiskService` evaluation uses only that refreshed
snapshot; any failure or state change blocks before a broker call.

Paper orders use a separate policy set:

- bot enabled
- valid settings
- active strategy version present
- fresh valid quote
- account sync successful and fresh
- projected simulated position after a buy does not exceed the maximum
- projected simulated sector exposure after a buy does not exceed the maximum
- daily loss below limit
- daily order count below limit
- order amount within limit
- simulated account cash covers the calculated whole-share buy notional
- no duplicate signal in cooldown window
- no critical negative news risk for buys
- liquidity sufficient
- volatility acceptable
- no cooldown

Paper policy excludes `mode_live`, `live_order_allowed`, `market_open`, and provider-health gates so
safe paper trading can run with `mode='paper'` and `live_order_allowed=false` outside market hours or
while a broker health probe is degraded. Missing quote/provider data can still prevent a decision from
being built. Paper position, liquidity, and volatility inputs are explicit simulation assumptions and are
stored in `feature_snapshot.raw.risk_evidence`; verified critical-news evidence is used when present.
Paper sell decisions do not reuse broker-synchronized holdings as simulated inventory. The current
paper engine has no persisted paper position ledger, so it records the simulated order quantity and
decision price but does not claim inventory enforcement. This limitation applies only to paper mode;
live sells still fail closed on unknown or insufficient synchronized holdings.

Live buy exposure evidence:

- Holdings must be synchronized from the broker and account equity must be positive and fresh.
- The worker checks current symbol/sector exposure plus the proposed buy amount.
- Unknown target sector, any unclassified held sector, or missing position sync blocks the buy.
- Critical-news, liquidity, and volatility inputs must be explicit booleans in the feature evidence.
- Position/sector maximum and critical-news policies do not block `sell` or `hold` decisions because
  they do not add exposure; the remaining live policies still apply.

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
| Unknown market calendar | Does not block paper by itself | Block |
| Missing quote | Block | Block |
| Stale quote | Block | Block |
| Unverified daily order count | Uses simulated paper count | Block |
| Supabase unavailable | Block | Block |
| Toss health probe degraded | Does not block paper by itself | Block |
| Unknown position or sector exposure | Uses recorded paper assumptions | Block new buy |
| Insufficient cash buying power | Block | Block |
| Unknown or insufficient sell quantity | Not enforced until a paper position ledger exists | Block |
| Missing liquidity or volatility evidence | Uses recorded paper assumptions | Block new buy |
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
