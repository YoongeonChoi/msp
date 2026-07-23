# G1+G2 Test Plan

이 계획은 로컬 저장소 구현의 자동 검증과 별도 승인이 필요한 운영 증거를 구분한다.
테스트 통과만으로 Hosted Staging, G1, G2 또는 Production Live를 승인하지 않는다.

## 1. 데이터베이스와 권한

- raw PG17, `extensions.pgcrypto`가 선설치된 Supabase-like PG17, 기존
  `0015/public`, 기존 `0015/extensions`, 기존 `0023` fixture에서 preflight와 전체
  migration/seed를 실제 적용한다. 기존 migration checksum은 변경하지 않는다.
- base에 존재한 migration의 수정·삭제·rename·type change와 중간 commit 변경 후
  원복, staged/unstaged 상쇄를 거부한다. 신규 migration은 base tail보다 큰 고유
  version의 canonical 이름과 regular `100644` blob/file만 허용한다.
- pgcrypto extension/member 소유권 분리, PG17 `pgcrypto 1.3`의 36개 member
  signature/metadata drift, empty·retained public/extensions의 untrusted direct,
  inherited 또는 `SET ROLE` reachable `CREATE`, SQL comment가 섞인 문자열 body의
  qualified 호출, 함수 및 global/database/role 기본 `search_path` 호출을 변경 전에
  거부한다.
- `private` source of truth, 최소 `api` surface, allowlist된 `worker_api` RPC만 남는다.
- exposed table/view마다 RLS와 명시적 GRANT를 검증한다.
- `anon`, `authenticated`, `service_role` 및 7개 application role의 positive/negative
  matrix를 검증한다.
- `worker_api`는 authenticated/anon/public 실행이 불가능하고 service role만 호출한다.
- exposed wrapper는 `SECURITY INVOKER`; private definer는 `search_path=''`와 제한된
  EXECUTE만 가진다.
- Desktop snapshot은 `view_audit`와 `view_reconciliation`을 각각 private SELECT
  전에 검사한다. viewer와 나머지 non-auditor role은 두 응답 키를 유지한 exact
  `[]`를 받고, auditor는 두 permission과 known evidence를 direct PostgreSQL 및
  실제 PostgREST에서 받는다. `anon`과 `service_role`의 RPC 거부도 유지한다.
- legacy public order/control write, Live command 생성·변경, Live settings tuple을
  모두 거부한다.
- `audit_events` UPDATE/DELETE와 hash-chain mutation을 일반 사용자·Worker 모두
  거부한다.

### 1.1 Daily-candle collection attempt fence

- one-shot application runner는 기본 비활성이며 명시적 수동 확인과 durable
  job/observation store를 모두 요구한다. 이 gate는 clock, job store, provider,
  append I/O보다 먼저 실패해야 한다. 두 durable store의 credential 비포함
  origin/profile authority fingerprint가 다르거나 비정상이면 같은 지점에서 차단한다.
- fresh run의 순서는 `load -> begin -> provider read -> candidate fence -> append
  -> confirm`으로 고정한다. `count=1`만 요청하고 반환 cursor를 따라가지 않으며,
  fenced snapshot의 candle을 append하고 각 외부 경계를 최대 한 번만 호출한다.
- job spec은 UUIDv4 job, provider, KR 6자리 symbol, `1d`, adjusted flag,
  canonical UTC `before`, pinned provider-contract SHA-256, `count=1`,
  `pagination_allowed=false`, `automatic_retry_allowed=false`, exact
  `trigger=manual`을 canonical SHA-256 하나에 결합한다.
- begin은 spec SHA, expected revision, UUIDv4 attempt·holder를 CAS로 먼저
  기록한다. stale revision, concurrent second winner, reused attempt, 잘못된
  holder/fence는 상태를 바꾸지 않고 거부한다.
- candle은 provider/symbol/market/interval/adjusted/contract hash가 spec과
  같고 `provider_event_at <= before`이며 read observation clock이 attempt
  이후일 때만 append 전에 `candidate_fenced`로 저장된다.
- `paused_retryable`은 candidate 전 exact
  `provider_read_failed_before_candidate` reason만 허용한다. fenced candidate,
  pre/post-candidate `blocked_unknown`, completed state에는 TTL takeover나 자동
  begin/append retry가 없다.
- confirm은 append receipt의 key/hash/revision/stored clock을 DB에서 exact
  occurrence의 key/hash/observed_at/payload와 content revision에 다시 join한다.
  unchanged replay에서 content revision의 stored clock이 candidate occurrence
  clock보다 과거인 정상 경로를 허용하되 occurrence/content UUID는 DB가
  선택해야 한다. client `inserted` 값만으로 완료하지 않는다.
- begin/fence/block/confirm commit 전후 응답 유실과 cancellation, process
  restart, stale CAS를 fault-injection한다. candidate fence나 unknown state를
  읽은 후 provider/append 호출이 0회임을 확인한다. 외부 경계와 cleanup에
  secret-bearing cancellation을 주입해 최종 exception cause/context/traceback에
  원문이 남지 않는지 확인한다.
- provider authentication/rate-limit failure처럼 candidate가 없음을 타입으로
  확인할 수 있는 경우만 pause할 수 있다. timeout, unavailable, unknown
  exception, invalid schema/evidence는 retryable로 추측하지 않는다. append
  진입 뒤 모든 예외와 불신 receipt는 write outcome unknown이다.
- completed는 load-only replay이며 paused, collecting, candidate-fenced,
  blocked state 재진입은 provider/append 전에 닫혀야 한다. 별도 recovery
  assessment 없이 paused 상태도 다시 실행하지 않는다.
- in-memory adapter는 process-local reference임을 확인하고, Supabase verifier는
  private RLS job table, append-only event ledger, service-role-only RPC, 직접
  CRUD 차단, bounded strict response, zero-order-write를 검사한다.

### 1.2 Point-in-time calendar collection

- 단일 날짜 collection request는 정확한 `date`만 허용하며 잘못된 값은 clock이나
  source/store I/O 전에 거부한다.
- 한 번의 실행은 source fetch와 append를 각각 정확히 한 번만 수행하고 내부 retry나
  날짜 범위 loop를 만들지 않는다.
- source session은 canonical payload로 다시 검증하며 `KR`, 요청 날짜, local read
  window 안의 `observed_at`에 결합한다. clock 역행이나 timezone 누락은 append 전에
  fail closed한다.
- durable receipt의 calendar identity SHA, evidence SHA, `observed_at`이 source
  session과 정확히 일치해야 하며 유효한 `stored`와 `replayed`만 반환한다.
- observation RPC는 identity response encoding과 64 KiB 상한을 강제하고,
  duplicate JSON key·압축 응답·초과 응답을 receipt parsing 전에 거부한다.
- source/store/clock 오류의 payload, credential, 응답 본문은 application error와
  traceback에 노출하지 않는다. append 시도 후 transport 오류는 write outcome
  unknown으로 취급하고 durable evidence 확인 없이 자동 재시도하지 않는다.
- 단일 날짜 저장 성공은 calendar completeness, provider authenticity/finality,
  corporate-action·DQ 승인, dataset/research/feature/order readiness를 의미하지 않는다.

### 1.3 Manual fenced calendar range collection job

- 기본 `manual_execution_enabled=false`와 exact `trigger=manual`을 함께 요구하고,
  비활성·잘못된 요청은 clock, job store, UUID factory, collector보다 먼저 거부한다.
- job 범위는 inclusive 1..366일만 허용한다. 한 invocation은 attempt를 먼저 CAS로
  기록한 뒤 정확히 다음 날짜 하나만 수집하며 내부 loop나 자동 연속 실행이 없다.
- spec SHA, expected revision, attempt UUID, holder UUID, target date를 모든 begin/pause/
  block/confirm에 결합한다. stale fence, attempt ID 재사용, 동시 begin의 두 번째
  winner, 연속 prefix가 아닌 checkpoint를 거부하고 실패 전 상태를 보존한다.
- checkpoint는 source session/receipt의 canonical copy와 attempt fence/timeline을
  보존한다. `observed_at`은 attempt begin과 confirm 사이여야 하고 completed 상태는
  전체 날짜와 재계산 가능한 terminal manifest를 모두 요구한다.
- `write_outcome=not_attempted`이면서 허용된 pre-write 오류 코드일 때만
  `paused_retryable`로 이동한다. `unknown`, 변조·예상 밖 오류, 잘못된 수집 증거는
  `blocked_unknown` 또는 unresolved active attempt로 남기며 자동 재시도하지 않는다.
- begin, pause, block, confirm 각각의 commit 전/후 응답 유실과 cancellation을
  fault-injection한다. begin 확인 전 collector 호출은 0회이고, confirm 응답 유실 뒤
  같은 날짜를 다시 수집하지 않으며, 오래된 active attempt를 TTL로 회수하지 않는다.
- in-memory adapter는 lock/CAS reference semantics만 증명하며 새 instance가 기존
  상태를 복원하지 못해야 한다. 별도 Supabase adapter는 service-role-only RPC와
  private RLS table/append-only attempt ledger를 사용하고 직접 table CRUD를 노출하지
  않아야 한다.
- disposable PostgreSQL에서 새 connection의 상태 복원, concurrent begin 단일 winner,
  stale CAS 무변경, attempt UUID 재사용 차단, pause 후 새 수동 attempt, blocked attempt
  무인 takeover 금지, complete manifest의 Python/SQL 동일성, ACL/RLS, 주문 경로
  zero-write를 검증한다. 기본 비활성 one-shot 수동 runtime 외의 main runtime,
  scheduler 연결이나 unresolved-write manual recovery 승인은 별도 항목으로 남긴다.
- pinned local PostgREST의 실제 `worker_api` profile에서 다섯 mutation RPC envelope와
  read-only inspect RPC를 확인한다. inspect는 유효한 missing/present UUID의
  `job_found`/`snapshot` envelope, invalid/non-v4 UUID 거부, service-role-only ACL과
  `anon`/`authenticated`/잘못된 profile 차단을 검증하고, 호출 전후 job·attempt-ledger
  count가 같으며 기존 job은 전체 fingerprint도 같아 zero-write임을 증명해야 한다.
  deterministic stale CAS는 retryable
  serialization failure 대신 bounded `PT409`로 종료되어야 한다. 별도 366일 batch는
  checkpoint 366개, revision 733, ledger 732건, Python/SQL manifest parity와 4 MiB
  response headroom을 검증한다. 이는 Gateway/Kong, Hosted Supabase 또는 Production
  Live 승인 증거가 아니다.
- read-only recovery assessment는 유효한 요청마다 inspector를 정확히 한 번만 호출하고
  missing/ready/paused_retryable/paused_unrecognized/collecting/blocked_unknown/completed를
  분류한다. `collection_failed_before_write`와 정확히 일치하는 pause reason만
  paused_retryable 실행 후보이고, 다른 reason은 paused_unrecognized로 닫혀야 한다. 각 상태의
  recommended operator action은 검토 방향일 뿐이며 mutation 수행·retry·manual execution·
  manual recovery·Live authorization은 항상 false여야 한다. collecting/blocked_unknown은
  unresolved write outcome으로 표시하고 explicit manual invocation 후보로 만들지 않는다.
  spec/spec SHA 불일치, 변조 snapshot, inspector 예외는 payload나 credential 없이 고정
  오류로 fail closed해야 한다.
- assessment unit test는 service의 순수 분류·단일 inspector-read 계약을 증명한다.
  별도 default-disabled assessment CLI는 exact canonical spec SHA와 state/revision/
  count/next-date를 안전한 JSON으로 출력하되 mutation·Toss 호출을 0으로 유지하고,
  executable state가 아니면 `run_precondition`을 출력하지 않아야 한다.
  별도 migration/adapter/DB verifier는 service-role-only RPC, 실제 PostgREST profile,
  missing/present/invalid UUID, ACL과 zero-write fingerprint를 증명한다. Supabase
  inspector와 mutation adapter는 기본 비활성 one-shot command에서만 함께 선택한다.
  command는 exact spec SHA와 operator-reviewed classification/state-reason/revision/
  confirmed-count/next-date를 요구하고 assessment 결과를 실행 load 직후 다시 결합해야 한다.
  assessment와 load 사이에 다른 실행이 진행되면 attempt UUID 생성, begin, provider
  read 전에 실패해야 한다. `paused_retryable`은 exact reason과 별도 review 확인이
  필요하고, `paused_unrecognized`/`collecting`/`blocked_unknown`/`completed`는 mutation
  후보가 아니어야 한다.
- one-shot command는 호출당 정확히 한 날짜만 진행하고 retry loop, TTL takeover,
  automatic continuation을 만들지 않는다. 성공 결과는 attempt/holder/fencing revision,
  observation/occurrence identity와 durable receipt를 구조화해 노출하되 calendar
  idempotency key는 reviewed provider/market/processed date에서 재계산한 값과 정확히
  일치해야 하며 credential이나 provider payload는 출력하지 않는다. 정상·실패·
  cancellation·부분 생성 모두에서
  소유 HTTP client를 닫고 production order network request는 0이어야 한다.
- main Worker, scheduler, Render, Desktop, timing, feature, backtest, strategy, order에는
  이 command를 연결하지 않는다. Hosted operation과 unresolved-write reconciliation은
  아직 미구현이다.
- 성공 결과도 provider authenticity/finality, official exchange completeness,
  corporate-action·DQ, dataset/research/feature/backtest/order 또는 Production Live
  승인을 의미하지 않는다.

### 1.4 Retained calendar date-range coverage

- 요청 inclusive 범위의 모든 calendar date가 정확히 한 번씩 하루 단위 오름차순으로
  존재해야 한다. 첫·중간·끝 누락, 중복, 역순, 범위 밖 날짜는 fail closed한다.
- query/provider/`KR`/`selected_as_of`를 다시 결합하고 selected revision ID,
  occurrence ID, calendar idempotency key 재사용과 mixed provider contract를 거부한다.
- caller scope 보관본과 reader 전달용 request를 별도 canonical 객체로 유지해 adapter가
  전달 객체를 유효한 다른 범위로 변조해도 원 요청 query/scope 결합을 우회하지 못한다.
- request와 lineage의 timezone-aware clock은 fresh UTC 값으로 분리해 mutable `tzinfo`
  사후 변경이 결과 invariant나 이미 계산한 manifest를 바꾸지 못하게 한다.
- raw `candidate_count`는 correction·re-observation 때문에 selected 날짜 수보다 클 수
  있지만 작을 수는 없다. `received_at`은 semantic cutoff가 아니며 오직
  `snapshot_issued_at`보다 늦지 않은 lineage인지 확인한다.
- 범위 안 `next_business_date`는 target open 상태, 중간 closed 날짜, 정규장 시간을
  대조한다. 범위 밖 오른쪽 target은 suffix 내부 주장만 일치시키고
  `right_boundary_next_session_verified=false`로 남긴다.
- source failure은 한 번의 read 뒤 payload/credential 없이 고정 오류로 변환하고
  cancellation은 전파한다. 결과는 source와 분리된 canonical copy여야 하며 외부에서
  직접 생성할 수 없어야 한다.
- stable scope/data-lineage fingerprint는 timezone 표현, page size, snapshot token,
  issue time에 영향받지 않아야 한다. selected content/lineage, candidate count, raw
  manifest, selected contract가 바뀌면 data manifest가 바뀌어야 한다.
- raw snapshot manifest의 전체 candidate 검증 책임은 official reader에 있다. coverage
  gate는 selected items로 이를 재계산하거나 검증 완료를 주장하지 않고 결합만 한다.
- 성공은 `retained_calendar_date_range_only`이며 official exchange calendar completeness,
  provider authenticity/finality, corporate-action·DQ, dataset/research/feature/backtest,
  order 또는 Production Live 승인을 의미하지 않는다.

### 1.5 Local research completeness/finality assessment

- research slice와 retained calendar coverage를 각각 원래 gate로 다시 구성해 schema,
  scope, limitations, false certification flags, counts, source query/manifest, nested
  content·lineage, spec/data SHA 변조를 거부한다.
- provider, market, inclusive range, `selected_as_of`, calendar contract, open-session set,
  calendar payload와 양 결과에 공통으로 있는 revision·occurrence ID, revision number,
  received clock, origin, idempotency key가 exact match해야 한다. candle reader 결과에
  없는 calendar revision 원본 `observed_at`은 limitation으로 고정한다.
- local retained chain/date-range 검증은 true로 기록할 수 있지만 official exchange
  completeness, provider history completeness, authenticity, finality, full research와
  promotion은 외부 증거 SHA가 없으면 모두 false여야 한다.
- assessment manifest는 두 source spec/data SHA와 cross-source shared-calendar-lineage SHA를
  canonical JSON으로 결합한다. timezone 표현, reader page size, snapshot token·issue time은
  동일 논리 입력의 digest를 바꾸지 않고, source data manifest 변화는 digest를 바꿔야 한다.
- `require_full_research_certification()`은 canonical blocked assessment도 고정 오류로
  거부하고, forged true flag·evidence SHA·manifest SHA는 invalid assessment로 거부한다.
- 이 순수 동기 V1은 external verifier나 raw JSON loader를 제공하지 않는다. 따라서
  cancellation·duplicate-key 검증은 해당 경계가 추가될 때 별도 테스트한다.
- runtime, dataset registry, feature, backtest, strategy, order 호출 경로는 0이어야 하며
  corporate-action·DQ와 certified replay는 각각 DI-7·FC-5 범위로 남긴다.

### 1.6 Corporate-action 상태와 local DQ assessment

- research slice와 retained calendar coverage를 원래 gate로 다시 구성하고 DI-5의
  cross-source assessment도 재생성한 뒤에만 local DQ 결과를 만든다. 최종 객체는 두
  canonical source 사본을 보존하고 serialize·validate·require 때 다시 재구성해야 한다.
- 고정 policy SHA와 10개 check ID는 canonical candle/calendar/timing payload·hash,
  OHLCV 구조, next-session timing, retained session/date coverage, revision·occurrence
  uniqueness, provider-contract pin, shared calendar lineage만 다룬다. 가격 이상치,
  거래정지, 상장·폐지, corporate-action event 또는 외부 completeness를 통과했다고
  주장하지 않는다.
- `adjusted=true|false` 어느 쪽도 corporate-action 증거가 아니다. opaque SHA, 빈 event
  list, Paper fixture를 입력으로 받거나 `not_required`로 승격하지 않는다.
- local retained check는 true일 수 있지만 corporate-action evidence는 `None`, coverage와
  adjustment 검증, full DQ, dataset registration, feature/backtest, strategy/order 사용은
  모두 false여야 한다. canonical blocked manifest도 require gate에서 거부한다.
- source top-level·nested field, check ID/count, policy/result SHA, corporate-action field,
  certification flag, rejection/limitation과 최종 digest를 함께 위조해도 거부해야 한다.
  source data·lineage 변화는 result/final digest를 바꾸고, page size·snapshot token·issue
  time만 바뀐 동일 논리 evidence는 digest를 바꾸지 않아야 한다.
- raw candidate 수가 selected session 수보다 큰 정상 correction/re-observation을 DQ
  실패 수로 오인하지 않는다. bool/int 혼동, naive clock, noncanonical digest와
  secret-like exception text를 fail closed한다.
- runtime, scheduler, dataset registry, feature, backtest, strategy, order 호출 경로에는
  import가 0이어야 한다. positive corporate-action/full-DQ path는 별도 official-source
  계약과 independently reviewed verifier 전까지 추가하지 않는다.

## 2. G1 실행·회계 불변식

- 동일 semantic intent 100개 경쟁 요청에서 정확히 하나만 예약된다.
- semantic hash는 Python과 PostgreSQL의 canonical byte contract가 일치한다.
- decision/risk/account/environment/strategy/policy/cost evidence가 없거나 유효기간이
  어긋나면 예약하지 않는다.
- stale lease, fencing token, `control_epoch`, disabled gate를 reserve와 dispatch에서
  각각 거부한다.
- 매수 cash commitment와 매도 가능 수량을 원자적으로 예약한다.
- partial fill은 예약을 부분 소비하고 terminal cancel/reject/expire는 잔량을 해제한다.
- cash, reserved cash, position, reserved quantity는 음수가 될 수 없다.
- opening journal은 `paper-primary`에 10,000,000 KRW를 한 번만 기록한다.
- 모든 accounting transaction은 debit 합계와 credit 합계가 같다.
- `moving_weighted_average_v1` 매수 원가와 부분 매도 cost relief/realized PnL을
  검증한다.
- 같은 observation/fill 재처리는 projection과 posting을 바꾸지 않는다.
- cumulative fill 감소, terminal 회귀, provider identity 불일치는 quarantined가 된다.
- unknown은 자동 terminal/재전송되지 않고 2인 검토 전까지 차단된다.
- unknown 또는 다른 비-legacy reconciliation break가 생기면 같은 transaction에서
  account execution이 disabled 되고 `control_epoch`가 증가하며, 미해결 상태에서는
  새 intent insert가 거부된다.
- 현재 로컬 release에서 unknown 케이스는 읽기 전용으로 노출한다. evidence-specific
  회계 복구와 2인 종결 RPC가 추가·검증되기 전에는 해당 intent를 manual 상태에서
  해제하지 않으며, 이 항목은 G1/G2 미통과 사유로 남는다.
- reconciliation keyset pagination이 오래된 50건 뒤의 주문을 굶기지 않는다.

## 3. 결정론적 Paper와 contract_test

- KRW, whole-share, `LIMIT`, `DAY`, buy/sell만 허용한다.
- decision 이후 첫 완전한 1분 bar 이전에는 체결하지 않는다.
- bar volume 참여율 1%, 불리한 slippage 10 bps, limit 비침범을 검증한다.
- 부분체결과 잔량 만료를 검증한다.
- 승인된 cost schedule, tick rule, volume evidence, corporate-action state 중 하나라도
  없거나 stale이면 체결하지 않는다.
- 로컬 `contract_test`의 create→status→partial→terminal 및 cancel lifecycle과
  timeout/5xx/malformed/duplicate/unknown fault injection을 검증한다.
- production Toss order host로 향하는 write request 수가 정확히 0인지 검증한다.

## 4. 복구·outbox·HA

- reserve, dispatch, provider response, ledger commit 각 crash point에서 재시작 후
  중복 주문·중복 분개 없이 복구한다.
- active lease 단일 소유, monotonic fencing, expiry reclaim, stale holder rejection을
  검증한다.
- reconciliation completion은 현재 account lease의 release SHA와 fencing token을
  재검증하고, caller가 제출한 시각이 아니라 DB 시각으로 lease expiry를 판정한다.
- outbox `SKIP LOCKED`, DB-clock lease reclaim, complete/fail ACK, exponential
  backoff, dedupe, dead-letter를 검증한다.
- alert transport 장애에서도 domain transaction은 보존되고 at-least-once delivery와
  수신측 dedupe가 성립한다.
- main/outbox/dead-man URL은 유효한 HTTPS 입력을 canonical request target으로
  정규화하고 userinfo, fragment, redirect, 공백·control, 잘못된 port를 거부한다.
- receiver ACK는 독립 golden HMAC vector, current/previous key, exact request/response
  hash, status, serialized target, timestamp, context와 event identity를 검증한다.
  unsigned/duplicate/unknown/stale/future/tampered ACK와 cross-item·destination·URL·payload
  replay는 실패해야 한다.
- dispatcher 실제 adapter 통합에서 인증 실패는 complete 0/fail 1, 정상 서명은
  complete 1, mixed batch는 각각 한 번만 settle해야 한다. Completion DB write crash 후
  attempt 2는 같은 request의 cached ACK를 still-configured current key 또는 rotation
  overlap의 previous key에 한해서만 허용한다. Retired/unknown key는 거부한다.
- dead-man 인증 실패는 exact pending alert와 episode를 유지하고, unhealthy가 인증되기
  전에 recovery를 보내지 않는다. observation이 바뀌면 request와 idempotency key도
  함께 바뀌며 exact retry만 동일해야 한다.
- request/response size와 전체 wall-clock timeout을 검증하고, `httpx`/`httpcore` INFO
  로그에 webhook path/query, body, key 또는 signature가 남지 않아야 한다.
- emergency stop은 같은 transaction에서 `enabled=false`, `control_epoch+1`을 만든다.
- command는 `requested → approved → claimed → applied` 순서를 벗어나지 않는다.
- qualification은 request뿐 아니라 Worker apply 시점에도 유효기간과 모든 policy,
  strategy, release, provider pin이 일치해야 한다.
- self approval, stale CAS, AAL1, 만료/재사용/다른 hash의 step-up grant를 거부한다.

## 5. Desktop

- strict Zod V1 계약은 unknown field, 누락 version, 잘못된 UUID/time/SHA를
  `DataContractError`로 처리한다.
- 파싱 실패 시 숫자/boolean/environment 기본값을 만들지 않고 mutation을 차단한다.
- 역할별 action 허용/거부, maker/checker, platform_admin 거래 승인 불가를 검증한다.
- ACK와 runtime postcondition 전에는 성공 표시를 하지 않는다.
- Realtime 단절, stale heartbeat, offline, session expiry에서 mutation을 차단한다.
- offline emergency stop은 “전송되지 않음”이며 reconnect 자동 replay가 없다.
- keyboard-only, focus restore, `aria-live`, axe critical/serious 0을 검증한다.
- ESLint, TypeScript, component/unit, Vite build, Playwright, Cargo check/test/build를
  CI 필수 job으로 실행한다.

## 6. 공급망 명세와 commit 결속

- npm lock v3의 root/workspace package, workspace link와 모든 nested
  `node_modules` locator를 보존한다. root/workspace의 5개 dependency map을 local
  lock descriptor와 exact 비교하고, registry artifact의 실제 name/version URL,
  alias 선언, SHA-512 integrity, 정규 locator를 검증한다.
- Worker production lock과 보안 도구 lock은 exact `name==version`, 하나 이상의
  SHA-256, 중복 없는 정규화 이름만 허용한다. `pyproject.toml`과
  `requirements.txt` 선언은 일치해야 한다. continuation과 Win32 marker는 의미를
  보존하고 direct dependency marker는 lock과 같아야 한다. URL·editable·index
  option·지원하지 않는 version/marker를 거부한다.
- Cargo lock v4의 crates.io package는 모두 SHA-256 checksum을 가져야 한다. checksum
  없는 package는 `Cargo.toml`과 같은 local root 하나만 허용하고 alternate registry와
  Git source는 거부한다. local root의 direct dependency 이름은 manifest와 정확히
  같아야 한다.
- 같은 input은 byte-identical canonical inventory를 만들고 timestamp, 절대 경로,
  runner 정보는 포함하지 않는다. lock 세 종류의 parse 가능한 한 글자 변조와
  committed inventory 수동 편집은 모두 stale failure가 되어야 한다.
- CI receipt는 full lowercase `${{ github.sha }}`, 실제 `HEAD^{commit}`과 tree,
  각 regular-file Git blob, canonical inventory digest를 다시 결속한다. evidence file이
  checkout 뒤 바뀌거나 revision이 다르면 receipt를 만들지 않는다.
- 새 job은 명시적 `contents: read`, full commit SHA-pinned Action,
  `persist-credentials: false`로 실행한다. project dependency를 설치하지 않고 앱·배포
  secret, OIDC·deploy·order 권한, job/step skip이나 `continue-on-error`를 허용하지
  않는다. 실패는 Security workflow를 실패시킨다. PR에서는 merge candidate SHA를
  검증하며 source branch head 또는 release artifact라고 부르지 않는다.
- 이 검증은 custom lock inventory와 unsigned CI receipt만 증명한다. 표준 SBOM,
  실제 설치 closure, Tauri/Worker artifact digest, signing, attestation, promotion,
  G0/G1/G2 또는 Production Live 승인은 별도 미완료 항목이다.
- dependency range와 resolver 호환성은 이 parser가 재구현하지 않는다. 기존
  `npm ci`, Worker production-lock packaging contract, Cargo `--locked` 검증이 각각
  담당한다.

## 7. 운영 증거 — 로컬 자동 테스트 밖

다음은 실제 환경과 서로 다른 두 명의 운영 사용자가 필요하므로 별도 사용자 승인
전에는 완료로 표시하지 않는다.

- Supabase Auth TOTP enrollment와 실제 AAL2 세션
- 외부 immutable audit archive receipt와 DB hash 대조
- 실제 alert channel 전달·5분 human ACK·escalation
- active/warm standby와 독립 failure-domain dead-man monitor
- isolated restore drill로 시장시간 RTO 30분 측정
- Hosted DB disaster-scope RPO 증명
- 24시간 fault soak와 연속 10거래일 Paper/Shadow 운영
- ledger imbalance, 허구 매도, 중복 주문·분개, Sev1/Sev2/P0/P1 최종 게이트

Hosted Staging 적용과 외부 credential 사용은 별도 사용자 승인 없이는 수행하지 않는다.
Production Live는 이 테스트 계획의 후속 게이트가 아니라 금지 경계다.
