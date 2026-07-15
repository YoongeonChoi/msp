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
| Toss Securities Open API | https://developers.tossinvest.com/docs | real-provider integration is read-only; every production order write is disabled |
| Toss Securities llms index | https://developers.tossinvest.com/llms.txt | official index for Markdown docs and canonical OpenAPI JSON |
| Toss Securities OpenAPI JSON | https://openapi.tossinvest.com/openapi-docs/latest/openapi.json | retained contract artifact for read contracts and local create/status/cancel qualification; production write paths remain quarantined |

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
- Production order URLs, including `GET /api/v1/orders` and
  `GET /api/v1/orders/{orderId}`, are contract evidence only and are not called by
  this release.

The G1+G2 release boundary is intentionally stricter than the provider contract. The
worker may use the read-only endpoints above for observation, but no data returned by
them authorizes a write. Production create, cancel, and modify operations are
quarantined even though their schemas exist in the official OpenAPI document.

Order lifecycle qualification is local only:

- Environment name is `contract_test`; it is never presented as an official sandbox.
- The simulator implements deterministic create/status/cancel and explicit fault
  injection without network access.
- The registered OpenAPI URL, retrieval time, and SHA-256 identify the contract being
  qualified. A hash mismatch blocks qualification.
- A test asserting zero requests to the production order host is a release gate.
- Production order endpoint or order-capable credential detection fails worker startup.

The public Toss source of truth currently identifies the single API server
`https://openapi.tossinvest.com`. Before any external write adapter is considered, the
provider must supply and the release manager must retain a separate sandbox contract,
credential scope, account-isolation guarantee, and artifact hash. Even then, enabling
external writes requires a new G0 business/regulatory decision and explicit user
approval; it is not an extension of the current release.

`TOSS_ACCOUNT_ID` is kept for read-only compatibility. When set, it must contain the
server-side `accountSeq`, not a raw account number. When omitted, the worker may infer
a single returned account; ambiguous account lists fail closed. Toss credentials are
worker-only and must never appear in Desktop, Git, audit payloads, or alert outbox rows.
