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
- receiver ACK authentication failure의 단일 안전 코드와 outbox retry/dead-letter
  전이. unknown key, stale/future ACK, signature·body 변조는 공격자에게 oracle을
  주지 않도록 같은 코드로 합친다. URL path/query, key ID/material, signature, body는
  기록하지 않는다.
- incident severity, human ACK age, escalation state
- provider read health, market-data as-of/staleness, loop duration, memory pressure

## 목표와 경보

- emergency stop Worker observed 목표 10초, runtime postcondition 목표 15초
- critical incident human ACK 목표 5분
- stale heartbeat, expired lease, fencing rejection, ledger imbalance, oldest outbox
  age 초과, incident ACK 초과는 critical 경보다.
- dead-man monitor는 Worker와 다른 failure domain에서 heartbeat, lease, outbox,
  incident ACK를 감시한다.
- dead-man alert의 `episode_id`는 한 monitor process 안에서 reason 변화와 recovery를
  동일 장애로 묶고, recovery 이후 재발에는 새 UUID를 사용한다. Idempotency key는
  episode, event, reason, observation time에 결합되어 같은 실패 요청의 exact retry는
  안정적이고 새 관측은 다른 요청이 된다. 인증 실패 payload는 성공할 때까지 보존하며,
  unhealthy ACK가 성공하기 전에는 recovery 상태를 만들지 않는다.
- process 재시작을 넘는 episode 연속성을 보장하는 외부 durable state store와 실제
  failure-domain 분리 배포가 아직 없다. 해당 저장소·복구 시험·수신측 upsert/dedupe
  증거가 확보될 때까지 Hosted `G2 Operational Readiness`는 차단 상태다.

UI는 `PAPER`/`CONTRACT TEST`, `LIVE 금지`, source/as-of, stale/offline 상태를 항상
표시한다. stale/offline 상태에서 mutation은 전송·queue·자동 재시도하지 않는다.

