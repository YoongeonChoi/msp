# 로컬 PAPER 실행 가이드

이 절차는 PostgreSQL 17 기반 Local Supabase, 실제 Supabase Auth 세션, 지속형
Python Worker와 Tauri Desktop을 함께 실행합니다. 모든 주문 경계는 `paper`이고
Production Live 및 실제 broker 주문 transport는 계속 금지됩니다.

## 무엇이 자동화되는가

`npm run local-paper:bootstrap`은 다음 작업을 수행합니다.

1. Docker daemon과 Local Supabase를 준비합니다.
2. PostgreSQL 17 pgcrypto preflight 뒤 repository의 전체 migration inventory와
   `seed.sql`을 적용합니다.
3. Python 3.12 가상환경과 Worker 개발 의존성을 준비합니다.
4. 서로 다른 `operator`와 `risk_approver` Auth 사용자를 생성합니다.
5. 두 사용자에게 서로 다른 TOTP factor를 등록·검증하고 AAL2 세션을 확인합니다.
6. 역할과 account-opening evidence를 등록합니다.
7. operator가 opening을 요청하고 서로 다른 risk approver가 승인합니다.
8. Worker 전용 secret은 `apps/worker/.env`에, Desktop publishable key는
   `apps/desktop/.env.local-paper.local`에 기록합니다.

두 env 파일은 Git에서 제외됩니다. Bootstrap과 검증 명령은 secret, 비밀번호,
TOTP secret 또는 JWT를 출력하지 않습니다. `supabase start` 출력도 캡처·폐기하므로
local Worker key가 terminal transcript에 나타나지 않습니다. 자격 파일과 env 파일의
Windows ACL은 현재 사용자, `SYSTEM`, 로컬 `Administrators`만 접근하도록 상속을
제거합니다.

## 사전 요구사항

- Windows와 PowerShell 7 (`pwsh`)
- 실행 중인 Docker Desktop
- Python 3.12
- Node.js와 npm
- native 창을 사용할 경우 Rust stable, MSVC, WebView2

Bootstrap은 현재 Windows 사용자의 표준 per-user Python 3.12 설치 위치를 사용합니다.
설치 여부와 버전은 다음처럼 확인할 수 있습니다.

```powershell
$python312 = Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"
& $python312 --version
```

의존성이 아직 없다면 저장소 루트에서 한 번 실행합니다.

```powershell
npm ci
```

## 1. Bootstrap

Worker와 Desktop dev server를 먼저 종료한 상태에서 실행합니다.

```powershell
npm run local-paper:bootstrap
```

Bootstrap은 `supabase db reset`을 호출하지 않습니다. 기존 Local Supabase 데이터와
사용자 파일을 임의로 삭제하지 않습니다. 적용 migration version이 repository의
정확한 prefix일 때만 pending additive migration을 적용하고, 다른 version이 있으면
`migration repair`나 reset 대신 중단합니다. 기존 env가 이 도구가 만든 loopback
PAPER 설정이 아니라면 자동 백업·덮어쓰기를 하지 않고 중단하므로, 사용자가 원본을
직접 안전한 위치로 옮긴 뒤 다시 실행해야 합니다.

비밀번호와 TOTP URI는 다음 사용자 전용 로컬 파일에 있습니다.

```text
%LOCALAPPDATA%\kr-auto-trading-lab\local-paper-credentials.json
```

이 파일은 공유하거나 Git에 추가하지 마십시오.

사용자가 직접 해야 하는 일은 두 `totpUri`를 서로 구분되는 인증 앱 항목으로
등록하는 것입니다. JSON 전체를 공유하지 말고 다음 필드만 각 사용자 본인의
authenticator로 가져옵니다.

- 요청자: `operator.totpUri`
- 검토자: `risk.totpUri`

두 URI는 서로 다른 factor이며 같은 사람·같은 브라우저 세션으로 요청과 검토를
동시에 수행하는 용도가 아닙니다. 가능하면 요청자와 검토자가 각자 보유한 별도
인증기기를 사용하십시오.

## 2. 실제 Desktop과 Worker 실행

native Tauri Desktop을 실행하는 기본 명령입니다.

```powershell
npm run local-paper:desktop
```

이 명령은 먼저 Worker를 백그라운드에서 실행하고 active
`paper-primary` lease를 확인한 다음, 승인된 account-opening 명령의 Worker ACK와
균형 journal·10,000,000 KRW cash postcondition을 기다리고 Tauri 창을 엽니다.
Tauri 창을 닫거나
terminal에서 `Ctrl+C`를 누르면 UI가 종료되지만 Worker는 명시적으로 stop할 때까지
유지됩니다.

브라우저에서만 확인하려면 다음을 실행합니다.

```powershell
npm run local-paper:start
```

그다음 아래 주소를 엽니다.

```text
http://127.0.0.1:1421/
```

`1421`은 이 절차의 local PAPER Desktop 전용 loopback 포트입니다. 다른 프로세스가
이미 사용 중이면 launcher는 포트를 재사용하거나 해당 프로세스를 종료하지 않고
fail-closed로 중단합니다.

Worker만 실행하려면 다음을 사용합니다.

```powershell
npm run local-paper:worker
```

## 3. 로그인과 TOTP

자격 파일의 `operator.email`/`operator.password`로 요청자 세션에 로그인하고,
`operator.totpUri`를 authenticator에 등록합니다. 검토자는 `risk` 항목을 사용합니다.

TOTP factor 등록은 유지되지만 AAL2는 로그인 세션별 상태입니다. 안전 관련 요청과
검토는 최근 5분 이내의 TOTP 검증을 요구하므로, UI가 step-up을 요청하면 최신 코드를
다시 입력해야 합니다.

두 세션을 동시에 확인하려면 다음 구성을 사용할 수 있습니다.

- Tauri Desktop: operator
- 같은 `http://127.0.0.1:1421/`을 연 별도 브라우저: risk approver

Tauri WebView와 브라우저는 세션 저장소가 분리됩니다. 한 브라우저만 사용하는 경우
요청 후 로그아웃하고 다른 사용자로 다시 로그인해도 durable 요청은 유지됩니다.

## 4. 상태 검증

Worker가 실행 중인 terminal과 별개 terminal에서 실행합니다.

```powershell
npm run local-paper:verify
```

검증 범위:

- repository migration 전체 inventory와 fail-closed seed
- loopback URL 및 publishable/Worker key 분리
- LIVE·Toss order endpoint 영구 비활성
- 서로 다른 두 Auth 사용자, verified TOTP, AAL2 및 역할
- 설정된 Worker UUID의 active lease
- account opening, 균형 journal, 10,000,000 KRW cash projection

이 명령은 두 사용자의 새 AAL2 검증 세션을 만들기 때문에 이미 열려 있는 Desktop
세션은 다시 로그인해야 할 수 있습니다. 먼저 검증을 실행한 뒤 Desktop에 로그인하는
순서가 가장 명확합니다.

브라우저 Desktop의 1421 응답까지 확인하려면 다음을 사용합니다.

```powershell
npm run local-paper:verify-desktop
```

요청자 명령 → 다른 사용자 승인 → Worker ACK → postcondition까지 실제 mutation으로
재검증하려면 다음을 사용합니다. 이 명령은 안전한 `pause_paper`를 적용하며 최종
상태를 `execution_enabled=false`로 남깁니다.

```powershell
npm run local-paper:verify-flow
```

이 smoke는 release qualification 없이도 허용되는 `pause_paper`만 사용합니다. 따라서
qualification을 위조하지 않고도 요청자 → 다른 검토자 → Worker claim/ACK →
`execution_enabled=false` postcondition을 확인할 수 있습니다.
Fresh Local Supabase에서도 opening 요청·승인·ACK를 먼저 완료한 뒤 같은 smoke를
실행하므로, 아직 열리지 않은 `paper-primary` 때문에 사전 검증에서 중단되지 않습니다.

현재 release의 최종 qualification gate만 별도로 확인하려면 다음을 실행합니다.

```powershell
npm run local-paper:verify-release
```

로컬 환경에서는 이 명령이 `maker/checker qualification finalization` 누락으로
실패하는 것이 정상입니다. 로컬 contract simulator의 PASS는 release qualification이
아닙니다. 최종 PAPER G1/G2를 완료하려면 사용자가 승인한 Hosted staging에서 아래
증거를 실제로 확보하고 서로 다른 release manager가 finalize해야 합니다.

- Hosted staging에 exact release SHA와 전체 migration/checksum 적용
- 공식 provider contract artifact와 release-bound qualification evidence
- 24시간 fault soak 및 10개 연속 Paper/Shadow 거래일
- ledger imbalance, fictional sell, duplicate order/journal 0건
- open Sev1/Sev2 및 P0/P1 0건
- 외부 alert receiver ACK와 immutable audit archive receipt
- 별도 failure domain monitor
- hosted committed-ledger RPO 증거
- 격리 복원 후 30분 이내 operator-ready restore drill

이 항목은 로컬 DB row를 수동 삽입해 대신할 수 없습니다. Hosted 프로젝트 적용과
finalization은 해당 환경 credential과 명시적 배포 승인이 준비된 뒤 별도로 수행합니다.

## 5. 종료

관리되는 Vite와 Worker 프로세스만 종료합니다.

```powershell
npm run local-paper:stop
```

Worker가 비정상 종료돼도 lease가 만료될 때까지 최대 30초가 걸릴 수 있습니다.
Stop 스크립트는 lease가 더 이상 active가 아님을 확인합니다.

Local Supabase 컨테이너도 데이터 reset 없이 중지하려면 다음을 사용합니다.

```powershell
npm run local-paper:stop -- -StopSupabase
```

다시 실행할 때는 bootstrap으로 컨테이너와 설정을 확인한 뒤 Desktop을 시작합니다.

## 로그와 문제 해결

관리되는 프로세스의 로그는 다음 위치에 남습니다.

```text
%LOCALAPPDATA%\kr-auto-trading-lab\worker.stdout.log
%LOCALAPPDATA%\kr-auto-trading-lab\worker.stderr.log
%LOCALAPPDATA%\kr-auto-trading-lab\desktop.stdout.log
%LOCALAPPDATA%\kr-auto-trading-lab\desktop.stderr.log
```

`1421`이 이미 사용 중이면 launcher는 임의로 프로세스를 종료하지 않고 중단합니다.
정확한 점유 프로세스를 확인한 뒤 사용자가 명시적으로 종료해야 합니다.

```powershell
Get-NetTCPConnection -State Listen -LocalPort 1421 |
  Select-Object LocalAddress, LocalPort, OwningProcess
```

Docker Desktop, Worker, Tauri/Vite를 실행 상태로 유지해야 UI가 실제 control plane과
통신합니다. 외부 broker credential, Hosted Supabase secret, Production 데이터는 이
절차에 제공하지 마십시오.
