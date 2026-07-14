from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.domain.trading.entities import AccountState, BotSettings, Quote, Signal


@dataclass(frozen=True, slots=True)
class RiskInput:
    settings: BotSettings
    signal: Signal
    account_state: AccountState | None
    quote: Quote | None
    now: datetime
    provider_health: Mapping[str, bool]
    market_open: bool | None
    existing_position_pct: float | None
    sector_position_pct: float | None
    available_position_quantity: int | None
    critical_news_risk: bool | None
    liquidity_ok: bool | None
    volatility_ok: bool | None
    cooldown_active: bool
    duplicate_order: bool
    strategy_version_id: UUID | None
    strategy_status: str
    strategy_approved: bool
    shutdown_requested: bool = False
