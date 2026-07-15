from __future__ import annotations

import asyncio

import structlog

from app.bootstrap import bootstrap, bootstrap_operations_v2
from app.config import Settings, load_settings
from app.domain.common.errors import KnownFailClosedError

logger = structlog.get_logger()


async def async_main(settings: Settings | None = None) -> None:
    resolved_settings = settings or load_settings()
    if resolved_settings.execution_v2_worker_api_enabled:
        runtime = bootstrap_operations_v2(resolved_settings)
        try:
            await runtime.operations_loop.run()
        except Exception:
            logger.exception("operations_v2_runtime_failed")
            raise
        finally:
            await runtime.close()
        return

    container = bootstrap(resolved_settings)
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

