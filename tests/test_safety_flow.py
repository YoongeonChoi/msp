from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from msp.adapters.paper import PaperBrokerAdapter
from msp.db import connect, init_db, transaction
from msp.domain.enums import OrderSide, SystemMode
from msp.exceptions import SafetyError
from msp.services.orders import create_order_intent
from msp.services.system_state import command_arm, command_kill, command_unlock, get_system_state
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


class SafetyFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.settings = make_settings(Path(self.temp_dir.name) / "msp-test.sqlite3")
        init_db(self.settings)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_read_only_fails_closed_for_order_generation(self) -> None:
        with connect(self.settings) as conn:
            with transaction(conn):
                with self.assertRaises(SafetyError):
                    create_order_intent(
                        conn,
                        settings=self.settings,
                        idempotency_key="intent-read-only",
                        actor="test",
                        symbol="005930",
                        side=OrderSide.BUY,
                        quantity=1,
                        limit_price=70000,
                        currency="KRW",
                        approved=True,
                    )

    def test_killed_state_requires_unlock_before_arm(self) -> None:
        with connect(self.settings) as conn:
            with transaction(conn):
                state = get_system_state(conn)
                command_kill(
                    conn,
                    settings=self.settings,
                    mode="CANCEL_OPEN_ORDERS",
                    reason="test kill",
                    expected_state_version=state["state_version"],
                    idempotency_key="kill-before-arm",
                    actor="test",
                )
                killed = get_system_state(conn)
                with self.assertRaises(SafetyError):
                    command_arm(
                        conn,
                        settings=self.settings,
                        target_mode=SystemMode.PAPER,
                        reason="bad arm",
                        expected_state_version=killed["state_version"],
                        idempotency_key="arm-from-killed",
                        actor="test",
                    )
                unlocked = command_unlock(
                    conn,
                    settings=self.settings,
                    reason="manual recovery",
                    expected_state_version=killed["state_version"],
                    idempotency_key="unlock-killed",
                    actor="test",
                    confirmation_phrase="I_ACCEPT_MANUAL_RECOVERY",
                )
                command_arm(
                    conn,
                    settings=self.settings,
                    target_mode=SystemMode.PAPER,
                    reason="arm after unlock",
                    expected_state_version=unlocked["state_version"],
                    idempotency_key="arm-after-unlock",
                    actor="test",
                )

    def test_paper_order_executes_once_and_updates_cash(self) -> None:
        with connect(self.settings) as conn:
            with transaction(conn):
                state = get_system_state(conn)
                command_arm(
                    conn,
                    settings=self.settings,
                    target_mode=SystemMode.PAPER,
                    reason="test arm",
                    expected_state_version=state["state_version"],
                    idempotency_key="arm-paper",
                    actor="test",
                )
                intent = create_order_intent(
                    conn,
                    settings=self.settings,
                    idempotency_key="intent-buy",
                    actor="test",
                    symbol="005930",
                    side=OrderSide.BUY,
                    quantity=2,
                    limit_price=70000,
                    currency="KRW",
                    approved=True,
                    max_notional=140000,
                )

        first = run_execution_once(settings=self.settings, owner="worker-test")
        second = run_execution_once(settings=self.settings, owner="worker-test")
        self.assertEqual(first["executed"], 1)
        self.assertEqual(second["executed"], 0)

        with connect(self.settings) as conn:
            order = conn.execute("SELECT * FROM order_intents WHERE id = ?", (intent["id"],)).fetchone()
            self.assertEqual(order["status"], "FILLED")
            broker = PaperBrokerAdapter(conn)
            cash = broker.list_cash(self.settings.account_id)[0]
            self.assertEqual(cash["balance"], 860000)

    def test_kill_blocks_new_orders_and_is_idempotent(self) -> None:
        with connect(self.settings) as conn:
            with transaction(conn):
                state = get_system_state(conn)
                command_arm(
                    conn,
                    settings=self.settings,
                    target_mode=SystemMode.PAPER,
                    reason="test arm",
                    expected_state_version=state["state_version"],
                    idempotency_key="arm-before-kill",
                    actor="test",
                )
                state = get_system_state(conn)
                killed = command_kill(
                    conn,
                    settings=self.settings,
                    mode="CANCEL_OPEN_ORDERS",
                    reason="test kill",
                    expected_state_version=state["state_version"],
                    idempotency_key="kill-once",
                    actor="test",
                )
                same = command_kill(
                    conn,
                    settings=self.settings,
                    mode="CANCEL_OPEN_ORDERS",
                    reason="test kill",
                    expected_state_version=state["state_version"],
                    idempotency_key="kill-once",
                    actor="test",
                )
                self.assertEqual(killed["state_version"], same["state_version"])
                with self.assertRaises(SafetyError):
                    create_order_intent(
                        conn,
                        settings=self.settings,
                        idempotency_key="intent-after-kill",
                        actor="test",
                        symbol="005930",
                        side=OrderSide.BUY,
                        quantity=1,
                        limit_price=70000,
                        currency="KRW",
                        approved=True,
                    )


if __name__ == "__main__":
    unittest.main()
