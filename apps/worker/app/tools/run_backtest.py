from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import date

import anyio
import httpx

from app.adapters.persistence.backtest_repository import SupabaseBacktestRepository
from app.application.services.backtest_models import BacktestRequest, BacktestResult
from app.application.services.backtest_service import BacktestService
from app.config import load_settings


class CliUsageError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class CliArgs:
    strategy: str
    start: date
    end: date


async def _run(args: CliArgs) -> BacktestResult:
    settings = load_settings()
    repository = SupabaseBacktestRepository(settings)
    service = BacktestService(repository)
    try:
        return await service.run(
            BacktestRequest(strategy=args.strategy, start=args.start, end=args.end)
        )
    finally:
        await repository.aclose()


def _parse_args(argv: list[str]) -> CliArgs:
    if len(argv) != 7:
        raise CliUsageError(_usage())
    values = {argv[index]: argv[index + 1] for index in (1, 3, 5)}
    if set(values) != {"--strategy", "--start", "--end"}:
        raise CliUsageError(_usage())
    return CliArgs(
        strategy=values["--strategy"],
        start=_parse_date(values["--start"], "start"),
        end=_parse_date(values["--end"], "end"),
    )


def _parse_date(value: str, label: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise CliUsageError(f"{label} must use YYYY-MM-DD format") from exc


def _usage() -> str:
    return (
        "usage: python -m app.tools.run_backtest "
        "--strategy strategy_v1_weighted_factor --start YYYY-MM-DD --end YYYY-MM-DD"
    )


def format_result(result: BacktestResult) -> str:
    cagr = "n/a" if result.cagr is None else f"{result.cagr:.6f}"
    sharpe = "n/a" if result.sharpe_like is None else f"{result.sharpe_like:.6f}"
    win_rate = "n/a" if result.win_rate is None else f"{result.win_rate:.6f}"
    blocked = ", ".join(
        f"{key}={value}" for key, value in sorted(result.blocked_reason_counts.items())
    )
    return "\n".join(
        [
            "Backtest Result",
            f"strategy={result.strategy}",
            f"period={result.start.isoformat()}..{result.end.isoformat()}",
            f"total_return={result.total_return:.6f}",
            f"cagr={cagr}",
            f"max_drawdown={result.max_drawdown:.6f}",
            f"sharpe_like={sharpe}",
            f"win_rate={win_rate}",
            f"average_win={result.average_win:.2f}",
            f"average_loss={result.average_loss:.2f}",
            f"turnover={result.turnover:.6f}",
            f"number_of_trades={result.number_of_trades}",
            f"transaction_cost_krw={result.transaction_cost_krw}",
            "blocked_reason_counts=" + (blocked or "none"),
        ]
    )


def main() -> None:
    try:
        args = _parse_args(sys.argv)
    except CliUsageError as exc:
        print(str(exc))
        raise SystemExit(2) from exc
    try:
        result = anyio.run(_run, args)
    except ValueError as exc:
        print("FINAL=FAIL")
        print(str(exc))
        raise SystemExit(1) from exc
    except httpx.HTTPError as exc:
        print("FINAL=FAIL")
        print("Supabase backtest query failed; check server-side env, migrations, and cached data.")
        raise SystemExit(1) from exc
    print(format_result(result))
    raise SystemExit(0)


if __name__ == "__main__":
    main()
