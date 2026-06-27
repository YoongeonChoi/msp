from __future__ import annotations

import psutil


class MemoryMonitor:
    def usage_pct(self) -> float:
        return float(psutil.virtual_memory().percent)

    def pressure_high(self, threshold_pct: float = 85.0) -> bool:
        return self.usage_pct() >= threshold_pct

