from __future__ import annotations

import sqlite3
from datetime import timedelta

from msp.services.audit import append_audit, append_outbox
from msp.time import utc_now, utc_now_iso


DEMO_INSTRUMENTS = [
    ("005930", "Samsung Electronics", "KR", "KRW"),
    ("000660", "SK Hynix", "KR", "KRW"),
    ("AAPL", "Apple", "US", "USD"),
    ("MSFT", "Microsoft", "US", "USD"),
]

DEMO_CLOSES = {
    "005930": [69000, 70000, 70500, 69800, 71000],
    "000660": [116000, 118500, 119000, 121000, 123000],
    "AAPL": [184.4, 185.2, 187.1, 186.8, 188.0],
    "MSFT": [421.0, 419.5, 422.4, 426.1, 427.0],
}


def seed_demo_market_data(conn: sqlite3.Connection, *, actor: str = "data-worker") -> dict:
    now = utc_now()
    now_iso = now.isoformat()
    end_date = now.date()

    for symbol, name, market_country, currency in DEMO_INSTRUMENTS:
        conn.execute(
            """
            INSERT INTO instruments (symbol, name, market_country, currency, active, updated_at)
            VALUES (?, ?, ?, ?, 1, ?)
            ON CONFLICT(symbol)
            DO UPDATE SET
                name = excluded.name,
                market_country = excluded.market_country,
                currency = excluded.currency,
                active = 1,
                updated_at = excluded.updated_at
            """,
            (symbol, name, market_country, currency, now_iso),
        )
        closes = DEMO_CLOSES[symbol]
        for offset, close in enumerate(closes):
            as_of_date = (end_date - timedelta(days=len(closes) - offset - 1)).isoformat()
            open_price = closes[max(0, offset - 1)]
            high = max(open_price, close) * 1.01
            low = min(open_price, close) * 0.99
            volume = 1_000_000 + (offset * 125_000)
            conn.execute(
                """
                INSERT INTO price_bars_daily
                    (symbol, as_of_date, open, high, low, close, volume, available_at, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'demo')
                ON CONFLICT(symbol, as_of_date)
                DO UPDATE SET
                    open = excluded.open,
                    high = excluded.high,
                    low = excluded.low,
                    close = excluded.close,
                    volume = excluded.volume,
                    available_at = excluded.available_at,
                    source = excluded.source
                """,
                (symbol, as_of_date, open_price, high, low, close, volume, now_iso),
            )

    payload = {"instrument_count": len(DEMO_INSTRUMENTS), "bar_count": len(DEMO_INSTRUMENTS) * 5}
    append_audit(conn, "data.demo_seeded", actor, payload)
    append_outbox(conn, "market_data", "demo", "data.demo_seeded", payload)
    return payload


def latest_market_date(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT MAX(as_of_date) AS as_of_date FROM price_bars_daily").fetchone()
    return row["as_of_date"] if row and row["as_of_date"] else None
