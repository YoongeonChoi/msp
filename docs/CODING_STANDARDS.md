# Coding Standards

Python:

- Python 3.12+.
- Pydantic v2 for external schemas.
- Domain imports no provider SDKs.
- `ruff` and tests required.
- Fail closed on unknown errors.

TypeScript:

- Strict TypeScript.
- Korean UI text.
- No broker secrets in desktop.
- Zod for user input validation.

SQL:

- RLS on all public tables.
- Typed columns for queryable fields.
- JSONB only for snapshots or extensible params.
- No destructive migration without explicit review.

Logging:

- JSON logs.
- Redact key/secret/token/password fields.
- Include correlation IDs where available.

