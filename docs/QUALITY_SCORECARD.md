# Engineering Quality Scorecard

> **Historical baseline:** 이 문서는 G1+G2 V2 전환 전의 품질 평가를 보존한
> 자료다. 현재 승인 기준, 검증 결과 또는 Production Live readiness를 나타내지
> 않는다. 현재 gate는 `ENTERPRISE_PROGRAM_PLAN.md`, ADR-0007 및
> `TEST_PLAN.md`를 따른다.

평가일: 2026-07-14 KST
기준선: `c58a7f8`
개선 후보: 당시 working tree snapshot (현재 V2 후보와 무관)

## 이 점수의 의미

이 문서는 코드, 운영성, 테스트, 문서의 **engineering quality**를 비교하기 위한
과거 내부 점수표다. 투자 성과, 수익 가능성, 보안 보증, 배포 승인 또는 현재
gate 통과를 의미하지 않는다. 높은 점수는 어떤 형태의 실주문 승인도 만들지
않는다.

평가는 각 항목을 0~100으로 채점한 뒤 가중 평균한다.

- 90~100: 강한 근거와 자동 검증이 있는 상태
- 80~89: 운영 가능한 기반이 있으나 명확한 후속 작업이 남은 상태
- 70~79: 핵심 흐름은 있으나 중요한 기능·검증 공백이 있는 상태
- 60~69: 제한적 사용은 가능하지만 운영 리스크가 큰 상태
- 0~59: 설계 또는 구현이 우선 필요한 상태

## 당시 평가 점수 (현재 gate에 사용 금지)

| 평가 영역 | 가중치 | 개선 전 | 개선 후 | 근거 |
| --- | ---: | ---: | ---: | --- |
| 거래 안전 경계 | 20% | 92 | 96 | buying power와 sell inventory를 `RiskService`에서 broker 호출 전에 차단하고, risk와 execution이 같은 whole-share 계산을 사용한다. |
| 기능 완성도 | 15% | 68 | 76 | 수동 확인 주문 안전 큐와 audit viewer를 추가했다. 지속형 Paper 계좌와 scheduler는 아직 없다. |
| 데이터·연구 무결성 | 10% | 70 | 86 | decision 가격과 실제 정수 주문 수량·가격을 보존하고 outcome이 이를 우선 사용한다. 비유한·비양수 가격도 거부한다. 자동 candle feature 적재는 남아 있다. |
| 운영 가시성 | 15% | 65 | 90 | 미확정 live 주문의 exact count와 페이지형 상세를 분리하고, 원본 snapshot 대신 admin 전용 RPC의 변경 필드명만 표시한다. 독립 dead-man monitor는 없다. |
| Desktop 효율·정확성 | 10% | 62 | 88 | Realtime 이벤트를 table별 query-key로 제한하고, 감사 갱신·admin query gate·로그아웃 cache 제거를 추가했다. polling fallback은 유지한다. |
| 테스트·CI | 10% | 84 | 92 | Worker 774 tests, Ruff, mypy, safety drill과 Desktop test/build가 통과했고, Desktop unit suite를 CI 필수 gate에 연결했다. |
| 문서·온보딩 | 10% | 58 | 92 | Worker/Desktop env 위치, PowerShell/Bash 문법, Vite/Tauri 차이, Paper 운영, 문제 해결, 남은 제한을 README에 정리했다. |
| 유지보수성 | 10% | 80 | 88 | 주문 수량 계산과 Realtime key mapping을 순수 helper로 분리하고, 로그 화면을 독립 섹션으로 나눴다. |
| **가중 종합** | **100%** | **74** | **89** | 기능 추가와 검증 가능한 리팩터링을 반영한 engineering score다. |

## 점수에 따라 수행한 리팩터링

### 1. 주문 수량 계산 단일화

개선 전에는 live execution이 `amount_krw // price_krw`를 직접 계산했고 risk policy에는 같은 계산이 없었다. `order_calculations.py`의 순수 함수로 통합해 risk가 허용한 수량, broker request, paper outcome이 같은 정수 주 계산을 사용하게 했다.

### 2. Realtime 전체 refetch 제거

개선 전에는 heartbeat 한 건만 바뀌어도 strategy, fundamentals, auth를 포함한 모든 query가 무효화됐다. publication table과 query-key prefix를 1:1로 연결해 관련 캐시만 갱신하도록 변경했다. Realtime 장애 시 polling이 계속 동작하므로 가용성 모델은 유지된다.

### 3. 운영 로그의 목적 분리

`LogsPage`를 변경 감사와 engine event 섹션으로 분리했다. Admin 전용 `get_audit_log_summaries` RPC가 서버에서 변경 필드명만 계산하고 Desktop의 원본 `audit_logs` 조회 권한을 제거해, snapshot 값과 actor UUID가 Data API 응답에 포함되지 않게 했다.

### 4. 존재하던 테스트를 CI gate로 승격

Desktop test suite가 로컬에만 존재하던 상태에서 `npm run desktop:test`를 CI의 lint/typecheck/build 사이에 추가했다. UI 안전 회귀가 main push와 pull request에서 차단된다.

## 당시 기록된 검증 근거

당시 후보에서 다음 검증을 실행했다. 아래 결과는 현재 G1+G2 후보의 검증
결과로 재사용할 수 없다.

| 명령 | 결과 |
| --- | --- |
| `python -m pytest -q --tb=short` | PASS — 774 tests |
| `python -m ruff check app` | PASS |
| `python -m mypy app` | PASS — 259 source files |
| `python -m app.tools.run_live_execution_safety_drill_once` | PASS |
| `npm run desktop:typecheck`과 동일한 `tsc -b` | PASS |
| `npm run desktop:lint`과 동일한 `eslint .` | PASS |
| `npm run desktop:test`과 동일한 test entry | PASS |
| `npm run desktop:build`과 동일한 Vite build | PASS |
| `python .github/scripts/repository_safety.py migrations` | PASS |
| `python .github/scripts/repository_safety.py workflows` | PASS |
| `git diff --check` | PASS |

당시 shell에는 `python`, `npm` launcher가 없어 Codex의 bundled Python/Node runtime과 기존 설치된 dependency를 직접 사용했다. 실행한 module과 script body는 repository 명령과 동일했다. Remote CI와 hosted Supabase/provider 검증은 이 점수의 PASS 근거에 포함하지 않았다.

Docker CLI는 있었으나 daemon이 실행 중이지 않아 disposable PostgreSQL migration 적용 검증은 `SKIP`됐다. 인앱 Browser 초기화도 런타임 오류로 실패해 시각적 smoke test는 점수 근거에서 제외했으며, React render fixture와 production build만 포함했다.

## 당시 남은 우선순위 (현재 계획으로 대체됨)

| 우선순위 | 남은 기능 | 현재 공백 | 완료 조건 |
| --- | --- | --- | --- |
| P0 | 지속형 Paper 가상계좌 | Paper 현금·보유수량이 체결에 따라 누적되지 않는다. | idempotent ledger, cash/position reconciliation, fee·tax·slippage, recovery test |
| P0 | candle feature pipeline | technical score와 장기 feature cadence가 provider 데이터로 자동 갱신되지 않는다. | 검증된 candle contract, 지표 계산, idempotent upsert, data-quality gate |
| P1 | runtime rebalance planner | 목표 비중, no-trade band, reserve, whole-share 수량을 함께 계산하지 않는다. | 순수 planner, cost model, Paper 검증, live와 분리된 승인 |
| P1 | 안전한 scheduler | outcome, backtest, monthly research, retention이 one-off 중심이다. | durable job state, concurrency guard, default dry-run, failure observability |
| P1 | 독립 dead-man monitor | Worker 내부 heartbeat만으로는 worker 자체 중단을 알릴 수 없다. | worker 외부 monitor, bounded alert, ACK drill |
| P1 | Desktop schema boundary | 일부 Supabase row가 관대한 기본값으로 매핑된다. | 핵심 row Zod 검증, schema mismatch 오류, compatibility tests |
| P2 | Native Desktop CI | Tauri/Rust build가 기본 CI gate에 없다. | pinned Cargo lock, OS matrix 또는 최소 `cargo check`, artifact policy |

## 결론

당시 개선은 주문 전 안전성, Paper 연구 데이터, 운영자 가시성, 캐시 효율,
CI, 문서를 강화했다. 이 문서의 종합 89점은 현재 구현이나 G1/G2 gate 통과를
의미하지 않는다. 현재 상태는 binary gate와 최신 검증 증거로만 판정한다.
