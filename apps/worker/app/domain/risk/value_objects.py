from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

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
    existing_position_pct: float
    sector_position_pct: float
    critical_news_risk: bool
    liquidity_ok: bool | None
    volatility_ok: bool | None
    cooldown_active: bool
    duplicate_order: bool
    shutdown_requested: bool = False
