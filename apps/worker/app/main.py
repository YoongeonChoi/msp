from __future__ import annotations

import asyncio

import structlog

from app.bootstrap import bootstrap, bootstrap_operations_v2
from app.config import Settings, load_settings
from app.domain.common.errors import KnownFailClosedError
from app.infrastructure.scheduler_fail_stop import SchedulerProcessFailStop

logger = structlog.get_logger()


async def async_main(settings: Settings | None = None) -> None:
    resolved_settings = settings or load_settings()
    if resolved_settings.execution_v2_worker_api_enabled:
        runtime = bootstrap_operations_v2(resolved_settings)
        try:
            try:
                await runtime.scheduler_loop.run()
            except SchedulerProcessFailStop as exc:
                logger.critical("durable_scheduler_fail_stop", reason=exc.reason)
                raise
            except Exception:
                logger.exception("operations_v2_runtime_failed")
                raise
        except BaseException as primary_failure:
            try:
                await runtime.close()
            except BaseException as cleanup_failure:
                primary_failure.add_note(
                    "operations runtime cleanup also failed: "
                    f"{type(cleanup_failure).__name__}"
                )
            raise
        else:
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
    finally:
        await container.close()


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
