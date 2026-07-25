# Engine

## Supported execution environments

The G1+G2 engine accepts only `paper` and local `contract_test`.
`ExecutionEnvironment` has no Live value. Real Toss adapters may read account,
quote, calendar, bar, and status data, but cannot create, cancel, or modify an
order. Startup fails if a production order endpoint or order-capable credential
is detected.

## Cycle and execution flow

1. Load account, execution control, active strategy, execution policy, cost
   schedule, release, and market evidence.
2. Acquire or renew the account lease and retain its monotonic fencing token.
3. Persist the decision and run `RiskService` without bypass.
4. Build the semantic key from account, environment, strategy, symbol, side,
   signal validity window, and execution-policy version.
5. Call `reserve_order_intent`. The DB atomically rechecks control epoch,
   versions, enabled state, semantic uniqueness, available cash/quantity, and
   fencing token, then reserves resources.
6. Immediately before adapter dispatch, call `mark_dispatch_started` to recheck
   the current epoch and fencing token.
7. `ExecutionService` calls the selected Paper or local contract-test execution
   adapter. No other service may call an execution create operation.
8. Record each immutable observation. The DB transaction validates provider
   identity and monotonic cumulative fill/state, appends the order event,
   creates balanced accounting postings and position movement, updates
   projections, and enqueues alerts.
9. Reconciliation resumes durable in-flight states after restart. Unknown and
   quarantined states block resend and require two-person review.
10. Renew/release the lease and record heartbeat/checkpoint metrics.

Only one scheduler may dispatch. A warm standby remains passive until the
fencing qualification gate passes. Outbox dispatch is independently parallel
and does not execute trades.

The required post-`0024` engine/database tail is
`20260714154520_control_qualification_workflow.sql`,
`20260714155117_paper_execution_source.sql`,
`20260714155744_cash_settlement_maturity.sql`,
`20260714160105_operations_runtime_scheduler.sql`,
`20260714161511_unknown_execution_resolution_v2.sql`, and
`20260714165910_unknown_resolution_desktop_projection.sql`, followed by
`20260715020752_kst_trading_date_convergence.sql`. The durable cadence boundary
is established by `20260724210000_durable_operations_scheduler.sql`, corrected
by `20260724234500_durable_scheduler_conflict_target.sql`, and capped at the
database boundary by `20260725090000_durable_scheduler_budget_policy.sql` after
the intervening evidence, calendar, and Desktop projection migrations
documented in [Supabase Setup](SUPABASE_SETUP.md).

## Paper execution v1

- KRW, whole-share, `LIMIT`, `DAY`, buy/sell only.
- First eligible bar is the first complete one-minute bar after the decision.
- Maximum participation is 1% of bar volume.
- Slippage is 10 bps in the adverse direction and never violates the limit.
- Partial fills and residual expiry are supported.
- Buy cash and sell quantity are reserved before dispatch.
- Commission, tax, settlement, tick rule, bar volume, and corporate-action state
  require valid, versioned evidence; missing evidence blocks the fill.
- Cost basis is `moving_weighted_average_v1`.
- Duplicate fill/observation replay is an accounting no-op.

Short, margin, market, IOC/FOK, and modify are not supported.

## Paper source input boundary

The normal operations scheduler consumes durable Paper candidates; it does not
derive a strategy signal or fetch/generate the required evidence batch. One
strict `schema_version=1` source artifact can be published with
`app.tools.publish_paper_execution_source_once` only when
`EXECUTION_V2_PAPER_SOURCE_INPUT_ENABLED=true` is explicitly set. The flag is
false by default.

The caller supplies the independently computed input SHA-256. The tool accepts
only an immutable `local_fixture` or `allowed_read_evidence` bar batch paired
with its exact candidate and calls the two allowlisted Worker RPCs for fixture
ingest and candidate enqueue. The DB rechecks the active account, release,
lease/fencing token, `control_epoch`, qualification, policy versions, and
evidence hashes. Hash equality proves file identity, not market-data provenance
or approval, so this path is not an automatic strategy/data producer.

## Cash settlement stage

Fill-time position and PnL accounting remains on trade date. The cash leg is
reclassified to payable/receivable clearing and appears as pending debit or
credit until the approved schedule reaches its Korean market settlement date.
Due checks use `Asia/Seoul`, while instants remain timezone-aware UTC values.
Reservation, candidate eligibility/expiry, Unknown fill, and qualification
calendar comparisons use the same explicit market date, including the
00:00-08:59 KST interval while the UTC calendar date is still the prior day.

The settlement runner lists/claims a bounded due batch under the current
lease/release/fencing token, completes each obligation atomically, and records
typed retry/dead-letter outcomes. It retries an ambiguous completion only by
checking the exact deterministic post-state; blind duplicate mutation is not
allowed. A future receivable does not increase settled cash.

## Unknown V2 recovery stage

Unknown V1 remains evidence-only. After a distinct operator and risk approver
complete the V2 review, reconciliation runs the dedicated
`list_unknown_resolution_v2` → `claim_unknown_resolution_v2` →
`apply_unknown_resolution_v2` protocol before the generic scanner. Candidate,
claim, and receipt are bound to request/review hashes, command/work revisions,
terminal status, current `control_epoch`, release, lease, and fencing token.

Generic command ACK cannot apply an Unknown V2 resolution. A lost apply response
permits one exact post-state replay check; it never causes a blind resend or a
second accounting mutation.

## Independent operations scheduler

Continuous operations use exactly five fixed durable definitions: command,
execution, settlement, reconciliation, and outbox. PostgreSQL time owns every
due, retry, and expiry decision. Process sleep is only bounded polling
backpressure; it is never the cadence source of truth. One definition can have
at most one pending, leased, or retry-wait run, and each inner lease is capped
by the current account-level outer lease and bound to its holder, fencing token,
and release SHA.

Startup first calls the typed definition-convergence boundary. `converged`
means the requested digest is installed and no active run remains; `claimed`
can lease only an already persisted command, reconciliation, or outbox recovery
run; `wait` carries a database-clock eligibility instant; and
`manual_resolution` leaves uncertain effects blocked. Convergence never creates
a cadence run and never auto-claims execution or settlement. Only after the
safe definitions converge may the normal claim path create a due run.

A command result is successful only when both `failed=0` and
`unacknowledged=0`. New execution additionally requires that command success in
the current outer fencing generation and enabled, ready settlement and
reconciliation recovery planes with no expired lease awaiting classification.
A failed command therefore blocks new execution. Settlement still matures
already captured obligations, while reconciliation and outbox continue recovery
and delivery progress; they are not treated as permission to create a new
order.

Scheduler-level automatic retry is limited to the exact allowlisted transient
reason for command, reconciliation, or outbox polling. Execution and settlement
are never automatically retried. If either lease expires, the run becomes an
immutable dead letter and generic replay remains forbidden until a separate,
evidence-backed resolution contract is approved. Eligible non-effectful replay
creates a new child run under an exact source revision/digest/reason/generation
compare-and-swap and never mutates its source.

## State and recovery rules

The engine persists intent, attempt/dispatch, observation, event, accounting,
projection, outbox, and audit state. It never relies on process memory as the
source of truth.

- Cumulative fill cannot decrease.
- Terminal state cannot regress.
- Provider/simulator identity cannot change for an intent.
- Debit and credit totals must match for every accounting transaction.
- Cash, reserved cash, available sell quantity, and position quantity cannot be
  negative.
- A crash at reserve, dispatch, response, or commit resumes from durable state;
  it does not create a new semantic intent.
- Reconciliation uses keyset pagination and priority so an old page does not
  starve later orders.

Legacy `public.orders` and `public.positions` remain evidence only and do not
participate in the V2 engine. `paper-primary` receives one 10,000,000 KRW
opening journal; it is never reset per cycle. `contract-test-primary` is isolated
from Paper accounting.

## Local commands

```bash
cd apps/worker
python -m pytest
python -m ruff check app
python -m mypy app
```

Use `MOCK_PROVIDERS=true RUN_ONCE=true python -m app.main` only for the documented
safe local cycle. Contract-test fault injection must remain local and must assert
zero requests to the production order host.

To produce the local contract qualification manifest without network order
transport:

```bash
cd apps/worker
python -m app.tools.run_contract_qualification_once
```

The suite is pinned to the recorded OpenAPI SHA-256 and covers create replay,
partial/terminal status, cancel, and fault injection. Its
`production_order_network_zero` check must report `request_count=0`. The
manifest is local evidence only; it is not an official sandbox result and does
not by itself register or finalize a release qualification.
