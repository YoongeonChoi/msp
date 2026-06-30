# API Connections

Official documentation consulted or linked for implementation verification:

| Area | Official source | MVP decision |
| --- | --- | --- |
| Render Background Workers | https://render.com/docs/background-workers | Use worker with no inbound HTTP dependency |
| Render Blueprint YAML | https://render.com/docs/blueprint-spec | `render.yaml` uses worker service and `autoDeployTrigger: "off"` |
| Render env vars/secrets | https://render.com/docs/configure-environment-variables | secrets use `sync: false` |
| Supabase API keys | https://supabase.com/docs/guides/api/api-keys | desktop publishable key only, worker secret key only |
| Supabase RLS | https://supabase.com/docs/guides/database/postgres/row-level-security | RLS enabled on all exposed tables |
| Supabase Realtime Postgres Changes | https://supabase.com/docs/guides/realtime/postgres-changes | Realtime only for lightweight control/status tables |
| Supabase Free constraints | https://supabase.com/pricing | official Free DB space is 500MB; worker warns at 450MB |
| GitHub Actions hardening | https://docs.github.com/en/actions/security-guides/security-hardening-for-github-actions | least permissions and no auto live deploy |
| GitHub CodeQL | https://docs.github.com/en/code-security/code-scanning/introduction-to-code-scanning/about-code-scanning-with-codeql | `security.yml` adds CodeQL |
| GitHub Dependabot | https://docs.github.com/en/code-security/dependabot | Dependabot enabled |
| OWASP ASVS | https://owasp.org/www-project-application-security-verification-standard/ | security checklist reference |
| OWASP Logging Cheat Sheet | https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html | JSON logs and redaction |
| Tauri Security | https://v2.tauri.app/security/ | minimal capabilities and CSP |
| WCAG 2.2 | https://www.w3.org/TR/WCAG22/ | AA-oriented cockpit UI |
| Naver Search News API | https://developers.naver.com/docs/serviceapi/search/news/news.md | read-only Search News adapter uses official headers, params, response fields, and error envelope mapping |
| OpenDART | https://opendart.fss.or.kr/guide/main.do | read-only corp code ZIP parser and financial statement account-name mapping for partial fundamentals |
| KRX Open API | https://openapi.krx.co.kr/contents/OPP/MAIN/main/index.cmd | not used in live/non-mock market data; direct `KrxClient` remains fail-closed until exact endpoint/schema verification |
| OpenAI Structured Outputs | https://platform.openai.com/docs/guides/structured-outputs | Responses API JSON schema output is guarded by a verified model allowlist |
| OpenAI Batch API | https://platform.openai.com/docs/guides/batch | reserved for monthly offline jobs |
| OpenAI data controls | https://platform.openai.com/docs/guides/your-data | no secrets and data minimization |
| Toss Securities Open API | https://developers.tossinvest.com/docs | read adapter plus guarded worker-only live order create/cancel and status reconciliation; modify disabled |
| Toss Securities llms index | https://developers.tossinvest.com/llms.txt | official index for Markdown docs and canonical OpenAPI JSON |
| Toss Securities OpenAPI JSON | https://openapi.tossinvest.com/openapi-docs/latest/openapi.json | verified `oauth2/token`, `accounts`, `buying-power`, `holdings`, `prices`, `candles`, `market-calendar/KR`, order status schemas, guarded `POST /api/v1/orders`, and guarded `POST /api/v1/orders/{orderId}/cancel` schema |

No provider-specific endpoint, parameter, auth flow, rate limit, or response schema is implemented unless represented by a typed placeholder, mock adapter, and `API_GAPS.md` entry.

## Toss Securities Contract

Verified from the official Toss Securities OpenAPI document:

- Base URL: `https://openapi.tossinvest.com`
- Auth: `POST /oauth2/token` with OAuth2 Client Credentials Grant, `application/x-www-form-urlencoded`, fields `grant_type=client_credentials`, `client_id`, `client_secret`
- API auth header: `Authorization: Bearer {access_token}`
- Account scoped read APIs use `X-Tossinvest-Account`; the value is the `accountSeq` returned by `GET /api/v1/accounts`. When `TOSS_ACCOUNT_ID` is not set, the worker infers this header only if `GET /api/v1/accounts` returns exactly one account; zero or multiple accounts fail closed.
- Read-only endpoints implemented in worker adapter:
  - `GET /api/v1/accounts`
  - `GET /api/v1/buying-power`
  - `GET /api/v1/holdings`
  - `GET /api/v1/prices`
  - `GET /api/v1/candles`
  - `GET /api/v1/market-calendar/KR`
  - `GET /api/v1/orders`
  - `GET /api/v1/orders/{orderId}`

Scheduled live cycles now derive cash buying power and holdings value from Toss read-only endpoints, and derive market-open state from Toss KR regular-session calendar. `GET /api/v1/orders status=CLOSED` remains documented as `400 closed-not-supported`, so broker-wide or externally placed daily order history is still not verified through Toss. The worker verifies only system-created live order count from local `orders` rows for the current KST trading day (`mode='live'` and `status <> 'blocked'`), and only after the operator explicitly accepts that system-originated scope with `LIVE_SYSTEM_ORDER_COUNT_SCOPE_ACCEPTED=true`. Without runtime acceptance, or if the repository count cannot be read, live risk fails closed with `daily_order_count_unverified` before any broker call. For live readiness, that runtime flag is not sufficient by itself: the release evidence bundle must also include retained `system_order_scope_evidence.json` proving the exact scope, Toss limitation, deployment environment, operator, `LIVE_SYSTEM_ORDER_COUNT_SCOPE_ACCEPTED=true` confirmation, evidence URI, and SHA-256 hash, and missing or weak retained evidence blocks bundle creation. The final bundle also rejects environment mixing: `staging` requires `deployment_environment=staging`, and `production-readiness` requires `deployment_environment=production`.

Implemented live write and reconciliation scope:

- `POST /api/v1/orders`: worker-only, quantity-based KRX `LIMIT` order payload, `clientOrderId` idempotency key, `X-Tossinvest-Account` header
- `POST /api/v1/orders/{orderId}/cancel`: worker-only manual cancellation for one local open live order. It must be invoked by local `orders.id`, never by a free-form provider ID. The response `orderId` is treated as the cancel operation id, not final cancellation proof; the worker then reads the original order with `GET /api/v1/orders/{orderId}` and marks local `canceled` only after confirmed `CANCELED`. Timeout, unknown provider result, or a non-`CANCELED` confirmation marks the order `unknown_requires_manual_check`.
- `GET /api/v1/orders/{orderId}`: worker reconciliation maps confirmed `FILLED`, `PARTIAL_FILLED`, `CANCELED`, and `REJECTED` statuses back to local `orders`; unknown status codes require manual review

Not implemented:

- order modify

`TOSS_ACCOUNT_ID` is kept for env compatibility and for multi-account deployments. When set, it must contain the server-side `accountSeq`, not a raw account number. When omitted, the worker may infer a single returned account; ambiguous account lists fail closed. Never put Toss credentials in Desktop or Git.
