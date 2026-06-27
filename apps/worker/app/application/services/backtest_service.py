from __future__ import annotations

from app.application.ports.backtest_port import BacktestRepositoryPort
from app.application.services.backtest_models import BacktestRequest, BacktestResult
from app.application.services.backtest_parsing import parse_backtest_rows
from app.application.services.backtest_simulator import simulate_backtest


class BacktestService:
    def __init__(self, repository: BacktestRepositoryPort) -> None:
        self.repository = repository

    async def run(self, request: BacktestRequest) -> BacktestResult:
        rows = await self.repository.load_backtest_rows(
            request.strategy, request.start, request.end
        )
        parsed = parse_backtest_rows(rows, request.strategy)
        if parsed.strategy is None:
            result = BacktestResult(
                strategy=request.strategy,
                start=request.start,
                end=request.end,
                total_return=0.0,
                cagr=None,
                max_drawdown=0.0,
                sharpe_like=None,
                win_rate=None,
                average_win=0.0,
                average_loss=0.0,
                turnover=0.0,
                number_of_trades=0,
                transaction_cost_krw=0,
                blocked_reason_counts={"missing_strategy": 1},
            )
        else:
            result = simulate_backtest(parsed.strategy, parsed.features_by_date)
        await self.repository.save_backtest_result(result.to_row())
        return result
