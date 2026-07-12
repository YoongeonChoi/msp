# Supabase

SQL migration 순서:

1. `0001_schema.sql`
2. `0002_rls.sql`
3. `0003_realtime.sql`
4. `0004_retention.sql`
5. `0005_schema_alignment.sql`
6. `0006_outcome_tracking.sql`
7. `0007_backtest_runs.sql`
8. `0008_backtest_runs_rls.sql`
9. `0009_live_operations_hardening.sql`
10. `0010_security_definer_hardening.sql`
11. `0011_data_api_grants.sql`
12. `0012_runtime_safety_invariants.sql`
13. `0013_worker_deployment_lock.sql`
14. `seed.sql`

Desktop은 authenticated user와 publishable key만 사용합니다. Worker만 server-side secret key를 사용합니다.

검증:

```bash
for f in supabase/migrations/*.sql; do echo "$f"; done
rg "enable row level security" supabase/migrations/0002_rls.sql
python supabase/verify_live_enable_migration.py
python supabase/verify_hosted_live_readiness.py
python supabase/verify_hosted_live_enable_flow.py \
  --confirm-staging-project "$SUPABASE_STAGING_PROJECT_REF"
```

로컬 operator 환경에서 값이 ignored env 파일에 나뉘어 있을 때는 명시적으로
병합할 수 있습니다. CLI 인자와 process env가 env file 값보다 우선합니다.

```bash
python supabase/verify_hosted_live_readiness.py --env-file apps/worker/.env --env-file apps/desktop/.env.local
python supabase/verify_hosted_live_enable_flow.py \
  --env-file apps/worker/.env \
  --env-file apps/desktop/.env.local \
  --confirm-staging-project "$SUPABASE_STAGING_PROJECT_REF"
```

`0011_data_api_grants.sql`는 Supabase Data API 노출을 명시적으로 고정합니다.
`anon`/`public` table access를 차단하고, authenticated desktop user에게는 RLS
정책이 허용하는 최소 table 권한만 grant하며, worker `service_role`에는 전체
table/sequence 권한을 grant합니다. 또한 future default privileges를 revoke하여
새 public table/function이 migration 없이 자동 노출되지 않도록 합니다.

`0012_runtime_safety_invariants.sql`는 live 승인 일회성 소비, 일일 주문 수 상한,
position 정수 범위, strategy 승인·승격·불변 조건을 DB에서 강제합니다.
`0013_worker_deployment_lock.sql`는 배포 시작 시 `enabled=false`와
`live_order_allowed=false`를 원자적으로 적용하고, target SHA를 관찰한 fresh/healthy
worker heartbeat가 확인될 때까지 잠금을 유지합니다. 배포 완료 뒤 live를 다시
요청하려면 `deployment_completed_at` 이후의 새 승인이 필요합니다.

`verify_live_enable_migration.py`는 Docker daemon이 실행 중인 환경에서 임시
`postgres:16-alpine` 컨테이너를 만들고, Supabase `auth.uid()`/Realtime 최소 stub,
전체 migration, seed를 적용한 뒤 `request_live_enable` 승인 row가 live enable 시
정확히 한 번 `applied`로 소모되는지, `anon`/`authenticated`가 destructive/read RPC를
직접 실행하지 못하는지, `service_role`만 retention dry-run RPC를 실행할 수 있는지
검증합니다.

`verify_hosted_live_readiness.py`는 실제 hosted/staging Supabase project에 대해
`SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY` 또는 `VITE_SUPABASE_PUBLISHABLE_KEY`,
`SUPABASE_SECRET_KEY`가 설정된 경우에만 실행됩니다. 이 verifier는 PostgREST root,
publishable/anon key의 destructive/read RPC denial, secret/service role key의 RPC
success path, publishable/anon key의 `bot_settings` Data API denial,
secret/service role key의 `bot_settings` Data API select, Realtime WebSocket
handshake를 확인하고 secret 값을 출력하지 않습니다.
`SUPABASE_URL`은 path/query/fragment/credentials가 없는 공식
`https://<project_ref>.supabase.co` project origin이어야 하며,
local/test/private IP/self-hosted/custom mock host는 live-readiness evidence로 인정하지 않습니다.
env가 없으면 `FINAL=SKIP hosted_supabase_env_missing`을 반환합니다.
`--env-file`로 ignored env 파일을 넘기면 verifier가 해당 값을 process env
아래 우선순위로 병합하며, unreadable env file path나 secret/JWT 값은 출력하지
않습니다.

`verify_hosted_live_enable_flow.py`는 실제 hosted/staging Supabase project에 대해
`SUPABASE_LIVE_REQUESTER_JWT`와 `SUPABASE_LIVE_REVIEWER_JWT`가 서로 다른 admin 사용자
세션일 때만 live-enable 사용자 플로우를 검증합니다. 추가로
`SUPABASE_STAGING_PROJECT_REF`, `SUPABASE_PRODUCTION_PROJECT_REF`,
`SUPABASE_LIVE_ENABLE_VERIFICATION_TARGET=staging`을 요구하고, production project를
명시적으로 거부합니다. `--confirm-staging-project`에는 staging project ref를
다시 정확히 입력해야 하며, 이 확인값은 env file에서 암묵적으로 읽지 않습니다.
요청자 JWT로
`request_live_enable`을 만들고, self-review 거부, 다른 reviewer admin의 승인,
`bot_settings.live_order_allowed` 활성화 시 승인 row의 정확히 한 번 `applied` 소모,
새 승인 없는 두 번째 활성화 거부를 확인합니다. 실행 전후로 worker/service role key를
사용해 `bot_settings`를 `enabled=false`, `mode=paper`, `live_order_allowed=false`로
되돌리며 secret/JWT 값을 출력하지 않습니다. mutation 전 최신 worker heartbeat가
2분 이내의 `status=ok`, `details.mock_providers=true`여야 하고, 최대 worker loop
주기와 여유 시간을 포함한 최근 3,720초의 heartbeat가 모두 mock이어야 합니다.
heartbeat가 없거나 stale이거나 최근 real-provider heartbeat가 있으면 즉시 거부합니다.
cleanup은 PostgREST가 정확히 하나의
singleton row를 반환하고 해당 row가 paper-disabled 상태일 때만 성공합니다.
실패 중 생성된 미적용 verifier command는 정확한 ID, status, verifier payload를
다시 확인한 뒤 service role로 삭제하며, 해당 delete는 DB audit log에 남습니다.
`SUPABASE_URL`은 같은 `.supabase.co` project
origin 제약을 통과해야 합니다. env가 없으면
`FINAL=SKIP hosted_live_enable_env_missing`을 반환합니다.
`--env-file` 동작은 hosted readiness verifier와 동일합니다.
