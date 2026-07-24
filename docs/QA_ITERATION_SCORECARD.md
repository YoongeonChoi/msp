# Current Engineering QA Scorecard — 02ba9be

상태: `VERIFIED SOURCE / SCORECARD PUBLICATION`

평가 source SHA: `02ba9be22efea4ff1123f2c6e4d0ac11cb03effe`

source tree SHA: `3baaef6160f11a22c98cc4460703b6bd4fd463dd`

merge parents: `621d4925b8196accc653030bad561e07caf4a39e`,
`8b4c57009c72df3c36600c73dde17183e4ca99f1`

통합 PR: [#17](https://github.com/YoongeonChoi/msp/pull/17)

평가일: 2026-07-24 KST

이 문서는 위 exact source를 고정된 8개 영역·67개 이진 ID로 다시 평가한 현행
보고서다. 소프트웨어 source와 이 평가 보고서를 같은 commit으로 만들면 보고서가
자기 자신의 SHA를 미리 기록할 수 없으므로, 평가 대상은 PR #17로 검증·통합된
`02ba9be…`로 고정한다. 이 파일을 갱신하는 publication commit은 제품 runtime을
바꾸지 않으며, 자체 push/PR gate를 통과해 `main`에 통합된 뒤 저장소 내부의
DO-7 현행 점수표 증거를 완결한다.

publication 전 `02ba9be…` tree 안의 오래된 `미확정` 문구만 기계적으로 보면
DO-7은 0점이고 임시 점수는 `88.40`이다. 이 보고서가 required gate를 통과해
통합되면 DO-7을 포함한 공식 engineering 점수는 `88.90`이다. 이 경계를 숨기거나
보고서 존재를 미리 PASS로 계산하지 않는다.

이 점수는 engineering 품질 추세이지 투자 성과, 수익 가능성, 보안 보증, 배포
승인 또는 Production Live 권한이 아니다. 기업 운영 stage gate `G0`, `G1`,
`G2`는 모두 계속 `FAIL`이며 Production Live는 `NO-GO`다.

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
- 한 기능을 검증·원자 커밋·`origin/develop` push·reviewed PR 통합한 뒤 새
  exact SHA에서 전 항목을 다시 계산한다.

## 현행 점수

| 영역                     |   가중치 | 점수 | 가중 점수 | PASS / 전체 |
| ------------------------ | -------: | ---: | --------: | ----------: |
| TS · 거래 안전 경계      |      20% |   96 |     19.20 |       4 / 5 |
| FC · 기능 완성도         |      15% |   74 |     11.10 |       4 / 7 |
| DI · 데이터·연구 무결성  |      10% |   95 |      9.50 |       6 / 7 |
| OP · 운영 가시성         |      15% |   80 |     12.00 |      8 / 11 |
| DT · Desktop 효율·정확성 |      10% |   93 |      9.30 |     10 / 12 |
| TC · 테스트·CI           |      10% |  100 |     10.00 |       9 / 9 |
| DO · 문서·온보딩         |      10% |  100 |     10.00 |       7 / 7 |
| MA · 유지보수성          |      10% |   78 |      7.80 |       6 / 9 |
| **가중 종합**            | **100%** |      | **88.90** | **54 / 67** |

```text
96×0.20 + 74×0.15 + 95×0.10 + 80×0.15
+ 93×0.10 + 100×0.10 + 100×0.10 + 78×0.10
= 88.90
```

Historical `0c6b5f6…`의 `88.60`보다 `0.30`점 높다. 점수 변화는 DO-6
`현재 상태와 역사 문서의 명확한 탐색` 3점을 획득한 것뿐이다. cache 격리,
migration-history 방어, dead-man 중복 억제와 Desktop E2E 동기화는 기존 PASS
항목을 강화했지만 이미 전점인 ID에 보너스를 주지 않았다.

## 67개 ID 판정 요약

| 영역 | PASS ID                                                         | MISSING ID        | 점수 |
| ---- | --------------------------------------------------------------- | ----------------- | ---: |
| TS   | TS-1, TS-2, TS-3, TS-4                                          | TS-5              |   96 |
| FC   | FC-1, FC-2, FC-3, FC-4                                          | FC-5, FC-6, FC-7  |   74 |
| DI   | DI-1, DI-2, DI-3, DI-4, DI-5, DI-6                              | DI-7              |   95 |
| OP   | OP-1, OP-2, OP-3, OP-4, OP-5, OP-6, OP-9, OP-10                 | OP-7, OP-8, OP-11 |   80 |
| DT   | DT-1, DT-2a, DT-2b, DT-2c, DT-2d, DT-2e, DT-3, DT-4, DT-5, DT-7 | DT-6, DT-8        |   93 |
| TC   | TC-1, TC-2, TC-3, TC-4, TC-5, TC-6, TC-7, TC-8, TC-9            | 없음              |  100 |
| DO   | DO-1, DO-2, DO-3, DO-4, DO-5, DO-6, DO-7                        | 없음              |  100 |
| MA   | MA-1, MA-2, MA-3, MA-4, MA-6, MA-8                              | MA-5, MA-7, MA-9  |   78 |

세부 배점과 완료 조건은 historical scorecard에 보존되어 있다. 현재 판정의 핵심
미완료 근거는 다음과 같다.

- TS-5: candle append의 미확정 결과를 durable evidence receipt로 조회·판정하고
  명시적으로 해결하는 수동 recovery API가 없다.
- FC-5: exact dataset/code/feature manifest registry와 certified replay receipt가
  없다.
- FC-6: one-shot candle runner가 runtime scheduler, certified DQ, feature 입력으로
  연결되지 않았다.
- FC-7·OP-11·MA-7: stage cadence는 process memory와 `asyncio.sleep`에 남아 있고,
  scheduler 작업 자체의 durable lease, retry budget, dead-letter, manual replay를
  하나의 restart-safe 조립 경계로 제공하지 않는다.
- DI-7: 공식 corporate-action PIT coverage·adjustment evidence와 독립 verifier
  receipt가 없다.
- OP-7: Worker와 다른 failure domain에 배치되는 dead-man service 계약이 없다.
- OP-8: transport ACK와 별도의 durable human ACK·owner·escalation 상태기계가
  완결되지 않았다.
- DT-6·DT-8: packaged Tauri window smoke, signing과 provenance가 없다.
- MA-5·MA-9: 대형 Worker API, SQL과 verifier의 변경 반경이 여전히 크다.

## exact-SHA 검증 영수증

| 범위                            | Run                                                                                              | 결과 |
| ------------------------------- | ------------------------------------------------------------------------------------------------ | ---- |
| exact `main` CI                 | [30088406559](https://github.com/YoongeonChoi/msp/actions/runs/30088406559)                      | PASS |
| exact `main` security           | [30088406624 attempt 2](https://github.com/YoongeonChoi/msp/actions/runs/30088406624/attempts/2) | PASS |
| exact `develop` CI              | [30088421894](https://github.com/YoongeonChoi/msp/actions/runs/30088421894)                      | PASS |
| exact `develop` security        | [30088421899 attempt 2](https://github.com/YoongeonChoi/msp/actions/runs/30088421899/attempts/2) | PASS |
| PR #17 merge-candidate CI       | [30087734890](https://github.com/YoongeonChoi/msp/actions/runs/30087734890)                      | PASS |
| PR #17 merge-candidate security | [30087734834](https://github.com/YoongeonChoi/msp/actions/runs/30087734834)                      | PASS |

`main` CI의 직접 결과:

- Worker: Ruff PASS, strict mypy 429 source files, `2416 passed`.
- Desktop: lint, typecheck, contract/unit fixture, Playwright `21 passed`, Vite build
  PASS.
- Tauri: `cargo check --locked`, `cargo test --locked`,
  `cargo build --locked` PASS.
- Migration: history guard, 47개 전체 migration replay, 실제 PostgREST 역할 경계,
  `FINAL=PASS g1_g2_migration_verifier`.
- Security: npm/pip audit, Bandit, gitleaks, common-pattern scan, CodeQL Python/JS,
  workflow policy와 lock-derived evidence PASS. Dependabot High/Critical은 0건이고
  Medium `glib 0.18.5` 한 건이 남아 있다.

Security attempt 1은 npm registry bulk 응답 실패 뒤 폐기 중인 quick-audit
endpoint로 fallback해 HTTP 400을 반환했다. source 변경 없이 attempt 2와 독립
PR/develop security run이 성공했다. `npm audit`를 우회하지 않았지만 이 외부
endpoint 불안정성은 CI 신뢰성 부채로 남긴다.

PR #17 head와 `02ba9be…` merge commit은 tree SHA
`3baaef6160f11a22c98cc4460703b6bd4fd463dd`가 같다. 원격 `main`과 `develop`도
평가 시점에 모두 `02ba9be…`를 가리켰다.

## 반복 실행 체크리스트

새 exact SHA마다 아래 checkbox를 비우고 다시 실행한다.

- [ ] 안전 기본값과 Production order network 격리를 확인한다.
- [ ] strategy decision, risk result, feature hash와 정책 버전을 결속한다.
- [ ] semantic duplicate, stale lease/fence/epoch와 자원 경쟁을 차단한다.
- [ ] Paper partial fill, expiry, 비용과 원장 균형을 재현한다.
- [ ] PIT revision·occurrence·as-of·quarantine 변조와 충돌을 거부한다.
- [ ] Desktop role, maker/checker, stale/offline/session/cache 경계를 검증한다.
- [ ] outbox retry/dead-letter와 receiver 인증 실패가 성공으로 표시되지 않는지
      확인한다.
- [ ] Worker, Ruff, strict mypy, Desktop, Playwright, Cargo와 migration replay를
      실행한다.
- [ ] secret, dependency, CodeQL, workflow 권한과 migration history를 검사한다.
- [ ] exact source와 report publication receipt를 기록하고 67개 ID를 다시
      계산한다.

## 숫자에서 제외하는 외부 항목

- Cyber Trusted Access가 필요한 hosted Supabase 적용
- 실제 AAL2 사용자 두 명의 maker/checker 운영
- 실제 alert/archive receiver와 human ACK
- 실제 Toss read-only provider 호출과 공식 계약 확인
- restore·24시간 soak·10거래일 Paper/Shadow 운영 증거

제외는 성공이 아니다. repository 내부의 권한, RLS, replay, 인증, 복구와 CI
계약은 계속 숫자 평가와 필수 gate의 대상이고 `G0/G1/G2`도 계속 FAIL이다.

## 다음 원자 구현 순서

1. **FC-7·OP-11·MA-7 — 최대 +4.00점:** collection/runtime scheduler에 durable
   job definition/run/lease, DB-clock fencing, bounded retry budget, dead-letter와
   reason-bound manual replay를 한 restart-safe 조립 경계로 추가한다. crash restart,
   stale lease takeover, duplicate dispatch, exhausted retry와 explicit replay를 직접
   검증하기 전에는 세 ID 어느 것도 PASS로 올리지 않는다.
2. **OP-7·OP-8 — 최대 +1.50점:** Worker와 다른 failure-domain dead-man service,
   delivery와 분리된 durable human ACK·owner·escalation 상태기계를 구현한다.
3. **TS-5 — 최대 +0.80점:** candle unknown-write를 추측·자동 retry하지 않고
   immutable evidence와 durable receipt로만 판정·해결하는 수동 recovery를 추가한다.
4. **FC-5·DI-7·FC-6 — 최대 +2.90점:** verified corporate-action source와 독립
   receipt를 확보한 뒤 dataset registry, certified replay, 자동 source→store→DQ→
   feature pipeline 순으로 연결한다.
5. **MA-5·MA-9 — 최대 +1.20점:** 대형 Worker API/test와 migration/verifier를
   authority를 유지하는 수직 slice로 분해한다.
6. **DT-6·DT-8 — 최대 +0.70점:** packaged window smoke와 signed artifact
   provenance를 exact release SHA에 결속한다.

`glib` Medium 경보는 위 고정 rubric의 High/Critical TC-9를 무효화하지 않지만
별도 보안 부채다. 사용자 작업 중인 `Cargo.toml`과 lock 관련 파일을 침범하지 않고
부모 Tauri/GTK/WebKit 호환 stack에서 `<0.20.0` 인스턴스가 완전히 사라지는
변경만 인정한다.
