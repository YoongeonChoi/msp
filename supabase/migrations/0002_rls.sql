create or replace function public.is_admin()
returns boolean
language sql
security definer
set search_path = public
as $$
  select exists (
    select 1
    from public.user_roles
    where user_id = auth.uid()
      and role = 'admin'
  );
$$;

alter table public.user_roles enable row level security;
alter table public.bot_settings enable row level security;
alter table public.sector_rules enable row level security;
alter table public.watchlist enable row level security;
alter table public.positions enable row level security;
alter table public.orders enable row level security;
alter table public.decision_snapshots enable row level security;
alter table public.outcomes enable row level security;
alter table public.features_daily enable row level security;
alter table public.fundamentals_quarterly enable row level security;
alter table public.account_mapping enable row level security;
alter table public.news_events enable row level security;
alter table public.strategy_versions enable row level security;
alter table public.ai_upgrade_candidates enable row level security;
alter table public.api_health enable row level security;
alter table public.worker_heartbeats enable row level security;
alter table public.engine_events enable row level security;
alter table public.audit_logs enable row level security;
alter table public.outbox_events enable row level security;
alter table public.manual_commands enable row level security;
alter table public.provider_rate_limits enable row level security;
alter table public.retention_runs enable row level security;
alter table public.release_versions enable row level security;

create policy user_roles_admin_read on public.user_roles for select to authenticated using (public.is_admin());
create policy bot_settings_admin_read on public.bot_settings for select to authenticated using (public.is_admin());
create policy sector_rules_admin_read on public.sector_rules for select to authenticated using (public.is_admin());
create policy watchlist_admin_read on public.watchlist for select to authenticated using (public.is_admin());
create policy positions_admin_read on public.positions for select to authenticated using (public.is_admin());
create policy orders_admin_read on public.orders for select to authenticated using (public.is_admin());
create policy decision_snapshots_admin_read on public.decision_snapshots for select to authenticated using (public.is_admin());
create policy outcomes_admin_read on public.outcomes for select to authenticated using (public.is_admin());
create policy features_daily_admin_read on public.features_daily for select to authenticated using (public.is_admin());
create policy fundamentals_quarterly_admin_read on public.fundamentals_quarterly for select to authenticated using (public.is_admin());
create policy account_mapping_admin_read on public.account_mapping for select to authenticated using (public.is_admin());
create policy news_events_admin_read on public.news_events for select to authenticated using (public.is_admin());
create policy strategy_versions_admin_read on public.strategy_versions for select to authenticated using (public.is_admin());
create policy ai_upgrade_candidates_admin_read on public.ai_upgrade_candidates for select to authenticated using (public.is_admin());
create policy api_health_admin_read on public.api_health for select to authenticated using (public.is_admin());
create policy worker_heartbeats_admin_read on public.worker_heartbeats for select to authenticated using (public.is_admin());
create policy engine_events_admin_read on public.engine_events for select to authenticated using (public.is_admin());
create policy audit_logs_admin_read on public.audit_logs for select to authenticated using (public.is_admin());
create policy outbox_events_admin_read on public.outbox_events for select to authenticated using (public.is_admin());
create policy manual_commands_admin_read on public.manual_commands for select to authenticated using (public.is_admin());
create policy provider_rate_limits_admin_read on public.provider_rate_limits for select to authenticated using (public.is_admin());
create policy retention_runs_admin_read on public.retention_runs for select to authenticated using (public.is_admin());
create policy release_versions_admin_read on public.release_versions for select to authenticated using (public.is_admin());

create policy bot_settings_admin_update on public.bot_settings for update to authenticated using (public.is_admin()) with check (public.is_admin());
create policy sector_rules_admin_write on public.sector_rules for all to authenticated using (public.is_admin()) with check (public.is_admin());
create policy watchlist_admin_write on public.watchlist for all to authenticated using (public.is_admin()) with check (public.is_admin());
create policy manual_commands_admin_insert on public.manual_commands for insert to authenticated with check (public.is_admin());
create policy manual_commands_admin_update on public.manual_commands for update to authenticated using (public.is_admin()) with check (public.is_admin());
create policy strategy_versions_admin_update on public.strategy_versions for update to authenticated using (public.is_admin()) with check (public.is_admin());
create policy ai_upgrade_candidates_admin_update on public.ai_upgrade_candidates for update to authenticated using (public.is_admin()) with check (public.is_admin());

