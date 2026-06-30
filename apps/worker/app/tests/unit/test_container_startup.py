from __future__ import annotations

from app.adapters.broker.toss_client import TossClient
from app.config import Settings
from app.container import build_container
from app.infrastructure.graceful_shutdown import ShutdownFlag


def test_real_provider_container_starts_without_toss_credentials() -> None:
    settings = Settings(
        MOCK_PROVIDERS=False,
        TOSS_CLIENT_ID=None,
        TOSS_CLIENT_SECRET=None,
        TOSS_ACCOUNT_ID=None,
    )

    container = build_container(settings, ShutdownFlag())

    assert container.trading_loop.settings.mock_providers is False
    assert isinstance(container.trading_loop.run_trading_cycle.broker, TossClient)
