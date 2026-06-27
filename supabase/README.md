# Supabase

SQL migration 순서:

1. `0001_schema.sql`
2. `0002_rls.sql`
3. `0003_realtime.sql`
4. `0004_retention.sql`
5. `seed.sql`

Desktop은 authenticated user와 publishable key만 사용합니다. Worker만 server-side secret key를 사용합니다.

검증:

```bash
for f in supabase/migrations/*.sql; do echo "$f"; done
rg "enable row level security" supabase/migrations/0002_rls.sql
```

