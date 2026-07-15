# Execution Policy

## Approved execution boundary

이 release train의 실행 환경은 `paper | contract_test`뿐이다. Production Live 주문은
DB, Worker configuration, Desktop action, network boundary에서 모두 금지한다.

- `paper`: 외부 broker write를 호출하지 않는 영속 모의 체결
- `contract_test`: 고정 OpenAPI 계약을 따르는 로컬 simulator와 fault injection
- 실제 Toss API: 인증, 시세, calendar, 계좌·보유의 명시적으로 허용된
  read-only 검증만 사용. Production order create/status/cancel URL은 호출하지 않는다.

`contract_test`는 공식 broker sandbox가 아니며 UI, log, evidence에서 sandbox로
표시하지 않는다. 별도 공식 sandbox host와 계정 계약이 검증되기 전에는 외부 주문
write를 추가하지 않는다.

## V2 execution sequence

`ExecutionService`만 execution adapter의 create 동작을 호출할 수 있다. V2 sequence는
다음 순서를 벗어날 수 없다.

1. DB-backed worker lease와 fencing token을 얻는다.
2. 현재 account, execution environment, strategy/policy version, `control_epoch`를
   포함한 `ExecutionGate`를 읽는다.
3. decision과 `RiskService` 결과를 저장한다. Risk 정책을 우회하지 않는다.
4. `reserve_order_intent`가 동일 transaction에서 다음을 검사한다.
   - active lease와 fencing token
   - 동일 `control_epoch`
   - `paper | contract_test` environment
   - 허용된 whole-share quantity와 `LIMIT DAY` 주문
   - semantic duplicate 부재
   - 매수 reserve 또는 매도 가능 수량
   - 유효한 execution/cost/tick/settlement policy
5. `mark_dispatch_started`가 adapter 호출 직전에 lease와 `control_epoch`를 다시
   검사한다.
6. Paper simulator 또는 local contract simulator를 한 번 호출한다. create blind
   retry는 금지한다.
7. execution observation, order event, balanced accounting postings, cash/position
   projection, alert outbox를 하나의 transaction으로 기록한다.

Semantic identity는 다음 값의 canonical JSON SHA-256이다.

```text
account + environment + strategy + symbol + side
+ signal_valid_from + signal_valid_until + execution_policy_version
```

Cooldown과 semantic idempotency는 별도 통제다. 동일 semantic key에는 최대 하나의
active intent와 create attempt만 존재할 수 있다.

## Paper execution policy v1

- Currency: KRW
- Quantity: positive whole-share only
- Side: buy/sell
- Order type: `LIMIT`
- Time in force: `DAY`
- Unsupported: market, IOC/FOK, modify, short, margin, credit, derivative
- Earliest fill: decision 이후 첫 완전한 1분 bar
- Maximum participation: bar volume의 1%
- Slippage: 불리한 방향 10 bps, 단 limit 가격을 침범하지 않음
- Missing volume, tick rule, cost schedule, settlement rule, corporate-action state:
  fail closed

Buy fill:

- bar open이 limit 이하이면 adverse slippage를 적용하되 limit 이하로 제한한다.
- open이 limit보다 높지만 low가 limit에 닿으면 limit에서 체결한다.

Sell fill:

- bar open이 limit 이상이면 adverse slippage를 적용하되 limit 이상으로 제한한다.
- open이 limit보다 낮지만 high가 limit에 닿으면 limit에서 체결한다.

각 bar의 fill quantity는 다음 값의 최솟값이다.

```text
remaining_quantity, floor(bar_volume * 0.01)
```

비용·세금·결제일은 effective period와 SHA-256 evidence가 있는 승인 schedule만
사용한다. Schedule이 없거나 만료되면 체결하지 않는다. Position cost basis는
`moving_weighted_average_v1`로 계산한다.

## Accounting invariants

- `paper-primary` opening capital 10,000,000 KRW는 한 번만 balanced journal로
  기록한다.
- 매수 주문은 limit notional과 최대 비용을 reserve한다.
- 매도 주문은 settled available quantity를 reserve하며 허구 매도는 거부한다.
- partial fill은 체결분만 회계 처리하고 cancel/reject/expire 시 잔여 reserve만
  해제한다.
- DAY 잔량은 `evaluated_at >= expires_at`일 때만 만료한다. 그 전에는 예약을
  유지하며 미래 `expired` observation을 미리 기록하지 않는다. Fill/partial fill의
  완전한 bar와 observation 시각은 intent 만료 시각을 넘을 수 없다.
- 같은 observation/fill을 반복 적용해도 cash, quantity, PnL은 한 번만 변한다.
- 모든 accounting transaction은 debit 합계와 credit 합계가 같아야 한다.
- cash, reserved cash, quantity는 승인되지 않은 음수가 될 수 없다.

## Unknown and reconciliation

Timeout, response loss, identity mismatch, cumulative fill 감소, terminal regression은
자동으로 정상 상태로 보정하지 않는다.

- `unknown_requires_manual_check` 또는 `quarantined`로 격리한다.
- 자동 create retry와 자동 terminal 전환을 금지한다.
- operator가 immutable evidence를 제출하고 다른 `risk_approver`가 검토해야 한다.
- keyset pagination과 `next_reconcile_at`을 사용해 오래된 row가 이후 case를
  starvation시키지 않게 한다.

## Legacy evidence

기존 `public.orders`, `public.positions`, `manual_commands`, `audit_logs`는 추정 fill이나
현금분개로 변환하지 않는다. Cutover 이후 `legacy_unreconciled` 읽기 전용 evidence로
보존하며 신규 V2 cash, position, performance 계산에 합산하지 않는다.

## Reopening external order writes

외부 주문 write는 코드 flag 변경으로 열 수 없다. 별도 ADR과 migration에서 공식
sandbox contract, 전용 credential, endpoint allowlist, 법무·리스크 승인, hosted
fault test를 모두 검증해야 한다. Production Live는 G0 사업·규제 심사를 다시 열기
전까지 계속 금지한다.
