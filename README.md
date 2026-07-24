# KR Auto Trading Lab

[![CI](https://github.com/YoongeonChoi/msp/actions/workflows/ci.yml/badge.svg)](https://github.com/YoongeonChoi/msp/actions/workflows/ci.yml)
[![Security](https://github.com/YoongeonChoi/msp/actions/workflows/security.yml/badge.svg)](https://github.com/YoongeonChoi/msp/actions/workflows/security.yml)

한국 국내 주식의 데이터 수집, 전략 연구, Paper Trading, 위험 통제, 운영 감사를 한곳에서 다루는 **paper-first 자동매매 연구·제어 시스템**입니다.

수익률보다 안전성, 재현성, 설명 가능성, 관측 가능성을 우선합니다. Desktop은 주문 엔진이 아니라 Supabase RLS를 통과한 control plane이며, broker 주문 경로는 Python Worker만 소유합니다.

> [!WARNING]
> 이 프로젝트는 투자 자문이나 수익을 보장하는 제품이 아닙니다. G1+G2 릴리스는 내부 전용 `paper`와 로컬 `contract_test`만 지원합니다. 기본값은 `enabled=false`, `mode=paper`, `live_order_allowed=false`이며 Production Live 주문은 UI·DB·설정·네트워크에서 금지됩니다.

## 현재 상태

| 영역 | 제공 기능 | 상태 |
| --- | --- | --- |
| Worker | 상시 trading cycle, provider health, heartbeat | 구현 |
| 전략 | 가중치 기반 점수, 결정 근거 snapshot, draft/승인 경계 | 구현 |
| Paper Trading V2 | 영속 계좌, 결정론적 부분체결, 예약, 복식부기 원장, 재시작 복구 커널 | 로컬 구현·검증 단계 |
| 주문 안전 | lease/fencing, `control_epoch`, semantic dedupe, 현금·수량 원자 예약 | 로컬 구현·검증 단계 |
| Desktop | 운영 상태, 명령/ACK, 승인함, incident, stale/offline 차단, 수동 reconciliation | 로컬 구현·검증 단계 |
| 운영 통제 | MFA/RBAC, maker/checker, append-only audit, transactional outbox | 로컬 구현·검증 단계 |
| Supabase | `private` 원장, 최소 `api` projection, Worker 전용 `worker_api` RPC | migration 검증 단계 |
| 실주문 | Production order write 경로와 credential | 금지 (`NO-LIVE`) |

현재 승인 범위와 남은 제한은 [G0 운영 경계](docs/G0_OPERATING_BOUNDARY.md), [기업 프로그램 계획](docs/ENTERPRISE_PROGRAM_PLAN.md), [ADR-0007](docs/00_DECISIONS/ADR-0007-execution-safety-kernel.md), [실행 정책](docs/EXECUTION_POLICY.md)에서 확인할 수 있습니다. 과거 Live readiness 문서는 추적성만을 위해 보존되며 현재 운영 또는 승인 기준이 아닙니다.

사업·규제·원장·데이터·운영을 함께 다루는 다음 단계의 실행 기준은
[Enterprise Trading Program Plan](docs/ENTERPRISE_PROGRAM_PLAN.md)입니다. 이 계획은
숫자형 준비도와 별도로 binary stage gate를 적용하며, 현재 live 상태를 `NO-GO`로
판정합니다.

## 주요 특징

- **Fail-closed 위험 통제**: 설정, market, quote freshness, provider health, account sync, 현금, 보유수량, 종목·섹터 노출, 일일 손실·주문 수, 중복·cooldown을 `RiskService`에서 평가합니다.
- **Paper Truth 실행**: Paper mode는 결정론적 1분 bar 체결, 현금·수량 예약, 부분체결·만료, 균형 복식부기와 idempotent replay를 사용합니다.
- **설명 가능한 결정**: component score, feature snapshot, strategy version, risk 결과를 하나의 decision snapshot으로 보존합니다.
- **운영자 중심 Cockpit**: heartbeat/lease/release/ledger checkpoint, 요청·승인·Worker ACK·postcondition, incident와 stale/offline 경계를 한국어 UI로 제공합니다.
- **AI와 주문의 분리**: OpenAI는 연구·분류·후보 제안에만 사용하며, 출력이 주문 실행이나 live 전략 승격으로 연결되지 않습니다.
- **최소 권한 데이터 경계**: Desktop은 publishable key와 AAL2 역할 세션만 사용하고, Worker는 검토된 `worker_api` RPC만 호출합니다.
- **수동 배포 원칙**: Render 자동 배포는 꺼져 있으며, hosted staging 적용도 별도 사용자 승인 전에는 수행하지 않습니다.

## 아키텍처

```text
Tauri + React Desktop Cockpit
        │  publishable key + AAL2 role session
        ▼
Supabase Auth / api projections + operation RPC
        │
        ▼
private ledger/control source of truth
        ▲
        │  worker_api RPC allowlist
        │
Python Render Background Worker
  ├─ Application: cycle, risk, execution kernel, reconciliation
  ├─ Domain: entities, policies, value objects
  ├─ Adapters: deterministic Paper, local contract_test, read-only providers
  └─ Infrastructure: logging, metrics, redaction, shutdown
```

핵심 trust boundary는 다음과 같습니다.

1. Desktop은 broker API를 호출하지 않습니다.
2. `ExecutionService`만 execution adapter의 create operation을 호출할 수 있습니다.
3. intent 예약과 dispatch 직전에 risk/control/lease/fencing 조건을 다시 확인합니다.
4. 실제 Toss는 read-only이며, 주문 lifecycle은 네트워크 없는 `contract_test`에서만 검증합니다.

자세한 구성은 [Architecture](docs/ARCHITECTURE.md)와 [Context Map](docs/CONTEXT_MAP.md)을 참고하세요.

## 사전 요구사항

- Python 3.12 이상
- Node.js 22 이상과 npm
- Desktop native 앱 실행 시 Rust stable 및 [Tauri 2 OS prerequisites](https://v2.tauri.app/start/prerequisites/)
- 실제 Cockpit 데이터 연동 시 Supabase project와 서로 다른 운영 Auth 계정 2개 이상
- 선택 사항: Docker가 실행 중인 환경은 disposable PostgreSQL migration 검증에 사용됩니다.

실제 API key 없이도 Worker mock one-shot과 Desktop build/test를 실행할 수 있습니다.

## 5분 Mock Quickstart

### 1. 환경 파일 준비

PowerShell:

```powershell
Copy-Item apps/worker/.env.example apps/worker/.env
Copy-Item apps/desktop/.env.example apps/desktop/.env.local
```

Bash:

```bash
cp apps/worker/.env.example apps/worker/.env
cp apps/desktop/.env.example apps/desktop/.env.local
```

`.env`와 `.env.local`은 Git에 커밋하지 마세요. Desktop에는 `VITE_SUPABASE_URL`과 `VITE_SUPABASE_PUBLISHABLE_KEY` 외의 secret을 넣지 않습니다.

### 2. Worker 설치

PowerShell:

```powershell
cd apps/worker
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

Bash:

```bash
cd apps/worker
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

### 3. 안전한 one-shot cycle 실행

PowerShell:

```powershell
$env:MOCK_PROVIDERS = "true"
$env:RUN_ONCE = "true"
python -m app.main
```

Bash:

```bash
MOCK_PROVIDERS=true RUN_ONCE=true python -m app.main
```

초기 설정은 `enabled=false`이므로 provider health와 heartbeat를 확인하고 주문은
만들지 않는 것이 정상입니다. 이 legacy one-shot은 V2 원장 smoke test가 아닙니다.
V2 Paper 실행에는 다음 절의 control-plane migration과 승인 evidence가 필요합니다.

### 4. Desktop 설치 및 실행

저장소 root에서:

```bash
npm ci
npm run desktop:dev
```

이 명령은 Vite 웹 개발 서버입니다. 실제 Tauri 창을 실행하려면 Rust와 OS prerequisites를 준비한 뒤 다음을 사용합니다.

```bash
npm --workspace apps/desktop run tauri -- dev
```

Supabase 값을 비워 둔 경우 UI는 연결 설정 안내를 표시합니다. 실제 control-plane 데이터를 보려면 다음 절을 진행하세요.

## Supabase 연결

> Hosted staging에 아래 절차를 적용하는 작업은 별도 사용자 승인이 필요합니다. 기본 구현·검증은 disposable local PostgreSQL에서 수행합니다.

1. [Supabase Setup](docs/SUPABASE_SETUP.md)에 따라 PG17 pgcrypto preflight를 먼저
   실행하고, checksum이 고정된 migration과 `seed.sql`을 순서대로 적용합니다.
2. Supabase Auth TOTP를 켜고 서로 다른 운영 사용자 두 명 이상을 AAL2로 등록한 뒤 V2 역할을 UUID에 할당합니다.
3. `apps/desktop/.env.local`에는 URL과 publishable key만 넣습니다.
4. 별도 hosted-staging 승인을 받은 뒤에만 Worker에 서버용 URL과 secret key를
   제공하고 `EXECUTION_V2_ENABLED=true`, `EXECUTION_V2_WORKER_API_ENABLED=true`를
   함께 설정합니다. 기존 `USE_SUPABASE_REPOSITORY` 경로는 V2 원장이 아닙니다.
5. Desktop의 **계정·보안** 화면에서 `이 기기 연결`을 한 번 완료하고 TOTP challenge를 진행합니다. 저장된 세션은 다음 실행부터 자동으로 복구되지만, 위험 작업의 AAL2 확인은 별도로 유지됩니다.

```dotenv
# apps/desktop/.env.local — 공개 가능한 client 설정만
VITE_SUPABASE_URL=https://<project-ref>.supabase.co
VITE_SUPABASE_PUBLISHABLE_KEY=<publishable-key>
```

```dotenv
# apps/worker/.env — 서버 전용, 절대 커밋 금지
EXECUTION_V2_ENABLED=true
EXECUTION_V2_WORKER_API_ENABLED=true
EXECUTION_V2_ENVIRONMENT=paper
SUPABASE_URL=https://<project-ref>.supabase.co
SUPABASE_SECRET_KEY=<server-secret-key>
ALERT_WEBHOOK_URL=https://<approved-receiver>/events
ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_ID=<rotation-id>
ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_B64=<canonical-base64-32-byte-key>
ALERT_WEBHOOK_RECEIVER_ACK_PREVIOUS_KEY_ID=
ALERT_WEBHOOK_RECEIVER_ACK_PREVIOUS_KEY_B64=
```

`seed_strategy_v1`, `seed_watchlist_demo`, `run_paper_cycle_once`는 legacy 연구
fixture이며 신규 V2 원장이나 G1 검증 증거를 만들지 않습니다. V2 계좌는 승인된
opening command, execution/cost evidence, worker lease를 갖춘 뒤에만 실행합니다.

## 환경 설정 요약

| 변수 | 위치 | 기본값/용도 |
| --- | --- | --- |
| `MOCK_PROVIDERS` | Worker | `true`, 외부 provider 대신 안전한 mock 사용 |
| `RUN_ONCE` | Worker | `false`, 한 cycle 후 종료 여부 |
| `USE_SUPABASE_REPOSITORY` | Worker | legacy compatibility only; V2 source of truth로 사용 금지 |
| `BOT_DEFAULT_MODE` | Worker | `paper` |
| `SUPABASE_URL` | Worker | server-side repository URL |
| `SUPABASE_SECRET_KEY` | Worker | 서버 전용 secret, Desktop 금지 |
| `TOSS_CREDENTIAL_SCOPE` | Worker | Toss credential 사용 시 반드시 `read_only` |
| `TOSS_ORDER_CAPABLE_CREDENTIALS` | Worker | 반드시 `false`; `true`/미확인은 startup 차단 |
| `LIVE_ORDER_EXECUTION_ENABLED` | Worker | 반드시 `false` |
| `TOSS_ORDER_ENDPOINT_ENABLED` | Worker | 반드시 `false` |
| `EXECUTION_V2_ENABLED` | Worker | V2 runtime을 명시적으로 구성했을 때만 `true` |
| `EXECUTION_V2_ENVIRONMENT` | Worker | `paper` 또는 로컬 `contract_test` |
| `EXECUTION_V2_WORKER_API_ENABLED` | Worker | migration/RPC 검증 후에만 `true` |
| `ALERT_WEBHOOK_URL` | Worker | HTTPS-only approved receiver; ACK key와 함께 설정 |
| `ALERT_WEBHOOK_RECEIVER_ACK_*` | Worker | current/previous 32-byte HMAC ACK keys; Desktop 금지 |
| `DEAD_MAN_ALERT_WEBHOOK_RECEIVER_ACK_*` | 별도 dead-man | main Worker와 공유하지 않는 별도 HMAC ACK keys |
| `VITE_SUPABASE_URL` | Desktop | Supabase project URL |
| `VITE_SUPABASE_PUBLISHABLE_KEY` | Desktop | client publishable key |
| `VITE_SUPABASE_REALTIME_DISABLED` | Desktop | `true`이면 polling 조회 전용; 모든 mutation 차단 |

Provider별 변수는 [Worker `.env.example`](apps/worker/.env.example)에서 확인하세요. 검증되지 않은 endpoint, parameter, rate limit은 구현하지 않고 [API Gaps](docs/API_GAPS.md)에 기록합니다.

## 권장 Paper Trading 운영 흐름

1. `enabled=false`, `mode=paper`, `live_order_allowed=false`와 `LIVE 금지`를 확인합니다.
2. 두 운영 사용자의 TOTP AAL2와 역할 분리를 확인합니다.
3. `paper-primary` opening command를 maker/checker로 승인하고 opening journal이 한
   번만 기록됐는지 확인합니다.
4. 승인된 execution/cost/calendar/tick/volume/corporate-action evidence와 release
   qualification을 확인합니다.
5. Worker lease/fencing과 `control_epoch`가 일치한 상태에서 Paper command를
   요청·승인하고 Worker ACK와 runtime postcondition을 확인합니다.
6. Desktop에서 원장 checkpoint, 부분체결, reserve, reconciliation, incident와
   audit archive receipt를 검토합니다.
7. outcome/backtest는 실행 원장과 분리된 연구 절차로 수행합니다.

```bash
cd apps/worker
python -m app.tools.update_outcomes_once
python -m app.tools.run_backtest --strategy strategy_v1_weighted_factor --start YYYY-MM-DD --end YYYY-MM-DD
python -m app.tools.paper_health_report
```

`paper_health_report`는 읽기 중심 운영 진단이며 실주문 승인 도구가 아닙니다. 자세한 절차는 [Paper Trading Operations](docs/PAPER_TRADING_OPERATIONS.md)를 참고하세요.

## Desktop 화면

| 화면 | 용도 |
| --- | --- |
| Operations Control | PAPER/CONTRACT TEST 상태, `LIVE 금지`, heartbeat, lease, release, ledger checkpoint |
| Safety Command Center | 요청·승인·claim·Worker ACK·postcondition timeline과 Emergency Stop |
| Approval Inbox | maker/checker, 변경 diff, MFA/step-up, 만료 상태 |
| Incident Center | ACK, 담당자, 완화, 서로 다른 risk approver의 종결 |
| Manual Reconciliation | `unknown_requires_manual_check`의 증거와 fail-closed 대사 상태(읽기 전용) |
| Access & MFA | 개인 세션, TOTP AAL2, 역할과 접근 변경 요청/검토 |

기존 연구용 Dashboard, Watchlist, Portfolio, Orders, Signals, Strategy Lab, Logs
페이지와 관대한 row adapter는 G1+G2 release source에서 제거됐으며 Git 이력에서만
보존됩니다.

Desktop mutation은 strict schema v1, stale/offline/session 경계, Supabase RLS/RPC를 통과합니다. `applied`와 runtime postcondition 전에는 완료로 표시하지 않습니다.

## 검증

Worker:

```bash
cd apps/worker
python -m ruff check app
python -m mypy app
python -m pytest
```

Desktop:

```bash
npm run desktop:lint
npm run desktop:typecheck
npm run desktop:test
npm run desktop:build
```

선택적 E2E:

```bash
npm run desktop:e2e
```

Migration·repository safety:

```bash
python .github/scripts/migration_history_guard.py --worktree
python .github/scripts/repository_safety.py migrations
python .github/scripts/repository_safety.py workflows
python supabase/verify_g1_g2_migration.py
```

마지막 명령은 Docker daemon과 disposable PostgreSQL container가 필요합니다. CI와 검증 정책은 [CI/CD](docs/CI_CD.md)와 [Test Plan](docs/TEST_PLAN.md)에 정리되어 있습니다.

## 프로젝트 구조

```text
apps/
  worker/       Python 3.12 trading engine와 operator tools
  desktop/      Tauri 2 + React + Vite management cockpit
packages/
  shared/       UI-facing TypeScript schema
supabase/
  migrations/   schema, RLS, Realtime, audit, runtime invariant
  seed.sql      fail-closed 초기 데이터
docs/           architecture, policy, runbook, security, readiness
.github/        CI, security scan, migration guard, CODEOWNERS
render.yaml     수동 배포 Render Background Worker blueprint
```

## Render 배포

Render는 fencing qualification 전까지 Background Worker 한 개만 실행하며 `autoDeployTrigger: "off"`를 유지합니다. 배포·복구 환경은 Paper disabled, order credential 없음으로 시작하고 target commit을 보고하는 새 heartbeat를 확인해야 합니다.

실제 절차는 [Render Deployment](docs/RENDER_DEPLOYMENT.md), [Release Process](docs/RELEASE_PROCESS.md), [Rollback](docs/ROLLBACK.md)을 따르세요. README는 의도적으로 live 활성화 절차나 secret 값을 제공하지 않습니다.

## 자주 발생하는 문제

### `python`, `node`, `npm`을 찾지 못함

사전 요구 버전을 설치하고 새 terminal을 여세요. Windows에서 Python launcher가 없다면 `py` 대신 `python`을 사용합니다.

### Desktop에 `권한 필요`가 표시됨

**계정·보안** 화면에서 이 기기가 개인 Supabase Auth 계정에 연결됐고 TOTP AAL2를
완료했는지, UUID에 필요한 V2 역할이 할당됐는지 확인하세요. 권한이 없는 mutation은
안전하게 차단되어야 합니다.

### Worker는 실행됐는데 Desktop에 데이터가 없음

mock 기본값은 in-memory kernel입니다. Desktop에서 보려면 별도 승인된 환경에서
V2 migration을 적용하고 Worker에 서버 전용 Supabase 설정과
`EXECUTION_V2_WORKER_API_ENABLED=true`가 필요합니다. legacy
`USE_SUPABASE_REPOSITORY` 데이터는 V2 snapshot에 합산되지 않습니다.

### 주문이 생성되지 않음

기본값 `enabled=false`는 의도된 안전 상태입니다. Paper V2는 설정·정책 version·bar/cost/tick/corporate-action evidence·lease/fencing·현금·보유수량·semantic duplicate gate를 적용합니다. 누락된 증거를 기본값으로 보정하지 않으며, Paper 매도는 V2 원장의 available quantity만 사용합니다.

### `unknown_requires_manual_check` 주문이 보임

자동 재시도하거나 UI에서 강제로 완료 처리하지 마세요. 주문 안전 큐와 [Runbook](docs/RUNBOOK.md)의 reconciliation 절차로 provider 상태와 감사 증거를 확인해야 합니다.

### Live mode를 선택할 수 없음

정상 동작입니다. 현재 릴리스는 Production Live를 지원하지 않으며 활성화 절차도 제공하지 않습니다. 외부 주문 요구가 생기면 기존 설정을 확장하지 않고 G0 사업·규제 심사를 다시 엽니다.

## 남은 주요 작업

- 검증된 candle 기반 technical feature 자동 적재
- 목표 비중·no-trade band·현금 reserve·비용을 포함한 rebalance planner
- outcome, backtest, 월간 연구, retention의 안전한 무인 scheduling
- 독립 failure domain의 worker dead-man monitor 운영 배치
- hosted staging RLS/MFA/alert/archive/restore 증거와 10거래일 Paper/Shadow gate

기능이 없다는 이유로 가짜 live endpoint나 mock 성공 응답을 추가하지 않습니다. provider 계약의 불확실성은 구현 차단 사유입니다.

## 문서 안내

- [Engine](docs/ENGINE.md) · [Risk Policy](docs/RISK_POLICY.md) · [Execution Policy](docs/EXECUTION_POLICY.md)
- [Database](docs/DATABASE.md) · [Supabase Setup](docs/SUPABASE_SETUP.md)
- [Security](docs/SECURITY.md) · [Threat Model](docs/THREAT_MODEL.md)
- [Runbook](docs/RUNBOOK.md) · [Incident Response](docs/INCIDENT_RESPONSE.md)
- [Backtesting Policy](docs/BACKTESTING_POLICY.md) · [AI Upgrade Policy](docs/AI_UPGRADE_POLICY.md)
- [API Connections](docs/API_CONNECTIONS.md) · [API Gaps](docs/API_GAPS.md)
- [Cost Limits](docs/COST_LIMITS.md) · [Observability](docs/OBSERVABILITY.md)
- [Current QA Iteration Scorecard](docs/QA_ITERATION_SCORECARD.md) · [Test Plan](docs/TEST_PLAN.md)

## 기여와 보안

변경 전 [Git Rules](docs/GIT_RULES.md), [Coding Standards](docs/CODING_STANDARDS.md), 각 디렉터리의 `AGENTS.md`를 확인하세요. risk, execution, broker, migration, workflow는 보호 영역이며 변경 이유·테스트·rollback 근거가 필요합니다.

비밀정보, 계좌 식별자, 실제 token, 미공개 취약점은 issue나 로그에 올리지 마세요. 보안 모델과 공개 범위는 [Security](docs/SECURITY.md)를 따릅니다.
