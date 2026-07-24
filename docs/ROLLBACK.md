# Rollback and Forward Recovery

The V2 execution ledger is append-only. After the first V2 journal entry,
database rollback must not delete, rewrite, or reinterpret committed execution
or accounting history. Recovery reduces risk first and then uses a forward fix.

## Immediate risk reduction

1. Submit `emergency_stop` from an online AAL2 operator session.
2. Verify the control-plane receipt separately from the Worker postcondition.
3. Confirm `enabled=false`, `live_order_allowed=false`, and a greater
   `control_epoch` in the runtime projection.
4. Confirm the active Worker has observed the new epoch. The target is 10 seconds
   for observation and 15 seconds for runtime stop confirmation.
5. If the Worker does not confirm, keep the account disabled, revoke its runtime
   credential, and open a critical incident. Do not queue or auto-retry the UI
   command offline.

Legacy control remains fail closed during transition:

```sql
update public.bot_settings
set enabled = false,
    mode = 'paper',
    live_order_allowed = false,
    updated_at = now()
where id = 'singleton';
```

## Application rollback

- Stop the scheduler and preserve the current lease, intent, observation,
  journal, outbox, and audit evidence.
- Roll back Worker/Desktop artifacts only through a manual release-manager
  action while the account remains Paper disabled.
- Start the replacement Worker without production order credentials and require
  a new lease/fencing token before reconciliation.
- Reconcile reserved, dispatched, and unknown intents before resuming Paper.
- Resume only through a new maker/checker command and verified runtime
  postcondition.

## Database recovery

- If the PG17 pgcrypto preflight transaction fails, do not start migrations; it
  rolls back its relocation. Preserve the sanitized error and catalog receipt
  for review.
- If preflight succeeds but no migration has applied, keep the project isolated
  and use only an approved inverse operation or recreate disposable staging.
  Do not improvise an extension move.
- If a separate runner session violates the approved identity or `public`-first
  path after preflight has staged pgcrypto in `public`, stop the runner and keep
  the external deployment mutex held while evidence is captured. Treat this as
  a failed migration boundary; do not move the extension manually.
- After any migration applies, do not move pgcrypto back manually. Keep Paper
  disabled and use a reviewed forward fix or recreate isolated staging.
- Treat preflight execution during active traffic or against an ambiguous
  migration ledger as an incident. Do not auto-repair migration history.
- Prefer an additive migration or compensating journal entry.
- Never reverse an applied migration by dropping V2 ledger or audit objects in
  the operating database.
- Never synthesize fills or cash from `legacy_unreconciled` rows.
- Restore to an isolated environment that starts `enabled=false`, Paper-only,
  with no provider order credential or production order route.
- Verify audit hash continuity, debit=credit, non-negative reserves/positions,
  latest checkpoint, and outbox state before any cutover.

## Release decision

An application rollback does not by itself authorize resume. Two different
users must approve the forward operating state. If the restore cannot prove the
committed ledger recovery point or complete in 30 minutes during market hours,
G2 remains closed and the incident stays open.

GitHub Actions must not deploy, roll back, enable Paper, or change trading
controls automatically.
