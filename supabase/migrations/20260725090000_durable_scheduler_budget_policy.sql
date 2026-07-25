begin;

set local search_path = pg_catalog, pg_temp;
set local lock_timeout = '5s';
set local statement_timeout = '60s';

-- Retry and replay budgets are part of the database authority boundary.  The
-- Worker validates the same policy before every RPC, but a direct trusted SQL
-- caller must not be able to persist a wider budget.
do $durable_scheduler_budget_policy_dependency$
begin
  if pg_catalog.to_regclass('private.scheduler_job_definitions') is null then
    raise exception 'durable_scheduler_budget_policy_dependency_missing'
      using errcode = '55000';
  end if;
end;
$durable_scheduler_budget_policy_dependency$;

lock table private.scheduler_job_definitions in access exclusive mode;

do $durable_scheduler_budget_policy_preflight$
begin
  if exists (
    select 1
    from private.scheduler_job_definitions as definition
    where (
      (
        definition.job_key in (
          'operations.execution',
          'operations.settlement'
        )
        and definition.max_attempts = 1
        and definition.max_manual_replays = 0
      )
      or
      (
        definition.job_key in (
          'operations.commands',
          'operations.reconciliation',
          'operations.outbox'
        )
        and definition.max_attempts between 1 and 3
        and definition.max_manual_replays between 0 and 1
      )
    ) is not true
  ) then
    raise exception 'durable_scheduler_budget_policy_existing_rows_invalid'
      using errcode = '23514';
  end if;
end;
$durable_scheduler_budget_policy_preflight$;

alter table private.scheduler_job_definitions
  add constraint scheduler_job_definitions_job_budget_v1_check
  check ((
    (
      job_key in (
        'operations.execution',
        'operations.settlement'
      )
      and max_attempts = 1
      and max_manual_replays = 0
    )
    or
    (
      job_key in (
        'operations.commands',
        'operations.reconciliation',
        'operations.outbox'
      )
      and max_attempts between 1 and 3
      and max_manual_replays between 0 and 1
    )
  ) is true) not valid;

alter table private.scheduler_job_definitions
  validate constraint scheduler_job_definitions_job_budget_v1_check;

do $durable_scheduler_budget_policy_postcondition$
begin
  if not exists (
    select 1
    from pg_catalog.pg_constraint as constraint_record
    where constraint_record.conrelid =
          'private.scheduler_job_definitions'::pg_catalog.regclass
      and constraint_record.conname =
          'scheduler_job_definitions_job_budget_v1_check'
      and constraint_record.contype = 'c'
      and constraint_record.convalidated
  ) then
    raise exception 'durable_scheduler_budget_policy_not_validated'
      using errcode = '23514';
  end if;
end;
$durable_scheduler_budget_policy_postcondition$;

commit;
