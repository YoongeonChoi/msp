# AI Upgrade Policy

Monthly flow:

1. Update paper outcomes with `python -m app.tools.update_outcomes_once`.
2. Collect compact monthly research dataset.
3. Redact secrets, account identifiers, credentials, and raw article bodies.
4. Generate report or candidate strategy params through OpenAI structured output.
5. Validate output with `MonthlyUpgradeCandidateSchema`.
6. Save `ai_upgrade_candidates.status='proposed'`.
7. Run offline or cached-data backtest.
8. Require user approval in Strategy Lab.
9. Promote to paper only.
10. Promote to live only after paper validation and rollback target.

Current one-off command:

```bash
cd apps/worker
python -m app.tools.generate_monthly_research --month YYYY-MM
```

When `MOCK_PROVIDERS=false`, `SUPABASE_URL`, `SUPABASE_SECRET_KEY`, and `OPENAI_API_KEY` are configured, the command reads monthly rows from Supabase and requests an OpenAI structured output. Otherwise it uses mock/in-memory adapters for local safety checks.

The command never creates orders, calls broker APIs, changes `strategy_versions.status`, changes `live_order_allowed`, or deploys a strategy. The older `generate_monthly_ai_candidate` command is kept as a compatibility wrapper over the same safe workflow.

Monthly research dataset contents:

- decision count by action
- order count by status
- outcome return summaries
- top winning and losing symbols
- risk block reason counts
- provider health summary
- news sentiment and event distribution
- strategy version performance summary
- latest `backtest_runs` summary when available
- DB/data quality warnings

Dataset exclusions:

- API keys and bearer tokens
- raw credentials
- full account identifiers
- raw full news article bodies
- unnecessary private account details

Outcome tracking is a separate database-only preparation step. It reads `decision_snapshots`, `features_daily`, `orders`, and `outcomes`, then upserts paper evaluation rows by `decision_id`. It does not send account numbers, API keys, raw credentials, or live order data to OpenAI.

Backtest requirements:

- out-of-sample validation
- walk-forward validation when enough data exists
- transaction costs
- slippage
- max drawdown
- turnover
- hit rate
- average win/loss
- daily loss limit simulation
- sector exposure
- liquidity filter
- comparison to previous strategy
- tried candidate count
- overfitting warning

Use the lightweight cached-data runner before paper promotion:

```bash
cd apps/worker
python -m app.tools.run_backtest --strategy strategy_v1_weighted_factor --start YYYY-MM-DD --end YYYY-MM-DD
```

`run_backtest` stores `backtest_runs` only. It does not create broker orders, does not deploy a strategy, and does not send account data to OpenAI.

Promotion gates:

- `ai_upgrade_candidates.status='proposed'` is a research artifact only.
- Strategy Lab approval may update the candidate to `approved_for_paper`; this is review state only.
- Approval must not deploy to live, create orders, call broker APIs, change `strategy_versions.status`, or change `live_order_allowed`.
- OpenAI output must never directly create or approve a `strategy_versions` row.
- Paper promotion requires a separate confirmation-gated workflow and must stay paper-first.
- Live promotion requires paper validation, rollback target, and manual release checklist.
- One-month performance alone is never sufficient evidence.

Desktop Strategy Lab:

- Uses only `VITE_SUPABASE_URL` and `VITE_SUPABASE_PUBLISHABLE_KEY`.
- Reads `strategy_versions`, `outcomes`, `orders`, `backtest_runs`, and `ai_upgrade_candidates` through Supabase RLS.
- Writes only safe review/update fields permitted by RLS: draft/proposed strategy JSON and AI candidate review status.
- Must not contain service_role/Supabase secret key or provider API keys.
- Must not call Toss, Naver, OpenDART, KRX, or OpenAI directly.

Future Batch API:

- `AIBatchPort` defines the application boundary for asynchronous OpenAI Batch jobs.
- Batch jobs must use the same sanitized dataset builder and schema validation.
- Batch completion may insert proposed candidates only after validation.
