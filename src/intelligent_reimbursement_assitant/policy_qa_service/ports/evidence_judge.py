"""Per-item evidence boundary; execution-local facts are read-only."""

from collections.abc import Mapping
from typing import Protocol

from intelligent_reimbursement_assitant.execution_state_service.domain.models import (
    ExecutionContext,
)
from intelligent_reimbursement_assitant.policy_qa_service.domain.models import (
    EvidenceCheckRequest,
    FactValue,
    ItemAssessment,
)


class EvidenceJudgePort(Protocol):
    async def assess(
        self,
        request: EvidenceCheckRequest,
        ctx: ExecutionContext,
        *,
        fact_values: Mapping[str, FactValue],
    ) -> ItemAssessment: ...
