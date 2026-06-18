from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from msp.db import connect, init_db, transaction
from msp.domain.enums import SystemMode
from msp.exceptions import SafetyError
from msp.services.data_platform import seed_demo_market_data
from msp.services.orders import list_order_intents
from msp.services.portfolio import approve_rebalance, generate_rebalance
from msp.services.research import compute_demo_scores, list_scores
from msp.services.system_state import command_arm, get_system_state
from msp.settings import Settings
from msp.workers.execution import run_once as run_execution_once


def make_settings(db_path: Path) -> Settings:
    return Settings(
        app_name="MSP Test",
        environment="test",
        db_path=db_path,
        account_id="paper-test",
        allow_live_mode=False,
        initial_cash_krw=1_000_000,
        worker_interval_seconds=0.1,
        lease_ttl_seconds=10,
    )


class RebalancePipelineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.settings = make_settings(Path(self.temp_dir.name) / "msp-test.sqlite3")
        init_db(self.settings)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_demo_research_rebalance_approval_creates_executable_orders(self) -> None:
        with connect(self.settings) as conn:
            with transaction(conn):
                state = get_system_state(conn)
                command_arm(
                    conn,
                    settings=self.settings,
                    target_mode=SystemMode.PAPER,
                    reason="test arm",
                    expected_state_version=state["state_version"],
                    idempotency_key="pipeline-arm",
                    actor="test",
                )
                seed_result = seed_demo_market_data(conn, actor="test")
                score_result = compute_demo_scores(conn, actor="test")
                scores = list_scores(conn)
                rebalance = generate_rebalance(
                    conn,
                    account_id=self.settings.account_id,
                    top_n=2,
                    gross_exposure=0.5,
                    actor="test",
                )
                approved = approve_rebalance(
                    conn,
                    settings=self.settings,
                    rebalance_id=rebalance["id"],
                    portfolio_hash=rebalance["portfolio_hash"],
                    max_notional=rebalance["total_notional"],
                    max_slippage_bps=30,
                    idempotency_key="pipeline-approve",
                    actor="test",
                )

        self.assertEqual(seed_result["instrument_count"], 4)
        self.assertEqual(score_result["score_count"], 4)
        self.assertGreaterEqual(len(scores), 4)
        self.assertEqual(approved["status"], "APPROVED")
        self.assertGreaterEqual(len(approved["created_order_intents"]), 1)

        for _ in range(4):
            run_execution_once(settings=self.settings, owner="pipeline-worker")

        with connect(self.settings) as conn:
            orders = list_order_intents(conn)
        approved_ids = set(approved["created_order_intents"])
        approved_orders = [order for order in orders if order["id"] in approved_ids]
        self.assertTrue(approved_orders)
        self.assertTrue(all(order["status"] == "FILLED" for order in approved_orders))

    def test_rebalance_approval_rejects_stale_hash(self) -> None:
        with connect(self.settings) as conn:
            with transaction(conn):
                state = get_system_state(conn)
                command_arm(
                    conn,
                    settings=self.settings,
                    target_mode=SystemMode.PAPER,
                    reason="test arm",
                    expected_state_version=state["state_version"],
                    idempotency_key="pipeline-arm-stale",
                    actor="test",
                )
                seed_demo_market_data(conn, actor="test")
                compute_demo_scores(conn, actor="test")
                rebalance = generate_rebalance(conn, account_id=self.settings.account_id, actor="test")
                with self.assertRaises(SafetyError):
                    approve_rebalance(
                        conn,
                        settings=self.settings,
                        rebalance_id=rebalance["id"],
                        portfolio_hash="sha256:stale",
                        max_notional=rebalance["total_notional"],
                        max_slippage_bps=30,
                        idempotency_key="pipeline-stale",
                        actor="test",
                    )


if __name__ == "__main__":
    unittest.main()
