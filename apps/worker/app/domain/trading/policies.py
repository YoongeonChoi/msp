from __future__ import annotations

from .entities import BotSettings


def settings_validation_reasons(settings: BotSettings) -> list[str]:
    reasons: list[str] = []
    if settings.mode not in {"paper", "live"}:
        reasons.append("invalid_mode")
    if settings.max_order_amount_krw > 100_000_000:
        reasons.append("invalid_max_order_amount_krw")
    if not 0 < settings.max_daily_loss_pct <= 0.20:
        reasons.append("invalid_max_daily_loss_pct")
    if not 0 < settings.max_position_pct <= 1:
        reasons.append("invalid_max_position_pct")
    if not 0 < settings.max_sector_pct <= 1:
        reasons.append("invalid_max_sector_pct")
    return reasons


def validate_settings(settings: BotSettings) -> None:
    reasons = settings_validation_reasons(settings)
    if reasons:
        raise ValueError(",".join(reasons))
