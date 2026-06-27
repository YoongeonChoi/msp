# AI Upgrade Policy

Monthly flow:

1. Collect compact monthly research dataset.
2. Generate report.
3. Generate candidate strategy params through OpenAI structured output.
4. Save `ai_upgrade_candidates.status='proposed'`.
5. Run offline or mocked backtest.
6. Require user approval in Strategy Lab.
7. Promote to paper only.
8. Promote to live only after paper validation and rollback target.

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

