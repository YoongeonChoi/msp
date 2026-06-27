# UI/UX Spec

Design principles:

- Korean labels.
- Calm professional fintech UI.
- No casino-like visuals.
- Strong risk visibility.
- Accessible contrast.
- Responsive by default.

Global status:

- 봇 상태
- 모드
- 실주문 허용 여부
- Worker heartbeat age
- API health
- 오늘 손익
- 오늘 주문 수
- 위험 경고

Navigation:

1. Dashboard
2. Control
3. Watchlist
4. Portfolio
5. Orders
6. Signals
7. Fundamentals
8. News
9. Strategy Lab
10. Settings
11. Logs

Dangerous changes require confirmation. Live trading requires typed Korean phrase.

## Strategy Lab

Purpose:

- Paper Trading 성과 분석과 AI strategy candidate 검토.
- Desktop은 Supabase publishable key와 RLS만 사용한다.
- Toss/OpenAI/Naver/KRX/OpenDART API를 Desktop에서 직접 호출하지 않는다.
- Supabase secret/service_role key를 Desktop에 두지 않는다.

Sections:

- Current strategy: active paper strategy, version, status, `weights_json`, `params_json`, `deployed_at`, `created_at`.
- Editable JSON form is shown only for `draft` or `proposed` strategy rows where RLS permits update.
- Paper performance: recent `outcomes`, return_1d/5d/20d averages, win rate, max drawdown, order count, blocked reason counts.
- Backtest: recent `backtest_runs` when the table and admin read policy exist, including return, CAGR, MDD, win rate, turnover, fee/slippage assumptions.
- AI candidates: `ai_upgrade_candidates`, candidate weights, rationale, expected improvement, risk notes, required backtests, approve/reject controls.

Safety UX:

- Approving an AI candidate changes candidate review status only, currently `approved_for_paper`.
- Approval does not deploy a strategy, does not call the worker, does not create orders, and does not change `live_order_allowed`.
- Paper promotion is confirmation-gated and remains a runbook/manual workflow until a server-side safe promotion use case exists.
- Live promotion is disabled and visibly marked as not implemented.
- Anything related to live trading must use danger styling and text labels, not color alone.

Empty/error states:

- Empty Strategy Lab sections must explain the next safe action: seed strategy, run outcome update, run backtest, or generate monthly AI candidate.
- `backtest_runs` missing table or RLS read policy should show a warning instead of breaking the entire page.
