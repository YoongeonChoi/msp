update public.bot_settings
set live_order_allowed = false
where live_order_allowed is true
  and (enabled is not true or mode <> 'live');

update public.bot_settings
set max_daily_order_count = 10
where max_daily_order_count < 1
   or max_daily_order_count > 1000;

update public.bot_settings
set max_order_amount_krw = 100000000
where max_order_amount_krw > 100000000;

alter table public.bot_settings
  drop constraint if exists bot_settings_live_execution_state_check;

alter table public.bot_settings
  add constraint bot_settings_live_execution_state_check
  check (
    live_order_allowed is false
    or (enabled is true and mode = 'live')
  );

alter table public.bot_settings
  drop constraint if exists bot_settings_max_daily_order_count_check;

alter table public.bot_settings
  add constraint bot_settings_max_daily_order_count_check
  check (max_daily_order_count between 1 and 1000);

alter table public.bot_settings
  drop constraint if exists bot_settings_max_order_amount_krw_check;

alter table public.bot_settings
  add constraint bot_settings_max_order_amount_krw_check
  check (max_order_amount_krw between 1 and 100000000);

alter table public.positions
  alter column market_value_krw type bigint using market_value_krw::bigint,
  alter column unrealized_pnl_krw type bigint using unrealized_pnl_krw::bigint;

update public.strategy_versions
set status = 'paper',
    approved_by = null,
    approved_at = null
where status = 'active'
  and (approved_by is null or approved_at is null);

create or replace function public.guard_strategy_version_safety()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
declare
  actor uuid;
  technical_weight numeric;
  fundamental_weight numeric;
  market_sector_weight numeric;
  news_event_weight numeric;
  portfolio_weight numeric;
  buy_threshold numeric;
  sell_threshold numeric;
begin
  if jsonb_typeof(new.weights) <> 'object'
     or not new.weights ?& array[
       'technical',
       'fundamental',
       'market_sector',
       'news_event',
       'portfolio'
     ]
     or new.weights - array[
       'technical',
       'fundamental',
       'market_sector',
       'news_event',
       'portfolio'
     ] <> '{}'::jsonb
     or exists (
       select 1
       from jsonb_each(new.weights) as weight
       where jsonb_typeof(weight.value) <> 'number'
     ) then
    raise exception 'strategy_weights_invalid'
      using errcode = '23514';
  end if;

  technical_weight := (new.weights->>'technical')::numeric;
  fundamental_weight := (new.weights->>'fundamental')::numeric;
  market_sector_weight := (new.weights->>'market_sector')::numeric;
  news_event_weight := (new.weights->>'news_event')::numeric;
  portfolio_weight := (new.weights->>'portfolio')::numeric;
  if technical_weight not between 0 and 1
     or fundamental_weight not between 0 and 1
     or market_sector_weight not between 0 and 1
     or news_event_weight not between 0 and 1
     or portfolio_weight not between 0 and 1
     or abs(
       technical_weight
       + fundamental_weight
       + market_sector_weight
       + news_event_weight
       + portfolio_weight
       - 1
     ) > 0.000001 then
    raise exception 'strategy_weights_invalid'
      using errcode = '23514';
  end if;

  if jsonb_typeof(new.params) <> 'object'
     or not new.params ?& array['buy_threshold', 'sell_threshold']
     or jsonb_typeof(new.params->'buy_threshold') <> 'number'
     or jsonb_typeof(new.params->'sell_threshold') <> 'number' then
    raise exception 'strategy_thresholds_invalid'
      using errcode = '23514';
  end if;
  buy_threshold := (new.params->>'buy_threshold')::numeric;
  sell_threshold := (new.params->>'sell_threshold')::numeric;
  if sell_threshold < 0
     or buy_threshold > 1
     or sell_threshold >= buy_threshold then
    raise exception 'strategy_thresholds_invalid'
      using errcode = '23514';
  end if;

  if tg_op = 'INSERT' and new.status = 'active' then
    raise exception 'strategy_must_not_start_active'
      using errcode = '23514';
  end if;

  if tg_op = 'UPDATE' then
    if old.status <> 'draft'
       and (
         new.weights is distinct from old.weights
         or new.params is distinct from old.params
         or new.strategy_type is distinct from old.strategy_type
         or new.version_name is distinct from old.version_name
       ) then
      raise exception 'promoted_strategy_is_immutable'
        using errcode = '23514';
    end if;

    if new.status is distinct from old.status
       and not (
         (old.status = 'draft' and new.status = 'paper')
         or (old.status = 'paper' and new.status = 'active')
         or (old.status = 'active' and new.status = 'retired')
       ) then
      raise exception 'strategy_status_transition_invalid'
        using errcode = '23514';
    end if;

    if old.status = 'paper' and new.status = 'active' then
      actor := auth.uid();
      if actor is null then
        raise exception 'strategy_activation_requires_authenticated_approver'
          using errcode = '23514';
      end if;
      if new.created_by is not null and new.created_by = actor then
        raise exception 'strategy_self_approval_forbidden'
          using errcode = '23514';
      end if;
      new.approved_by := actor;
      new.approved_at := now();
    elsif new.approved_by is distinct from old.approved_by
       or new.approved_at is distinct from old.approved_at then
      raise exception 'strategy_approval_fields_immutable'
        using errcode = '23514';
    end if;
  end if;

  if new.status = 'active'
     and (new.approved_by is null or new.approved_at is null) then
    raise exception 'active_strategy_requires_approval'
      using errcode = '23514';
  end if;

  return new;
exception
  when invalid_text_representation or numeric_value_out_of_range then
    raise exception 'strategy_numeric_value_invalid'
      using errcode = '23514';
end;
$$;

drop trigger if exists guard_strategy_version_safety on public.strategy_versions;
create trigger guard_strategy_version_safety
  before insert or update on public.strategy_versions
  for each row
  execute function public.guard_strategy_version_safety();

create or replace function public.guard_bot_settings_live_enable()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
declare
  live_enable_command_id uuid;
  old_executable boolean;
  new_executable boolean;
begin
  old_executable := old.enabled is true
    and old.mode = 'live'
    and old.live_order_allowed is true;
  new_executable := new.enabled is true
    and new.mode = 'live'
    and new.live_order_allowed is true;

  if new.live_order_allowed is true and new.mode <> 'live' then
    raise exception 'live_order_allowed_requires_live_mode'
      using errcode = '23514';
  end if;

  if new.live_order_allowed is true and new.enabled is not true then
    raise exception 'live_order_allowed_requires_enabled_bot'
      using errcode = '23514';
  end if;

  if new_executable and not old_executable then
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
      raise exception 'live_execution_requires_fresh_accepted_manual_command'
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
