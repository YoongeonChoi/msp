# Extension Guide

Add strategy:

1. Implement `StrategyPort`.
2. Store weights/params in `strategy_versions`.
3. Write explanation JSON.
4. Add tests.
5. Paper-test first.
6. Never call `ExecutionService` directly.

Add provider:

1. Define port method or adapter model.
2. Add mock.
3. Add schema validation.
4. Add contract fixture.
5. Add `API_GAPS.md` verification entry until official docs are confirmed.

Add broker:

1. Implement `BrokerPort`.
2. Keep live disabled by default.
3. Add idempotency and unknown-status handling.
4. Add audit logs and final risk check.

Split service later:

- Use `outbox_events` as migration boundary.
- Extract only after measurable scaling or reliability need.

