from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class KrxQuotePlaceholder(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    price_krw: int

