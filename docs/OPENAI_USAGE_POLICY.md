# OpenAI Usage Policy

Allowed:

- Korean news/disclosure classification.
- Disclosure/news summaries.
- Monthly performance report.
- Strategy weight/parameter candidate generation.
- Backtest hypothesis generation.

Forbidden:

- Direct broker order execution.
- Direct live strategy deployment.
- Risk engine bypass.
- Automatic code modification for live strategy.
- Receiving API secrets, credentials, account numbers, or unnecessary private data.

Structured outputs must validate against `openai_schemas.py`. Invalid or low-confidence output is rejected or downgraded to manual review.

