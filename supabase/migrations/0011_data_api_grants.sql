revoke all on schema public from public, anon, authenticated, service_role;
revoke all on all tables in schema public from public, anon, authenticated, service_role;
revoke all on all sequences in schema public from public, anon, authenticated, service_role;
revoke execute on all functions in schema public from public, anon, authenticated, service_role;

alter default privileges in schema public
  revoke select, insert, update, delete on tables from public, anon, authenticated, service_role;

alter default privileges in schema public
  revoke usage, select, update on sequences from public, anon, authenticated, service_role;

alter default privileges in schema public
  revoke execute on functions from public, anon, authenticated, service_role;

grant usage on schema public to authenticated, service_role;

grant select on table
  public.user_roles,
  public.bot_settings,
  public.sector_rules,
  public.watchlist,
  public.positions,
  public.strategy_versions,
  public.decision_snapshots,
  public.orders,
  public.outcomes,
  public.features_daily,
  public.fundamentals_quarterly,
  public.account_mapping,
  public.news_events,
  public.ai_upgrade_candidates,
  public.api_health,
  public.worker_heartbeats,
  public.engine_events,
  public.audit_logs,
  public.outbox_events,
  public.manual_commands,
  public.provider_rate_limits,
  public.retention_runs,
  public.release_versions,
  public.backtest_runs
to authenticated;

grant insert, update, delete on table
  public.sector_rules,
  public.watchlist
to authenticated;

grant insert on table
  public.manual_commands
to authenticated;

grant update on table
  public.bot_settings,
  public.manual_commands,
  public.strategy_versions,
  public.ai_upgrade_candidates
to authenticated;

grant select, insert, update, delete on all tables in schema public to service_role;
grant usage, select, update on all sequences in schema public to service_role;

grant execute on function public.is_admin() to authenticated, service_role;
grant execute on function public.database_size_bytes() to service_role;
grant execute on function public.run_retention_cleanup(boolean) to service_role;
