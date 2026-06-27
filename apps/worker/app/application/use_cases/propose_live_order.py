from app.application.services.execution_service import ExecutionService
from app.domain.risk.entities import RiskResult
from app.domain.risk.value_objects import RiskInput
from app.domain.trading.entities import DecisionSnapshot, Order


class ProposeLiveOrder:
    def __init__(self, execution_service: ExecutionService) -> None:
        self.execution_service = execution_service

    async def execute(
        self,
        snapshot: DecisionSnapshot,
        risk_input: RiskInput,
    ) -> tuple[Order, RiskResult]:
        return await self.execution_service.propose_live_order(snapshot, risk_input)
