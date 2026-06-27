from __future__ import annotations

import asyncio

import structlog

from app.bootstrap import bootstrap
from app.domain.common.errors import KnownFailClosedError

logger = structlog.get_logger()


async def async_main() -> None:
    container = bootstrap()
    try:
        await container.trading_loop.run()
    except KnownFailClosedError as exc:
        await container.repository.record_engine_event(
            "warning", exc.component, exc.safe_message, {"fail_closed": True}
        )
        logger.warning("known_fail_closed", component=exc.component, message=exc.safe_message)
    except Exception as exc:
        await container.repository.record_engine_event(
            "critical",
            "worker",
            "unexpected error; live orders blocked",
            {"error_type": type(exc).__name__},
        )
        logger.exception("unexpected_error_live_orders_blocked")
        raise


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()

