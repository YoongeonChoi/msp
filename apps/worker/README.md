# Worker

Python 3.12 기반의 trading engine입니다. 현재 주문 실행 경계는 명시적으로
`paper`와 로컬 `contract_test`만 지원합니다. `live`/`production` 주문 실행은
설정, 서비스, Toss adapter에서 모두 격리되어 있습니다.

## 안전 경계

- `EXECUTION_V2_ENVIRONMENT`는 `paper|contract_test`만 허용합니다.
- `contract_test`는 qualified artifact hash가 일치하는 zero-network
  `ContractTestBroker`만 사용합니다.
- Toss의 create/status/list/get/cancel 주문 API는 모두 fail-closed입니다.
- production order host 요청 수는 항상 0이어야 합니다.
- V2는 `worker_api` RPC만 사용하며 public table CRUD를 사용하지 않습니다.
- 자동 strategy signal producer와 자동 market-data collector는 연결하지
  않았습니다. 증거가 없으면 주문을 생성하지 않고 fail-closed합니다. 별도의
  기본 비활성화 explicit publisher만 hash-pinned 승인 fixture/candidate를
  allowlisted `ingest`/`enqueue` RPC로 게시할 수 있습니다.
- `app.main`의 기존 trading loop는 V2 주문 runtime이 아닙니다. V2 execution 또는
  operations를 실행했다고 검증할 때 사용하지 마십시오.

## 설치 및 전체 검증

```powershell
cd apps/worker
py -m pip install -e ".[dev]"
py -m pytest
ruff check app
mypy app
```

## 로컬 V2 실행 경계 검증

다음 명령은 실제 `RunExecutionV2` paper 흐름과 로컬 contract lifecycle을 실행합니다.
외부 broker/order host 네트워크는 사용하지 않습니다.

```powershell
py -m pytest app/tests/integration/test_execution_v2_flow.py -q
py -m pytest app/tests/unit/test_run_execution_v2_restart.py -q
py -m pytest app/tests/unit/test_contract_test_execution.py -q
py -m pytest app/tests/unit/test_toss_readonly.py -q
```

검증 범위는 다음과 같습니다.

- next-full-minute completed bar, tick/volume/cost/calendar/settlement evidence
- semantic reservation, dispatch marker, fill observation, balanced ledger posting
- reserve/dispatch/각 observation commit 직후 crash와 deterministic replay
- contract create→open→partial→filled/canceled, duplicate, timeout, malformed 응답
- Toss production order create/status/list/get/cancel 네트워크 요청 0

## V2 control/operations runtime

이 runtime 자체는 trading candidate를 만들지 않습니다. command, execution,
cash settlement, reconciliation(승인된 Unknown V2 전용 apply 포함), alert outbox를
독립 cadence로 실행합니다. 안정적인 UUID worker identity를 배포 설정으로
고정해야 합니다.

```powershell
$env:EXECUTION_V2_ENABLED="true"
$env:EXECUTION_V2_WORKER_API_ENABLED="true"
$env:EXECUTION_V2_WORKER_ID="<stable-worker-uuid>"
$env:EXECUTION_V2_ENVIRONMENT="paper"
$env:SUPABASE_URL="<project-url>"
$env:SUPABASE_SECRET_KEY="<worker-only-secret>"
py -m app.tools.run_execution_v2_operations
```

지속 loop로 실행할 때만 명시적으로 `--loop`를 사용합니다.

```powershell
py -m app.tools.run_execution_v2_operations --loop --interval-sec 30
```

`ALERT_WEBHOOK_URL`이 없으면 outbox 항목은 전달 완료로 가장하지 않고 retryable
failure로 기록됩니다. 현재 재구성에 필요한 bar/provider evidence source가 없으면
reconciliation 항목은 `manual`로 격리합니다.

## 명시적 Paper source 게시

`publish_paper_execution_source_once`는 자동 signal/data producer가 아니다. 승인한
`local_fixture` 또는 `allowed_read_evidence` bar batch와 그에 대응하는 candidate를
하나의 strict `schema_version=1` JSON 파일로 준비하고, 별도로 계산한 SHA-256을
일치시킨 명시 실행만 허용한다. 입력 gate의 기본값은 false다.

```powershell
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

이 경로는 `ingest_paper_bar_fixture_v1`과
`enqueue_paper_execution_candidate_v1`만 호출한다. DB는 active lease/fencing
token, `control_epoch`, release, qualification, policy/cost/calendar/tick/volume/
corporate-action evidence를 다시 확인한다. 파일 hash는 byte identity만 증명하며
출처 승인이나 시장 데이터 정확성을 대신하지 않는다. 게시 작업이 끝나면 입력
gate를 다시 비활성화한다.

## 제한형 paper partial 재개

자동 strategy signal producer와 market-data collector는 여전히 연결하지 않았고,
operations loop 자체도 신규 candidate를 생성하지 않습니다. 신규 intent는 위의
별도 explicit publisher가 승인 fixture/candidate를 ingest/enqueue했을 때만 source에
나타납니다. 이미 최소 한 번의 `partial_filled` observation이 저장된 **동일
release의 기존 paper intent 하나**는 hash로 고정한 외부 evidence bundle을 사용해
명시적으로 재개할 수 있습니다.

이 도구는 기본 비활성화이며 다음 조건을 모두 DB RPC에서 다시 확인합니다.

- 현재 활성 worker lease의 holder/fencing token/release
- 현재 활성 execution control과 원 intent의 control epoch/policy
- 동일 release에서 생성된 원 intent와 기존 durable observation prefix
- 원 dispatch request identity와 전체 deterministic observation history
- intent 만료 전 실행; 만료 시 operations reconciliation의 atomic expiry 경로 사용

bundle은 `schema_version=1`과 함께 원 `ExecutionIntent`, 전체 bar prefix(기존 fill을
만든 bar부터 새 completed bar까지), 고정 cost schedule, execution evidence, SELL이면
고정 position cost basis를 포함합니다. 파일은 1MB/600 bars로 제한되며 symlink,
중복 JSON key, 미등록 field를 거부합니다.

```powershell
$env:EXECUTION_V2_ENABLED="true"
$env:EXECUTION_V2_WORKER_API_ENABLED="true"
$env:EXECUTION_V2_PAPER_RESUME_INPUT_ENABLED="true"
$env:EXECUTION_V2_WORKER_ID="<active-lease-holder-uuid>"
$bundleSha = (Get-FileHash -Algorithm SHA256 .\paper-resume.json).Hash.ToLowerInvariant()
py -m app.tools.resume_paper_execution_v2_once `
  --input .\paper-resume.json `
  --input-sha256 $bundleSha
```

이 엔트리포인트는 `reserve_order_intent`를 호출하지 않으므로 신규 주문을 만들지
않습니다. 그러나 bundle을 발행·서명하는 신뢰 가능한 자동 upstream evidence
boundary는 아직 없습니다. 운영자가 만든 hash는 파일 고정성만 증명하며 출처의
진실성을 증명하지 않습니다. 따라서 이 도구는 제한형 수동 복구 경계이고,
무인 production paper 실행 G1 완료 근거가 아닙니다.

## Historical/quarantined 도구

다음 legacy-live 도구는 과거 감사 자료로만 남아 있으며 실행 경로는 hard
quarantine입니다.

- `app.tools.run_live_execution_safety_drill_once`
- `app.tools.run_live_recovery_drill_once`
- `app.tools.cancel_live_order_once`

문자열이나 `execution_environment`를 `contract_test`로 바꿔도 legacy live row 또는
production 주문 경로가 활성화되지 않습니다. Contract lifecycle 검증은 반드시
`ContractTestBroker`와 V2 전용 테스트/오케스트레이션을 사용합니다.
