alter table public.bot_settings replica identity full;
alter table public.worker_heartbeats replica identity full;
alter table public.api_health replica identity full;
alter table public.positions replica identity full;
alter table public.orders replica identity full;
alter table public.decision_snapshots replica identity full;
alter table public.ai_upgrade_candidates replica identity full;
alter table public.engine_events replica identity full;

alter publication supabase_realtime add table public.bot_settings;
alter publication supabase_realtime add table public.worker_heartbeats;
alter publication supabase_realtime add table public.api_health;
alter publication supabase_realtime add table public.positions;
alter publication supabase_realtime add table public.orders;
alter publication supabase_realtime add table public.decision_snapshots;
alter publication supabase_realtime add table public.ai_upgrade_candidates;
alter publication supabase_realtime add table public.engine_events;

