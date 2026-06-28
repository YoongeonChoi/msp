create or replace function public.is_admin()
returns boolean
language sql
security definer
set search_path = ''
as $$
  select exists (
    select 1
    from public.user_roles
    where user_id = (select auth.uid())
      and role = 'admin'
  );
$$;

revoke execute on function public.is_admin() from public, anon;
grant execute on function public.is_admin() to authenticated, service_role;

create or replace function public.database_size_bytes()
returns bigint
language sql
security definer
set search_path = ''
as $$
  select pg_database_size(current_database());
$$;

revoke execute on function public.database_size_bytes() from public, anon, authenticated;
grant execute on function public.database_size_bytes() to service_role;

create or replace function public.run_retention_cleanup(dry_run boolean default true)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  result jsonb := '{}'::jsonb;
  affected integer := 0;
begin
  if dry_run then
    select count(*) into affected from public.worker_heartbeats where created_at < now() - interval '7 days';
  else
    delete from public.worker_heartbeats where created_at < now() - interval '7 days';
    get diagnostics affected = row_count;
  end if;
  result := result || jsonb_build_object('worker_heartbeats', affected);

  if dry_run then
    select count(*) into affected from public.api_health where checked_at < now() - interval '30 days';
  else
    delete from public.api_health where checked_at < now() - interval '30 days';
    get diagnostics affected = row_count;
  end if;
  result := result || jsonb_build_object('api_health', affected);

  if dry_run then
    select count(*) into affected from public.engine_events where level in ('debug', 'info') and created_at < now() - interval '30 days';
  else
    delete from public.engine_events where level in ('debug', 'info') and created_at < now() - interval '30 days';
    get diagnostics affected = row_count;
  end if;
  result := result || jsonb_build_object('engine_events_debug_info', affected);

  if dry_run then
    select count(*) into affected from public.news_events where linked_decision_id is null and created_at < now() - interval '180 days';
  else
    delete from public.news_events where linked_decision_id is null and created_at < now() - interval '180 days';
    get diagnostics affected = row_count;
  end if;
  result := result || jsonb_build_object('news_events_unlinked', affected);

  insert into public.retention_runs (dry_run, deleted_counts, db_size_bytes, finished_at)
  values (dry_run, result, public.database_size_bytes(), now());

  return result;
end;
$$;

revoke execute on function public.run_retention_cleanup(boolean) from public, anon, authenticated;
grant execute on function public.run_retention_cleanup(boolean) to service_role;
