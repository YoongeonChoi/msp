# KR Auto Trading Lab

[![CI](https://github.com/YoongeonChoi/msp/actions/workflows/ci.yml/badge.svg)](https://github.com/YoongeonChoi/msp/actions/workflows/ci.yml)
[![Security](https://github.com/YoongeonChoi/msp/actions/workflows/security.yml/badge.svg)](https://github.com/YoongeonChoi/msp/actions/workflows/security.yml)

한국 국내 주식의 데이터 수집, 전략 연구, Paper Trading, 위험 통제, 운영 감사를 한곳에서 다루는 **paper-first 자동매매 연구·제어 시스템**입니다.

수익률보다 안전성, 재현성, 설명 가능성, 관측 가능성을 우선합니다. Desktop은 주문 엔진이 아니라 Supabase RLS를 통과한 control plane이며, broker 주문 경로는 Python Worker만 소유합니다.

> [!WARNING]
> 이 프로젝트는 투자 자문이나 수익을 보장하는 제품이 아닙니다. 기본값은 `enabled=false`, `mode=paper`, `live_order_allowed=false`입니다. 외부 provider·호스팅·사고 대응 증거가 모두 검증되기 전에는 무인 실거래에 사용할 수 없습니다.

## 현재 상태

| 영역 | 제공 기능 | 상태 |
| --- | --- | --- |
| Worker | 상시 trading cycle, provider health, heartbeat | 구현 |
| 전략 | 가중치 기반 점수, 결정 근거 snapshot, draft/승인 경계 | 구현 |
| Paper Trading | 위험 정책, 정수 수량·결정 가격 기록, 중복 방지, outcome·backtest 도구 | 구현 |
| 주문 안전 | 정수 주 buying power, live 매도 보유수량, 노출·손실·횟수·신선도 gate | 구현 |
| Desktop | 대시보드, 제어, 관심종목, 포트폴리오, 주문, 시그널, 전략 Lab | 구현 |
| 운영 가시성 | 수동 확인 주문 안전 큐, engine event, 변경 감사 로그 | 구현 |
| Supabase | Auth, RLS, Realtime, audit, deployment lock | 구현 |
| 실주문 | Worker 전용 guarded path와 fail-closed gate | 외부 증거 완료 전 사용 금지 |

현재 구현 범위와 남은 제한은 [Live Readiness Scorecard](docs/LIVE_READINESS_SCORECARD.md), [작업 현황](docs/LIVE_READINESS_WORK_SUMMARY.md), [품질 점수표](docs/QUALITY_SCORECARD.md)에서 확인할 수 있습니다.

## 주요 특징

- **Fail-closed 위험 통제**: 설정, market, quote freshness, provider health, account sync, 현금, 보유수량, 종목·섹터 노출, 일일 손실·주문 수, 중복·cooldown을 `RiskService`에서 평가합니다.
- **Paper-first 실행**: Paper mode는 `BrokerPort.place_order`를 호출하지 않으며, 허용 주문은 정수 수량과 결정 가격을, 차단 주문은 이유와 risk snapshot을 남깁니다.
- **설명 가능한 결정**: component score, feature snapshot, strategy version, risk 결과를 하나의 decision snapshot으로 보존합니다.
- **운영자 중심 Cockpit**: heartbeat, provider 상태, 위험 설정, 정확한 미해결 주문 수와 페이지형 안전 큐, 값이 제거된 audit 요약을 한국어 UI로 제공합니다.
- **AI와 주문의 분리**: OpenAI는 연구·분류·후보 제안에만 사용하며, 출력이 주문 실행이나 live 전략 승격으로 연결되지 않습니다.
- **최소 권한 데이터 경계**: Desktop은 publishable key와 authenticated admin 세션만 사용하고, Worker secret key는 서버에만 둡니다.
- **수동 배포 원칙**: Render 자동 배포는 꺼져 있으며, 배포 잠금과 새 heartbeat 증거 없이는 live 상태를 복원할 수 없습니다.

## 아키텍처

```text
Tauri + React Desktop Cockpit
        │  publishable key + authenticated admin session
        ▼
Supabase Auth / RLS / Realtime Control Plane
        ▲
        │  server-side secret key
        │
Python Render Background Worker
  ├─ Application: cycle, risk, execution, research
  ├─ Domain: entities, policies, value objects
  ├─ Adapters: Toss, KRX, OpenDART, Naver, OpenAI
  └─ Infrastructure: logging, metrics, redaction, shutdown
```

핵심 trust boundary는 다음과 같습니다.

1. Desktop은 broker API를 호출하지 않습니다.
2. `ExecutionService`만 `BrokerPort.place_order`를 호출할 수 있습니다.
3. 모든 live proposal은 `RiskService`의 최종 평가를 다시 통과해야 합니다.
4. 알 수 없거나 오래되었거나 mock인 증거는 live 주문을 허용하지 않습니다.

자세한 구성은 [Architecture](docs/ARCHITECTURE.md)와 [Context Map](docs/CONTEXT_MAP.md)을 참고하세요.

## 사전 요구사항

- Python 3.12 이상
- Node.js 22 이상과 npm
- Desktop native 앱 실행 시 Rust stable 및 [Tauri 2 OS prerequisites](https://v2.tauri.app/start/prerequisites/)
- 실제 Cockpit 데이터 연동 시 Supabase project와 admin Auth 계정
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

초기 설정은 `enabled=false`이므로 provider health와 heartbeat를 확인하고 주문은 만들지 않는 것이 정상입니다. 주문·decision을 Supabase에 남기는 Paper smoke는 server-side Supabase 환경이 필요하며, 다음 절의 설정을 마친 뒤 `run_paper_cycle_once`를 사용합니다.

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

1. [Supabase Setup](docs/SUPABASE_SETUP.md)에 따라 migration과 `seed.sql`을 순서대로 적용합니다.
2. Supabase Auth에서 운영자 계정을 만든 뒤 `public.user_roles.role='admin'`으로 등록합니다.
3. `apps/desktop/.env.local`에는 URL과 publishable key만 넣습니다.
4. Worker가 Supabase에 기록해야 할 때만 `apps/worker/.env`에 서버용 URL과 secret key를 넣고 `USE_SUPABASE_REPOSITORY=true`로 설정합니다.
5. Desktop의 **설정** 화면에서 admin 계정으로 로그인합니다.

```dotenv
# apps/desktop/.env.local — 공개 가능한 client 설정만
VITE_SUPABASE_URL=https://<project-ref>.supabase.co
VITE_SUPABASE_PUBLISHABLE_KEY=<publishable-key>
```

```dotenv
# apps/worker/.env — 서버 전용, 절대 커밋 금지
USE_SUPABASE_REPOSITORY=true
SUPABASE_URL=https://<project-ref>.supabase.co
SUPABASE_SECRET_KEY=<server-secret-key>
```

안전한 demo 데이터를 준비하고 Supabase-backed mock cycle을 실행할 수 있습니다. `run_paper_cycle_once`는 `SUPABASE_URL`과 `SUPABASE_SECRET_KEY`가 설정된 Worker 환경에서만 사용합니다.

```bash
cd apps/worker
python -m app.tools.seed_strategy_v1
python -m app.tools.seed_watchlist_demo
python -m app.tools.run_paper_cycle_once
```

`MOCK_PROVIDERS=true`와 `USE_SUPABASE_REPOSITORY=false` 조합은 in-memory repository를 사용하므로 해당 cycle의 데이터가 Desktop에 보이지 않는 것이 정상입니다.

## 환경 설정 요약

| 변수 | 위치 | 기본값/용도 |
| --- | --- | --- |
| `MOCK_PROVIDERS` | Worker | `true`, 외부 provider 대신 안전한 mock 사용 |
| `RUN_ONCE` | Worker | `false`, 한 cycle 후 종료 여부 |
| `USE_SUPABASE_REPOSITORY` | Worker | mock 실행 데이터를 Supabase에 기록할 때만 `true` |
| `BOT_DEFAULT_MODE` | Worker | `paper` |
| `SUPABASE_URL` | Worker | server-side repository URL |
| `SUPABASE_SECRET_KEY` | Worker | 서버 전용 secret, Desktop 금지 |
| `VITE_SUPABASE_URL` | Desktop | Supabase project URL |
| `VITE_SUPABASE_PUBLISHABLE_KEY` | Desktop | client publishable key |
| `VITE_SUPABASE_REALTIME_DISABLED` | Desktop | `true`이면 Realtime을 끄고 polling만 사용 |

Provider별 변수는 [Worker `.env.example`](apps/worker/.env.example)에서 확인하세요. 검증되지 않은 endpoint, parameter, rate limit은 구현하지 않고 [API Gaps](docs/API_GAPS.md)에 기록합니다.

## 권장 Paper Trading 운영 흐름

1. `enabled=false`, `mode=paper`, `live_order_allowed=false`를 확인합니다.
2. provider와 Supabase 연결 상태를 확인합니다.
3. 관심종목과 paper strategy를 준비합니다.
4. `run_paper_cycle_once`로 한 cycle을 실행합니다.
5. Desktop에서 decision, 주문 차단 이유, 수동 확인 큐, engine event, audit log를 검토합니다.
6. outcome update와 backtest를 실행해 전략 성능을 별도로 검증합니다.
7. `paper_health_report`가 경고·실패한 이유를 해소하기 전에는 다음 단계로 진행하지 않습니다.

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
| 대시보드 | 봇·heartbeat·provider 상태, 오늘 decision/order, 수동 확인 주문 경고 |
| 제어 | Paper 시작/정지, Emergency Stop, 별도 검토가 필요한 live 승인 요청 |
| 관심종목 | 분석 universe와 종목별 제한 관리 |
| 포트폴리오 | 동기화된 보유수량, 평가금액, 손익, 섹터 확인 |
| 주문 | Paper/blocked/live 상태와 `unknown_requires_manual_check` 안전 큐 |
| 시그널 | component score, feature/risk snapshot 확인 |
| 전략 Lab | paper 성과, backtest, draft, AI 후보 검토 |
| 로그 | engine event와 DB 변경 감사 이력 확인 |
| 설정 | Supabase admin 로그인과 안전 설정 관리 |

Desktop의 변경은 Supabase RLS와 DB trigger를 다시 통과합니다. UI의 성공 표시만으로 live readiness를 판단하지 마세요.

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
python .github/scripts/repository_safety.py migrations
python .github/scripts/repository_safety.py workflows
python supabase/verify_live_enable_migration.py
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

Render는 Background Worker 한 개를 실행하며 `autoDeployTrigger: "off"`를 유지합니다. 배포 전후에는 반드시 bot과 live permission을 끄고, target commit을 보고하는 새 heartbeat를 확인해야 합니다.

실제 절차는 [Render Deployment](docs/RENDER_DEPLOYMENT.md), [Release Process](docs/RELEASE_PROCESS.md), [Rollback](docs/ROLLBACK.md)을 따르세요. README는 의도적으로 live 활성화 절차나 secret 값을 제공하지 않습니다.

## 자주 발생하는 문제

### `python`, `node`, `npm`을 찾지 못함

사전 요구 버전을 설치하고 새 terminal을 여세요. Windows에서 Python launcher가 없다면 `py` 대신 `python`을 사용합니다.

### Desktop에 `권한 필요`가 표시됨

**설정** 화면에서 Supabase Auth admin 계정으로 로그인했는지, 해당 user id가 `public.user_roles`에 등록됐는지 확인하세요. RLS는 권한이 없는 조회에 빈 결과를 반환할 수 있습니다.

### Worker는 실행됐는데 Desktop에 데이터가 없음

mock 기본값은 in-memory repository입니다. Desktop에서 보려면 Worker에 `USE_SUPABASE_REPOSITORY=true`와 서버 전용 Supabase 설정이 필요합니다.

### 주문이 생성되지 않음

기본값 `enabled=false`는 의도된 안전 상태입니다. Paper cycle은 설정·quote·simulated cash·노출·중복 gate를 적용하고, live cycle은 여기에 동기화된 보유수량과 provider 증거를 추가합니다. gate가 실패하면 `blocked` 주문과 이유가 남습니다. 별도 Paper position ledger가 아직 없으므로 Paper 매도는 실계좌 보유수량을 재사용하지 않습니다.

### `unknown_requires_manual_check` 주문이 보임

자동 재시도하거나 UI에서 강제로 완료 처리하지 마세요. 주문 안전 큐와 [Runbook](docs/RUNBOOK.md)의 reconciliation 절차로 provider 상태와 감사 증거를 확인해야 합니다.

### Live mode가 계속 차단됨

정상 동작일 수 있습니다. hosted Supabase, provider lifecycle, incident ACK, system-order scope, retained artifact, 배포 freshness 증거가 하나라도 없으면 live는 fail-closed 상태를 유지합니다.

## 남은 주요 작업

- 체결에 따라 현금과 보유수량이 변하는 지속형 Paper 가상계좌
- 검증된 candle 기반 technical feature 자동 적재
- 목표 비중·no-trade band·현금 reserve·비용을 포함한 rebalance planner
- outcome, backtest, 월간 연구, retention의 안전한 무인 scheduling
- 권위 있는 일일 계좌 PnL snapshot과 독립적인 worker dead-man monitor
- 실제 hosted/provider 환경에서의 최종 live-readiness evidence

기능이 없다는 이유로 가짜 live endpoint나 mock 성공 응답을 추가하지 않습니다. provider 계약의 불확실성은 구현 차단 사유입니다.

## 문서 안내

- [Engine](docs/ENGINE.md) · [Risk Policy](docs/RISK_POLICY.md) · [Execution Policy](docs/EXECUTION_POLICY.md)
- [Database](docs/DATABASE.md) · [Supabase Setup](docs/SUPABASE_SETUP.md)
- [Security](docs/SECURITY.md) · [Threat Model](docs/THREAT_MODEL.md)
- [Runbook](docs/RUNBOOK.md) · [Incident Response](docs/INCIDENT_RESPONSE.md)
- [Backtesting Policy](docs/BACKTESTING_POLICY.md) · [AI Upgrade Policy](docs/AI_UPGRADE_POLICY.md)
- [API Connections](docs/API_CONNECTIONS.md) · [API Gaps](docs/API_GAPS.md)
- [Cost Limits](docs/COST_LIMITS.md) · [Observability](docs/OBSERVABILITY.md)

## 기여와 보안

변경 전 [Git Rules](docs/GIT_RULES.md), [Coding Standards](docs/CODING_STANDARDS.md), 각 디렉터리의 `AGENTS.md`를 확인하세요. risk, execution, broker, migration, workflow는 보호 영역이며 변경 이유·테스트·rollback 근거가 필요합니다.

비밀정보, 계좌 식별자, 실제 token, 미공개 취약점은 issue나 로그에 올리지 마세요. 보안 모델과 공개 범위는 [Security](docs/SECURITY.md)를 따릅니다.
