# Current Engineering QA Status

상태: `INTEGRATED SOURCE / DOCUMENTATION GATES PENDING`

통합 source SHA: `621d4925b8196accc653030bad561e07caf4a39e`

merge parents: `81110cdfdc935420a9511d470856927c0ba7da8b`,
`6793f60afab5e970c42881d618f9f4f8d17ace85`

통합 PR: [#16](https://github.com/YoongeonChoi/msp/pull/16) · merge candidate
`d990bd2d6052c6aa0f8011cf1d70dddbc43e6390`

평가일: 2026-07-24 KST

이 문서는 통합된 source와 그 다음 documentation 변경의 검증 상태를 보여 주는
current-status index다. `main`과 `develop`은 위 merge commit으로 동기화됐지만 이
README/QA 변경은 아래 `621d492…` source receipt에 포함되지 않았다. 이 문서를
포함한 새 commit의 push/PR gate와 재채점이 끝나기 전에는 숫자 점수를 부여하지
않는다. 마지막으로 완료된 고정 규칙·8개 영역·67개 ID와 `88.60` 기록은
[0c6b5f6 historical scorecard](archive/QA_ITERATION_SCORECARD_0c6b5f6.md)에
보존한다. 과거 점수는 통합 source나 Production Live 준비도로 승계되지 않는다.

## 고정 채점 계약

```math
QA_{total}=\sum_{k=1}^{8}Score_k\times Weight_k
```

| 영역                     |   가중치 | current 재평가 상태 |
| ------------------------ | -------: | ------------------: |
| TS · 거래 안전 경계      |      20% |           재평가 중 |
| FC · 기능 완성도         |      15% |           재평가 중 |
| DI · 데이터·연구 무결성  |      10% |           재평가 중 |
| OP · 운영 가시성         |      15% |           재평가 중 |
| DT · Desktop 효율·정확성 |      10% |           재평가 중 |
| TC · 테스트·CI           |      10% |           재평가 중 |
| DO · 문서·온보딩         |      10% |           재평가 중 |
| MA · 유지보수성          |      10% |           재평가 중 |
| **가중 종합**            | **100%** |          **미확정** |

채점 규칙은 다음과 같다.

- 커밋된 exact SHA만 평가한다.
- 각 ID는 요구 증거가 모두 있으면 전점, 하나라도 없으면 0점이다.
- 코드 존재만으로 PASS를 주지 않는다. 직접 테스트, exact source inspection 또는
  해당 SHA의 CI receipt가 요구사항을 증명해야 한다.
- 외부 접근권한이 필요한 항목은 `N/A (external)`로 숫자에서 제외하되 PASS로
  승격하지 않고 `G0/G1/G2` stage gate도 바꾸지 않는다.
- 커밋되지 않은 working-tree 변경은 평가에서 제외한다.
- 현재 67개 ID의 배점과 완료 조건은
  [historical scorecard의 엄격 QA 체크리스트](archive/QA_ITERATION_SCORECARD_0c6b5f6.md#엄격-qa-체크리스트)를
  source of truth로 유지한다. rubric을 바꾸려면 별도 근거와 version을 남긴다.

## 통합 source에서 확인된 변경

- Desktop principal 전환 시 이전 사용자의 query·mutation cache를 격리한다.
- migration history guard가 모든 merge parent, branch creation과 변경되지 않은
  base-only migration을 구분한다.
- 동일 dead-man episode의 성공 알림은 억제하고 실패한 exact delivery는 다시
  시도한다.
- `main`과 `develop`의 creation, deletion, non-fast-forward 변경을 GitHub
  ruleset으로 보호한다.
- Desktop navigation performance gate와 unknown-resolution E2E 동기화를
  runner scheduling noise에 견디도록 하면서 실제 누락·중복·severe 회귀는 계속
  실패시킨다.

이 목록은 점수가 아니다. 이 문서 변경을 포함한 documentation commit에서 각
변경이 기존 ID의 완료 조건을 실제로 충족하는지 새 exact SHA로 다시 증명해야 한다.

## exact-SHA gate 영수증

| 범위                        | Run                                                                         | 상태 |
| --------------------------- | --------------------------------------------------------------------------- | ---- |
| head push CI                | [30085780257](https://github.com/YoongeonChoi/msp/actions/runs/30085780257) | PASS |
| head push security          | [30085780344](https://github.com/YoongeonChoi/msp/actions/runs/30085780344) | PASS |
| PR merge-candidate CI       | [30085783543](https://github.com/YoongeonChoi/msp/actions/runs/30085783543) | PASS |
| PR merge-candidate security | [30085783454](https://github.com/YoongeonChoi/msp/actions/runs/30085783454) | PASS |
| PR full migration replay    | [30085783510](https://github.com/YoongeonChoi/msp/actions/runs/30085783510) | PASS |

위 영수증은 `6793f60…` source head와 두 parent가 정확한 `d990bd2…` merge
candidate를 검증했다. PR #16은 merge commit `621d492…`로 통합됐고 `main`과
`develop`이 같은 commit을 가리킨다. 현재 문서 후보는 이 영수증에 포함되지 않으므로
문서 commit의 push/PR gate가 성공한 뒤 67개 ID를 다시 채점하고, 이 표를 그 평가
SHA의 점수·근거·다음 구현 순서로 교체한다.

## 숫자에서 제외하는 외부 항목

- Cyber Trusted Access가 필요한 hosted Supabase 적용
- 실제 AAL2 사용자 두 명의 maker/checker 운영
- 실제 alert/archive receiver와 human ACK
- 실제 Toss read-only provider 호출과 공식 계약 확인
- restore·soak·10거래일 운영 증거

제외는 성공이 아니다. repository 내부의 권한, RLS, replay, 인증, 복구와 CI
계약은 계속 숫자 평가와 필수 gate의 대상이다.

## 현재 확인된 보안 부채

- `brace-expansion` High 2건은 통합 lockfile에서 수정됐고 `main` dependency
  graph 갱신 뒤 자동 해소됐다.
- Tauri의 Linux GTK/WebKit 전이 경로에는 `glib 0.18.5` Medium 경보가 남아
  있다. 호환되는 부모 stack을 확인해 취약한 `<0.20.0` 인스턴스가 완전히
  사라지는 별도 원자 변경이 필요하다. 단순 직접 의존성 추가나 근거 없는 경보
  dismiss는 완료로 인정하지 않는다.

## 다음 판정 순서

1. 이 문서를 포함한 documentation commit을 `origin/develop`에 push하고 exact
   SHA의 gate를 완료한다.
2. reviewed PR을 merge commit으로 `main`에 통합하고 `develop`을 fast-forward한다.
3. README/current-status 문서의 DO-6, unknown-write TS-5와 새 CI receipt를 증거로
   67개 ID를 재평가한다.
4. `glib` 부모 stack을 먼저 검증하고, 그다음 가장 큰 미획득 로컬 항목을 하나씩
   구현·검증·원자 커밋·push한 뒤 반복한다.
