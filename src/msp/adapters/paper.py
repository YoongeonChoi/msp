from __future__ import annotations

import json
import sqlite3

from msp.db import row_to_dict
from msp.domain.enums import OrderSide
from msp.domain.models import BrokerOrderCommand, BrokerOrderResult
from msp.services.ids import new_id
from msp.time import utc_now_iso


class PaperBrokerAdapter:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def submit_order(self, command: BrokerOrderCommand) -> BrokerOrderResult:
        existing = self.conn.execute(
            "SELECT * FROM paper_orders WHERE broker_client_order_id = ?",
            (command.broker_client_order_id,),
        ).fetchone()
        if existing is not None:
            return BrokerOrderResult(
                broker_order_id=existing["broker_order_id"],
                status=existing["status"],
                filled_quantity=float(existing["filled_quantity"]),
                average_price=float(existing["average_price"]),
                raw_response=json.loads(existing["raw_response_json"]),
            )

        now = utc_now_iso()
        broker_order_id = new_id("paper_order")
        notional = command.quantity * command.limit_price
        status = "FILLED"
        filled_quantity = command.quantity
        average_price = command.limit_price
        reason = None

        if command.side == OrderSide.BUY:
            cash = self._cash_balance(command.account_id, command.currency)
            if cash < notional:
                status = "REJECTED"
                filled_quantity = 0.0
                average_price = 0.0
                reason = "insufficient paper cash"
            else:
                self._set_cash(command.account_id, command.currency, cash - notional)
                self._increase_position(command)
        else:
            current = self._position(command.account_id, command.symbol)
            current_qty = float(current["quantity"]) if current else 0.0
            if current_qty < command.quantity:
                status = "REJECTED"
                filled_quantity = 0.0
                average_price = 0.0
                reason = "insufficient paper position"
            else:
                cash = self._cash_balance(command.account_id, command.currency)
                self._set_cash(command.account_id, command.currency, cash + notional)
                self._decrease_position(command)

        raw_response = {
            "broker": "paper",
            "broker_order_id": broker_order_id,
            "broker_client_order_id": command.broker_client_order_id,
            "status": status,
            "reason": reason,
        }
        self.conn.execute(
            """
            INSERT INTO paper_orders
                (
                    id, account_id, order_intent_id, broker_order_id, broker_client_order_id,
                    symbol, side, quantity, limit_price, currency, status,
                    filled_quantity, average_price, raw_response_json, submitted_at, updated_at
                )
            VALUES
                (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id("paper"),
                command.account_id,
                command.order_intent_id,
                broker_order_id,
                command.broker_client_order_id,
                command.symbol,
                command.side.value,
                command.quantity,
                command.limit_price,
                command.currency,
                status,
                filled_quantity,
                average_price,
                json.dumps(raw_response, sort_keys=True),
                now,
                now,
            ),
        )
        return BrokerOrderResult(
            broker_order_id=broker_order_id,
            status=status,
            filled_quantity=filled_quantity,
            average_price=average_price,
            raw_response=raw_response,
        )

    def cancel_order(self, broker_order_id: str) -> BrokerOrderResult:
        row = self.conn.execute(
            "SELECT * FROM paper_orders WHERE broker_order_id = ?",
            (broker_order_id,),
        ).fetchone()
        if row is None:
            return BrokerOrderResult(
                broker_order_id=broker_order_id,
                status="UNKNOWN",
                filled_quantity=0.0,
                average_price=0.0,
                raw_response={"broker": "paper", "status": "UNKNOWN"},
            )
        return BrokerOrderResult(
            broker_order_id=broker_order_id,
            status=row["status"],
            filled_quantity=float(row["filled_quantity"]),
            average_price=float(row["average_price"]),
            raw_response=json.loads(row["raw_response_json"]),
        )

    def list_positions(self, account_id: str) -> list[dict]:
        rows = self.conn.execute(
            """
            SELECT account_id, symbol, quantity, avg_cost, market_price, currency, updated_at
            FROM paper_positions
            WHERE account_id = ?
            ORDER BY symbol
            """,
            (account_id,),
        ).fetchall()
        return [row_to_dict(row) or {} for row in rows]

    def list_cash(self, account_id: str) -> list[dict]:
        rows = self.conn.execute(
            """
            SELECT account_id, currency, balance, updated_at
            FROM paper_cash
            WHERE account_id = ?
            ORDER BY currency
            """,
            (account_id,),
        ).fetchall()
        return [row_to_dict(row) or {} for row in rows]

    def seed_cash(self, account_id: str, currency: str, amount: float) -> None:
        self._set_cash(account_id, currency, amount)

    def _cash_balance(self, account_id: str, currency: str) -> float:
        row = self.conn.execute(
            "SELECT balance FROM paper_cash WHERE account_id = ? AND currency = ?",
            (account_id, currency),
        ).fetchone()
        return float(row["balance"]) if row else 0.0

    def _set_cash(self, account_id: str, currency: str, balance: float) -> None:
        now = utc_now_iso()
        self.conn.execute(
            """
            INSERT INTO paper_cash (account_id, currency, balance, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(account_id, currency)
            DO UPDATE SET balance = excluded.balance, updated_at = excluded.updated_at
            """,
            (account_id, currency, balance, now),
        )

    def _position(self, account_id: str, symbol: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM paper_positions WHERE account_id = ? AND symbol = ?",
            (account_id, symbol),
        ).fetchone()

    def _increase_position(self, command: BrokerOrderCommand) -> None:
        now = utc_now_iso()
        existing = self._position(command.account_id, command.symbol)
        if existing is None:
            self.conn.execute(
                """
                INSERT INTO paper_positions
                    (account_id, symbol, quantity, avg_cost, market_price, currency, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    command.account_id,
                    command.symbol,
                    command.quantity,
                    command.limit_price,
                    command.limit_price,
                    command.currency,
                    now,
                ),
            )
            return

        old_qty = float(existing["quantity"])
        old_avg = float(existing["avg_cost"])
        new_qty = old_qty + command.quantity
        new_avg = ((old_qty * old_avg) + (command.quantity * command.limit_price)) / new_qty
        self.conn.execute(
            """
            UPDATE paper_positions
            SET quantity = ?, avg_cost = ?, market_price = ?, currency = ?, updated_at = ?
            WHERE account_id = ? AND symbol = ?
            """,
            (
                new_qty,
                new_avg,
                command.limit_price,
                command.currency,
                now,
                command.account_id,
                command.symbol,
            ),
        )

    def _decrease_position(self, command: BrokerOrderCommand) -> None:
        now = utc_now_iso()
        existing = self._position(command.account_id, command.symbol)
        if existing is None:
            return
        new_qty = float(existing["quantity"]) - command.quantity
        if new_qty <= 0:
            self.conn.execute(
                "DELETE FROM paper_positions WHERE account_id = ? AND symbol = ?",
                (command.account_id, command.symbol),
            )
            return
        self.conn.execute(
            """
            UPDATE paper_positions
            SET quantity = ?, market_price = ?, updated_at = ?
            WHERE account_id = ? AND symbol = ?
            """,
            (new_qty, command.limit_price, now, command.account_id, command.symbol),
        )
