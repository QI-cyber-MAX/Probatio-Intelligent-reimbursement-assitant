from typing import cast
from uuid import UUID

from intelligent_reimbursement_assitant.errors.codes import ErrorCode as Code
from intelligent_reimbursement_assitant.errors.contracts import ExecutionError
from intelligent_reimbursement_assitant.execution_state_service.domain.models import ExecutionStatus
from intelligent_reimbursement_assitant.knowledge_wiki.ports.repository import WikiRepository


def reconcile_wiki_completion(repository: WikiRepository, job_id: UUID) -> bool:
    with repository.session() as session:
        build = repository.load_build(session, job_id)
    checkpoint = build["checkpoint"]
    if build["status"] == ExecutionStatus.SUCCEEDED and checkpoint.get("result_ref"):
        return True
    steps = cast(dict[str, dict], checkpoint.get("steps", {}))
    if any(step.get("model_call_started") for step in steps.values()):
        raise ExecutionError(
            Code.WIKI_MODEL_OUTCOME_UNKNOWN,
            "OUTCOME_UNKNOWN",
            "Model call has no reliable committed output",
        )
    if build["status"] == ExecutionStatus.CANCELLING:
        raise ExecutionError(Code.EXECUTION_CANCEL_REQUESTED, "ABORT", "Cancellation requested")
    raise ExecutionError(Code.EXECUTION_LEASE_EXPIRED, "TASK_RETRYABLE", "Execution lease expired")
