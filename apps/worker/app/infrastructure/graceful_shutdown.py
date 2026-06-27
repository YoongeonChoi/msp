from __future__ import annotations

import signal


class ShutdownFlag:
    def __init__(self) -> None:
        self.requested = False

    def request(self, *_args: object) -> None:
        self.requested = True


def install_signal_handlers(shutdown: ShutdownFlag) -> None:
    signal.signal(signal.SIGTERM, shutdown.request)
    signal.signal(signal.SIGINT, shutdown.request)

