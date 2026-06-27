# Security

Principles:

- Least privilege.
- Defense in depth.
- Fail closed.
- Explicit trust boundaries.
- No secrets in client.
- No direct order execution from UI.
- Full audit trail for dangerous changes.

Desktop/Tauri:

- No broker secrets.
- No Supabase secret key.
- Minimal Tauri capabilities.
- Strict CSP.
- No shell command exposure.
- Validate UI input with Zod.

Supabase:

- RLS on all exposed tables.
- Admin role via `user_roles`.
- No anon writes.
- Worker secret key server-side only.

OpenAI:

- No secrets, credentials, or unnecessary private account data.
- Validate structured output.
- Never route model output directly to execution.

