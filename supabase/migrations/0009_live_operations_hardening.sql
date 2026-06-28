alter table public.manual_commands
  add column if not exists expires_at timestamptz,
  add column if not exists reviewed_by uuid references auth.users(id),
  add column if not exists reviewed_at timestamptz,
  add column if not exists rejection_reason text;

do $$
begin
  if exists (
    select 1
    from pg_constraint
    where conrelid = 'public.orders'::regclass
      and conname = 'orders_status_check'
  ) then
    alter table public.orders
      drop constraint orders_status_check;
  end if;

  if not exists (
    select 1
    from pg_constraint
    where conrelid = 'public.orders'::regclass
      and conname = 'orders_status_check'
  ) then
    alter table public.orders
      add constraint orders_status_check
      check (
        status in (
          'proposed',
          'paper',
          'blocked',
          'sent',
          'partial_filled',
          'filled',
          'canceled',
          'rejected',
          'failed',
          'unknown_requires_manual_check'
        )
      );
  end if;
end $$;

do $$
begin
  if exists (
    select 1
    from pg_constraint
    where conrelid = 'public.manual_commands'::regclass
      and conname = 'manual_commands_command_type_check'
  ) then
    alter table public.manual_commands
      drop constraint manual_commands_command_type_check;
  end if;

  if not exists (
    select 1
    from pg_constraint
    where conrelid = 'public.manual_commands'::regclass
      and conname = 'manual_commands_command_type_check'
  ) then
    alter table public.manual_commands
      add constraint manual_commands_command_type_check
      check (
        command_type in (
          'pause_bot',
          'resume_paper',
          'emergency_stop',
          'approve_candidate',
          'reject_candidate',
          'request_live_enable'
        )
      );
  end if;
end $$;

do $$
begin
  if exists (
    select 1
    from pg_constraint
    where conrelid = 'public.manual_commands'::regclass
      and conname = 'manual_commands_live_enable_request_evidence_check'
  ) then
    alter table public.manual_commands
      drop constraint manual_commands_live_enable_request_evidence_check;
  end if;

  if not exists (
    select 1
    from pg_constraint
    where conrelid = 'public.manual_commands'::regclass
      and conname = 'manual_commands_live_enable_request_evidence_check'
  ) then
    alter table public.manual_commands
      add constraint manual_commands_live_enable_request_evidence_check
      check (
        command_type <> 'request_live_enable'
        or (
          expires_at is not null
          and expires_at > created_at
          and expires_at <= created_at + interval '4 hours'
          and nullif(btrim(payload->>'provider_contract_version'), '') is not null
          and nullif(btrim(payload->>'risk_report_id'), '') is not null
          and nullif(btrim(payload->>'release_version'), '') is not null
        )
      );
  end if;
end $$;

do $$
begin
  if exists (
    select 1
    from pg_constraint
    where conrelid = 'public.manual_commands'::regclass
      and conname = 'manual_commands_live_enable_acceptance_check'
  ) then
    alter table public.manual_commands
      drop constraint manual_commands_live_enable_acceptance_check;
  end if;

  if not exists (
    select 1
    from pg_constraint
    where conrelid = 'public.manual_commands'::regclass
      and conname = 'manual_commands_live_enable_acceptance_check'
  ) then
    alter table public.manual_commands
      add constraint manual_commands_live_enable_acceptance_check
      check (
        command_type <> 'request_live_enable'
        or status <> 'accepted'
        or (
          expires_at is not null
          and requested_by is not null
          and reviewed_by is not null
          and reviewed_at is not null
          and applied_at is null
          and reviewed_by <> requested_by
          and expires_at > reviewed_at
          and nullif(btrim(payload->>'provider_contract_version'), '') is not null
          and nullif(btrim(payload->>'risk_report_id'), '') is not null
          and nullif(btrim(payload->>'release_version'), '') is not null
        )
      );
  end if;
end $$;

do $$
begin
  if exists (
    select 1
    from pg_constraint
    where conrelid = 'public.manual_commands'::regclass
      and conname = 'manual_commands_live_enable_applied_check'
  ) then
    alter table public.manual_commands
      drop constraint manual_commands_live_enable_applied_check;
  end if;

  if not exists (
    select 1
    from pg_constraint
    where conrelid = 'public.manual_commands'::regclass
      and conname = 'manual_commands_live_enable_applied_check'
  ) then
    alter table public.manual_commands
      add constraint manual_commands_live_enable_applied_check
      check (
        command_type <> 'request_live_enable'
        or status <> 'applied'
        or (
          expires_at is not null
          and requested_by is not null
          and reviewed_by is not null
          and reviewed_at is not null
          and applied_at is not null
          and reviewed_by <> requested_by
          and expires_at > applied_at
          and applied_at >= reviewed_at
          and nullif(btrim(payload->>'provider_contract_version'), '') is not null
          and nullif(btrim(payload->>'risk_report_id'), '') is not null
          and nullif(btrim(payload->>'release_version'), '') is not null
        )
      );
  end if;
end $$;

create index if not exists idx_manual_commands_live_enable_ready
  on public.manual_commands (expires_at desc, reviewed_at desc)
  where command_type = 'request_live_enable'
    and status = 'accepted';

create index if not exists idx_orders_live_reconciliation
  on public.orders (created_at asc)
  where mode = 'live'
    and status in ('sent', 'partial_filled', 'unknown_requires_manual_check');

create index if not exists idx_orders_live_daily_count
  on public.orders (created_at desc)
  where mode = 'live'
    and status <> 'blocked';

create index if not exists idx_audit_logs_target_created_at
  on public.audit_logs (target_table, target_id, created_at desc);

create or replace function public.audit_table_change()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
declare
  actor uuid;
begin
  actor := auth.uid();

  if tg_op = 'INSERT' then
    insert into public.audit_logs (
      actor_user_id,
      action,
      target_table,
      target_id,
      before_snapshot,
      after_snapshot
    )
    values (
      actor,
      lower(tg_op),
      tg_table_name,
      to_jsonb(new)->>'id',
      null,
      jsonb_strip_nulls(to_jsonb(new))
    );
    return new;
  end if;

  if tg_op = 'UPDATE' then
    insert into public.audit_logs (
      actor_user_id,
      action,
      target_table,
      target_id,
      before_snapshot,
      after_snapshot
    )
    values (
      actor,
      lower(tg_op),
      tg_table_name,
      coalesce(to_jsonb(new)->>'id', to_jsonb(old)->>'id'),
      jsonb_strip_nulls(to_jsonb(old)),
      jsonb_strip_nulls(to_jsonb(new))
    );
    return new;
  end if;

  insert into public.audit_logs (
    actor_user_id,
    action,
    target_table,
    target_id,
    before_snapshot,
    after_snapshot
  )
  values (
    actor,
    lower(tg_op),
    tg_table_name,
    to_jsonb(old)->>'id',
    jsonb_strip_nulls(to_jsonb(old)),
    null
  );
  return old;
end;
$$;

create or replace function public.guard_manual_command_live_enable_review()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
declare
  actor uuid;
begin
  if new.command_type = 'request_live_enable' and tg_op = 'INSERT' then
    actor := auth.uid();

    if actor is null then
      raise exception 'live_enable_request_requires_authenticated_actor'
        using errcode = '23514';
    end if;

    if new.status <> 'pending' then
      raise exception 'live_enable_request_must_start_pending'
        using errcode = '23514';
    end if;

    if new.expires_at is null
       or new.expires_at < now() + interval '5 minutes'
       or new.expires_at > now() + interval '4 hours' then
      raise exception 'live_enable_request_expiry_out_of_range'
        using errcode = '23514';
    end if;

    if nullif(btrim(new.payload->>'provider_contract_version'), '') is null
       or nullif(btrim(new.payload->>'risk_report_id'), '') is null
       or nullif(btrim(new.payload->>'release_version'), '') is null then
      raise exception 'live_enable_request_requires_evidence_payload'
        using errcode = '23514';
    end if;

    new.requested_by := actor;
    new.reviewed_by := null;
    new.reviewed_at := null;
    new.rejection_reason := null;
    new.applied_at := null;
  end if;

  if tg_op = 'UPDATE' and old.command_type = 'request_live_enable' then
    if new.command_type <> old.command_type then
      raise exception 'live_enable_command_type_immutable'
        using errcode = '23514';
    end if;

    if old.status = 'accepted' and new.status = 'applied' then
      if new.payload is distinct from old.payload
         or new.expires_at is distinct from old.expires_at
         or new.requested_by is distinct from old.requested_by
         or new.reviewed_by is distinct from old.reviewed_by
         or new.reviewed_at is distinct from old.reviewed_at
         or new.rejection_reason is distinct from old.rejection_reason
         or new.created_at is distinct from old.created_at then
        raise exception 'live_enable_accepted_command_apply_fields_immutable'
          using errcode = '23514';
      end if;

      if new.applied_at is null
         or old.reviewed_at is null
         or new.applied_at < old.reviewed_at then
        raise exception 'live_enable_apply_requires_applied_at_after_review'
          using errcode = '23514';
      end if;

      if new.expires_at is null or new.expires_at <= new.applied_at then
        raise exception 'live_enable_apply_requires_fresh_expiry'
          using errcode = '23514';
      end if;

      return new;
    end if;

    if old.status <> 'pending' then
      raise exception 'live_enable_review_is_immutable'
        using errcode = '23514';
    end if;

    if new.payload is distinct from old.payload
       or new.expires_at is distinct from old.expires_at
       or new.requested_by is distinct from old.requested_by
       or new.created_at is distinct from old.created_at
       or new.applied_at is distinct from old.applied_at then
      raise exception 'live_enable_request_evidence_immutable'
        using errcode = '23514';
    end if;

    if new.status = 'pending'
       and (
         new.reviewed_by is distinct from old.reviewed_by
         or new.reviewed_at is distinct from old.reviewed_at
         or new.rejection_reason is distinct from old.rejection_reason
       ) then
      raise exception 'live_enable_review_fields_require_status_transition'
        using errcode = '23514';
    end if;

    if new.status not in ('pending', 'accepted', 'rejected') then
      raise exception 'live_enable_invalid_status_transition'
        using errcode = '23514';
    end if;
  end if;

  if new.command_type = 'request_live_enable'
     and tg_op = 'UPDATE'
     and new.status in ('accepted', 'rejected')
     and new.status is distinct from old.status then
    actor := auth.uid();

    if actor is null then
      raise exception 'manual_command_review_requires_authenticated_actor'
        using errcode = '23514';
    end if;

    if new.requested_by is null then
      raise exception 'live_enable_review_requires_requester'
        using errcode = '23514';
    end if;

    new.reviewed_by := actor;
    new.reviewed_at := now();
    new.applied_at := null;

    if new.status = 'accepted' then
      if new.reviewed_by = new.requested_by then
        raise exception 'live_enable_self_review_forbidden'
          using errcode = '23514';
      end if;

      if new.expires_at is null or new.expires_at <= new.reviewed_at then
        raise exception 'live_enable_review_requires_future_expiry'
          using errcode = '23514';
      end if;

      new.rejection_reason := null;
    else
      if nullif(btrim(new.rejection_reason), '') is null then
        raise exception 'live_enable_rejection_requires_reason'
          using errcode = '23514';
      end if;
    end if;
  end if;

  return new;
end;
$$;

create or replace function public.guard_bot_settings_live_enable()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
declare
  live_enable_command_id uuid;
begin
  if new.live_order_allowed is true
     and coalesce(old.live_order_allowed, false) is false then
    if new.mode <> 'live' then
      raise exception 'live_order_allowed_requires_live_mode'
        using errcode = '23514';
    end if;

    if new.enabled is not true then
      raise exception 'live_order_allowed_requires_enabled_bot'
        using errcode = '23514';
    end if;

    select id
    into live_enable_command_id
      from public.manual_commands
      where command_type = 'request_live_enable'
        and status = 'accepted'
        and expires_at > now()
        and applied_at is null
        and requested_by is not null
        and reviewed_by is not null
        and reviewed_at is not null
        and reviewed_by <> requested_by
        and nullif(btrim(payload->>'provider_contract_version'), '') is not null
        and nullif(btrim(payload->>'risk_report_id'), '') is not null
        and nullif(btrim(payload->>'release_version'), '') is not null
      order by reviewed_at desc
      limit 1
      for update skip locked;

    if live_enable_command_id is null then
      raise exception 'live_order_allowed_requires_fresh_accepted_manual_command'
        using errcode = '23514';
    end if;

    update public.manual_commands
    set status = 'applied',
        applied_at = now()
    where id = live_enable_command_id;
  end if;

  return new;
end;
$$;

drop trigger if exists guard_bot_settings_live_enable on public.bot_settings;
create trigger guard_bot_settings_live_enable
  before update on public.bot_settings
  for each row
  execute function public.guard_bot_settings_live_enable();

drop trigger if exists guard_manual_command_live_enable_review on public.manual_commands;
create trigger guard_manual_command_live_enable_review
  before insert or update on public.manual_commands
  for each row
  execute function public.guard_manual_command_live_enable_review();

drop trigger if exists audit_bot_settings_changes on public.bot_settings;
create trigger audit_bot_settings_changes
  after update on public.bot_settings
  for each row
  execute function public.audit_table_change();

drop trigger if exists audit_watchlist_changes on public.watchlist;
create trigger audit_watchlist_changes
  after insert or update or delete on public.watchlist
  for each row
  execute function public.audit_table_change();

drop trigger if exists audit_strategy_versions_changes on public.strategy_versions;
create trigger audit_strategy_versions_changes
  after insert or update or delete on public.strategy_versions
  for each row
  execute function public.audit_table_change();

drop trigger if exists audit_ai_upgrade_candidates_changes on public.ai_upgrade_candidates;
create trigger audit_ai_upgrade_candidates_changes
  after update on public.ai_upgrade_candidates
  for each row
  execute function public.audit_table_change();

drop trigger if exists audit_manual_commands_changes on public.manual_commands;
create trigger audit_manual_commands_changes
  after insert or update on public.manual_commands
  for each row
  execute function public.audit_table_change();

drop policy if exists bot_settings_admin_read on public.bot_settings;
create policy bot_settings_admin_read
  on public.bot_settings
  for select
  to authenticated
  using ((select public.is_admin()));

drop policy if exists bot_settings_admin_update on public.bot_settings;
create policy bot_settings_admin_update
  on public.bot_settings
  for update
  to authenticated
  using ((select public.is_admin()))
  with check ((select public.is_admin()));

drop policy if exists manual_commands_admin_read on public.manual_commands;
create policy manual_commands_admin_read
  on public.manual_commands
  for select
  to authenticated
  using ((select public.is_admin()));

drop policy if exists manual_commands_admin_insert on public.manual_commands;
create policy manual_commands_admin_insert
  on public.manual_commands
  for insert
  to authenticated
  with check ((select public.is_admin()));

drop policy if exists manual_commands_admin_update on public.manual_commands;
create policy manual_commands_admin_update
  on public.manual_commands
  for update
  to authenticated
  using ((select public.is_admin()))
  with check ((select public.is_admin()));

drop policy if exists audit_logs_admin_read on public.audit_logs;
create policy audit_logs_admin_read
  on public.audit_logs
  for select
  to authenticated
  using ((select public.is_admin()));
