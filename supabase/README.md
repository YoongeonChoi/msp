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
14. `0014_desktop_audit_summary.sql`
15. `0015_paper_order_execution_details.sql`
16. `0016_private_foundation.sql`
17. `0017_execution_accounting_truth.sql`
18. `0018_control_plane_api.sql`
19. `0019_rpc_access_contract.sql`
20. `0020_canonical_operations_contract.sql`
21. `0021_reconciliation_and_cutover.sql`
22. `0022_operational_workflows.sql`
23. `0023_operational_safety_closure.sql`
24. `0024_operational_upgrade_convergence.sql`
25. `20260714154520_control_qualification_workflow.sql`
26. `20260714155117_paper_execution_source.sql`
27. `20260714155744_cash_settlement_maturity.sql`
28. `20260714160105_operations_runtime_scheduler.sql`
29. `20260714161511_unknown_execution_resolution_v2.sql`
30. `20260714165910_unknown_resolution_desktop_projection.sql`
31. `20260715020752_kst_trading_date_convergence.sql`
32. `20260715041903_paper_bar_participation_guard.sql`
33. `20260715041909_operation_claim_fencing.sql`
34. `20260715041912_sell_cost_basis_checkpoint_guard.sql`
35. `20260715041915_paper_evidence_and_sell_reservation_guards.sql`
36. `20260718165749_pgcrypto_schema_convergence.sql`
37. `seed.sql` (로컬 non-live 기본값만)

Desktop은 authenticated user와 publishable key만 사용합니다. Worker만 server-side secret key를 사용합니다.

검증:

```bash
for f in supabase/migrations/*.sql; do echo "$f"; done
rg "enable row level security" supabase/migrations/0002_rls.sql
python supabase/verify_live_enable_migration.py
python supabase/verify_g1_g2_migration.py
python supabase/verify_hosted_live_readiness.py
python supabase/verify_hosted_live_enable_flow.py \
  --confirm-staging-project "$SUPABASE_STAGING_PROJECT_REF"
```

`verify_g1_g2_migration.py`가 현재 repository-local migration 검증 진입점입니다.
Docker의 새 `postgres:16-alpine`에 `0001`부터
`20260718165749_pgcrypto_schema_convergence.sql`까지 적용하는
clean-install 경로와, `0015`까지 데이터가 있는 상태에서 `0016` 이후 전체를
적용하는 upgrade 경로, 운영 row가 채워진 `0023` 상태에서 `0024` 이후 전체를
적용하는 수렴 경로를 각각 검증합니다. clean-install 경로에서는 실제 PostgREST
컨테이너도 실행해
`anon`/`authenticated`/`service_role` RPC 경계를 확인합니다. 추가로 다음을
검증합니다.

- exposed `api`/`worker_api` 함수가 모두 `SECURITY INVOKER`인지와 정확한 worker
  RPC allowlist
- private/public source-of-truth table에 runtime role의 직접 권한이 없는지
- TOTP 기반 recent AAL2, one-time server-hashed step-up, strict JSON type
- access maker-checker와 account opening의 분리 승인 및 정확히 한 번 원장 기표
- worker lease acquire/release CAS, command claim allowlist, DB-clock 기반 outbox
  reclaim/complete/fail, attempt token ABA 차단, 최종 attempt crash dead letter
- qualification 적용 시점·upgrade 재검증, 미해결 reconciliation break의 계정
  정지, reconciliation claim별 release/fencing token 재검증
- 50건을 넘는 reconciliation keyset drain과 signal-only Realtime publication
- 검증되지 않은 시가를 원가/0으로 보정하지 않는 snapshot 계약
- 기존 public order/position을 신규 private 원장에 합산하지 않는 upgrade 격리
- hash-pinned Paper bar/candidate ingest, claim/load/complete와 stale
  lease/fencing/control-epoch 거부
- KST 결제일 기준 cash obligation, pending debit/credit, claim/complete/retry,
  exact replay와 dead-letter 경계
- Unknown V2의 maker/checker 요청·검토와 전용 Worker
  `list_unknown_resolution_v2` → `claim_unknown_resolution_v2` →
  `apply_unknown_resolution_v2` CAS 경계
- 00:00~08:59 KST 경계에서도 reserve, Paper candidate, Unknown fill,
  qualification calendar 비교가 모두 `Asia/Seoul` 날짜를 사용하는지
- 동일 계좌·종목·완료 bar의 Paper 체결 참여량이 semantic intent 전체에서
  bar volume의 1%를 넘지 않는지
- operation command와 reconciliation claim/ACK가 현재 account lease의 release와
  fencing token에 결합되고 tokenless legacy overload가 fail-closed인지
- 동일 완료 bar의 Paper fill이 하나의 series/source/volume evidence만 사용하고,
  계좌·종목별 active sell reservation이 하나만 존재하는지

PostgREST image를 받을 수 없는 로컬 parser 디버깅에만
`--skip-postgrest`를 사용할 수 있습니다. 이 옵션을 사용한 결과는 staging 승인
근거가 아닙니다.

## G1/G2 경계

- 환경은 `paper`와 로컬 `contract_test`만 허용합니다. production live 환경,
  production 주문 credential, 실제 broker order transport는 존재하지 않습니다.
- Toss OpenAPI의 공식 URL/hash는 disposable verifier fixture에서만 사용합니다.
  production seed에 승인 reviewer나 승인 artifact를 만들지 않습니다.
- Desktop은 `api` RPC와 `api.control_plane_signal`만 사용합니다. Realtime
  publication에는 이 monotonic invalidation signal만 존재하며 raw order,
  decision, audit, heartbeat payload는 게시하지 않습니다.
- Worker는 `worker_api` RPC만 사용합니다. public/private table direct write는
  cutover 이후 차단됩니다.
- `unknown_requires_manual_check`는 먼저 quarantine, reconciliation break,
  critical incident를 만들고 재전송을 차단합니다. 기존 V1 unknown workflow는
  evidence-only입니다. V2에서만 서로 다른 operator/risk approver가 증거와 최종
  상태를 승인한 뒤 전용 Worker `list/claim/apply` CAS가 검증된 fill/원장을
  반영할 수 있습니다. generic operation-command ACK는 이 적용을 수행할 수 없고,
  승인 전 자동 복구나 재주문도 허용하지 않습니다.
- `release_promotion`과 V1 `unknown_resolution` command는 generic Worker claim
  allowlist에서 제외됩니다. V2 unknown 적용은 별도 RPC allowlist와 receipt를
  사용합니다.

## Paper execution source publisher

`20260714155117_paper_execution_source.sql`은 `local_fixture` 또는
`allowed_read_evidence`로 표시된 immutable one-minute bar batch와 Paper candidate를
보존하고, Worker가 claim/load/complete할 수 있는 재시작 가능 source를 제공합니다.
이 migration은 strategy signal이나 provider data를 자동으로 만들지 않습니다.

로컬/승인 artifact 한 건을 게시할 때만 기본값이 false인
`EXECUTION_V2_PAPER_SOURCE_INPUT_ENABLED`를 명시적으로 true로 설정하고, 파일의
SHA-256을 별도 인자로 전달합니다. 파일 hash는 입력 바이트 고정성을 확인할 뿐
artifact 출처 승인이나 시장 데이터 진실성을 대신하지 않습니다. DB는 현재
account/lease/fencing token/`control_epoch`/release/qualification과 모든 evidence
hash를 다시 검사합니다.

`20260715041903_paper_bar_participation_guard.sql`은 bundle에 현재 intent를 제외한
동일 계좌·종목·완료 bar의 누적 체결량을 명시하고, Paper fill commit을 직렬화해
전체 semantic intent 합계가 bar volume의 1%를 넘으면 transaction을 거부합니다.
또한 contract qualification manifest에 journal 균형, projection 반영, provider
identity 고정 증거를 갖춘 `ledger_invariants` check를 필수로 추가합니다.
`20260715041909_operation_claim_fencing.sql`은 operation command claim/ACK와
reconciliation claim을 현재 account worker lease의 release SHA와 fencing token에
결합합니다. 이전 tokenless RPC overload는 rolling upgrade 중에도 실행되지 않고
`worker_upgrade_required`로 fail-closed합니다.
`20260715041912_sell_cost_basis_checkpoint_guard.sql`은 매도 fill의
`POSITION_COST` posting을 intent 예약 시 고정된 이동가중평균 원가 checkpoint에
결합하고, 현재 projection과 checkpoint가 어긋나면 전체 transaction을
rollback합니다.
`20260715041915_paper_evidence_and_sell_reservation_guards.sql`은 동일 계좌·종목·
완료 minute의 Paper fill이 서로 다른 series/source/volume evidence를 섞지 못하게
하고, 동일 계좌·종목에는 미소진 매도 reservation을 하나만 허용합니다. 기존
reservation이 terminal consumption 또는 release로 0이 된 뒤에만 다음 매도
reservation이 허용됩니다.

```powershell
cd apps/worker
$env:EXECUTION_V2_ENABLED="true"
$env:EXECUTION_V2_WORKER_API_ENABLED="true"
$env:EXECUTION_V2_PAPER_SOURCE_INPUT_ENABLED="true"
$env:EXECUTION_V2_ENVIRONMENT="paper"
$env:EXECUTION_V2_ACCOUNT_ID="paper-primary"
$env:EXECUTION_V2_WORKER_ID="<active-lease-holder-uuid>"
$sourceSha = (Get-FileHash -Algorithm SHA256 .\paper-source.json).Hash.ToLowerInvariant()
py -m app.tools.publish_paper_execution_source_once `
  --input .\paper-source.json `
  --input-sha256 $sourceSha
```

## Local contract qualification

`app.tools.run_contract_qualification_once`는 고정 OpenAPI SHA-256에 묶인
`local_contract_simulator`로 create/status/partial/terminal/cancel과 16개 fault
scenario를 실행하고 `production_order_network_zero.request_count=0`인 manifest를
출력합니다.

```powershell
cd apps/worker
py -m app.tools.run_contract_qualification_once
```

이 실행은 공식 sandbox 호출이나 broker 자격 증명이 아니며, DB의 release-bound
qualification 등록·maker/checker finalization 또는 Hosted Staging 승인을 자동으로
완료하지 않습니다.

Hosted staging 적용은 별도 프로젝트에서 migration checksum과 G1/G2 verifier를
확인한 뒤 사람이 승인해야 합니다. production project, live credential, broker
endpoint를 verifier에 제공하지 마십시오.

아래의 `verify_live_enable_*` 스크립트 설명은 `0009`~`0015` legacy live-enable
설계의 회귀 참고용입니다. `0016` 이후 최종 아키텍처에서는 public direct access와
live-enable 경로가 차단되므로, 이 스크립트의 성공을 G1/G2 또는 live 준비 증거로
사용하면 안 됩니다.

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
`0014_desktop_audit_summary.sql`은 desktop authenticated role의 원본
`audit_logs` 조회 권한을 제거하고, admin 확인 뒤 변경 필드명만 반환하는 제한된 RPC를
노출합니다. actor UUID와 before/after snapshot 값은 desktop Data API 응답에 포함되지
않습니다.
`0015_paper_order_execution_details.sql`은 기존 주문과 호환되는 nullable
`quantity`/`price_krw`를 추가하고, 값이 존재할 때 양수만 허용합니다. paper 주문은
공유 정수 수량 계산 결과를 이 필드에 저장하며 live 주문도 broker dispatch 전에 같은
실행 수량과 limit price를 기록합니다.

`verify_live_enable_migration.py`는 Docker daemon이 실행 중인 환경에서 임시
`postgres:16-alpine` 컨테이너를 만들고, Supabase `auth.uid()`/Realtime 최소 stub,
전체 migration, seed를 적용한 뒤 `request_live_enable` 승인 row가 live enable 시
정확히 한 번 `applied`로 소모되는지, `anon`/`authenticated`가 destructive/read RPC를
직접 실행하지 못하는지, `service_role`만 retention dry-run RPC를 실행할 수 있는지
검증합니다.

`verify_hosted_live_readiness.py`는 실제 hosted/staging Supabase project에 대해
`SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY` 또는 `VITE_SUPABASE_PUBLISHABLE_KEY`,
`SUPABASE_SECRET_KEY`, `SUPABASE_LIVE_REQUESTER_JWT`,
`SUPABASE_LIVE_REVIEWER_JWT`가 설정된 경우에만 실행됩니다. 이 verifier는
PostgREST root의 publishable-key 접근 거부와 secret-key 접근 성공,
publishable/anon key의 destructive/read RPC denial, secret/service role key의 RPC
success path, publishable/anon key의 `bot_settings` Data API denial,
secret/service role key의 `bot_settings` Data API select, Realtime WebSocket
handshake를 확인하고 secret 값을 출력하지 않습니다.
`sb_publishable_`/`sb_secret_` key는 `apikey` header에만 보내고, user session 또는
legacy JWT key만 `Authorization: Bearer`로 전송합니다.
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
