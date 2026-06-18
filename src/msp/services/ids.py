from __future__ import annotations

import hashlib
import re
import uuid


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def broker_client_order_id(idempotency_key: str) -> str:
    digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:24]
    return f"msp_{digest}"


def normalize_symbol(symbol: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "", symbol.strip().upper())
    if not cleaned:
        raise ValueError("symbol is required")
    return cleaned
