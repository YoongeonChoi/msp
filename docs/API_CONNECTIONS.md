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
| Supabase Free constraints | https://supabase.com/pricing | assume 500MB hard DB budget, verify current limits before deploy |
| GitHub Actions hardening | https://docs.github.com/en/actions/security-guides/security-hardening-for-github-actions | least permissions and no auto live deploy |
| GitHub CodeQL | https://docs.github.com/en/code-security/code-scanning/introduction-to-code-scanning/about-code-scanning-with-codeql | `security.yml` adds CodeQL |
| GitHub Dependabot | https://docs.github.com/en/code-security/dependabot | Dependabot enabled |
| OWASP ASVS | https://owasp.org/www-project-application-security-verification-standard/ | security checklist reference |
| OWASP Logging Cheat Sheet | https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html | JSON logs and redaction |
| Tauri Security | https://v2.tauri.app/security/ | minimal capabilities and CSP |
| WCAG 2.2 | https://www.w3.org/TR/WCAG22/ | AA-oriented cockpit UI |
| Naver Search News API | https://developers.naver.com/docs/serviceapi/search/news/news.md | adapter placeholder and mock |
| OpenDART | https://opendart.fss.or.kr/guide/main.do | adapter placeholder and mapping gaps |
| KRX Open API | https://openapi.krx.co.kr/contents/OPP/MAIN/main/index.cmd | adapter placeholder and mock |
| OpenAI Structured Outputs | https://platform.openai.com/docs/guides/structured-outputs | Pydantic schema mirrors expected output |
| OpenAI Batch API | https://platform.openai.com/docs/guides/batch | reserved for monthly offline jobs |
| OpenAI data controls | https://platform.openai.com/docs/guides/your-data | no secrets and data minimization |
| Toss Securities Open API | https://developers.tossinvest.com/docs | live order implementation blocked until endpoint contract verified |
| Toss Securities llms index | https://developers.tossinvest.com/llms.txt | points to official OpenAPI docs for future contract work |

No provider-specific endpoint, parameter, auth flow, rate limit, or response schema is implemented unless represented by a typed placeholder, mock adapter, and `API_GAPS.md` entry.

