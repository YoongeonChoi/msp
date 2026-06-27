# Risk Policy

All live orders require every policy to pass:

- bot enabled
- mode live
- live permission true
- market open known true
- fresh valid quote
- account sync successful and fresh
- Toss and Supabase healthy
- max position not exceeded
- max sector not exceeded
- daily loss below limit
- daily order count below limit
- order amount within limit
- unique idempotency key
- no critical negative news risk for buys
- liquidity sufficient
- volatility acceptable
- no cooldown
- no shutdown in progress

Fail-closed matrix:

| Condition | Live order |
| --- | --- |
| Missing setting | Block |
| Unknown market calendar | Block |
| Stale quote | Block |
| Supabase unavailable | Block |
| Toss unavailable | Block |
| OpenAI unavailable | Use cached news risk or block affected new buys |
| DB write failure | Block |
| Unknown exception | Block |

Every blocked order must persist a reason.

