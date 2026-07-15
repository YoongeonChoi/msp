# Archived Live Readiness Work Summary

> 상태: `ARCHIVED / SUPERSEDED BY G1+G2`

이 문서는 과거 live-readiness 작업 이름을 유지하되, 더 이상 운영 절차나 승인
근거가 아니다. 현재 릴리스 트레인은 내부 전용 `paper`와 로컬
`contract_test`만 허용하고 Production Live를 네 계층에서 차단한다.

## What remains useful

- 과거 provider 불확실성, reconciliation 실패, alert 전달 실패에서 얻은
  fail-closed 요구사항
- legacy 코드가 주문 네트워크를 호출하지 않는지 확인하는 regression test
- 자격증명·host·schema drift를 탐지하는 정적 안전 검사

## What is superseded

- `request_live_enable`, live approval, live activation 또는
  `live_order_allowed=true`를 목표로 하는 모든 절차
- provider sandbox/live 주문 lifecycle을 릴리스 승인으로 사용하는 절차
- 점수 기반 readiness 주장과 hosted 환경 미실행 상태의 완료 주장

위 항목은 실행하지 않는다. 관련 legacy 도구가 저장소에 남아 있더라도 현재
startup/config/network quarantine을 우회할 권한을 만들지 않는다.

## Current workstream

현재 구현과 남은 외부 증거는 [기업 프로그램 계획](ENTERPRISE_PROGRAM_PLAN.md),
[G0 운영 경계](G0_OPERATING_BOUNDARY.md), [Runbook](RUNBOOK.md),
[테스트 계획](TEST_PLAN.md)을 따른다. Hosted Supabase/Render 적용은 별도 사용자
승인 전에는 수행하지 않는다.
