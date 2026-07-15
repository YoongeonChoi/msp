# Risk Policy

## Safety boundary

All execution fails closed. This release has no Production Live path; database,
UI, settings, credentials, and network controls keep
`live_order_allowed=false`. Paper and local `contract_test` still require the
same durable control, lease, reservation, and evidence checks.

`RiskService` remains the aggregate risk decision point. Database reservation
repeats the authoritative invariants so a stale process decision cannot dispatch.

## Mandatory execution gate

Every new intent must prove:

- account environment is `paper` or `contract_test` and the account is enabled;
- current `control_epoch`, active strategy version, execution policy version,
  cost schedule version, and release identity match the command;
- caller holds the unexpired account lease and current fencing token;
- semantic key is unique for account + environment + strategy + symbol + side +
  signal validity window + execution policy version;
- decision, risk result, quote/bar, calendar, tick, corporate-action, and cost
  evidence are complete and fresh;
- quantity is a positive whole share and the order is KRW `LIMIT` `DAY`;
- buy cash after reservation is non-negative;
- sell available quantity after reservation is non-negative, with no short or
  margin behavior;
- symbol/strategy/account exposure, daily loss, order count, cooldown, and
  concentration limits pass;
- no unresolved unknown/quarantined intent blocks the affected account/symbol.

The dispatch gate rechecks `control_epoch`, enabled state, and fencing token
immediately before calling an execution adapter. An emergency stop increments
the epoch, invalidating every stale reservation or claimed command.

## Paper fill risk

The deterministic policy allows a fill only from the first complete one-minute
bar after the decision. Fill quantity is the lesser of remaining quantity and
1% of bar volume. Price uses 10 bps adverse slippage but cannot cross the limit.
Partial fill and expiry release the correct residual reserve.

Missing, expired, or hash-mismatched commission/tax/settlement schedule, tick
rule, volume, or corporate-action evidence blocks the fill; the engine does not
substitute zero, `false`, Paper, or another silent default.

## Accounting and observation invariants

- Every accounting transaction has equal debit and credit totals.
- Opening cash is a one-time 10,000,000 KRW journal for `paper-primary`.
- Cash, reserved cash, total/available position quantity, and settlement
  projections cannot be negative.
- Cost basis is `moving_weighted_average_v1`.
- Fill identity and observation identity are unique; duplicates are no-ops.
- Cumulative fill is monotonic and cannot exceed intent quantity.
- Terminal order state cannot regress and provider identity cannot change.
- Any violation is quarantined and creates audit/incident evidence in the same
  domain transaction where applicable.

Unknown state is not terminal and cannot be retried automatically. An operator
and a different risk approver must review retained evidence before a compensating
transaction or resolution.

## Command risk

Risk-increasing commands require AAL2, a fresh five-minute step-up grant bound to
the command hash, maker/checker UUID separation, expiry, and compare-and-set
version. UI permission checks are informative only; the database enforces the
decision.

`emergency_stop` is the only one-person command because it only reduces risk.
One AAL2 operator may set `enabled=false` and increment `control_epoch`. Offline
stop attempts are displayed as not sent and are never queued.

## Release gate

Passing unit tests does not approve operations. G1 requires zero imbalance,
fictional sell, duplicate intent/journal, and recovery of every tested crash
point. G2 additionally requires two-person controls, command ACK/postconditions,
alert delivery and human ACK, fencing, independent monitoring, immutable archive
receipt, and the isolated RTO/RPO drill. Any uncertainty keeps the account Paper
disabled.
