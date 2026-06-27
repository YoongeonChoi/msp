from app.domain.trading.entities import DecisionSnapshot


class CreateDecisionSnapshot:
    async def execute(self, snapshot: DecisionSnapshot) -> DecisionSnapshot:
        return snapshot

