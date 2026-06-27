from __future__ import annotations

from app.config import load_settings
from app.container import Container, build_container
from app.infrastructure.graceful_shutdown import ShutdownFlag, install_signal_handlers
from app.logging_config import configure_logging


def bootstrap() -> Container:
    configure_logging()
    settings = load_settings()
    shutdown = ShutdownFlag()
    install_signal_handlers(shutdown)
    return build_container(settings, shutdown)
