# Toss Securities Integration Notes

Source checked during implementation:

- `https://developers.tossinvest.com/docs`
- `https://developers.tossinvest.com/llms.txt`
- `https://openapi.tossinvest.com/openapi-docs/overview.md`
- `https://openapi.tossinvest.com/openapi-docs/latest/openapi.json`

The official `llms.txt` points coding agents to the OpenAPI JSON as the source
of truth. The current fetched OpenAPI document reports version `1.1.1`.

## Auth

Use OAuth 2.0 Client Credentials:

- `POST /oauth2/token`
- content type `application/x-www-form-urlencoded`
- fields: `grant_type=client_credentials`, `client_id`, `client_secret`

Every non-auth request uses:

- `Authorization: Bearer {access_token}`

Account, asset, and order calls also require:

- `X-Tossinvest-Account: {accountSeq}`

## Orders

MSP currently maps only quantity-based limit orders:

```json
{
  "clientOrderId": "msp_...",
  "symbol": "005930",
  "side": "BUY",
  "orderType": "LIMIT",
  "timeInForce": "DAY",
  "quantity": "10",
  "price": "70000",
  "confirmHighValueOrder": false
}
```

Important constraints from the OpenAPI schema:

- `clientOrderId` is optional in Toss but mandatory in MSP execution.
- Toss treats `clientOrderId` as an idempotency key for a limited window.
- Quantity-based orders require whole-share quantity.
- `LIMIT` orders require `price`; `MARKET` orders must not include `price`.
- Orders at or above KRW 100,000,000 need `confirmHighValueOrder=true`.

## Error Handling

MSP maps ambiguous create-order outcomes to `UNKNOWN`:

- network timeout or uncertain connection failure
- 409 `request-in-progress`
- 429 rate limit
- 500 class server errors

`UNKNOWN` must be resolved by order lookup/reconciliation before retry. This is
the core protection against duplicate orders after a timeout.

## Local Safety

Toss execution is disabled unless all gates are open:

- `MSP_BROKER=toss`
- `MSP_ENABLE_REAL_BROKER=true`
- `MSP_ALLOW_LIVE_MODE=true`
- `TOSSINVEST_CLIENT_ID`
- `TOSSINVEST_CLIENT_SECRET`
- `TOSSINVEST_ACCOUNT_SEQ`
- system mode is `LIVE`

In `PAPER`, `SHADOW`, and `ARMED`, execution still uses PaperBroker. This keeps
the Windows local setup safe while the real broker adapter is being tested.
