from __future__ import annotations

import re

from .entities import BotSettings


def settings_validation_reasons(settings: BotSettings) -> list[str]:
    reasons: list[str] = []
    if settings.deployment_lock:
        if settings.enabled or settings.live_order_allowed:
            reasons.append("deployment_lock_requires_disabled_execution")
        if (
            settings.deployment_target_sha is None
            or re.fullmatch(r"[0-9a-f]{40}([0-9a-f]{24})?", settings.deployment_target_sha)
            is None
        ):
            reasons.append("deployment_target_sha_invalid")
    elif settings.deployment_target_sha is not None:
        reasons.append("deployment_target_sha_requires_lock")
    if settings.mode not in {"paper", "live"}:
        reasons.append("invalid_mode")
    if settings.live_order_allowed and (
        not settings.enabled or settings.mode != "live"
    ):
        reasons.append("live_order_allowed_state_invalid")
    if not 1 <= settings.max_order_amount_krw <= 100_000_000:
        reasons.append("invalid_max_order_amount_krw")
    if not 0 < settings.max_daily_loss_pct <= 0.20:
        reasons.append("invalid_max_daily_loss_pct")
    if not 1 <= settings.max_daily_order_count <= 1000:
        reasons.append("invalid_max_daily_order_count")
    if not 0 < settings.max_position_pct <= 1:
        reasons.append("invalid_max_position_pct")
    if not 0 < settings.max_sector_pct <= 1:
        reasons.append("invalid_max_sector_pct")
    if not 5 <= settings.loop_interval_sec <= 3600:
        reasons.append("invalid_loop_interval_sec")
    return reasons


def validate_settings(settings: BotSettings) -> None:
    reasons = settings_validation_reasons(settings)
    if reasons:
        raise ValueError(",".join(reasons))
