# Security

Principles:

- Least privilege.
- Defense in depth.
- Fail closed.
- Explicit trust boundaries.
- No secrets in client.
- No direct order execution from UI.
- Full audit trail for dangerous changes.

## CI Security

GitHub Actions default permissions must stay minimal:

```yaml
permissions:
  contents: read
```

Allowed job-level exceptions:

- CodeQL may use `security-events: write`.
- Dependency Review may use `pull-requests: read`.

Disallowed:

- `pull_request_target` without a written security rationale.
- Production secrets in PR workflows.
- Printing secrets or environment dumps.
- Automatic Render deploys.
- Workflow steps that enable live trading.

Security workflow coverage:

- CodeQL for Python and TypeScript.
- Dependency Review on pull requests.
- Gitleaks-compatible secret scanning.
- Built-in common secret pattern scanning for OpenAI, GitHub, Slack, AWS-style keys, private keys, and configured provider secret assignments.
- Committed `.env` blocking, with `.env.example` as the only allowed env-shaped file.
- `npm audit` advisory checks.
- `pip-audit` advisory checks.
- `bandit` advisory checks.
- Workflow policy guard for unsafe patterns.

Secret scans must print only file paths, not matched secret values. CI must fail if workflow files reference production trading/API secrets such as `SUPABASE_SECRET_KEY`, `TOSS_CLIENT_SECRET`, `OPENAI_API_KEY`, `NAVER_CLIENT_SECRET`, `KRX_API_KEY`, or `OPENDART_API_KEY`.

## Secrets

- `.env` files stay ignored.
- `.env`, `.env.local`, and any non-example env file must not be tracked by Git.
- Render owns Worker secrets.
- Desktop may use only `VITE_SUPABASE_URL` and `VITE_SUPABASE_PUBLISHABLE_KEY`.
- Supabase secret/service role key is Worker-only.
- Toss, Naver, OpenAI, KRX, and OpenDART keys are never stored in Git or Desktop.
- Logs must redact keys, tokens, credentials, authorization headers, and account identifiers where feasible.

## Desktop/Tauri

- No broker secrets.
- No Supabase secret key.
- Minimal Tauri capabilities.
- Strict CSP.
- No shell command exposure.
- Validate UI input before writing to Supabase.
- Desktop writes only through Supabase RLS.

## Supabase

- RLS on all exposed tables.
- Admin role via `user_roles`.
- No anon writes.
- No public write policies.
- Worker secret key server-side only.
- Migration PRs must preserve RLS and include rollback notes for destructive changes.

## OpenAI

- No secrets, credentials, or unnecessary private account data.
- Validate structured output.
- Never route model output directly to execution.
- Monthly candidates are stored only as `ai_upgrade_candidates.status='proposed'`.

## Trading Safety Boundary

CI and security tooling do not authorize live trading. Live remains blocked unless runtime settings and risk gates explicitly allow it later:

- `bot_settings.enabled=true`
- `mode='live'`
- `live_order_allowed=true`
- market/quote/account/provider/risk/idempotency gates pass

Default and release states must keep `live_order_allowed=false`.
