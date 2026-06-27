from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class TossOrderPlaceholder(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    side: str
    amount_krw: int


class TossQuotePlaceholder(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    price_krw: int

