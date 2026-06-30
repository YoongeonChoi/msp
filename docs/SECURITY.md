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

Secret scans must print only file paths, not matched secret values. CI must fail if workflow files reference production trading/API secrets such as `SUPABASE_SECRET_KEY`, `TOSS_CLIENT_SECRET`, `OPENAI_API_KEY`, `NAVER_CLIENT_SECRET`, `KRX_API_KEY`, `OPENDART_API_KEY`, or `ALERT_WEBHOOK_URL`.

## Secrets

- `.env` files stay ignored.
- `.env`, `.env.local`, and any non-example env file must not be tracked by Git.
- Render owns Worker secrets.
- Desktop may use only `VITE_SUPABASE_URL` and `VITE_SUPABASE_PUBLISHABLE_KEY`.
- `supabase/verify_hosted_live_readiness.py` may read `SUPABASE_SECRET_KEY` from a
  worker/operator shell, but it must not print secret values. It is not a desktop tool.
- Hosted Supabase verifier scripts must reject anything other than an official
  `https://<project_ref>.supabase.co` project origin, plus URL credentials, path,
  query, and fragment values, before making network calls.
- Hosted live-enable verifier failures must redact configured publishable/secret keys
  and requester/reviewer JWTs.
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
- Live enable requests require non-empty provider contract, risk report, release evidence, and a 5-240 minute expiry.
- Accepted/rejected live enable rows are immutable at the database trigger layer; approval evidence cannot be edited after review.
- Live control RLS policies use cached `select public.is_admin()` checks for the hottest control tables.
- Live order placement must not treat mock/static or unready strategy features
  as live-ready evidence. Live mode feature snapshots use `provider_live_v1`
  with retained quote, fundamentals, news, positive PER/PBR valuation inputs,
  and market/sector provenance; broker placement requires
  `feature_snapshot.raw.live_trading_ready=true` and a non-mock `feature_source`
  after final risk evaluation.
- Release evidence artifact/report URIs must be retained remote HTTPS references with a valid DNS host or global IP; local, test, private-IP, non-global-IP, localhost, invalid-host, invalid-port, credential-bearing, query/fragment, raw or percent-decoded path traversal, encoded slash/backslash path separators, and ad hoc inline evidence are not accepted.
- Published provider lifecycle artifacts must pass `--verify-remote-artifacts`, which fetches retained HTTPS artifact bytes, rejects GitHub `blob` pages, caps response size, and compares the downloaded bytes to the declared SHA-256 without leaking URI, body, or hash values in failures.
- Provider contract gap release evidence must include a retained `provider_gap_evidence` manifest binding the exact `docs/API_GAPS.md` SHA-256, every parsed provider gap id in order, provider-matching retained source artifacts, unique retained URI/SHA-256 pairs, and non-future capture timestamps; final bundle verification requires `provider_gap_evidence=1`.
- Published incident-channel proof must pass `--verify-remote-channel-evidence`, and published system-order-scope proof must pass `--verify-remote-evidence`; both fetch retained HTTPS evidence bytes, reject GitHub `blob` pages, cap response size, and compare downloaded bytes to the declared SHA-256 without leaking URI, body, or hash values in failures.
- Final bundle release evidence must show `remote_provider_artifacts=1`, `remote_incident_evidence=1`, and `remote_system_order_scope_evidence=1`; any `0` remote flag means the bundle was only locally validated and is not post-publication release evidence.
- Retained security report `report_path` values must be relative paths under the security summary directory; absolute, drive-qualified, and `..`-escaping paths are not accepted.
- Provider lifecycle evidence must not show provider or local order status regression after terminal status has been observed.
- Incident evidence `channel_name` values must be logical identifiers, not webhook URLs, paths, raw payloads, or retained artifact endpoints.
- Security scan `scan_id` values must be lowercase logical identifiers, not URLs, filesystem paths, drive-qualified paths, email/contact values, or retained artifact references.
- Release evidence operator identities such as `operator_ack_by`, `accepted_by`, provider `operator_reviewed_by`, and audit `reviewed_by` must be internal logical handles, not email addresses, URLs, paths, raw contact values, or retained artifact references. Provider unknown-recovery review and repository audit review must use distinct normalized logical handles; unknown-recovery review must be recorded after provider status evidence, and repository audit review must be recorded after the unknown-recovery operator review.
- Release evidence timestamps must not be in the future beyond five minutes of clock skew.
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
- a non-expired `request_live_enable` manual command accepted by an authenticated admin different from the requester
- unchanged live approval evidence captured before review
- market/quote/account/provider/risk/idempotency gates pass

The accepted live-enable command is one-time evidence. The database trigger consumes the selected row by
moving it to `applied` when `live_order_allowed` changes from false to true, so re-enabling live after an
Emergency Stop requires a new request and a different reviewer.

Default and release states must keep `live_order_allowed=false`.
