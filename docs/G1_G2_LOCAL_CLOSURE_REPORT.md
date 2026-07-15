# G1+G2 로컬 구현 종료 보고서

기준일: 2026-07-15
적용 범위: repository 구현 및 로컬 격리 검증
릴리스 판정: **로컬 구현 범위 존재 / Hosted·운영 게이트 차단**

## 1. 결론

G1 Paper Truth와 G2 Operational Readiness를 지원하는 source-of-truth,
Worker 경계, Desktop projection, 로컬 계약 시뮬레이터가 저장소에 구현되어 있다.
그러나 이 문서는 G0, G1, G2 또는 Hosted Staging의 최종 승인을 선언하지 않는다.
운영 기간, 사람 승인, 외부 전달/보관, 별도 failure domain, hosted RPO/RTO 증거는
로컬 코드와 일회성 테스트로 대체할 수 없다.

Production Live는 이번 범위가 아니다. 실제 Toss 주문 create/cancel/modify transport,
order-capable credential, Production Live UI/DB 효과를 추가하지 않는다. 로컬
`contract_test`는 공식 sandbox가 아니다.

## 2. 스키마 기준선

`0016_private_foundation.sql`부터 `0024_operational_upgrade_convergence.sql`까지의
V2 기준선 뒤에 다음 migration을 순서대로 적용한다.

| 순서 | Migration                                                  | 로컬 구현 범위                                                                  |
| ---- | ---------------------------------------------------------- | ------------------------------------------------------------------------------- |
| 1    | `20260714154520_control_qualification_workflow.sql`        | reference bundle, qualification run/finalization, release-bound approval        |
| 2    | `20260714155117_paper_execution_source.sql`                | immutable minute bars, Paper candidate, restart-safe claim/load/complete        |
| 3    | `20260714155744_cash_settlement_maturity.sql`              | trade-date clearing, KST settlement obligation, pending cash, retry/dead-letter |
| 4    | `20260714160105_operations_runtime_scheduler.sql`          | 독립 stage cadence와 heartbeat stage evidence                                   |
| 5    | `20260714161511_unknown_execution_resolution_v2.sql`       | Unknown V2 maker/checker와 전용 Worker list/claim/apply                         |
| 6    | `20260714165910_unknown_resolution_desktop_projection.sql` | Desktop용 strict Unknown V2 case projection                                     |
| 7    | `20260715020752_kst_trading_date_convergence.sql`          | reserve/candidate/Unknown/qualification의 KST calendar-date 수렴                |

Canonical 순서는 [Supabase README](../supabase/README.md)와
[Supabase Setup](SUPABASE_SETUP.md)에 유지한다. `seed.sql`은 전체 migration 뒤에
적용하며 non-live 로컬 기본값만 포함한다.

## 3. 로컬 구현 증거

### Paper source와 결정론적 실행

- `apps/worker/app/tools/publish_paper_execution_source_once.py`는 한 개의 strict
  `schema_version=1` fixture/candidate 파일과 별도로 계산한 SHA-256을 요구한다.
- `EXECUTION_V2_PAPER_SOURCE_INPUT_ENABLED` 기본값은 false다. 명시적으로 켠
  `paper` Worker만 `ingest_paper_bar_fixture_v1`과
  `enqueue_paper_execution_candidate_v1`을 호출할 수 있다.
- DB는 active lease/fencing token, `control_epoch`, release, qualification,
  policy/cost/calendar/tick/volume/corporate-action evidence를 다시 확인한다.
- 이 도구는 자동 strategy signal producer나 자동 market-data collector가 아니다.
  파일 hash는 byte identity만 증명하며 데이터 출처 승인이나 정확성을 증명하지
  않는다.

### 현금 결제 원장

- fill 시점의 position, cost basis, realized PnL은 trade date에 기록한다.
- cash leg는 payable/receivable clearing으로 재분류하고
  `pending_debit_cash_krw`/`pending_credit_cash_krw`로 projection한다.
- obligation maturity는 `Asia/Seoul` 날짜로 판정한다. future receivable은 settled
  cash가 아니다.
- reserve, Paper candidate eligibility/expiry, Unknown fill, qualification
  window도 같은 `Asia/Seoul` 날짜를 사용해 00:00~08:59 KST 경계를 수렴한다.
- settlement stage는 lease/release/fencing token에 묶인 claim/complete/fail,
  exact replay, bounded retry와 dead-letter evidence를 사용한다.

### Unknown V2

- unknown observation은 먼저 quarantine되고 자동 재전송되지 않는다.
- V1 resolution은 evidence-only다. V2만 서로 다른 operator와 risk approver의
  request/review 뒤 accounting mutation을 허용한다.
- Worker는 generic command ACK가 아니라
  `list_unknown_resolution_v2` → `claim_unknown_resolution_v2` →
  `apply_unknown_resolution_v2`를 사용한다.
- request/review digest, command/work revision, terminal status,
  `control_epoch`, release, lease, fencing token이 일치해야 한다. exact replay는
  동일 fill, journal, settlement obligation을 중복 생성하지 않아야 한다.
- Desktop은 `api.get_unknown_resolution_cases_v2` projection을 사용하며 private
  evidence table을 직접 읽지 않는다.

### 로컬 계약 자격 검증

- `apps/worker/app/tools/run_contract_qualification_once.py`는 기록된 OpenAPI
  SHA-256에 묶인 `local_contract_simulator`만 실행한다.
- manifest는 create replay, partial-to-terminal status, cancel, 16개 fault
  scenario, `production_order_network_zero`를 포함한다.
- network check의 요구값은 `request_count=0`이다. 이 출력만으로 DB의
  release-bound qualification 등록/finalization이나 Hosted 승인이 완료되지는 않는다.

## 4. 최종 로컬 검증 묶음

2026-07-15의 현재 uncommitted worktree snapshot에서 다음 결과를 확인했다.

| 검증                                                                        | 결과                                                               |
| --------------------------------------------------------------------------- | ------------------------------------------------------------------ |
| 전체 fresh migration, seed, populated `0015`/`0023` upgrade, 실제 PostgREST | PASS (`FINAL=PASS g1_g2_migration_verifier`)                       |
| durable Paper source fixture/candidate/claim/reclaim/manual-outcome         | PASS                                                               |
| Worker pytest                                                               | PASS (`1012 passed`)                                               |
| Worker Ruff / Mypy                                                          | PASS (`336 source files`)                                          |
| local contract qualification                                                | PASS (`16` fault scenarios, production order request `0`)          |
| Desktop ESLint / unit / typecheck / Vite build                              | PASS                                                               |
| Desktop Playwright                                                          | PASS (`8 passed`, desktop/mobile/Unknown V2/offline/accessibility) |
| migration-order repository assertion / `git diff --check`                   | PASS                                                               |
| Tauri/Rust check·test·build                                                 | NOT RUN — 현재 로컬 환경에 `cargo`/`rustc` 없음                    |

이 결과는 worktree 단위 로컬 증거이며 commit SHA나 Hosted 환경 증거가 아니다.
Tauri/Rust 미실행 상태에서는 전체 릴리스 검증 완료를 선언하지 않는다.

아래 명령은 동일 최종 snapshot에서 다시 실행하고 PASS 로그를 release evidence에
첨부해야 한다. 이 문서는 실행되지 않은 항목을 통과로 간주하지 않는다.

```powershell
python supabase/verify_g1_g2_migration.py

cd apps/worker
py -m pytest
py -m ruff check app
py -m mypy app
py -m app.tools.run_contract_qualification_once

cd ../..
npm run desktop:lint
npm run desktop:test
npm run desktop:typecheck
npm run desktop:build
npm run desktop:e2e
```

추가로 Tauri/Rust check·test·build와 repository migration-order check를 같은
release SHA에 대해 실행한다. 로컬 fixture publisher의 성공은 승인된 실제 데이터
upstream이나 장기 Paper 운용의 증거가 아니다.

## 5. 완료되지 않은 외부 게이트

| 게이트               | 현재 판정 | 필요한 증거                                                                  |
| -------------------- | --------- | ---------------------------------------------------------------------------- |
| G0 책임자/사용자     | 차단      | 책임자와 서로 다른 운영 사용자 2명 지정, 내부 전용·NO-LIVE 승인              |
| Hosted Staging       | 차단      | 별도 사용자 승인, 전용 Supabase/Render 자격증명, synthetic/redacted 데이터   |
| 장기 Paper 운용      | 차단      | 24시간 fault soak와 연속 10거래일 Paper/Shadow 기록                          |
| Alert·감사 외부 증거 | 차단      | 실제 사람의 critical ACK, 외부 immutable archive receipt와 DB hash 일치      |
| Failure-domain 분리  | 차단      | Worker와 다른 failure domain의 dead-man monitor 및 검증된 standby 운영       |
| Hosted RPO           | 차단      | hosted 장애 범위에서 committed-ledger RPO 0 지원 및 실제 증명                |
| Restore RTO          | 차단      | isolated restore, reconciliation, operator-ready까지 시장시간 30분 이내 기록 |
| 최종 품질 게이트     | 차단      | imbalance/허구 매도/중복 주문·분개 0, 미해결 Sev1/Sev2·P0/P1 0의 운용 증거   |

## 6. 다음 승인 순서

1. 동일 최종 commit SHA에서 전체 로컬 검증 묶음을 실행하고 결과를 보존한다.
2. G0 책임자와 두 운영 사용자를 지정하고 NO-LIVE 경계를 승인한다.
3. 사용자가 별도로 승인한 뒤에만 전용 Hosted Staging에 migration을 적용한다.
4. staging에서 release-bound qualification, 외부 alert/archive, failure-domain,
   RPO/RTO 증거를 수집한다.
5. 24시간 fault soak와 10거래일 Paper/Shadow 결과가 모두 충족될 때만 G1/G2
   최종 승인 회의를 연다.

현재 판정은 **Hosted 적용·Production Live·G1/G2 최종 승인 NO-GO**다. 이 차단은
로컬 구현 실패를 의미하는 것이 아니라, 외부 운영 증거가 아직 존재하지 않는다는
의미다.
