from __future__ import annotations

from pydantic import BaseModel, Field

from msp.domain.enums import KillMode, OrderSide, SystemMode


class CommandRequest(BaseModel):
    reason: str = "manual command"
    expected_state_version: int | None = None
    idempotency_key: str | None = None


class ArmRequest(CommandRequest):
    target_mode: SystemMode = SystemMode.PAPER


class KillRequest(CommandRequest):
    mode: KillMode = KillMode.CANCEL_OPEN_ORDERS


class UnlockRequest(CommandRequest):
    confirmation_phrase: str


class CreateOrderIntentRequest(BaseModel):
    symbol: str
    side: OrderSide
    quantity: float = Field(gt=0)
    limit_price: float = Field(gt=0)
    currency: str = "KRW"
    approved: bool = True
    priority: int = 0
    approval_minutes: int = Field(default=60, gt=0, le=1440)
    portfolio_hash: str | None = None
    max_notional: float | None = Field(default=None, gt=0)
    max_slippage_bps: int | None = Field(default=None, ge=0)
    idempotency_key: str | None = None


class ApproveOrderIntentRequest(BaseModel):
    max_notional: float = Field(gt=0)
    max_slippage_bps: int = Field(default=30, ge=0)
    expires_minutes: int = Field(default=60, gt=0, le=1440)
    portfolio_hash: str | None = None
    reason: str = "manual approval"
    idempotency_key: str | None = None


class SeedCashRequest(BaseModel):
    amount: float = Field(gt=0)
    currency: str = "KRW"


class GenerateRebalanceRequest(BaseModel):
    top_n: int = Field(default=2, gt=0, le=20)
    gross_exposure: float = Field(default=0.5, gt=0, le=1)
    as_of_date: str | None = None


class ApproveRebalanceRequest(BaseModel):
    portfolio_hash: str
    max_notional: float = Field(gt=0)
    max_slippage_bps: int = Field(default=30, ge=0)
    create_orders: bool = True
    idempotency_key: str | None = None
