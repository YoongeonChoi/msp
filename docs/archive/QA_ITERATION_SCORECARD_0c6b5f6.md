# Engineering QA Iteration Scorecard — 0c6b5f6

상태: `HISTORICAL BASELINE / TREND-ONLY`

평가 기준 SHA: `0c6b5f6d5c3fb928cdf0ea40b676514cd2c893a1`

평가일: 2026-07-23 KST

이 점수는 위 평가 SHA의 기록이며 저장소 최신 HEAD의 현재 점수가 아니다. 이후
commit은 동일한 고정 규칙으로 재평가하기 전까지 이 점수를 승계하지 않는다.

이 문서는 로컬에서 개선할 수 있는 engineering quality를 반복 측정한다. 투자
성과, 수익 가능성, 보안 보증, 배포 승인 또는 Production Live 권한을 뜻하지
않는다. `G0`, `G1`, `G2`는 숫자 점수와 별도로 모두 `FAIL`이며, 필요한 외부
증거 하나라도 없으면 높은 점수와 관계없이 계속 실패다.

## 고정 평가 규칙

- 평가는 반드시 커밋된 exact SHA를 대상으로 한다.
- 영역별 점수는 아래 고정 항목의 `획득/배점` 합계다. 각 항목은 증거가 모두
  있으면 전점, 하나라도 없으면 0점이다. 부분 충족은 더 작은 이진 항목으로
  분리하며 임의의 부분점수나 보너스를 주지 않는다.
- 종합 점수는 `영역 점수 × 가중치`의 합이다.
- `PASS`는 exact SHA에서 직접 실행한 검증, 해당 계약을 직접 확인하는 자동
  테스트, 또는 본질적으로 정적인 policy·CI wiring의 exact source inspection 중
  하나가 있어야 한다. 코드가 존재하기만 하고 요구사항을 증명하지 못하면 0점이다.
- Cyber Trusted Access가 필요한 hosted Supabase, 실제 AAL2 사용자, 외부
  alert/archive receiver, restore·soak 증거는 `N/A (external)`로 숫자에서
  제외한다. 같은 흐름의 로컬 코드·인증·복구 계약이 미완성이면 그 로컬 항목은
  계속 감점한다. 제외는 통과로 계산하지 않으며 binary gate 상태도 바꾸지 않는다.
- 현재 checkout의 커밋되지 않은 변경은 평가에서 제외한다.
- 이전 SHA의 독립 보안 finding이라도 현재 SHA의 관련 source blob이 동일하고
  재현 probe가 같은 위반을 증명하면 기존 `PASS`를 무효화한다. 단순히 오래된
  scan이라는 이유로 모순 증거를 제외하지 않는다.
- 다음 반복은 가장 큰 미획득 항목 중 안전 선행조건을 먼저 구현한다. 한 기능을
  검증·커밋·`origin/develop` 푸시한 뒤 새 SHA에서 다시 평가한다.

## 평가 SHA의 기록 점수

| 영역                |   가중치 | 점수 | 가중 점수 |
| ------------------- | -------: | ---: | --------: |
| 거래 안전 경계      |      20% |   96 |     19.20 |
| 기능 완성도         |      15% |   74 |     11.10 |
| 데이터·연구 무결성  |      10% |   95 |      9.50 |
| 운영 가시성         |      15% |   80 |     12.00 |
| Desktop 효율·정확성 |      10% |   93 |      9.30 |
| 테스트·CI           |      10% |  100 |     10.00 |
| 문서·온보딩         |      10% |   97 |      9.70 |
| 유지보수성          |      10% |   78 |      7.80 |
| **가중 종합**       | **100%** |      | **88.60** |

산식:

```text
96×0.20 + 74×0.15 + 95×0.10 + 80×0.15
+ 93×0.10 + 100×0.10 + 97×0.10 + 78×0.10
= 88.60
```

## 엄격 QA 체크리스트

### 거래 안전 경계 — 96/100

| ID   | 요구사항                                                      | 배점 | 획득 | 상태    | 근거 또는 완료 조건                                                                             |
| ---- | ------------------------------------------------------------- | ---: | ---: | ------- | ----------------------------------------------------------------------------------------------- |
| TS-1 | Production 주문 network 경로 격리와 안전 기본값               |   30 |   30 | PASS    | `paper \| contract_test`만 허용하고 broker 주문 URL은 로컬 qualification에서도 호출하지 않는다. |
| TS-2 | 주문 전 risk·수량·현금·보유수량 gate                          |   25 |   25 | PASS    | `RiskService`와 공용 whole-share 계산 계약 및 회귀 테스트가 존재한다.                           |
| TS-3 | semantic reservation, lease/fencing, control epoch, 원장 결속 |   25 |   25 | PASS    | V2 execution kernel과 DB verifier가 동시성·재시작·stale write를 검사한다.                       |
| TS-4 | Toss/candle 전송·저장 경계의 fail-closed 오류와 비밀 비노출   |   16 |   16 | PASS    | `fc06b3f`가 이 범위에 bounded JSON, 고정 오류 코드, exact canonical type을 추가했다.            |
| TS-5 | 증거에 결합된 unknown-write 수동 복구                         |    4 |    0 | MISSING | 추측이나 자동 retry 없이 durable receipt로 미확정 쓰기를 판정·해결해야 한다.                    |

### 기능 완성도 — 74/100

| ID   | 요구사항                                     | 배점 | 획득 | 상태    | 근거 또는 완료 조건                                                                                                                                                                            |
| ---- | -------------------------------------------- | ---: | ---: | ------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| FC-1 | Worker·Paper V2·전략 기본 흐름               |   25 |   25 | PASS    | 로컬 application·domain·adapter 흐름과 통합 테스트가 존재한다.                                                                                                                                 |
| FC-2 | Supabase control plane과 Desktop 운영 흐름   |   20 |   20 | PASS    | 상태·명령·승인·감사·세션 흐름이 좁은 계약으로 연결된다.                                                                                                                                        |
| FC-3 | durable PIT raw/read primitives              |   25 |   25 | PASS    | candle/calendar revision·occurrence·as-of reader·quarantine 계약이 있다.                                                                                                                       |
| FC-4 | retained research slice와 stable fingerprint |    4 |    4 | PASS    | bounded retained slice가 scope와 lineage fingerprint를 분리해 계산한다.                                                                                                                        |
| FC-5 | dataset registry와 certified research replay |    6 |    0 | MISSING | exact dataset/code/feature manifest로 재현 가능한 replay가 필요하다.                                                                                                                           |
| FC-6 | candle 자동 source→store→feature pipeline    |   10 |    0 | MISSING | `eea14ef8`의 default-disabled 수동 one-shot runner가 source→fence→append→confirm을 강제한다. 하지만 runtime/scheduler 선택, DQ 승인, feature 연결이 없어 자동 pipeline은 아직 완성되지 않았다. |
| FC-7 | durable scheduler, retry budget, dead-letter |   10 |    0 | MISSING | 현재 in-memory scheduler를 restart-safe job state로 대체해야 한다.                                                                                                                             |

### 데이터·연구 무결성 — 95/100

| ID   | 요구사항                                           | 배점 | 획득 | 상태    | 근거 또는 완료 조건                                                                                                                                                                                                                                                                                                                                       |
| ---- | -------------------------------------------------- | ---: | ---: | ------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| DI-1 | canonical PIT timestamp·identity·checksum          |   25 |   25 | PASS    | exact type과 canonical reconstruction으로 입력 우회를 막는다.                                                                                                                                                                                                                                                                                             |
| DI-2 | immutable revision·occurrence·quarantine           |   25 |   25 | PASS    | replay, correction, regression, recurrence를 DB에서 구분한다.                                                                                                                                                                                                                                                                                             |
| DI-3 | bounded strict transport와 durable receipt binding |   20 |   20 | PASS    | auth/read 64 KiB·4 MiB, RPC 64 KiB, duplicate/NaN/deep JSON 거부가 검증됐다.                                                                                                                                                                                                                                                                              |
| DI-4 | retained timing·lineage research slice             |   10 |   10 | PASS    | exact source occurrence와 retained session chain을 재검증한다.                                                                                                                                                                                                                                                                                            |
| DI-5 | 로컬 completeness·finality 인증 gate               |    5 |    5 | PASS    | `b4b99a4`가 retained candle/calendar 산출물을 원래 gate로 재검증해 공통 lineage를 결합하고, 외부 completeness·authenticity·finality 증거 부재와 외부 인증·완전 인증·승격 관련 positive claim을 canonical manifest에 false로 고정한다. V1 출력과 downstream require gate는 digest를 다시 계산한 위조도 거부하며 정상 blocked manifest도 승격시키지 않는다. |
| DI-6 | candle collection attempt/CAS fence                |   10 |   10 | PASS    | `f189932f`가 durable job/CAS 계약을 만들었고 `eea14ef8`이 동일 persistence authority에서 `begin→read→fence→append→confirm` 순서와 exact receipt 확인을 application boundary로 강제한다.                                                                                                                                                                   |
| DI-7 | corporate action·DQ 인증                           |    5 |    0 | MISSING | `6f0d8f3`은 exact source lineage에 결합된 10개 local DQ check와 corporate-action 증거 부재·downstream 차단을 canonical manifest로 고정한다. 그러나 공식 source의 PIT coverage·adjustment evidence, 독립 verifier receipt와 positive certified type이 없어 0점이다. Dataset/code/feature registry와 replay는 중복 없이 FC-5 책임으로 분리한다.             |

### 운영 가시성 — 80/100

| ID    | 요구사항                                    | 배점 | 획득 | 상태    | 근거 또는 완료 조건                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| ----- | ------------------------------------------- | ---: | ---: | ------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| OP-1  | heartbeat, 상태, command/ACK correlation    |   20 |   20 | PASS    | versioned operation state와 worker ACK 경로가 있다.                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| OP-2  | incident, audit, reconciliation projection  |   20 |   20 | PASS    | operator read model과 bounded evidence flow가 있다.                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| OP-3  | durable outbox state와 retry ledger         |   10 |   10 | PASS    | lease·attempt·완료 상태가 durable 계약으로 보존된다.                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| OP-4  | bounded alert delivery adapter              |    4 |    4 | PASS    | 좁은 payload와 receipt shape를 검증한다.                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| OP-5  | receiver 인증과 fail-closed escalation 정책 |    6 |    6 | PASS    | `115000d`가 Outbox·legacy notifier·dead-man에 공통 HTTPS-only transport와 current/previous exact 32-byte key의 canonical HMAC ACK를 적용했다. method·exact URL·request/response hash·status·timestamp·item binding을 결속하고 unsigned·tampered·stale/future·cross-item/destination/payload replay를 거부한다. 인증 실패는 Outbox retry/dead-letter, legacy 명시 실패, dead-man exact pending episode 재시도로 이어진다. Signed receiver ACK는 human ACK나 immutable archive 증거로 승격하지 않는다. |
| OP-6  | dead-man source와 로컬 verifier             |    5 |    5 | PASS    | worker 상태를 읽고 보수적으로 판정하는 로컬 경계가 있다.                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| OP-7  | worker와 독립된 dead-man 실행기             |    5 |    0 | MISSING | worker 프로세스 중단 자체를 외부에서 감지해야 한다.                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| OP-8  | alert ACK·owner·escalation 상태기계         |    5 |    0 | MISSING | 전송 성공과 인간 확인을 분리해 보존해야 한다.                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| OP-9  | SLO·incident·restore runbook과 verifier     |    8 |    8 | PASS    | 정책과 로컬 verifier가 존재한다.                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| OP-10 | 로컬 복구·fault 계약                        |    7 |    7 | PASS    | fail-closed 복구 경로와 fault 테스트가 있다.                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| OP-11 | restart-safe durable scheduler              |   10 |    0 | MISSING | lease, retry budget, dead-letter, manual replay가 필요하다.                                                                                                                                                                                                                                                                                                                                                                                                                                          |

### Desktop 효율·정확성 — 93/100

| ID    | 요구사항                                               | 배점 | 획득 | 상태    | 근거 또는 완료 조건                                                                                                                                                                                              |
| ----- | ------------------------------------------------------ | ---: | ---: | ------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| DT-1  | strict versioned row/schema boundary                   |   25 |   25 | PASS    | permissive legacy row path가 release source에서 제거됐다.                                                                                                                                                        |
| DT-2a | server-side role-bound mutation authorization          |    5 |    5 | PASS    | mutation은 UI 표시가 아니라 server permission에 결속된다.                                                                                                                                                        |
| DT-2b | maker/checker와 platform-admin 권한 분리               |    5 |    5 | PASS    | requester·reviewer 분리와 platform admin의 거래 승인 금지가 서버 계약으로 검증된다.                                                                                                                              |
| DT-2c | stale/offline/freshness mutation guard                 |    5 |    5 | PASS    | stale snapshot, offline session과 만료된 freshness에서 mutation을 차단한다.                                                                                                                                      |
| DT-2d | auditor-only audit-event projection                    |    5 |    5 | PASS    | `ec7ec98b`의 forward-only migration은 `view_audit`을 private SELECT 전에 검사한다. 6개 non-auditor role의 exact `[]`와 auditor의 exact current audit fixture가 direct PostgreSQL 및 실제 PostgREST에서 통과했다. |
| DT-2e | auditor-only reconciliation projection                 |    5 |    5 | PASS    | 같은 migration은 `view_reconciliation`을 독립적으로 검사한다. 6개 non-auditor role의 exact `[]`와 auditor의 exact break ID·run ID·evidence ref가 두 실행 경로에서 통과했다.                                      |
| DT-3  | session lifecycle, cache 격리, query-specific realtime |   20 |   20 | PASS    | logout cache 제거와 좁은 invalidation 계약이 있다.                                                                                                                                                               |
| DT-4  | component·render fixture                               |   10 |   10 | PASS    | exact SHA의 `desktop:test`가 render와 mutation guard fixture를 통과했다.                                                                                                                                         |
| DT-5  | interaction·accessibility contract tests               |    8 |    8 | PASS    | dialog, role, offline, session interaction fixture가 통과했다.                                                                                                                                                   |
| DT-6  | packaged visual smoke                                  |    2 |    0 | MISSING | 실제 packaged window에서 핵심 화면을 확인한 증거가 필요하다.                                                                                                                                                     |
| DT-7  | Cargo check·test·build CI                              |    5 |    5 | PASS    | `.github/workflows/ci.yml`이 세 명령을 `--locked`로 실행한다.                                                                                                                                                    |
| DT-8  | signed packaged artifact와 provenance                  |    5 |    0 | MISSING | package·sign·attestation을 exact SHA와 결합해야 한다.                                                                                                                                                            |

### 테스트·CI — 100/100

| ID   | 요구사항                                   | 배점 | 획득 | 상태 | 근거 또는 완료 조건                                                                                                                                                                                                                              |
| ---- | ------------------------------------------ | ---: | ---: | ---- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| TC-1 | Worker 전체 회귀 테스트                    |   25 |   25 | PASS | exact candidate: `2411 passed, 2 skipped`.                                                                                                                                                                                                       |
| TC-2 | Ruff와 strict mypy                         |   20 |   20 | PASS | exact candidate의 전체 Ruff check와 mypy 2.3.0이 429개 source file을 통과한다. 변경 Python 40개는 focused format check도 통과한다.                                                                                                               |
| TC-3 | migration·history·security contract tests  |   15 |   15 | PASS | exact SHA의 Worker suite, 6개 신규 static contract와 migration history guard가 통과했다. Safety-guard는 34개 중 31개 PASS, Windows symlink 권한 3개 skip이다.                                                                                    |
| TC-4 | disposable PostgreSQL 전체 migration apply |    5 |    5 | PASS | `verify_g1_g2_migration.py`가 PostgreSQL 17 fresh·populated upgrade, 47개 migration, ACL/owner, direct role matrix와 실제 PostgREST를 `FINAL=PASS`로 실행했다.                                                                                   |
| TC-5 | Desktop typecheck·lint·test·build          |   15 |   15 | PASS | exact SHA의 clean worktree에서 네 명령이 모두 통과했다.                                                                                                                                                                                          |
| TC-6 | 일반 concurrency·fault·restart 회귀        |   10 |   10 | PASS | execution·job·reader fault와 restart 회귀 테스트가 있다.                                                                                                                                                                                         |
| TC-7 | candle unknown-write fault 검증            |    5 |    5 | PASS | exact candidate의 108개 focused test가 load/begin/read/fence/append/confirm/pause/block 전후 실패·취소, 실제 `Task.cancel()`, 정확한 1회 호출, non-executable replay, cause/context/traceback 비밀 비노출을 직접 검증한다.                       |
| TC-8 | Cargo check·test·build CI wiring           |    3 |    3 | PASS | native Rust job이 `--locked` 명령을 보존한다.                                                                                                                                                                                                    |
| TC-9 | high/critical dependency audit 0건         |    2 |    2 | PASS | `0c6b5f6`이 GHSA-3jxr-9vmj-r5cp의 네 transitive `brace-expansion` 인스턴스를 patched `1.1.16`·`5.0.7`로 고정하고 canonical lock manifest를 갱신했다. exact SHA의 `npm audit --audit-level=moderate`와 GitHub dependency gate가 0건으로 통과했다. |

### 문서·온보딩 — 97/100

| ID   | 요구사항                               | 배점 | 획득 | 상태    | 근거 또는 완료 조건                                                                                               |
| ---- | -------------------------------------- | ---: | ---: | ------- | ----------------------------------------------------------------------------------------------------------------- |
| DO-1 | architecture·safety·policy 문서        |   30 |   30 | PASS    | 현재 NO-LIVE 경계와 책임 분리가 문서화됐다.                                                                       |
| DO-2 | 운영·배포·incident runbook             |   25 |   25 | PASS    | 장애·배포·복구 제한과 명령이 기록돼 있다.                                                                         |
| DO-3 | data/API contract와 알려진 공백        |   20 |   20 | PASS    | `DATA_PIPELINE.md`, `API_GAPS.md`, provider artifact 정책이 있다.                                                 |
| DO-4 | 설치·개발 명령 안내                    |   10 |   10 | PASS    | Worker/Desktop 환경과 명령이 문서화됐다.                                                                          |
| DO-5 | 문제 해결과 알려진 제한                |    7 |    7 | PASS    | provider·배포·운영 제한과 해결 방향이 기록됐다.                                                                   |
| DO-6 | 현재 상태와 역사 문서의 명확한 탐색    |    3 |    0 | MISSING | 긴 계획과 archived scorecard 사이의 현재 요약 index가 필요하다.                                                   |
| DO-7 | exact SHA 기반 재현 가능한 현행 점수표 |    5 |    5 | PASS    | `d4b78ac`부터 고정 이진 항목·가중치·외부 제외 규칙을 보존하고, 이번 반복을 `0c6b5f6` exact SHA에서 다시 계산했다. |

### 유지보수성 — 78/100

| ID   | 요구사항                             | 배점 | 획득 | 상태    | 근거 또는 완료 조건                                             |
| ---- | ------------------------------------ | ---: | ---: | ------- | --------------------------------------------------------------- |
| MA-1 | domain·port·adapter 경계             |   25 |   25 | PASS    | 주문·데이터·저장소의 authority boundary가 분리된다.             |
| MA-2 | strict typed models와 오류 taxonomy  |   20 |   20 | PASS    | mypy와 고정 domain/provider error 계약이 있다.                  |
| MA-3 | 공용 helper와 직접 regression tests  |   15 |   15 | PASS    | `bounded_json_response` 등 공통 경계를 한 곳에서 검증한다.      |
| MA-4 | 핵심 domain module의 응집도          |    8 |    8 | PASS    | 권한과 불변식이 명시적 module boundary에 모여 있다.             |
| MA-5 | 대형 adapter·migration의 분해        |    7 |    0 | MISSING | 큰 Worker API·SQL·test module의 변경 반경을 줄여야 한다.        |
| MA-6 | 명시적인 runtime wiring boundary     |    5 |    5 | PASS    | 미인증 primitive는 main runtime에 자동 연결되지 않는다.         |
| MA-7 | cohesive runtime과 durable scheduler |   10 |    0 | MISSING | 선택·복구·scheduling을 한 restart-safe 조립 경계로 묶어야 한다. |
| MA-8 | migration·verifier coverage          |    5 |    5 | PASS    | schema safety와 계약 검증 스크립트가 있다.                      |
| MA-9 | migration·verifier 모듈성            |    5 |    0 | MISSING | 대형 SQL·verifier의 변경 비용을 줄여야 한다.                    |

## 숫자에서 제외한 외부 항목

다음 항목은 Cyber Trusted Access 또는 승인된 외부 운영환경이 있어야 검증할 수
있으므로 이번 숫자에는 배점도 감점도 주지 않는다.

- hosted Supabase의 실제 AAL2 두 사용자·RLS·role separation 증거
- 실제 alert/archive receiver의 delivery·ACK·escalation 및 immutable 보존 증거
- 실제 Toss read-only 계약 호출과 배포 credential·endpoint 검증
- 24시간 fault soak, 10거래일 Paper/Shadow, hosted backup restore·RPO/RTO 증거

이 항목들은 `N/A (external)`이지 `PASS`가 아니다. 승인환경이 제공되면 별도의
binary gate 증거로 평가하며, 현재 `G0/G1/G2 FAIL`을 바꾸지 않는다.

## 평가 SHA의 직접 검증 증거

| 검증                                                         | 결과                                             | 수준                                                                                                                             |
| ------------------------------------------------------------ | ------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------- |
| OP-5 protocol·wiring·failure-path inventory                  | PASS — 333 tests                                 | candidate-isolated; HTTPS-only, current/previous key, exact binding, replay 거부, Outbox·legacy·dead-man 실패 경로를 직접 검증   |
| `python -m pytest`                                           | PASS — 2411 passed, 2 skipped                    | exact staged feature candidate의 detached worktree와 `0c6b5f6` lock candidate에서 각각 통과                                      |
| `python -m ruff check app`                                   | PASS                                             | candidate-isolated; 변경 Python 40개는 `ruff format --check`도 통과                                                              |
| `python -m mypy --strict app`                                | PASS — mypy 2.3.0, 429 source files              | candidate-isolated                                                                                                               |
| alert·incident receiver mock drills                          | PASS — 각각 `delivered=4`                        | 독립 signer fixture의 authenticated ACK; 외부 receiver 또는 human ACK 증거가 아님                                                |
| `npm ci --ignore-scripts`·`npm audit --audit-level=moderate` | PASS — 0 vulnerabilities                         | exact `0c6b5f6` dependency candidate; 취약 lock 4개만 patch                                                                      |
| Desktop lint·typecheck·test·E2E·build                        | PASS — Playwright 21 tests                       | exact dependency candidate; Node 24 local 보완 뒤 Node 22 GitHub CI로 재확인                                                     |
| dependency manifest·repository safety                        | PASS                                             | canonical manifest `sha256=a4e4110a…267eb9`, all-scope safety 검사                                                               |
| 독립 protocol·config·test·structure·lock 감사                | PASS — P0–P3 0건                                 | 하위 에이전트가 feature와 dependency staged blobs를 독립 검토                                                                    |
| commit snapshot 감사                                         | PASS — `115000d` 56 paths, `0c6b5f6` 2 paths     | 격리 candidate와 실제 commit blob 일치; 사용자 lock 233줄은 제외                                                                 |
| `git status --branch`                                        | PASS — `develop...origin/develop` ahead/behind 0 | exact `0c6b5f6`을 `origin/develop`에 push                                                                                        |
| GitHub Actions feature CI run `30002575925`                  | PASS                                             | exact `115000d`; Worker, Desktop, Tauri, migration, repository safety와 docs 6개 job 모두 성공                                   |
| GitHub Actions migration-check run `30002575874`             | PASS                                             | exact `115000d`; full G1/G2, 8개 PIT·collection verifier와 singleton Paper safety seed 성공                                      |
| GitHub Actions CI run `30003387905`                          | PASS                                             | exact `0c6b5f6`; Node 22 install·Desktop·Worker·Tauri·migration·repository safety·docs 6개 job 모두 성공                         |
| GitHub Actions security run `30003387840`                    | PASS                                             | exact `0c6b5f6`; npm/Python audit, Bandit, gitleaks, common-pattern scan, 두 CodeQL job, lock evidence와 workflow guard 성공     |
| 최초 feature security run `30002575873`                      | FAIL 후 해소                                     | exact `115000d`에서 새 GHSA의 dev-only transitive high 1건을 발견했고, 별도 `0c6b5f6` lock fix와 성공한 security run으로 닫았다. |

실제 Toss, hosted Supabase, 외부 receiver 네트워크는 호출하지 않았고 receiver
transport 검증은 `MockTransport`와 로컬 signer fixture 중심이다. Disposable
PostgreSQL과 local PostgREST migration 검증은 CI에서 실행했다. 로컬에는 gitleaks가
없어 dedicated scan을 실행하지 못했지만 exact SHA의 GitHub gitleaks와
common-pattern scan은 PASS했다. 이는 secret 부재를 보장하지 않는다. 전체 Ruff
lint와 변경 Python의 focused format은 통과했지만 repository-wide format clean은
이번 점수의 근거로 주장하지 않는다.

Codex Security scan `ab0ec850-95e7-4230-863d-14ba333d8965`는 과거
`07c6ec6` snapshot에서 완료된 historical scan이다. 그 scan의 고정 probe나 완료
상태를 현재 SHA의 clean-scan 증거로 사용하지 않는다. Snapshot authorization은
forward-only migration 전체 replay와 역할 매트릭스로, receiver authentication은
현재 source·회귀 테스트·drill·CI로 각각 검증했다. 새 전체 보안 스캔을 실행했다는
주장은 하지 않는다.

## 0c6b5f6 평가 당시의 다음 구현 순서

1. `FC-7`·`OP-11`·`MA-7`: lease, retry budget, dead-letter, manual replay를 갖는
   restart-safe scheduler 조립 경계를 구현한다.
2. `TS-5`: 미확정 쓰기를 durable receipt로만 판정하는 수동 복구 경계를 구현한다.
3. `DO-6`: 현행 scorecard·미완료 gate·archived 계획을 연결하는 짧은 current-status
   index를 추가한다.
4. 큰 기능 단위 리팩터링에서 `MA-5` 대형 adapter·test 분해와 `MA-9` migration·
   verifier 모듈화를 각각 독립적으로 검증한다.
5. `DI-7` positive path와 `FC-5` registry/replay는 verified official corporate-action
   source contract와 independent receipt가 확보된 뒤 각각 별도 원자 커밋으로 진행한다.
6. `FC-6`는 certified DQ와 dataset registry가 생긴 뒤 feature 입력을 연결한다.
7. `OP-7` 독립 failure-domain 실행과 `OP-8` human ACK·owner·escalation 상태기계는
   receiver transport 인증과 분리된 로컬 기능으로 구현한다.
8. `DT-6` packaged visual smoke와 `DT-8` signed artifact·provenance를 release 단위로
   검증한다.
9. 각 기능마다 focused test, 전체 영향 test, Ruff, mypy, migration verifier를
   실행하고 커밋·푸시 후 이 표를 새 exact SHA로 다시 계산한다.
