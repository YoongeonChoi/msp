from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from msp.settings import Settings, load_settings
from msp.time import utc_now_iso


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS system_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    mode TEXT NOT NULL,
    state_version INTEGER NOT NULL,
    trading_enabled INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS commands (
    id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    command_type TEXT NOT NULL,
    target_mode TEXT,
    reason TEXT NOT NULL,
    expected_state_version INTEGER,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
    id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    idempotency_key TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outbox_events (
    id TEXT PRIMARY KEY,
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    published_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS order_intents (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity REAL NOT NULL,
    limit_price REAL NOT NULL,
    currency TEXT NOT NULL,
    status TEXT NOT NULL,
    approved_at TEXT,
    approval_expires_at TEXT,
    priority INTEGER NOT NULL DEFAULT 0,
    idempotency_key TEXT NOT NULL UNIQUE,
    internal_order_id TEXT NOT NULL UNIQUE,
    broker_client_order_id TEXT NOT NULL UNIQUE,
    attempt INTEGER NOT NULL DEFAULT 0,
    execution_owner TEXT,
    fencing_token INTEGER,
    reject_reason TEXT,
    portfolio_hash TEXT,
    max_notional REAL,
    max_slippage_bps INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_orders (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    order_intent_id TEXT NOT NULL,
    broker_order_id TEXT NOT NULL UNIQUE,
    broker_client_order_id TEXT NOT NULL UNIQUE,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity REAL NOT NULL,
    limit_price REAL NOT NULL,
    currency TEXT NOT NULL,
    status TEXT NOT NULL,
    filled_quantity REAL NOT NULL,
    average_price REAL NOT NULL,
    raw_response_json TEXT NOT NULL,
    submitted_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_cash (
    account_id TEXT NOT NULL,
    currency TEXT NOT NULL,
    balance REAL NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (account_id, currency)
);

CREATE TABLE IF NOT EXISTS paper_positions (
    account_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    quantity REAL NOT NULL,
    avg_cost REAL NOT NULL,
    market_price REAL NOT NULL,
    currency TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (account_id, symbol)
);

CREATE TABLE IF NOT EXISTS position_snapshots (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    quantity REAL NOT NULL,
    avg_cost REAL NOT NULL,
    market_price REAL NOT NULL,
    currency TEXT NOT NULL,
    as_of TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cash_snapshots (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    currency TEXT NOT NULL,
    balance REAL NOT NULL,
    as_of TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS execution_leases (
    account_id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    fencing_token INTEGER NOT NULL,
    expires_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reconciliation_runs (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    status TEXT NOT NULL,
    mismatch_count INTEGER NOT NULL,
    details_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS instruments (
    symbol TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    market_country TEXT NOT NULL,
    currency TEXT NOT NULL,
    active INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS price_bars_daily (
    symbol TEXT NOT NULL,
    as_of_date TEXT NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume REAL NOT NULL,
    available_at TEXT NOT NULL,
    source TEXT NOT NULL,
    PRIMARY KEY (symbol, as_of_date)
);

CREATE TABLE IF NOT EXISTS research_scores (
    symbol TEXT NOT NULL,
    as_of_date TEXT NOT NULL,
    feature_version TEXT NOT NULL,
    momentum_score REAL NOT NULL,
    quality_score REAL NOT NULL,
    ai_event_score REAL NOT NULL,
    total_score REAL NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (symbol, as_of_date, feature_version)
);

CREATE TABLE IF NOT EXISTS rebalance_runs (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    as_of_date TEXT NOT NULL,
    status TEXT NOT NULL,
    portfolio_hash TEXT NOT NULL,
    total_notional REAL NOT NULL,
    created_at TEXT NOT NULL,
    approved_at TEXT,
    approval_idempotency_key TEXT
);

CREATE TABLE IF NOT EXISTS rebalance_targets (
    id TEXT PRIMARY KEY,
    rebalance_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    target_weight REAL NOT NULL,
    target_notional REAL NOT NULL,
    reference_price REAL NOT NULL,
    score REAL NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (rebalance_id) REFERENCES rebalance_runs(id)
);
"""


class ManagedConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback) -> None:
        try:
            super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def connect(settings: Settings | None = None) -> sqlite3.Connection:
    settings = settings or load_settings()
    Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.db_path, factory=ManagedConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_db(settings: Settings | None = None) -> None:
    settings = settings or load_settings()
    with connect(settings) as conn:
        conn.executescript(SCHEMA)
        now = utc_now_iso()
        conn.execute(
            """
            INSERT OR IGNORE INTO system_state
                (id, mode, state_version, trading_enabled, updated_at)
            VALUES (1, 'READ_ONLY', 1, 0, ?)
            """,
            (now,),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO paper_cash
                (account_id, currency, balance, updated_at)
            VALUES (?, 'KRW', ?, ?)
            """,
            (settings.account_id, settings.initial_cash_krw, now),
        )


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}
