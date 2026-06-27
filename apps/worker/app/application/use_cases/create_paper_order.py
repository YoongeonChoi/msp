from app.application.services.execution_service import ExecutionService
from app.domain.trading.entities import DecisionSnapshot, Order


class CreatePaperOrder:
    def __init__(self, execution_service: ExecutionService) -> None:
        self.execution_service = execution_service

    async def execute(self, snapshot: DecisionSnapshot) -> Order | None:
        return await self.execution_service.create_paper_order(snapshot)

