from __future__ import annotations

from typing import NoReturn

import pytest

from app import main as main_module
from app.config import Settings


async def test_worker_api_flag_routes_only_to_operations_v2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _v2_settings()
    runtime = FakeOperationsRuntime()

    def build_operations(received: Settings) -> FakeOperationsRuntime:
        assert received is settings
        return runtime

    def reject_legacy(_settings: Settings) -> NoReturn:
        raise AssertionError("legacy_runtime_must_not_be_built")

    monkeypatch.setattr(main_module, "bootstrap_operations_v2", build_operations)
    monkeypatch.setattr(main_module, "bootstrap", reject_legacy)

    await main_module.async_main(settings)

    assert runtime.operations_loop.calls == 1
    assert runtime.closed is True


async def test_default_runtime_routes_only_to_legacy_trading_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(RUN_ONCE=True)
    runtime = FakeLegacyRuntime()

    def build_legacy(received: Settings) -> FakeLegacyRuntime:
        assert received is settings
        return runtime

    def reject_operations(_settings: Settings) -> NoReturn:
        raise AssertionError("operations_runtime_must_not_be_built")

    monkeypatch.setattr(main_module, "bootstrap", build_legacy)
    monkeypatch.setattr(main_module, "bootstrap_operations_v2", reject_operations)

    await main_module.async_main(settings)

    assert runtime.trading_loop.calls == 1
    assert runtime.closed is True


async def test_legacy_runtime_is_closed_when_loop_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = FakeLegacyRuntime(fail=True)
    monkeypatch.setattr(main_module, "bootstrap", lambda _settings: runtime)

    with pytest.raises(RuntimeError, match="legacy_loop_failed"):
        await main_module.async_main(Settings(RUN_ONCE=True))

    assert runtime.repository.recorded == 1
    assert runtime.closed is True


async def test_operations_runtime_is_closed_when_loop_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = FakeOperationsRuntime(fail=True)
    monkeypatch.setattr(
        main_module,
        "bootstrap_operations_v2",
        lambda _settings: runtime,
    )

    with pytest.raises(RuntimeError, match="operations_loop_failed"):
        await main_module.async_main(_v2_settings())

    assert runtime.closed is True


class FakeOperationsLoop:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    async def run(self) -> None:
        self.calls += 1
        if self.fail:
            raise RuntimeError("operations_loop_failed")


class FakeOperationsRuntime:
    def __init__(self, *, fail: bool = False) -> None:
        self.operations_loop = FakeOperationsLoop(fail=fail)
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class FakeTradingLoop:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    async def run(self) -> None:
        self.calls += 1
        if self.fail:
            raise RuntimeError("legacy_loop_failed")


class FakeRepository:
    def __init__(self) -> None:
        self.recorded = 0

    async def record_engine_event(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        self.recorded += 1


class FakeLegacyRuntime:
    def __init__(self, *, fail: bool = False) -> None:
        self.trading_loop = FakeTradingLoop(fail=fail)
        self.repository = FakeRepository()
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def _v2_settings() -> Settings:
    return Settings(
        RUN_ONCE=True,
        EXECUTION_V2_ENABLED=True,
        EXECUTION_V2_WORKER_API_ENABLED=True,
        EXECUTION_V2_WORKER_ID="00000000-0000-4000-8000-000000000001",
        EXECUTION_V2_ACCOUNT_ID="paper-primary",
    )
