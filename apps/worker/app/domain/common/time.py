from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")


def now_utc() -> datetime:
    return datetime.now(UTC)


def now_kst() -> datetime:
    return now_utc().astimezone(KST)


def age_seconds(value: datetime, now: datetime | None = None) -> float:
    current = now or now_utc()
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return max(0.0, (current - value.astimezone(UTC)).total_seconds())

