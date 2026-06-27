# API Gaps

| Provider | Gap | Status | Exact verification step |
| --- | --- | --- | --- |
| Toss | Auth token issue/refresh flow | unknown | Read official Toss OpenAPI auth section and create contract fixture before coding |
| Toss | account/position query schema | unknown | Download official OpenAPI schema from Toss docs and map typed response models |
| Toss | quote/current price query | unknown | Verify endpoint, rate limit, auth, and response freshness fields in official docs |
| Toss | candle/history query | unknown | Verify endpoint and pagination in official docs |
| Toss | order create/cancel/status | unknown | Verify official order endpoints, idempotency semantics, and unknown-status reconciliation before enabling code |
| KRX | market calendar endpoint | unknown | Verify KRX Open API market calendar product and response schema |
| KRX | listing/sector statistics | unknown | Verify KRX Open API approval/key requirements and schema |
| OpenDART | corp code mapping | unknown | Verify corp code file format and update cadence from OpenDART guide |
| OpenDART | account name mapping | unknown | Build fixture from official financial statement response and map Korean account names |
| Naver | rate limits and error schema | unknown | Verify Naver Search API current rate limit and error response docs |
| OpenAI | model/version choice for structured outputs | unknown | Verify current structured output model support before production use |
| Supabase | Free plan database limit | unknown | Confirm current pricing/limits before deploy; MVP enforces 500MB budget internally |

