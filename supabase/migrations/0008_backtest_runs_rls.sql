do $$
begin
  if to_regclass('public.backtest_runs') is not null then
    alter table public.backtest_runs enable row level security;

    if not exists (
      select 1
      from pg_policies
      where schemaname = 'public'
        and tablename = 'backtest_runs'
        and policyname = 'backtest_runs_admin_read'
    ) then
      create policy backtest_runs_admin_read
        on public.backtest_runs
        for select
        to authenticated
        using (public.is_admin());
    end if;
  end if;
end $$;
