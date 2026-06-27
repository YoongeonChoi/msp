# CI/CD

Workflows:

- `ci.yml`: worker lint/test/type check, desktop lint/typecheck/build, docs and migration checks.
- `security.yml`: CodeQL, dependency review, secret scan, npm audit, pip-audit, bandit, cargo audit where available.
- `migration-check.yml`: migration order, RLS presence, no unsafe public table omission.

Deployment is manual. Render auto deploy is off. Live trading must be disabled before deployment.

