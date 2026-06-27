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

MVP status: Toss live order endpoint is not implemented.

