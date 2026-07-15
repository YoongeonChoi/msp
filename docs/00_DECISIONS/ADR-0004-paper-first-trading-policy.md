# ADR-0004 Paper First Trading Policy

Status: superseded by ADR-0007 (2026-07-14)

All strategies start in paper mode. `bot_settings.enabled=false`, `mode=paper`, and `live_order_allowed=false` are hard defaults.

이 ADR의 과거 Live 전환 조건은 더 이상 유효하지 않다. ADR-0007 범위에서는
`paper | contract_test`만 허용하며 Production Live 활성화 절차는 존재하지 않는다.
외부 주문 요구가 생기면 이 설계를 확장하지 않고 G0 사업·규제 심사를 다시 연다.

