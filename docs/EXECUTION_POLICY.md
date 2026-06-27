# Execution Policy

Only `ExecutionService` may call `BrokerPort.place_order`.

Live sequence:

1. Load persisted decision snapshot.
2. Verify snapshot freshness and active strategy.
3. Refresh account and quote.
4. Run final risk check immediately before broker call.
5. Generate idempotency key.
6. Persist proposed order.
7. Call broker through `BrokerPort`.
8. Update order status.
9. Sync account and positions.
10. Write audit log and engine event.

If broker result is uncertain, status becomes `unknown_requires_manual_check`, blind retry is forbidden, and further orders for that symbol are blocked until reconciled.

MVP status: Toss live order execution is not implemented.

## Toss Read-Only Boundary

The worker may use the Toss adapter only for verified read-only operations:

- token issue through `POST /oauth2/token`
- account list through `GET /api/v1/accounts`
- holdings through `GET /api/v1/holdings`
- current prices through `GET /api/v1/prices`
- candles through `GET /api/v1/candles`
- order status/history reads through `GET /api/v1/orders` and `GET /api/v1/orders/{orderId}`

The adapter must not call `POST /api/v1/orders`, cancel, or modify endpoints. `TossClient.place_order()` remains disabled and raises a provider unavailable error. Paper Trading may create only local `paper`, `proposed`, or `blocked` rows; it must never create `sent`, `filled`, or `partial_filled` rows from Toss read-only checks.

The Desktop app must not call Toss directly. It only reads and writes through Supabase under RLS.
