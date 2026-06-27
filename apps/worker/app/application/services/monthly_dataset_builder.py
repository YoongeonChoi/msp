from __future__ import annotations

from app.application.services.monthly_dataset_summaries import build_monthly_payload
from app.domain.strategy.research import MonthlyResearchDataset, MonthlyResearchRows, MonthPeriod


class MonthlyResearchDatasetBuilder:
    def build(self, period: MonthPeriod, rows: MonthlyResearchRows) -> MonthlyResearchDataset:
        return MonthlyResearchDataset(
            period=period,
            base_strategy_version_id=rows.base_strategy_version_id,
            base_strategy_version=rows.base_strategy_version,
            payload=build_monthly_payload(period, rows),
        )
