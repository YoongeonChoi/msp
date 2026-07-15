# Archived Live Readiness Scorecard

> 상태: `ARCHIVED / NON-AUTHORITATIVE`
>
> 이 파일은 과거 Production Live readiness 작업의 이름을 보존하기 위한
> 역사 문서다. 현재 프로그램은 `paper | contract_test`만 지원하며 Production
> Live는 UI, DB, 설정, credential, network 네 계층에서 금지된다.

## Current interpretation

- 과거 점수, live-enable RPC, hosted activation drill, sandbox/live 주문 증거는
  현재 G1/G2 승인 근거로 사용하지 않는다.
- legacy `public.orders`, `public.positions`, `manual_commands`, `audit_logs`는
  `legacy_unreconciled` evidence이며 V2 현금·포지션·성과에 합산하지 않는다.
- Toss production order create/status/cancel은 호출하지 않는다. 로컬 계약
  시뮬레이터는 공식 sandbox로 표시하지 않는다.
- 외부 주문, 고객, 다법인, 다계좌 요구가 생기면 기능 플래그를 열지 않고 G0
  사업·규제 심사를 새로 시작한다.

## Authoritative gates

현재 판단은 다음 문서와 실제 검증 산출물을 사용한다.

- [G0 운영 경계](G0_OPERATING_BOUNDARY.md)
- [기업 프로그램 계획](ENTERPRISE_PROGRAM_PLAN.md)
- [Execution safety kernel ADR](00_DECISIONS/ADR-0007-execution-safety-kernel.md)
- [테스트 계획](TEST_PLAN.md)
- [배포 절차](RENDER_DEPLOYMENT.md)

G1/G2는 코드가 존재한다는 이유로 통과하지 않는다. 별도 Hosted Staging 승인,
두 명의 AAL2 운영 사용자, 외부 alert/archive destination, 24시간 fault soak,
10거래일 Paper/Shadow, restore drill 및 RPO/RTO 증거가 모두 남아야 한다.

## Production Live decision

`NOT AUTHORIZED`. 현재 저장소에는 Production Live 활성화 절차가 없으며, 과거
live-readiness 도구와 migration은 legacy hard-quarantine 검증 대상으로만 남는다.
