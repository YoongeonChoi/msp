create or replace function public.get_audit_log_summaries(row_limit integer default 100)
returns table (
  id uuid,
  action text,
  target_table text,
  target_id text,
  changed_fields text[],
  created_at timestamptz
)
language plpgsql
stable
-- Required to derive field names after authenticated raw-table access is revoked.
security definer
set search_path = ''
as $$
begin
  if not (select public.is_admin()) then
    raise exception 'admin_role_required'
      using errcode = '42501';
  end if;

  return query
  select
    audit.id,
    audit.action,
    audit.target_table,
    audit.target_id,
    coalesce(changed.changed_fields, array[]::text[]) as changed_fields,
    audit.created_at
  from public.audit_logs as audit
  left join lateral (
    select array_agg(candidate.field_name order by candidate.field_name) as changed_fields
    from (
      select jsonb_object_keys(coalesce(audit.before_snapshot, '{}'::jsonb)) as field_name
      union
      select jsonb_object_keys(coalesce(audit.after_snapshot, '{}'::jsonb)) as field_name
    ) as candidate
    where coalesce(audit.before_snapshot, '{}'::jsonb) -> candidate.field_name
      is distinct from coalesce(audit.after_snapshot, '{}'::jsonb) -> candidate.field_name
  ) as changed on true
  order by audit.created_at desc
  limit least(greatest(coalesce(row_limit, 100), 1), 200);
end;
$$;

revoke select on table public.audit_logs from authenticated;

revoke execute on function public.get_audit_log_summaries(integer)
  from public, anon, authenticated, service_role;
grant execute on function public.get_audit_log_summaries(integer)
  to authenticated;
