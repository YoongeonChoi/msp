# Windows Desktop 설치 파일

이 문서는 현재 소스에서 Tauri Desktop의 NSIS 설치형 `.exe`를 만들고 설치·검증하는
절차를 정의합니다. Desktop은 Supabase 운영 Cockpit이며 Python Worker나 Render
서비스를 설치하거나 시작하지 않습니다. Production Live 주문도 지원하지 않습니다.

## 결과물과 보안 경계

- 결과물: `apps/desktop/src-tauri/target/release/bundle/nsis/*-setup.exe`
- 설치 범위: 기본 NSIS `currentUser` 모드 (`%LOCALAPPDATA%`, 관리자 권한 불필요)
- 허용되는 빌드 입력: `VITE_SUPABASE_URL`, `VITE_SUPABASE_PUBLISHABLE_KEY`
- 금지되는 입력: Supabase secret/service-role key, broker credential, Render secret,
  OpenAI/provider key, 계좌 식별자
- 설치 파일은 설정된 hosted Supabase URL과 publishable key를 빌드 시 포함합니다.
  설치 뒤 `.env.local`을 바꿔도 이미 만들어진 `.exe`의 연결 대상은 바뀌지 않습니다.

Desktop publishable key는 RLS를 우회하지 않습니다. 그래도 server-only secret을
`VITE_` 변수에 넣으면 브라우저 번들에 공개되므로 빌드 사전검사가 허용된 두 변수
외의 `VITE_` 입력을 거부합니다.

## 사전 요구사항

Windows 10/11 x64에서 다음을 준비합니다.

1. Node.js 22 이상과 npm
2. Rust stable MSVC toolchain
3. Tauri 2 Windows prerequisites와 WebView2
4. Playwright Chromium (`npx playwright install chromium`)
5. 저장소 root에서 `npm ci`

`apps/desktop/.env.local`은 Git에 추가하지 않고 다음 공개 설정만 둡니다.

```dotenv
VITE_SUPABASE_URL=https://<project-ref>.supabase.co
VITE_SUPABASE_PUBLISHABLE_KEY=sb_publishable_<public-value>
```

값을 화면이나 로그에 출력하지 않고 설정만 검사하려면 다음을 실행합니다.

```powershell
npm run desktop:bundle:windows -- -PreflightOnly
```

## 한 번에 검사하고 빌드하기

저장소 root에서 실행합니다.

```powershell
npm run desktop:bundle:windows
```

이 명령은 순서대로 공개 설정 사전검사, Desktop lint/typecheck/contract test,
Playwright E2E, locked Cargo check/test, Vite production build와 NSIS packaging을
실행합니다. 성공하면 최신 `setup.exe`, SHA-256과 Authenticode 상태를 출력하고
설치 파일 옆에 `.sha256` 파일을 만듭니다. 과거 `target` 파일이 남아 있어도 현재
실행에서 갱신된 설치 파일이 정확히 하나가 아니면 실패합니다.

공식 배포 후보에서 유효한 코드 서명을 필수화하려면 다음을 사용합니다.

```powershell
npm run desktop:bundle:windows -- -RequireSignature
```

## 설치와 실행 확인

1. 출력된 `*-setup.exe`와 같은 위치의 `.sha256`을 보관합니다.
2. 설치 전 해시를 다시 계산해 `.sha256`과 일치하는지 확인합니다.

   ```powershell
   Get-FileHash -Algorithm SHA256 -LiteralPath '<setup.exe path>'
   ```

3. 설치 파일을 실행합니다. 기본값은 현재 사용자 설치이므로 관리자 권한을 요구하지
   않습니다.
4. 앱에서 로그인 화면 또는 fail-closed 연결 안내가 보이고 빈 창/즉시 종료가 없는지
   확인합니다.
5. 인증 뒤 Dashboard, Operations, Settings를 열어 RLS/RPC 경계가 유지되는지
   확인합니다.

같은 `identifier`와 더 높은 버전으로 만든 설치 파일은 기존 설치를 갱신합니다.
제거는 Windows 설정의 **설치된 앱**에서 `KR Auto Trading Lab`을 선택합니다.

## 코드 서명

인증서가 없는 로컬 빌드는 정상적으로 실행 가능한 unsigned 설치 파일이지만 Windows
SmartScreen에서 **알 수 없는 게시자** 경고가 날 수 있습니다. 이 경고를 숨기기 위해
임의 self-signed 인증서를 신뢰 증거처럼 사용하지 않습니다. 외부 배포 전 소유자가
법적 publisher와 Windows code-signing 인증서 또는 승인된 signing service를 준비하고,
Tauri 서명 설정을 별도 검토해야 합니다. Authenticode 서명과 SHA-256은 서로 다른
검증이며 둘 다 보존합니다.

## Worker와 Render

설치형 Desktop만 실행해도 Background Worker가 시작되지는 않습니다. Hosted Paper
운영에는 Render Worker의 정상 heartbeat가 별도로 필요합니다.

현재 `ENV=production` Worker는 승인된 HTTPS alert receiver와 다음 current 설정이
없으면 의도적으로 startup을 거부합니다.

- `ALERT_WEBHOOK_URL`
- `ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_ID`
- `ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_B64` (canonical base64 32-byte key)

키 rotation 중에는 다음 previous pair를 둘 다 설정합니다.

- `ALERT_WEBHOOK_RECEIVER_ACK_PREVIOUS_KEY_ID`
- `ALERT_WEBHOOK_RECEIVER_ACK_PREVIOUS_KEY_B64`

rotation 기간이 아니면 previous ID/key는 둘 다 없거나 빈 값일 수 있지만 current
ID/key와 URL은 필요합니다. receiver를 검증하지 않은 임시 URL, HTTP URL, 키 검증 비활성화,
`ENV` 하향으로 startup gate를 우회하지 않습니다. 값을 Render에 저장한 뒤에도
[Render Deployment](RENDER_DEPLOYMENT.md)의 manual deploy와 heartbeat 확인을
수행해야 합니다.

## 문제 해결

- `Desktop release configuration rejected`: `.env.local`의 두 공개 변수 이름과
  hosted HTTPS URL/`sb_publishable_` 형식을 확인합니다. 실제 값은 로그에 붙이지 않습니다.
- Playwright executable 없음: `npx playwright install chromium`을 한 번 실행합니다.
- NSIS bundling 실패: `npm --workspace apps/desktop run tauri -- info`로 MSVC,
  WebView2와 Rust target을 확인합니다.
- 앱은 열리지만 데이터가 없음: 빌드된 Supabase project, 사용자 인증, RLS/RPC와
  Render Worker heartbeat를 각각 확인합니다.
- SmartScreen 경고: 해시만 확인했다고 publisher 신뢰가 생기지는 않습니다. 공식
  배포에는 유효한 Authenticode 서명이 필요합니다.
