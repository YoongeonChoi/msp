from __future__ import annotations

import argparse
import json

from msp.adapters.paper import PaperBrokerAdapter
from msp.db import connect, init_db, transaction
from msp.services.audit import append_audit
from msp.services.data_platform import seed_demo_market_data
from msp.services.orders import list_order_intents
from msp.services.portfolio import generate_rebalance
from msp.services.research import compute_demo_scores, list_scores
from msp.services.system_state import get_system_state
from msp.settings import load_settings
from msp.workers.execution import run_once as run_execution_once
from msp.workers.reconcile import run_once as run_reconcile_once


def _print(data: dict | list[dict]) -> None:
    print(json.dumps(data, indent=2, sort_keys=True))


def cmd_init_db(_: argparse.Namespace) -> None:
    settings = load_settings()
    init_db(settings)
    _print({"ok": True, "db_path": str(settings.db_path)})


def cmd_status(_: argparse.Namespace) -> None:
    settings = load_settings()
    init_db(settings)
    with connect(settings) as conn:
        broker = PaperBrokerAdapter(conn)
        _print(
            {
                "system": get_system_state(conn),
                "cash": broker.list_cash(settings.account_id),
                "positions": broker.list_positions(settings.account_id),
                "orders": list_order_intents(conn, limit=20),
            }
        )


def cmd_seed_cash(args: argparse.Namespace) -> None:
    settings = load_settings()
    init_db(settings)
    with connect(settings) as conn:
        with transaction(conn):
            broker = PaperBrokerAdapter(conn)
            broker.seed_cash(settings.account_id, args.currency.upper(), args.amount)
            append_audit(
                conn,
                "paper_cash.seeded",
                "cli",
                {"account_id": settings.account_id, "currency": args.currency.upper(), "amount": args.amount},
            )
    _print({"ok": True, "currency": args.currency.upper(), "amount": args.amount})


def cmd_run_execution_once(_: argparse.Namespace) -> None:
    _print(run_execution_once())


def cmd_run_reconcile_once(_: argparse.Namespace) -> None:
    _print(run_reconcile_once())


def cmd_run_data_once(_: argparse.Namespace) -> None:
    settings = load_settings()
    init_db(settings)
    with connect(settings) as conn:
        with transaction(conn):
            _print(seed_demo_market_data(conn, actor="cli"))


def cmd_run_research_once(_: argparse.Namespace) -> None:
    settings = load_settings()
    init_db(settings)
    with connect(settings) as conn:
        with transaction(conn):
            _print(compute_demo_scores(conn, actor="cli"))


def cmd_run_portfolio_once(args: argparse.Namespace) -> None:
    settings = load_settings()
    init_db(settings)
    with connect(settings) as conn:
        with transaction(conn):
            _print(
                generate_rebalance(
                    conn,
                    account_id=settings.account_id,
                    top_n=args.top_n,
                    gross_exposure=args.gross_exposure,
                    actor="cli",
                )
            )


def cmd_scores(_: argparse.Namespace) -> None:
    settings = load_settings()
    init_db(settings)
    with connect(settings) as conn:
        _print(list_scores(conn))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="msp")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init-db")
    init_parser.set_defaults(func=cmd_init_db)

    status_parser = subparsers.add_parser("status")
    status_parser.set_defaults(func=cmd_status)

    seed_parser = subparsers.add_parser("seed-cash")
    seed_parser.add_argument("--amount", type=float, required=True)
    seed_parser.add_argument("--currency", default="KRW")
    seed_parser.set_defaults(func=cmd_seed_cash)

    execution_parser = subparsers.add_parser("run-execution-once")
    execution_parser.set_defaults(func=cmd_run_execution_once)

    reconcile_parser = subparsers.add_parser("run-reconcile-once")
    reconcile_parser.set_defaults(func=cmd_run_reconcile_once)

    data_parser = subparsers.add_parser("run-data-once")
    data_parser.set_defaults(func=cmd_run_data_once)

    research_parser = subparsers.add_parser("run-research-once")
    research_parser.set_defaults(func=cmd_run_research_once)

    portfolio_parser = subparsers.add_parser("run-portfolio-once")
    portfolio_parser.add_argument("--top-n", type=int, default=2)
    portfolio_parser.add_argument("--gross-exposure", type=float, default=0.5)
    portfolio_parser.set_defaults(func=cmd_run_portfolio_once)

    scores_parser = subparsers.add_parser("scores")
    scores_parser.set_defaults(func=cmd_scores)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
