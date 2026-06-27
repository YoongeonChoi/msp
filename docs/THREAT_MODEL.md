# Threat Model

Assets:

- Toss API credentials
- Supabase secret key
- OpenAI key
- account/position data
- order execution path
- strategy parameters
- audit logs
- local desktop session

Trust boundaries:

- Desktop to Supabase Auth/RLS
- Worker to Supabase secret-key channel
- Worker to external providers
- OpenAI input/output boundary
- GitHub Actions to deployment credentials

| Threat | STRIDE | Mitigation | Residual risk |
| --- | --- | --- | --- |
| Leaked API key | Information disclosure | `.env` ignored, Render env vars, redaction | Local machine compromise |
| Compromised Actions | Elevation | least permissions, CodeQL, dependency review | Third-party action compromise |
| Malicious dependency | Tampering | Dependabot, audits, lock review | transitive zero-day |
| Prompt injection | Tampering | sanitize inputs, schema validation, no execution authority | bad classification |
| RLS misconfig | Elevation | migration check, all public tables RLS | policy bug |
| Duplicate order retry | Tampering | idempotency key, no blind retry | broker unknown status |
| Stale quote | Integrity | quote freshness policy | provider clock skew |
| Worker restart mid-order | Reliability | persist before broker call, unknown status state | manual reconciliation needed |
| DB full | Availability | 500MB budget, retention | writes may fail |
| Accidental live enable | Safety | typed confirmation, defaults false | user override |

