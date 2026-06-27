# Incident Response

Key leak:

- Disable bot and live permission.
- Rotate key at provider.
- Revoke old Render env var.
- Audit logs for suspicious use.

Bad deploy:

- Disable live first.
- Roll back Render deploy.
- Verify heartbeat and paper mode.

Wrong order:

- Stop bot.
- Record incident.
- Reconcile broker state manually.
- Keep all audit logs.

Provider outage:

- Confirm provider health.
- Do not manually bypass risk gates.
- Resume paper before any live consideration.

Suspicious OpenAI output:

- Reject candidate/classification.
- Preserve compact output for review.
- Add fixture regression test.

