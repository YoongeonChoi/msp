# ADR-0007 Execution Safety Kernel

Status: accepted
Accepted: 2026-07-14

## Context

현재 `orders` row는 주문 요청과 최신 상태를 보여주는 read model 역할은 하지만,
주문 의도, provider dispatch, 부분체결, 수수료·세금, 현금 이동, position lot과
재처리 과정을 완전히 재구성하지 못한다. Paper account도 cycle 사이에 지속되지
않는다.

현재 단일 worker는 중복 가능성을 줄이지만, 같은 경제적 의미의 주문이 서로 다른
`decision_id`를 가지면 기존 idempotency key만으로 경쟁 주문을 막을 수 없다.
또한 live disable과 broker dispatch 사이에 설정 변경이 생기는 TOCTOU 경계가 있고,
worker lease/fencing이 없어 안전한 수평 확장이나 failover를 지원할 수 없다.

이 ADR은 새로운 live 기능을 허용하기 위한 것이 아니다. 이번 release train의
외부 실행 상한은 고정 OpenAPI 계약을 따르는 로컬 `contract_test` simulator이며,
실제 Toss API는 읽기 전용 작업만 허용한다. Paper/Shadow 환경에서 동일한
실행·회계 불변식을 검증하고, 미래의 broker write 경로를 재현 가능한 안전 커널
뒤에 두기 위한 결정이다.

## Decision

### 1. Source of truth와 projection을 분리한다

- append-only `order_intents`, `order_attempts`, `order_events`, `fills`,
  `accounting_transactions`, `accounting_postings`를 실행·회계 source of
  truth로 사용한다.
- 현재 `orders`와 향후 `positions`는 UI와 조회를 위한 projection으로 취급한다.
- projection을 직접 수정해 source event를 우회하는 경로를 허용하지 않는다.
- broker 상태는 외부 사실의 기준이며, reconciliation observation도 append-only
  event로 남긴다.

### 2. 계좌 범위를 모든 실행 identity에 포함한다

모든 intent, reservation, attempt, fill, posting, reconciliation은 최소한 다음
identity에 결합한다.

- environment
- broker
- account
- strategy version
- decision/correlation id
- symbol, side, order type
- release SHA, risk/config version

`provider_order_id`는 broker와 account 범위에서 unique해야 한다. raw account id나
credential은 audit·log·Desktop에 노출하지 않고 내부 stable identifier를 사용한다.

### 3. Semantic reservation을 DB transaction으로 원자화한다

broker call 전에 Postgres transaction 또는 단일 server-side RPC가 다음을 한 번에
수행해야 한다.

1. 활성 worker lease와 fencing token 확인
2. 최신 monotonic `control_epoch`와 risk/config version 확인
3. account + strategy + symbol + side + intent window 기준 중복 확인
4. risk-approved intent와 reservation 생성
5. immutable audit correlation 생성

reservation 성공 전에는 broker adapter를 호출할 수 없다. 같은 의미의 intent가
경쟁하면 하나만 reservation을 얻고 나머지는 명시적으로 차단된다.

### 4. Worker lease와 fencing을 사용한다

- trading worker는 DB-backed lease를 얻어야 cycle을 실행할 수 있다.
- lease renewal마다 증가하거나 단조 증가하는 fencing token을 사용한다.
- stale token의 reservation, attempt, event write는 DB에서 거부한다.
- lease/fencing 검증이 끝나기 전에는 `numInstances`를 늘리거나 자동 failover를
  켜지 않는다.

### 5. Control epoch를 dispatch 직전에 재검증한다

- stop/disable은 단순 boolean 외에 monotonic `control_epoch`를 증가시킨다.
- risk approval과 reservation은 관찰한 epoch를 기록한다.
- `ExecutionService`는 broker dispatch 직전에 동일 epoch와 reservation validity를
  server-side check로 다시 확인한다.
- stale epoch는 무조건 fail-closed하고 새 intent 생성을 요구한다.

### 6. Fill과 회계 posting은 idempotent하게 처리한다

- provider execution identity 또는 검증된 composite identity로 fill을 deduplicate한다.
- 같은 fill을 여러 번 수신해도 cash와 quantity는 한 번만 변한다.
- fill은 quantity, price, commission, tax, filled time, settlement date와 source
  observation을 보존한다.
- 모든 cash posting은 balanced journal entry를 형성한다.
- 현재 범위에서는 승인되지 않은 negative cash, short quantity, margin balance를
  허용하지 않는다.
- Paper simulator와 live reconciliation은 같은 ledger interface를 사용하되 서로
  다른 execution adapter와 account를 사용한다.

### 7. Unknown resolution은 dual control을 요구한다

- `unknown_requires_manual_check`는 provider observation만으로 자동 terminal 상태가
  되지 않는다.
- operator가 evidence를 첨부해 resolution을 요청하고 다른 approver가 검토한다.
- 승인된 worker command만 compensating event 또는 confirmed terminal event를
  기록할 수 있다.
- 원본 event를 수정하거나 삭제하지 않는다.
- pagination과 fairness를 적용해 오래된 unknown row가 이후 open order의
  reconciliation을 starvation시키지 않게 한다.

### 8. Alert delivery를 transactional outbox로 처리한다

- critical event와 alert outbox row는 같은 transaction 경계 또는 검증 가능한
  causal link로 생성한다.
- dispatcher는 bounded retry, next-attempt time, delivery result, dead-letter,
  escalation과 operator ACK를 기록한다.
- webhook 실패를 호출부에서 무시하지 않는다.
- outbox 전달 실패 자체가 독립 monitor와 Cockpit에 보인다.

## Invariants

다음 항목은 database constraint, transaction, property test 또는 fault-injection
test로 강제한다.

1. 하나의 semantic reservation에는 최대 하나의 active broker attempt가 있다.
2. reservation 없는 broker attempt는 존재할 수 없다.
3. stale fencing token과 stale kill epoch는 write/dispatch 권한이 없다.
4. 같은 provider fill은 ledger effect를 최대 한 번 만든다.
5. journal transaction의 debit과 credit 합계는 일치한다.
6. 지원하지 않는 negative cash, negative quantity, short, margin 상태는 생성되지
   않는다.
7. append-only event와 posting은 update/delete할 수 없다.
8. terminal state regression은 correction/compensating event 없이 일어나지 않는다.
9. 모든 intent→attempt→event→fill→posting은 하나의 correlation chain으로 추적된다.
10. Paper와 live account, environment, provider identity는 섞이지 않는다.

## Delivery sequence

1. Domain model과 property-based invariant test를 먼저 추가한다.
2. Additive migration으로 account, lease, intent, reservation, event, fill, posting
   table과 제한된 RPC를 만든다.
3. RLS, explicit GRANT, private schema, service identity와 append-only trigger를
   검증한다.
4. Paper adapter만 새 kernel에 연결하고 current read model을 projection한다.
5. restart, duplicate, two-worker race, alert outage, partial-fill/cancel fault test를
   통과한다.
6. Cockpit은 strict read model로 ledger와 command receipt를 표시한다.
7. Sandbox provider lifecycle과 independent review 전에는 live adapter를 새
   kernel로 활성화하지 않는다.

Migration은 additive/forward-first로 진행한다. 기존 `orders`를 즉시 삭제하거나
기존 데이터를 추정 fill로 변환하지 않는다. historical row의 정확한 fill을 알 수
없으면 `legacy_unreconciled` evidence로 격리한다.

## Consequences

- table과 state transition 수가 늘고 구현 복잡도가 증가한다.
- 대신 crash, duplicate delivery, partial fill, reconciliation과 감사 재현을 하나의
  일관된 모델로 처리할 수 있다.
- single worker 제한은 lease/fencing 검증이 완료될 때까지 유지한다.
- current `orders.status`만으로 회계나 실행 진실을 판단하는 코드와 UI를 단계적으로
  제거해야 한다.
- Supabase Data API에는 좁은 projection/RPC만 노출하고 worker-only source tables는
  private schema에 둔다.

## Rejected alternatives

### Existing `orders` row에 column만 계속 추가

최신 상태 조회에는 간단하지만 event history, duplicate fill, correction, balanced
cash movement와 crash recovery를 안전하게 표현하지 못한다.

### Redis lock 또는 process memory lock만 사용

broker dispatch와 durable reservation을 같은 증거 경계에 묶지 못하고 process
restart 또는 split-brain에서 안전하지 않다.

### Broker 응답 성공 후 ledger 기록

broker 성공과 DB failure 사이에서 주문이 사라질 수 있다. pre-dispatch reservation과
append-only attempt가 먼저 필요하다.

### Unknown 상태 자동 해제

provider observation 오류나 잘못된 identity mapping이 실제 position/cash를 오염할
수 있다. evidence와 maker-checker가 있는 명시적 resolution을 유지한다.

## Approved decisions

- 운영 범위는 단일 법인 자기자본·전용 계좌이며 고객·tenant 기능은 제외한다.
- 실행 환경은 `paper | contract_test`만 허용하고 production live write는 DB,
  runtime config, UI, network policy에서 차단한다.
- `paper-primary`의 opening capital은 10,000,000 KRW이며 한 번만 분개한다.
- fee, tax, settlement, tick rule은 effective-dated evidence와 승인 hash가 없으면
  체결을 차단한다. 값을 코드 상수로 추정하지 않는다.
- semantic identity는 account, environment, strategy, symbol, side, signal validity
  window, execution-policy version의 SHA-256으로 계산한다.
- `execution_controls.control_epoch`가 kill epoch와 runtime/config revision의 단일
  소유권을 가지며 reservation과 pre-dispatch에서 모두 검증한다.
- journal은 balanced debit/credit postings를 사용하고 position cost basis는
  `moving_weighted_average_v1`로 고정한다.
- 운영 역할은 platform admin, operator, risk approver, strategy reviewer, auditor,
  release manager, viewer로 분리한다. 위험을 높이는 변경은 AAL2와 서로 다른
  requester/reviewer를 요구한다. Emergency Stop은 안전을 낮추지 않는 방향이므로
  AAL2 operator 한 명이 즉시 실행할 수 있다.
- 기존 `orders`, `positions`, `manual_commands`, `audit_logs`는 삭제하거나 가상
  fill로 backfill하지 않고 `legacy_unreconciled` 읽기 전용 증거로 보존한다.
- migration은 expand/cutover/contract 순서의 forward fix 방식으로 수행하며,
  최초 V2 journal 이후 destructive rollback을 금지한다.
- 외부 immutable audit sink와 hosted RPO 0 증명은 G2 운영 gate의 외부 증거다.
  저장소 구현만으로 해당 gate를 통과한 것으로 표시하지 않는다.
