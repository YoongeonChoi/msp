# Threat Model

## Assets and trust boundaries

Protected assets are Worker/provider credentials, Supabase identity and secret
keys, proprietary account data, order intent and accounting ledgers, execution
policy, command approvals, audit chain, incident/outbox state, release evidence,
and the local desktop session.

Trust boundaries are Desktop → Supabase Auth/Data API, Worker → `worker_api`,
Worker → read-only providers, outbox dispatcher → alert receiver, dead-man
monitor → control plane, CI → repository artifacts, and operators → MFA and
maker/checker review.

| Threat | Category | Primary controls | Residual risk / gate |
| --- | --- | --- | --- |
| Production order call | Safety | startup quarantine, no write adapter, network-denial test, no order credential | external writes require a new G0 review |
| Accidental Live enable | Safety | DB forces Paper and `live_order_allowed=false`; UI has no Live action | migration/config drift must fail CI/startup |
| Duplicate semantic intent | Integrity | SHA-256 semantic key, unique reservation, transaction retry | semantic window/policy definition errors |
| Stale or split-brain Worker | Integrity | lease, monotonic fencing token, `control_epoch` recheck | DB/clock outage stops dispatch |
| Crash between reserve and commit | Reliability | durable state machine, pre-dispatch mark, idempotent observation, reconciliation | unknown response needs manual two-person review |
| Forged/fictional accounting | Integrity | balanced double-entry, immutable fill identity, no legacy backfill | bad cost evidence blocks fills |
| Oversell or negative cash | Safety | atomic reservations and DB constraints | reconciliation remains fail closed |
| Provider status regression | Tampering | cumulative/identity monotonicity checks and quarantine | operator evidence needed to resolve |
| Command spoofing/replay | Spoofing | AAL2, 5-minute hash-bound step-up, CAS version, expiry | compromised enrolled device |
| Self-approval | Elevation | UUID maker/checker constraint and role separation | collusion between two accounts |
| Stale/offline UI mutation | Integrity | source/as-of boundary, disabled mutation, no offline command queue | operator may use another trusted station |
| RLS/GRANT misconfiguration | Elevation | dedicated schemas, explicit grants, negative role matrix, catalog assertions | policy bugs require independent review |
| Audit alteration | Repudiation | append-only trigger, hash chain, external immutable receipt | privileged DB/platform compromise before archive |
| Alert loss/duplication | Availability | transactional outbox, leases, retry/backoff, dead letter, receiver dedupe | third-party outage; dead-man escalation |
| Secret disclosure | Information disclosure | server-only secrets, redaction, no raw payloads, secret scanning | host/operator endpoint compromise |
| Dependency/workflow compromise | Tampering | lockfiles, pinned Actions, audits, least permissions | upstream zero-day |
| Prompt injection | Tampering | strict structured output and no execution authority | poor research recommendation only |
| Database disaster | Availability | restore evidence, Paper-disabled recovery, RTO drill | hosted disaster RPO must be proven externally |

## Abuse cases that must remain impossible

- Desktop or model output calls a broker/order API.
- A Worker without the current fencing token dispatches an order.
- One person requests and approves a risk-increasing command or access change.
- An ACK-less command is shown as completed.
- An unknown provider state is automatically retransmitted or forced terminal.
- Legacy order rows create opening cash, fills, or positions.
- Recovery starts enabled or with an order-capable credential.
