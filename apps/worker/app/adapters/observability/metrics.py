from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LoopMetrics:
    cycle_id: str
    duration_ms: int
    decisions: int
    orders: int

