from __future__ import annotations

import asyncio

import structlog

from app.adapters.alerts.dead_man_webhook_destination import (
    DeadManWebhookDestination,
)
from app.adapters.persistence.supabase_dead_man_source import SupabaseDeadManSource
from app.application.services.dead_man_loop import DeadManMonitorLoop
from app.application.services.dead_man_service import DeadManEvaluator
from app.application.use_cases.run_dead_man_monitor import RunDeadManMonitor
from app.dead_man_config import DeadManSettings, load_dead_man_settings
from app.infrastructure.graceful_shutdown import ShutdownFlag, install_signal_handlers
from app.logging_config import configure_logging

logger = structlog.get_logger()


async def async_main(settings: DeadManSettings | None = None) -> None:
    configure_logging()
    resolved_settings = settings or load_dead_man_settings()
    shutdown = ShutdownFlag()
    install_signal_handlers(shutdown)
    source = SupabaseDeadManSource(resolved_settings)
    destination = DeadManWebhookDestination(
        resolved_settings.alert_webhook_url.get_secret_value(),
        key_ring=resolved_settings.receiver_ack_key_ring(),
        timeout_sec=resolved_settings.request_timeout_sec,
    )
    runner = RunDeadManMonitor(
        source,
        destination,
        DeadManEvaluator(),
        account_id=resolved_settings.account_id,
    )
    try:
        await DeadManMonitorLoop(
            runner,
            shutdown,
            interval_sec=resolved_settings.interval_sec,
        ).run()
    except Exception:
        logger.exception("dead_man_monitor_failed")
        raise
    finally:
        try:
            await source.close()
        finally:
            await destination.close()


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
