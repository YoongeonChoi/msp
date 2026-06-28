from __future__ import annotations

import hashlib


def build_idempotency_key(
    mode: str,
    decision_id: str,
    symbol: str,
    action: str,
    amount_krw: int,
) -> str:
    raw = "|".join([mode, decision_id, symbol, action, str(amount_krw)])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
