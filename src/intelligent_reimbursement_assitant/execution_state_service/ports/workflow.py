from typing import Protocol

from intelligent_reimbursement_assitant.execution_state_service.domain.models import (
    ExecutionContext,
    WorkflowResult,
)


class WorkflowExecutor(Protocol):
    async def execute(self, ctx: ExecutionContext) -> WorkflowResult:
        """Return success only after business publication and complete commit together.

        Task cancellation requests cooperative stopping. Await owned execution resources;
        only raise INTERRUPTED when stopped and safe to resume, or CANCEL_REQUESTED when
        cancellation is proven stopped. CancelledError alone supplies neither proof.
        """
        ...
