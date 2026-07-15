# Architecture

The system remains a modular monolith Worker plus a Tauri Desktop control cockpit.
G1+G2 adds durable execution and operations boundaries without splitting the codebase into
microservices.

## Runtime roles

- `trading-worker`: bounded cycle, risk evaluation, intent reservation, Paper execution,
  reconciliation
- `alert-dispatcher`: transactional outbox claim, retry, delivery receipt, dead letter
- `warm-standby`: acquires the trading lease only after the previous lease expires
- `external-monitor`: observes heartbeat, lease, outbox age and incident ACK from a separate
  failure domain
- `desktop-cockpit`: read models and operation commands only; never calls a broker

Only one trading-worker may hold the active lease. Horizontal execution is forbidden until
the database fencing tests pass.

## Database boundaries

- `private`: execution, accounting, IAM, commands, audit, incidents and outbox source of truth
- `api`: data-minimized Desktop read models and authenticated operation RPCs
- `worker_api`: service-only lease, reservation, observation, command ACK and outbox RPCs
- `public`: legacy compatibility tables during expand/cutover; direct application access is
  removed at contract cutover

Realtime publishes only `api.control_plane_signal`, a monotonic invalidation row.
Desktop refetches the strict snapshot after a signal; order events, audit payloads,
decision snapshots, postings, and legacy tables never enter the publication.

New source tables use RLS as defense in depth and explicit grants. `anon` receives no
application access. Desktop `authenticated` sessions can reach only the `api` surface.
Worker credentials can execute only the approved `worker_api` surface and never appear in
Desktop configuration.

Existing `public.orders`, `public.positions`, `manual_commands`, and `audit_logs` are retained
as `legacy_unreconciled` evidence. They are not backfilled into synthetic fills or accounting
postings.

## Execution flow

```text
decision + RiskService
  -> DB lease/fencing/control-epoch check
  -> atomic semantic intent and cash/quantity reservation
  -> pre-dispatch control-epoch check
  -> Paper or local contract-test adapter
  -> immutable observation and order event
  -> balanced accounting postings
  -> cash/position read projections
  -> transactional alert/audit outbox
```

Production external order writes are not part of this architecture. Actual Toss integration
is read-only until a separately approved sandbox contract exists.

## Operations flow

```text
Desktop request
  -> strict schema v1 validation
  -> role, AAL2, step-up and CAS checks
  -> distinct reviewer for risk-increasing changes
  -> worker claim/ACK with fencing token
  -> postcondition
  -> Desktop applied state
```

Emergency Stop is the only single-operator exception. Its database transaction disables
execution and increments `control_epoch` immediately; the UI separately reports database
receipt and Worker observation.

## Dependency rules

- `domain` imports no HTTP, Supabase, OpenAI or provider client.
- `application` depends on domain models and ports.
- `adapters` implement provider, simulator and persistence ports.
- `infrastructure` owns runtime lifecycle, observability and delivery mechanisms.
- Legacy `BrokerPort.place_order` remains confined to `ExecutionService` and is
  unconditionally quarantined. V2 create/status/cancel behavior uses its explicit
  Paper/contract-test ports and has no production order transport.
- Desktop has no business, risk or execution authority; UI authorization is never a
  substitute for database enforcement.
