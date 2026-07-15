# UI/UX Spec

Design principles:

- Korean labels.
- Calm professional fintech UI.
- No casino-like visuals.
- Strong risk visibility.
- Accessible contrast.
- Responsive by default.

Global status:

- `PAPER` 또는 `CONTRACT TEST`
- 항상 표시되는 `LIVE 금지`
- Worker heartbeat age, lease/fencing token, release SHA
- ledger checkpoint와 data source/as-of
- connection/session freshness와 mutation 가능 여부
- 오늘 손익·주문 수·위험 경고

G1+G2 release navigation:

1. Operations Control
2. Access & MFA

기존 Dashboard, Watchlist, Portfolio, Orders, Signals, Fundamentals, News,
Strategy Lab, Logs 화면과 관대한 row adapter는 release source에서 제거하고 Git
이력에만 보존한다. 필요한 읽기 projection은 strict V1 contract로 Operations
Control에만 다시 도입한다.

Operations는 다음 surface를 제공한다.

- `OperationsStatusRail`: heartbeat, lease, release SHA, ledger checkpoint
- `SafetyCommandCenter`: 요청·승인·Worker ACK·postcondition timeline
- `ApprovalInbox`: maker/checker, 변경 diff, MFA, 만료
- `IncidentCenter`: ACK·담당·완화·서로 다른 승인자에 의한 종결
- `StaleDataBoundary`: stale/offline mutation 차단
- `ManualReconciliationCase`: unknown 증거와 fail-closed 대사 상태. 현재 release는
  읽기 전용이며, evidence-specific 회계 복구·2인 종결 RPC가 검증되기 전에는
  상태를 해제하지 않는다.

`applied`와 runtime postcondition을 모두 확인하기 전에는 완료로 표시하지 않는다.
offline emergency stop은 “전송되지 않음”으로 표시하며 queue나 reconnect replay를
허용하지 않는다.

Dangerous changes require confirmation, a fresh TOTP step-up grant, and the required
maker/checker role. Production Live action은 렌더링하지 않는다.
`bot_settings.enabled=false` means order creation is stopped; it must not imply
that cached Supabase data, provider health display, or feature pages are
unavailable.

## 후속 연구 화면

Strategy Lab과 연구 분석은 현재 release navigation에 포함하지 않는다. 향후 다시
도입할 때는 인증된 point-in-time 결과와 별도 strict read contract를 먼저 만들고,
전략 승격은 `strategy_reviewer` maker/checker command로만 수행한다. Desktop에서
provider API, Worker, broker 또는 `live_order_allowed`를 직접 호출·변경하지 않는다.
- Paper promotion is confirmation-gated and remains a runbook/manual workflow until a server-side safe promotion use case exists.
- Live promotion action is absent. `LIVE 금지` 상태는 색상뿐 아니라 텍스트로 항상
  표시한다.

Empty/error states:

- Empty Strategy Lab sections must explain the next safe action: seed strategy, run outcome update, run backtest, or generate monthly AI candidate.
- `backtest_runs` missing table or RLS read policy should show a warning instead of breaking the entire page.
