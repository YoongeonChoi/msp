# kr-auto-trading-lab

한국 국내 주식 자동매매 제어 시스템 MVP입니다. 기본 목표는 수익률보다 safety, correctness, maintainability, testability, observability, security입니다.

## Architecture

```text
Desktop Cockpit (Tauri + React)
  -> Supabase Auth/RLS/Realtime control plane
      -> Render Background Worker (Python modular monolith)
          -> Ports
              -> Toss / KRX / OpenDART / Naver / OpenAI adapters
```

Desktop은 broker secret을 보관하지 않고 broker 주문 API를 직접 호출하지 않습니다. Worker만 execution path를 소유하며, 실주문 생성은 공식 Toss contract에 맞춘 guarded KRX `LIMIT` 주문 경로로만 제한됩니다.

## Local Setup

```bash
cp .env.example .env
cd apps/worker
py -m pip install -e ".[dev]"
MOCK_PROVIDERS=true RUN_ONCE=true py -m app.main
py -m pytest
cd ../..
npm install
npm run desktop:dev
```

## Safety Defaults

- `bot_settings.enabled=false`
- `mode=paper`
- `live_order_allowed=false`
- `max_order_amount_krw=100000`
- 실주문 생성과 수동 취소는 공식 Toss contract 기반 guarded worker path로만 허용
- live cycle은 Toss 계좌/시장 데이터와 local KST 일일 주문 수를 검증하며, 검증 실패 시 fail-closed
- OpenAI output은 거래 실행에 직접 연결되지 않음

## Tests

```bash
cd apps/worker && py -m pytest
npm run desktop:typecheck
npm run desktop:build
```

## Live Readiness

- Strict scorecard: `docs/LIVE_READINESS_SCORECARD.md`
- Current work summary: `docs/LIVE_READINESS_WORK_SUMMARY.md`
- Operator runbook: `docs/RUNBOOK.md`

This repository is hardened for live-readiness review, but live operation is
blocked until the external hosted Supabase, provider lifecycle, incident ACK,
system-order-scope, and remote artifact evidence gates pass.
