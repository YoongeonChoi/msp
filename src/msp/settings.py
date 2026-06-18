from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Settings:
    app_name: str
    environment: str
    db_path: Path
    account_id: str
    allow_live_mode: bool
    initial_cash_krw: float
    worker_interval_seconds: float
    lease_ttl_seconds: int
    broker_adapter: str = "paper"
    enable_real_broker: bool = False
    toss_base_url: str = "https://openapi.tossinvest.com"
    toss_client_id: str | None = None
    toss_client_secret: str | None = None
    toss_account_seq: str | None = None
    toss_timeout_seconds: float = 10.0


def _bool_from_env(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _resolve_path(raw_path: str) -> Path:
    path = Path(raw_path)
    if not path.is_absolute():
        path = project_root() / path
    return path


def load_settings() -> Settings:
    config_path = _resolve_path(os.getenv("MSP_CONFIG", "config/local.toml"))
    config: dict = {}
    if config_path.exists():
        with config_path.open("rb") as handle:
            config = tomllib.load(handle)

    app_cfg = config.get("app", {})
    db_cfg = config.get("database", {})
    trading_cfg = config.get("trading", {})
    workers_cfg = config.get("workers", {})
    broker_cfg = config.get("broker", {})
    toss_cfg = config.get("toss", {})

    db_path = _resolve_path(os.getenv("MSP_DB_PATH", db_cfg.get("path", "data/msp.sqlite3")))

    return Settings(
        app_name=os.getenv("MSP_APP_NAME", app_cfg.get("name", "MSP")),
        environment=os.getenv("MSP_ENV", app_cfg.get("environment", "local")),
        db_path=db_path,
        account_id=os.getenv("MSP_ACCOUNT_ID", trading_cfg.get("account_id", "paper-main")),
        allow_live_mode=_bool_from_env(
            os.getenv("MSP_ALLOW_LIVE_MODE"),
            bool(trading_cfg.get("allow_live_mode", False)),
        ),
        initial_cash_krw=float(trading_cfg.get("initial_cash_krw", 10_000_000)),
        worker_interval_seconds=float(workers_cfg.get("interval_seconds", 2.0)),
        lease_ttl_seconds=int(workers_cfg.get("lease_ttl_seconds", 15)),
        broker_adapter=os.getenv("MSP_BROKER", broker_cfg.get("adapter", "paper")).lower(),
        enable_real_broker=_bool_from_env(
            os.getenv("MSP_ENABLE_REAL_BROKER"),
            bool(broker_cfg.get("enable_real_broker", False)),
        ),
        toss_base_url=os.getenv("TOSSINVEST_BASE_URL", toss_cfg.get("base_url", "https://openapi.tossinvest.com")),
        toss_client_id=os.getenv("TOSSINVEST_CLIENT_ID", toss_cfg.get("client_id")),
        toss_client_secret=os.getenv("TOSSINVEST_CLIENT_SECRET", toss_cfg.get("client_secret")),
        toss_account_seq=os.getenv("TOSSINVEST_ACCOUNT_SEQ", toss_cfg.get("account_seq")),
        toss_timeout_seconds=float(os.getenv("TOSSINVEST_TIMEOUT_SECONDS", toss_cfg.get("timeout_seconds", 10.0))),
    )
