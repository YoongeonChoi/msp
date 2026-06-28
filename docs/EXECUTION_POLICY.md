# Execution Policy

Only `ExecutionService` may call `BrokerPort.place_order`.

Live sequence:

1. Load persisted decision snapshot.
2. Verify snapshot freshness and active strategy.
3. Refresh account and quote.
4. Run final risk check immediately before broker call.
5. Generate idempotency key and reject duplicates before any broker call.
6. Persist blocked rows for failed evidence/risk checks, or persist an allowed live order as
   `unknown_requires_manual_check` with `reason='live_broker_order_result_pending'` immediately
   before broker submission.
7. Call broker through `BrokerPort`.
8. Update the same order row with provider status, provider order id, and provider payload summary.
9. Sync account and positions.
10. Write audit log and engine event.

If broker result is uncertain, status becomes `unknown_requires_manual_check`, blind retry is forbidden, and further orders for that symbol are blocked until reconciled.

The pre-broker `unknown_requires_manual_check` row is mandatory for allowed live orders. It records the
final idempotency key before `BrokerPort.place_order`, so a worker crash, timeout, or unknown provider
result cannot be retried as a fresh live order. Successful broker responses update that existing row;
deterministic pre-broker validation failures are persisted or updated as `failed` without broker
submission.

Live order creation is implemented only for the guarded worker path. It is limited to KRX quantity-based
`LIMIT` orders derived from the final fresh quote, after `RiskService` passes and database live-enable
approval controls are satisfied. Live enable approval requires a fresh `request_live_enable` manual command
accepted by an authenticated admin different from the requester; self-review is invalid at the database layer.
The approval row must carry non-empty provider contract, risk report, and release evidence; reviewed rows are
immutable, so changing evidence requires a new approval request. Enabling `live_order_allowed` consumes one
fresh accepted approval by moving it to `applied`; the same approval cannot be reused after live is disabled.
The scheduled worker cycle does not use simulated account data for live orders. It reads Toss cash buying
power and holdings for live account state. Broker-wide or externally placed daily order history remains
unverified because the official `GET /api/v1/orders status=CLOSED` response is documented as
`400 closed-not-supported`. For the enforceable live limit, the worker counts system-created live orders
from local `orders` rows for the current KST trading day before final risk evaluation. Production live
operation must be limited to system-originated orders unless a broker-wide closed-order history source is
proven. That limitation requires two controls: the runtime gate must be set with
`LIVE_SYSTEM_ORDER_COUNT_SCOPE_ACCEPTED=true`, and live-readiness release evidence must retain
`system_order_scope_evidence.json` proving the exact scope, Toss limitation, deployment environment,
operator, runtime env confirmation, evidence URI, and SHA-256 hash. The default runtime value is `false`.
The final release bundle binds this evidence to the target environment: a `staging` bundle requires
`deployment_environment=staging`, and a `production-readiness` bundle requires
`deployment_environment=production`. Any mismatch blocks the bundle.
Without runtime acceptance, live cycles record `live_external_order_history_scope_not_accepted`, persist blocked orders with
`daily_order_count_unverified`, and never call the broker. If the repository count cannot be read, live
cycles also persist blocked orders with `daily_order_count_unverified` before any broker call.

Each worker cycle first reconciles existing live orders in `sent`, `partial_filled`, or
`unknown_requires_manual_check` status by reading Toss order status through the broker adapter. Confirmed
`FILLED`, `PARTIAL_FILLED`, `CANCELED`, and `REJECTED` states are persisted back to `orders`; unknown Toss
codes, missing provider order IDs, and non-timeout provider failures require manual review instead of blind retry.
Once a local order is already `unknown_requires_manual_check`, reconciliation must not automatically clear it to
`sent`, `partial_filled`, `filled`, `canceled`, or `rejected` based on a later provider status read. It records
`live_order_manual_check_provider_status_observed`, preserves the manual-check status, and requires operator
review before the order can leave the manual recovery path.
If any live order remains in `sent`, `partial_filled`, or
`unknown_requires_manual_check` after that reconciliation pass, the worker records
`live_pending_reconciliation_blocks_new_decisions` and stops before creating new
live decisions or broker calls.

## Toss Adapter Boundary

The worker may use the Toss adapter only for verified read-only operations:

- token issue through `POST /oauth2/token`
- account list through `GET /api/v1/accounts`
- cash buying power through `GET /api/v1/buying-power`
- holdings through `GET /api/v1/holdings`
- current prices through `GET /api/v1/prices`
- candles through `GET /api/v1/candles`
- KR market calendar through `GET /api/v1/market-calendar/KR`
- order status/history reads through `GET /api/v1/orders` and `GET /api/v1/orders/{orderId}`

The adapter may call `POST /api/v1/orders` only from `ExecutionService.propose_live_order` after final
risk approval, quote-to-quantity validation, idempotency check, and the pre-broker manual-check row
has been durably persisted. The adapter may call `POST /api/v1/orders/{orderId}/cancel` only from the worker
manual cancellation service, using a local `orders.id` for an existing `sent` or `partial_filled`
live order. The cancel response is not treated as final cancellation proof; the service must confirm
the original order status through `GET /api/v1/orders/{orderId}` and mark local `canceled` only after
official `CANCELED`. Timeout, unknown cancel results, or non-`CANCELED` confirmation must mark the
local order `unknown_requires_manual_check` and require manual reconciliation before retry. The adapter
must not call modify endpoints until price/quantity policy and rollback workflows exist. Paper Trading may create only local `paper` or
`blocked` rows; it must never create `sent`, `filled`, or live-like rows from Toss read-only checks.

The Desktop app must not call Toss directly. It only reads and writes through Supabase under RLS.
