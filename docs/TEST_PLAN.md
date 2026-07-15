# G1+G2 Test Plan

이 계획은 로컬 저장소 구현의 자동 검증과 별도 승인이 필요한 운영 증거를 구분한다.
테스트 통과만으로 Hosted Staging, G1, G2 또는 Production Live를 승인하지 않는다.

## 1. 데이터베이스와 권한

- fresh PostgreSQL과 기존 `0015` fixture에서 전체 migration과 seed를 실제 적용한다.
- `private` source of truth, 최소 `api` surface, allowlist된 `worker_api` RPC만 남는다.
- exposed table/view마다 RLS와 명시적 GRANT를 검증한다.
- `anon`, `authenticated`, `service_role` 및 7개 application role의 positive/negative
  matrix를 검증한다.
- `worker_api`는 authenticated/anon/public 실행이 불가능하고 service role만 호출한다.
- exposed wrapper는 `SECURITY INVOKER`; private definer는 `search_path=''`와 제한된
  EXECUTE만 가진다.
- legacy public order/control write, Live command 생성·변경, Live settings tuple을
  모두 거부한다.
- `audit_events` UPDATE/DELETE와 hash-chain mutation을 일반 사용자·Worker 모두
  거부한다.

## 2. G1 실행·회계 불변식

- 동일 semantic intent 100개 경쟁 요청에서 정확히 하나만 예약된다.
- semantic hash는 Python과 PostgreSQL의 canonical byte contract가 일치한다.
- decision/risk/account/environment/strategy/policy/cost evidence가 없거나 유효기간이
  어긋나면 예약하지 않는다.
- stale lease, fencing token, `control_epoch`, disabled gate를 reserve와 dispatch에서
  각각 거부한다.
- 매수 cash commitment와 매도 가능 수량을 원자적으로 예약한다.
- partial fill은 예약을 부분 소비하고 terminal cancel/reject/expire는 잔량을 해제한다.
- cash, reserved cash, position, reserved quantity는 음수가 될 수 없다.
- opening journal은 `paper-primary`에 10,000,000 KRW를 한 번만 기록한다.
- 모든 accounting transaction은 debit 합계와 credit 합계가 같다.
- `moving_weighted_average_v1` 매수 원가와 부분 매도 cost relief/realized PnL을
  검증한다.
- 같은 observation/fill 재처리는 projection과 posting을 바꾸지 않는다.
- cumulative fill 감소, terminal 회귀, provider identity 불일치는 quarantined가 된다.
- unknown은 자동 terminal/재전송되지 않고 2인 검토 전까지 차단된다.
- unknown 또는 다른 비-legacy reconciliation break가 생기면 같은 transaction에서
  account execution이 disabled 되고 `control_epoch`가 증가하며, 미해결 상태에서는
  새 intent insert가 거부된다.
- 현재 로컬 release에서 unknown 케이스는 읽기 전용으로 노출한다. evidence-specific
  회계 복구와 2인 종결 RPC가 추가·검증되기 전에는 해당 intent를 manual 상태에서
  해제하지 않으며, 이 항목은 G1/G2 미통과 사유로 남는다.
- reconciliation keyset pagination이 오래된 50건 뒤의 주문을 굶기지 않는다.

## 3. 결정론적 Paper와 contract_test

- KRW, whole-share, `LIMIT`, `DAY`, buy/sell만 허용한다.
- decision 이후 첫 완전한 1분 bar 이전에는 체결하지 않는다.
- bar volume 참여율 1%, 불리한 slippage 10 bps, limit 비침범을 검증한다.
- 부분체결과 잔량 만료를 검증한다.
- 승인된 cost schedule, tick rule, volume evidence, corporate-action state 중 하나라도
  없거나 stale이면 체결하지 않는다.
- 로컬 `contract_test`의 create→status→partial→terminal 및 cancel lifecycle과
  timeout/5xx/malformed/duplicate/unknown fault injection을 검증한다.
- production Toss order host로 향하는 write request 수가 정확히 0인지 검증한다.

## 4. 복구·outbox·HA

- reserve, dispatch, provider response, ledger commit 각 crash point에서 재시작 후
  중복 주문·중복 분개 없이 복구한다.
- active lease 단일 소유, monotonic fencing, expiry reclaim, stale holder rejection을
  검증한다.
- reconciliation completion은 현재 account lease의 release SHA와 fencing token을
  재검증하고, caller가 제출한 시각이 아니라 DB 시각으로 lease expiry를 판정한다.
- outbox `SKIP LOCKED`, DB-clock lease reclaim, complete/fail ACK, exponential
  backoff, dedupe, dead-letter를 검증한다.
- alert transport 장애에서도 domain transaction은 보존되고 at-least-once delivery와
  수신측 dedupe가 성립한다.
- emergency stop은 같은 transaction에서 `enabled=false`, `control_epoch+1`을 만든다.
- command는 `requested → approved → claimed → applied` 순서를 벗어나지 않는다.
- qualification은 request뿐 아니라 Worker apply 시점에도 유효기간과 모든 policy,
  strategy, release, provider pin이 일치해야 한다.
- self approval, stale CAS, AAL1, 만료/재사용/다른 hash의 step-up grant를 거부한다.

## 5. Desktop

- strict Zod V1 계약은 unknown field, 누락 version, 잘못된 UUID/time/SHA를
  `DataContractError`로 처리한다.
- 파싱 실패 시 숫자/boolean/environment 기본값을 만들지 않고 mutation을 차단한다.
- 역할별 action 허용/거부, maker/checker, platform_admin 거래 승인 불가를 검증한다.
- ACK와 runtime postcondition 전에는 성공 표시를 하지 않는다.
- Realtime 단절, stale heartbeat, offline, session expiry에서 mutation을 차단한다.
- offline emergency stop은 “전송되지 않음”이며 reconnect 자동 replay가 없다.
- keyboard-only, focus restore, `aria-live`, axe critical/serious 0을 검증한다.
- ESLint, TypeScript, component/unit, Vite build, Playwright, Cargo check/test/build를
  CI 필수 job으로 실행한다.

## 6. 운영 증거 — 로컬 자동 테스트 밖

다음은 실제 환경과 서로 다른 두 명의 운영 사용자가 필요하므로 별도 사용자 승인
전에는 완료로 표시하지 않는다.

- Supabase Auth TOTP enrollment와 실제 AAL2 세션
- 외부 immutable audit archive receipt와 DB hash 대조
- 실제 alert channel 전달·5분 human ACK·escalation
- active/warm standby와 독립 failure-domain dead-man monitor
- isolated restore drill로 시장시간 RTO 30분 측정
- Hosted DB disaster-scope RPO 증명
- 24시간 fault soak와 연속 10거래일 Paper/Shadow 운영
- ledger imbalance, 허구 매도, 중복 주문·분개, Sev1/Sev2/P0/P1 최종 게이트

Hosted Staging 적용과 외부 credential 사용은 별도 사용자 승인 없이는 수행하지 않는다.
Production Live는 이 테스트 계획의 후속 게이트가 아니라 금지 경계다.
