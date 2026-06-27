# Supabase Setup

1. Create Supabase project.
2. Enable Auth email provider suitable for one admin user.
3. Run migrations in order.
4. Run `seed.sql`.
5. Insert admin user into `user_roles`.
6. Use publishable key in desktop.
7. Use secret key only in Render worker env vars.
8. Verify RLS with non-admin authenticated user.
9. Enable Realtime only for lightweight control/status tables.

Desktop must not use service role or secret key.

