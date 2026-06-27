# API Gaps

| Provider | Gap | Status | Exact verification step |
| --- | --- | --- | --- |
| Toss | Auth token issue/refresh flow | verified-readonly-implemented | Official OpenAPI JSON confirms `POST /oauth2/token`, OAuth2 Client Credentials Grant, form fields `grant_type`, `client_id`, `client_secret`, and no refresh token |
| Toss | account/position query schema | verified-readonly-implemented | Official OpenAPI JSON confirms `GET /api/v1/accounts`, `GET /api/v1/holdings`, and `X-Tossinvest-Account=accountSeq` |
| Toss | quote/current price query | verified-readonly-implemented | Official OpenAPI JSON confirms `GET /api/v1/prices?symbols=...` result fields `symbol`, `timestamp`, `lastPrice`, `currency` |
| Toss | candle/history query | verified-readonly-implemented | Official OpenAPI JSON confirms `GET /api/v1/candles` params `symbol`, `interval`, `count`, `before`, `adjusted` and `CandlePageResponse` |
| Toss | order status read | verified-readonly-implemented | Official OpenAPI JSON confirms `GET /api/v1/orders` and `GET /api/v1/orders/{orderId}` as read-only order history/status APIs |
| Toss | order create/cancel/modify execution | disabled-not-implemented | Do not implement until a separate live-trading task verifies idempotency, final risk sequencing, contract fixtures, rollback, and manual live safety approval |
| KRX | market calendar endpoint | unknown | Verify KRX Open API market calendar product and response schema |
| KRX | listing/sector statistics | unknown | Verify KRX Open API approval/key requirements and schema |
| OpenDART | corp code mapping | unknown | Verify corp code file format and update cadence from OpenDART guide |
| OpenDART | account name mapping | unknown | Build fixture from official financial statement response and map Korean account names |
| Naver | rate limits and error schema | unknown | Verify Naver Search API current rate limit and error response docs |
| OpenAI | model/version choice for structured outputs | unknown | Verify current structured output model support before production use |
| Supabase | Free plan database limit | unknown | Confirm current pricing/limits before deploy; MVP enforces 500MB budget internally |
