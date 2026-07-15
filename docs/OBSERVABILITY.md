# Observability

## 안전 불변식

- Production Live 관련 설정, credential, endpoint가 감지되면 startup/CI가 실패한다.
- 로그에는 secret, 계좌번호, provider raw payload를 기록하지 않는다.
- `cycle_id`, `decision_id`, `risk_result_id`, `order_intent_id`, `correlation_id`,
  `release_sha`, account-safe identifier로 실행 흐름을 연결한다.
- `audit_events`는 append-only hash chain이며 외부 immutable archive receipt와
  DB hash를 별도로 대조한다.

## 필수 신호

- structured JSON logs와 redaction 결과
- scheduler heartbeat 및 active lease holder/fencing token
- `control_epoch`, runtime environment, release SHA
- ledger checkpoint, debit/credit imbalance, negative cash/reserve/quantity 시도
- reconciliation backlog age와 quarantined/unknown 건수
- command requested/approved/claimed/applied latency 및 postcondition
- outbox oldest age, retry count, dead-letter count, receiver dedupe result
- incident severity, human ACK age, escalation state
- provider read health, market-data as-of/staleness, loop duration, memory pressure

## 목표와 경보

- emergency stop Worker observed 목표 10초, runtime postcondition 목표 15초
- critical incident human ACK 목표 5분
- stale heartbeat, expired lease, fencing rejection, ledger imbalance, oldest outbox
  age 초과, incident ACK 초과는 critical 경보다.
- dead-man monitor는 Worker와 다른 failure domain에서 heartbeat, lease, outbox,
  incident ACK를 감시한다.

UI는 `PAPER`/`CONTRACT TEST`, `LIVE 금지`, source/as-of, stale/offline 상태를 항상
표시한다. stale/offline 상태에서 mutation은 전송·queue·자동 재시도하지 않는다.

