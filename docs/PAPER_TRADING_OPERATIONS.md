# Paper Trading Operations

Paper V2 is a persistent execution/accounting environment, not a stateless mock.
It has one `paper-primary` account, one 10,000,000 KRW opening journal, durable
cash/position reserves, deterministic fills, and restart reconciliation.

The required Paper V2 migration tail after `0024` is:

1. `20260714154520_control_qualification_workflow.sql`
2. `20260714155117_paper_execution_source.sql`
3. `20260714155744_cash_settlement_maturity.sql`
4. `20260714160105_operations_runtime_scheduler.sql`
5. `20260714161511_unknown_execution_resolution_v2.sql`
6. `20260714165910_unknown_resolution_desktop_projection.sql`
7. `20260715020752_kst_trading_date_convergence.sql`

## Before resume

1. Confirm `PAPER`, `LIVE 금지`, expected release SHA, fresh heartbeat, valid
   lease/fencing token, current `control_epoch`, and ledger checkpoint.
2. Confirm execution/cost policy versions are approved and within their
   effective windows, with matching evidence SHA-256.
3. Confirm quote, complete one-minute bar, market calendar, tick rule, bar
   volume, and corporate-action evidence are present and fresh.
4. Confirm debit=credit, non-negative cash/reserves/positions, no duplicate
   source IDs, and no open unknown/quarantine case.
5. Confirm outbox age/dead-letter and critical incident ACK state are healthy.
6. Have an operator request resume and a different risk approver approve it with
   a fresh command-bound step-up grant.
7. Wait for `applied` and the runtime postcondition. `requested`, `approved`, or
   `claimed` is not success.

## Fill interpretation

The v1 policy supports KRW whole-share `LIMIT` `DAY` buy/sell only. The first
complete one-minute bar after the decision is eligible. Participation is capped
at 1% of volume and price applies 10 bps adverse slippage without crossing the
limit. Partial fill and residual expiry are expected states.

Buy cash and sell quantity are reserved before dispatch. On partial fill the
filled portion is consumed and the remainder stays reserved; cancel/expiry
releases only the remaining reserve. A duplicate observation/fill changes no
ledger or projection state.

## Explicit source publication

The Worker scheduler does not fabricate bars or strategy candidates. To publish
one reviewed `local_fixture` or `allowed_read_evidence` artifact, explicitly
enable the normally disabled input gate and pin the exact file bytes:

```powershell
cd apps/worker
$env:EXECUTION_V2_ENABLED="true"
$env:EXECUTION_V2_WORKER_API_ENABLED="true"
$env:EXECUTION_V2_PAPER_SOURCE_INPUT_ENABLED="true"
$env:EXECUTION_V2_ENVIRONMENT="paper"
$env:EXECUTION_V2_ACCOUNT_ID="paper-primary"
$env:EXECUTION_V2_WORKER_ID="<active-lease-holder-uuid>"
$sourceSha = (Get-FileHash -Algorithm SHA256 .\paper-source.json).Hash.ToLowerInvariant()
py -m app.tools.publish_paper_execution_source_once `
  --input .\paper-source.json `
  --input-sha256 $sourceSha
```

The JSON must contain the strict fixture/candidate pair, current fencing token,
and `control_epoch`. The DB rechecks release, lease, qualification, policy/cost,
calendar, tick, volume, and corporate-action evidence. The hash proves byte
identity only. This operator-triggered publisher is not an automatic strategy
producer or an approval step.

## Cash settlement interpretation

Position quantity, moving-weighted-average cost, and realized PnL are recognized
on trade date. Cash is reclassified through settlement payable/receivable and
shown as pending debit/credit until the schedule's `settlement_date` is due in
`Asia/Seoul`. A future sell receivable is not settled cash and cannot be treated
as such in the status rail.

Reservation, Paper candidate eligibility/expiry, Unknown fill evidence, and
qualification windows use that same explicit Korean market date. Do not derive
those dates with a session-timezone `timestamptz::date` cast, especially between
00:00 and 08:59 KST.

Settlement is a separate durable scheduler stage with claim, complete,
retry/backoff, exact replay, and dead-letter evidence. Investigate any stale due
obligation, projection conflict, retry exhaustion, or cash snapshot mismatch
before resume.

## Investigation rules

- Do not edit a ledger, projection, reserve, intent, observation, or audit row.
- Do not resend unknown intent or force it terminal. V1 unknown resolution is
  evidence-only.
- Preserve evidence and use the V2 operator request plus distinct risk approver
  review. The Desktop reads `get_unknown_resolution_cases_v2`; the Worker alone
  performs dedicated `list/claim/apply` under command/work revision,
  `control_epoch`, release, lease, and fencing-token CAS.
- Do not use generic operation-command ACK to close an Unknown V2 case. Apply
  only the reviewed fill manifest or verified no-fill terminal result; exact
  replay must not create a second fill, journal, or settlement obligation.
- Never include `legacy_unreconciled` orders or positions in V2 balance/PnL.
- Emergency stop control-plane receipt and Worker stop confirmation are separate.

## Local validation

```bash
cd apps/worker
python -m pytest
python -m ruff check app
python -m mypy app
python -m app.tools.run_contract_qualification_once
cd ../..
python supabase/verify_g1_g2_migration.py
```

The contract command uses only the hash-pinned local simulator and must report
zero production-order network requests. It is not an official sandbox result or
a completed DB qualification approval.

Local test success does not complete G1/G2. Final approval also needs the
named G0 responsible people and two operating users, explicit Hosted Staging
approval/credentials, a 24-hour fault soak, ten consecutive Paper/Shadow trading
days, external alert ACK/archive evidence, monitoring in a separate failure
domain, hosted committed-ledger RPO proof, and an isolated restore drill within
30 minutes. See [Operations Runbook](RUNBOOK.md). None of those external gates
is completed by the local commands above.
