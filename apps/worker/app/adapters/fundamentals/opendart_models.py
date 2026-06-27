from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class OpenDartFinancialStatementPlaceholder(BaseModel):
    model_config = ConfigDict(extra="allow")

    corp_code: str
    fiscal_year: str

