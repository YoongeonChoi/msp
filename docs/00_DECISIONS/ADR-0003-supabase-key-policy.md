# ADR-0003 Supabase Key Policy

Status: accepted

Desktop may use only Supabase publishable key with authenticated user RLS. Worker may use server-side Supabase secret key from Render env vars.

Secret key never goes into desktop, Git, logs, or local screenshots.

