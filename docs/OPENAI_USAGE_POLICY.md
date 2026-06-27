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

## Monthly Research Candidate Generation

Worker command:

```bash
cd apps/worker
python -m app.tools.generate_monthly_research --month YYYY-MM
```

The command builds a compact monthly research dataset from:

- `decision_snapshots`
- `outcomes`
- `orders`
- `news_events` summaries only
- `features_daily` aggregates
- `api_health` summary
- `backtest_runs` summary when the table exists

The dataset is sanitized before it is sent to OpenAI:

- API keys, tokens, authorization headers, credentials, and passwords are redacted.
- Account identifiers and account-number-like strings are redacted.
- Full news article bodies and raw content fields are not included.
- Order/account details are aggregated where possible.
- No Toss, Supabase, Naver, KRX, OpenDART, or OpenAI secrets are sent.

The payload contains compact aggregates only:

- decision counts by action
- order counts by status
- outcome return summaries for 1/5/20 day horizons
- top winning and losing symbols by 20-day return
- risk block reason counts
- provider health summary
- news sentiment and event distributions
- strategy version performance summary
- latest backtest summary
- DB/data quality warnings

OpenAI may return only the structured monthly strategy candidate schema. The result is stored in `ai_upgrade_candidates` with `status='proposed'` and `approval_required=true`.

OpenAI must never:

- call broker APIs
- create live orders
- change `strategy_versions.status`
- approve a candidate
- promote a candidate to paper or live
- change `live_order_allowed`
- bypass `RiskService`

`python -m app.tools.generate_monthly_ai_candidate --month YYYY-MM` remains as a compatibility command and uses the same safe service path.

## Structured Output Schema

The worker validates monthly candidates with `MonthlyUpgradeCandidateSchema`:

```json
{
  "base_strategy_version": "string",
  "candidate_name": "string",
  "candidate_weights": {
    "technical": 0.3,
    "fundamental": 0.25,
    "market_sector": 0.15,
    "news_event": 0.2,
    "portfolio": 0.1
  },
  "candidate_params": {
    "buy_threshold": 0.7
  },
  "rationale": "string",
  "expected_improvement": "string",
  "risk_notes": "string",
  "required_backtests": ["out_of_sample", "walk_forward"],
  "approval_required": true
}
```

Invalid JSON, missing fields, extra fields, or `approval_required=false` are rejected and do not create a candidate row.

## Batch API Interface

MVP uses the synchronous OpenAI structured output path. Future offline monthly processing can use `AIBatchPort` in `apps/worker/app/application/ports/ai_batch_port.py`.

Batch support must preserve the same rules:

- submit only sanitized monthly datasets
- store output as `ai_upgrade_candidates.status='proposed'`
- require schema validation after batch completion
- never auto-approve or deploy
- never include secrets or raw credentials in batch input files

Official docs to consult before implementing a concrete adapter:

- OpenAI Structured Outputs: https://platform.openai.com/docs/guides/structured-outputs
- OpenAI Batch API: https://platform.openai.com/docs/guides/batch
