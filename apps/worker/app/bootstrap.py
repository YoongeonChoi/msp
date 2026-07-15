from __future__ import annotations

from app.config import Settings, load_settings
from app.container import (
    Container,
    OperationsV2Runtime,
    build_container,
    build_operations_v2_runtime,
)
from app.infrastructure.graceful_shutdown import ShutdownFlag, install_signal_handlers
from app.logging_config import configure_logging


def bootstrap(settings: Settings | None = None) -> Container:
    configure_logging()
    resolved_settings = settings or load_settings()
    shutdown = ShutdownFlag()
    install_signal_handlers(shutdown)
    return build_container(resolved_settings, shutdown)


def bootstrap_operations_v2(settings: Settings | None = None) -> OperationsV2Runtime:
    configure_logging()
    resolved_settings = settings or load_settings()
    shutdown = ShutdownFlag()
    install_signal_handlers(shutdown)
    return build_operations_v2_runtime(resolved_settings, shutdown)
