# Current Engineering QA Scorecard — eb396c7

상태: `VERIFIED SOURCE / SCORECARD PUBLICATION`

평가 source SHA: `eb396c701eec4d8ffec0667ba32479dee4ab50a8`

source tree SHA: `d8fd15e2ca1e22905c4b79dd8adafdab79156970`

exact-source push receipts:

- [CI 30164099586](https://github.com/YoongeonChoi/msp/actions/runs/30164099586)
- [migration-check 30164099581](https://github.com/YoongeonChoi/msp/actions/runs/30164099581)
- [security 30164099578](https://github.com/YoongeonChoi/msp/actions/runs/30164099578)

publication receipt authority:
[PR #31](https://github.com/YoongeonChoi/msp/pull/31) — 이 PR의 exact head,
merge-candidate checks, merge commit과 post-merge `main` checks가 이 보고서 게시의
canonical receipt다. 보고서가 자기 자신의 아직 존재하지 않는 SHA와 run ID를 미리
기록하는 순환을 피하기 위해 제품 source와 publication receipt를 분리한다.

평가일: 2026-07-26 KST

이 문서는 위 exact source를 고정된 8개 영역·67개 이진 ID로 평가한 현행 보고서다.
`eb396c7…` source tree 자체에는 이전 점수표가 남아 있으므로 publication 전에는
DO-7을 0점으로 두고 `92.40`으로 계산한다. 이 보고서가 required gate와 reviewed
integration을 통과하고 post-merge `main` receipt까지 성공하면 DO-7을 포함한 공식
engineering 점수는 `92.90`이다. 보고서 존재를 미리 PASS로 계산하지 않는다.

이 점수는 engineering 품질 추세이지 투자 성과, 수익 가능성, 보안 보증, 배포 승인
또는 Production Live 권한이 아니다. 기업 운영 stage gate `G0`, `G1`, `G2`는 모두
계속 `FAIL`이며 Production Live는 `NO-GO`다.

이전 공식 기준선 `02ba9be…`의 원문은
[archive/QA_ITERATION_SCORECARD_02ba9be.md](archive/QA_ITERATION_SCORECARD_02ba9be.md)에
보존한다.

## 고정 채점 계약

```math
QA_{total}=\sum_{k=1}^{8}Score_k\times Weight_k
```

- 커밋된 exact source SHA만 평가한다.
- 각 ID는 완료 조건과 증거가 모두 있으면 전점, 하나라도 없으면 0점이다.
- 코드 존재만으로 PASS를 주지 않는다. 직접 테스트, exact source inspection 또는
  해당 SHA의 CI receipt가 요구사항을 증명해야 한다.
- rubric·배점·완료 조건은
  [0c6b5f6 historical scorecard의 엄격 QA 체크리스트](archive/QA_ITERATION_SCORECARD_0c6b5f6.md#엄격-qa-체크리스트)를
  변경 없이 사용한다.
- Cyber Trusted Access나 승인된 외부 운영환경이 필요한 증거는
  `N/A (external)`로 숫자에서 제외하되 PASS로 승격하지 않는다.
- 커밋되지 않은 working-tree 변경은 평가에서 제외한다.
- 한 기능을 검증·원자 커밋·`origin/develop` push한 뒤 exact-source gate와 reviewed
  publication receipt를 확인하고 전 항목을 다시 계산한다.

## 현행 점수

| 영역                     |   가중치 | 점수 | 가중 점수 | PASS / 전체 |
| ------------------------ | -------: | ---: | --------: | ----------: |
| TS · 거래 안전 경계      |      20% |   96 |     19.20 |       4 / 5 |
| FC · 기능 완성도         |      15% |   84 |     12.60 |       5 / 7 |
| DI · 데이터·연구 무결성  |      10% |   95 |      9.50 |       6 / 7 |
| OP · 운영 가시성         |      15% |   90 |     13.50 |      9 / 11 |
| DT · Desktop 효율·정확성 |      10% |   93 |      9.30 |     10 / 12 |
| TC · 테스트·CI           |      10% |  100 |     10.00 |       9 / 9 |
| DO · 문서·온보딩         |      10% |  100 |     10.00 |       7 / 7 |
| MA · 유지보수성          |      10% |   88 |      8.80 |       7 / 9 |
| **가중 종합**            | **100%** |      | **92.90** | **57 / 67** |

```text
96×0.20 + 84×0.15 + 95×0.10 + 90×0.15
+ 93×0.10 + 100×0.10 + 100×0.10 + 88×0.10
= 92.90
```

이전 공식 기준선 `88.90`보다 `4.00`점 높다. `FC-7`, `OP-11`, `MA-7`만
고정 완료 조건에 따라 승격했다. 추가 테스트 수, 코드량 또는 비채점 hardening에는
보너스를 주지 않았다.

## 67개 ID 판정 요약

| 영역 | PASS ID                                                         | MISSING ID | 점수 |
| ---- | --------------------------------------------------------------- | ---------- | ---: |
| TS   | TS-1, TS-2, TS-3, TS-4                                          | TS-5       |   96 |
| FC   | FC-1, FC-2, FC-3, FC-4, FC-7                                    | FC-5, FC-6 |   84 |
| DI   | DI-1, DI-2, DI-3, DI-4, DI-5, DI-6                              | DI-7       |   95 |
| OP   | OP-1, OP-2, OP-3, OP-4, OP-5, OP-6, OP-9, OP-10, OP-11          | OP-7, OP-8 |   90 |
| DT   | DT-1, DT-2a, DT-2b, DT-2c, DT-2d, DT-2e, DT-3, DT-4, DT-5, DT-7 | DT-6, DT-8 |   93 |
| TC   | TC-1, TC-2, TC-3, TC-4, TC-5, TC-6, TC-7, TC-8, TC-9            | 없음       |  100 |
| DO   | DO-1, DO-2, DO-3, DO-4, DO-5, DO-6, DO-7                        | 없음       |  100 |
| MA   | MA-1, MA-2, MA-3, MA-4, MA-6, MA-7, MA-8                        | MA-5, MA-9 |   88 |

## FC-7·OP-11·MA-7 승격 근거

| ID    |  획득 | 판정 근거                                                                                                                                                                                                |
| ----- | ----: | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| FC-7  | 10/10 | 정확한 5개 job definition, DB-clock claim, outer/inner lease·fencing, bounded retry, effectful unknown dead-letter와 reason-bound manual replay가 durable DB state와 sealed Worker runtime으로 연결됐다. |
| OP-11 | 10/10 | startup convergence, command drain, restart takeover, stale writer 차단, heartbeat business-health evidence, shutdown drain과 lease release가 직접 검증됐다.                                             |
| MA-7  | 10/10 | legacy per-stage sleep loop를 제거하고 선택·복구·dispatch·settlement를 하나의 sealed production registry, facade와 durable runtime lifecycle로 조립했다.                                                 |

세 ID의 승격으로 `1.50 + 1.50 + 1.00 = 4.00`점을 획득했다.

직접 검증은 다음을 포함한다.

- exact 5개 definition과 7개 RPC allowlist
- DB clock과 lock 대기 후 due-time 재검증
- outer/inner lease, release SHA, fencing token과 invocation deadline 결속
- rolling definition convergence와 startup command drain
- competing claim의 single winner, stale revision/write 차단
- safe retry budget과 effectful unknown-effect dead letter 분리
- source digest, reason, generation, request UUID에 결합된 manual replay CAS
- sealed production registry와 arbitrary alias/reconstruction 거부
- renewal/heartbeat supervisor failure의 active invocation cancellation
- shutdown drain, cleanup failure 보존과 idempotent resource close

## 남은 점수 항목

- **TS-5:** candle unknown-write를 immutable evidence와 durable write receipt로 판정하고
  명시적으로 해결하는 수동 recovery API가 없다.
- **FC-5:** exact dataset/code/feature manifest registry와 certified replay receipt가 없다.
- **FC-6:** certified source→store→DQ→feature→decision 자동 pipeline이 없다.
- **DI-7:** 공식 corporate-action PIT coverage·adjustment evidence와 독립 verifier
  receipt가 없다.
- **OP-7:** Worker와 다른 failure domain에 배치되는 dead-man service가 없다.
- **OP-8:** delivery ACK와 분리된 durable human ACK·owner·escalation 상태기계가 없다.
- **DT-6·DT-8:** packaged Tauri window smoke와 signed artifact provenance가 없다.
- **MA-5·MA-9:** 대형 Worker API, scheduler migration과 verifier의 변경 반경이 크다.

## exact-SHA 검증 영수증

| 범위           | 결과                                                                                           | 영수증                                                                                      |
| -------------- | ---------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------- |
| Worker         | Ruff PASS, strict mypy 448 source files, `2890 passed`                                         | [CI 30164099586](https://github.com/YoongeonChoi/msp/actions/runs/30164099586)              |
| Desktop        | lint, typecheck, contract/render tests, Playwright `21 passed`, build PASS                     | [CI 30164099586](https://github.com/YoongeonChoi/msp/actions/runs/30164099586)              |
| Tauri          | `cargo check/test/build --locked` PASS                                                         | [CI 30164099586](https://github.com/YoongeonChoi/msp/actions/runs/30164099586)              |
| DB baseline    | fresh·retained migration replay와 G1/G2 contracts PASS                                         | [CI 30164099586](https://github.com/YoongeonChoi/msp/actions/runs/30164099586)              |
| DB full matrix | PIT verifiers, collection stores, scheduler verifier와 seed PASS                               | [migration-check 30164099581](https://github.com/YoongeonChoi/msp/actions/runs/30164099581) |
| Security       | CodeQL, dependency gates, Bandit, gitleaks, pattern scan, lock evidence와 workflow policy PASS | [security 30164099578](https://github.com/YoongeonChoi/msp/actions/runs/30164099578)        |

Dedicated migration-check의 authoritative 종료 표시는 다음 두 receipt를 포함한다.

```text
FINAL=PASS g1_g2_migration_verifier
FINAL=PASS durable_operations_scheduler_verifier
```

Windows 격리 후보에서도 Worker `2888 passed, 2 skipped`, Ruff, mypy, Desktop
lint/typecheck/test/build, Playwright `21 passed`, migration history와 repository safety가
통과했다. Windows Application Control 때문에 로컬 Cargo 실행은 시작되지 않았고,
Docker Desktop engine 부재로 로컬 PostgreSQL replay는 실행하지 않았다. 이 두 항목은
같은 exact source의 Linux CI 결과로 검증했으며 로컬 PASS로 바꾸어 쓰지 않는다.

## 반복 실행 체크리스트

새 exact source SHA마다 아래 checkbox를 비우고 다시 실행한다.

- [ ] 안전 기본값과 Production order network 격리를 확인한다.
- [ ] strategy decision, risk result, feature hash와 정책 버전을 결속한다.
- [ ] semantic duplicate, stale lease/fence/epoch와 자원 경쟁을 차단한다.
- [ ] Paper partial fill, expiry, 비용과 원장 균형을 재현한다.
- [ ] PIT revision·occurrence·as-of·quarantine 변조와 충돌을 거부한다.
- [ ] Desktop role, maker/checker, stale/offline/session/cache 경계를 검증한다.
- [ ] outbox retry/dead-letter와 receiver 인증 실패가 성공으로 표시되지 않는지 확인한다.
- [ ] Worker, Ruff, strict mypy, Desktop, Playwright, Cargo와 migration replay를 실행한다.
- [ ] secret, dependency, CodeQL, workflow 권한과 migration history를 검사한다.
- [ ] exact source와 report publication receipt를 기록하고 67개 ID를 다시 계산한다.

## 숫자에서 제외하는 외부 항목

- Cyber Trusted Access가 필요한 hosted Supabase 적용
- 실제 AAL2 사용자 두 명의 maker/checker 운영
- 실제 alert/archive receiver와 human ACK
- 실제 Toss read-only provider 호출과 공식 계약 확인
- restore·24시간 soak·10거래일 Paper/Shadow 운영 증거

제외는 성공이 아니다. repository 내부의 권한, RLS, replay, 인증, 복구와 CI 계약은
계속 숫자 평가와 필수 gate 대상이고 `G0/G1/G2`도 계속 FAIL이다.

deployment-pause evidence와 scheduler fairness는 Cyber Trusted Access 항목이 아니다.
둘 다 저장소 내부 구현 backlog이며 `N/A (external)`로 숨기지 않는다.

## 비채점 Production activation blocker

고정 67개 ID에 별도 배점은 없지만 다음 두 항목이 해결되기 전에는 scheduler V2를
Production runtime에서 활성화하지 않는다.

1. Worker와 scheduler claim이 현재 deployment lock·target SHA·attempt를 함께
   재검증하고 lock 동안 lease·heartbeat는 유지하되 모든 신규 claim을 멈추는
   deployment-pause evidence가 없다.
2. 고정 priority가 지속적인 상위 작업 부하에서도 하위 due job을 starvation시키지
   않는 aging 또는 bounded-fairness 계약이 없다.

`render.yaml`의 `autoDeployTrigger`, `EXECUTION_V2_ENABLED`,
`EXECUTION_V2_WORKER_API_ENABLED`는 계속 비활성이다.

## 다음 원자 구현 순서

1. **Production activation hard blocker · 비채점:** deployment-pause evidence와
   claim 직전 DB 재검증을 구현한다.
2. **Scheduler fairness · 비채점:** aging 또는 bounded-fairness와 최대 대기시간
   회귀 테스트를 추가한다.
3. **OP-7·OP-8 — 최대 +1.50점:** 독립 dead-man과 durable human ACK·owner·escalation을
   구현한다.
4. **TS-5 — 최대 +0.80점:** evidence-bound unknown-write 수동 recovery를 추가한다.
5. **FC-5·DI-7·FC-6 — 최대 +2.90점:** verified source, dataset registry, certified
   replay와 source→DQ→feature pipeline을 순서대로 연결한다.
6. **MA-5·MA-9 — 최대 +1.20점:** 대형 Worker API/test와 migration/verifier를
   authority를 보존하는 수직 slice로 분해한다.
7. **DT-6·DT-8 — 최대 +0.70점:** packaged smoke와 signed artifact provenance를
   exact release SHA에 결속한다.

Tauri Linux GTK/WebKit 경로의 `glib 0.18.5` Medium 경보는 고정 rubric의
High/Critical TC-9를 무효화하지 않지만 별도 보안 부채로 남는다. 근거 없이 경보를
dismiss하지 않는다.
