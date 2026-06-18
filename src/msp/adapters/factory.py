from __future__ import annotations

import sqlite3

from msp.adapters.base import BrokerAdapter
from msp.adapters.paper import PaperBrokerAdapter
from msp.adapters.toss import TossBrokerAdapter
from msp.domain.enums import SystemMode
from msp.exceptions import SafetyError
from msp.settings import Settings


def broker_for_execution(conn: sqlite3.Connection, settings: Settings, mode: str) -> BrokerAdapter:
    if mode != SystemMode.LIVE.value:
        return PaperBrokerAdapter(conn)

    if settings.broker_adapter == "paper":
        return PaperBrokerAdapter(conn)

    if settings.broker_adapter == "toss":
        if not settings.allow_live_mode or not settings.enable_real_broker:
            raise SafetyError("Toss execution requires MSP_ALLOW_LIVE_MODE=true and MSP_ENABLE_REAL_BROKER=true")
        return TossBrokerAdapter.from_settings(settings)

    raise SafetyError(f"unknown broker adapter: {settings.broker_adapter}")


def broker_for_reconciliation(conn: sqlite3.Connection, settings: Settings) -> BrokerAdapter:
    if settings.broker_adapter == "paper":
        return PaperBrokerAdapter(conn)
    if settings.broker_adapter == "toss":
        if not settings.enable_real_broker:
            return PaperBrokerAdapter(conn)
        return TossBrokerAdapter.from_settings(settings)
    raise SafetyError(f"unknown broker adapter: {settings.broker_adapter}")
