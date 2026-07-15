# Context Map

- **Trading** owns cycles, decisions, strategy-version use, and the
  `paper`/`contract_test` boundary.
- **MarketData** owns quote/bar/calendar/tick/corporate-action evidence and
  freshness; missing evidence blocks fills.
- **Fundamentals** owns OpenDART normalization and remains outside direct
  execution authority.
- **NewsIntel** owns Naver ingestion and OpenAI classification; model output has
  no execution or strategy-promotion authority.
- **PortfolioAccounting** owns the persistent account, balanced transaction and
  posting ledger, cash/position projections, reservations, snapshots, and
  reconciliation.
- **Risk** owns policy aggregation in `RiskService`; the DB repeats invariant
  checks during reserve and dispatch.
- **Execution** owns semantic intents, lease/fencing, deterministic Paper or
  local contract-test dispatch, immutable observations, and crash recovery.
- **Operations** owns commands/ACK/postconditions, heartbeat, outbox, incidents,
  dead-man monitoring, release evidence, DR, and runbooks.
- **IdentityAccess** owns Supabase Auth TOTP, seven roles, five-minute
  hash-bound step-up grants, maker/checker review, and access-change workflows.
- **AuditEvidence** owns the append-only hash chain, redacted export, and
  immutable archive receipt.

The Desktop is a control-plane client only. It uses `api` read models/RPCs and
never calls a provider or writes private tables directly. The Worker uses the
fixed `worker_api` RPC surface and never performs public/private table CRUD.

Production order writes are outside every bounded context in this release. A
new requirement reopens G0 rather than extending the current Execution context.
