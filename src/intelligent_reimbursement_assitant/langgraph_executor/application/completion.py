"""Maintenance reads native facts without running a graph or loading models."""

from uuid import UUID

from intelligent_reimbursement_assitant.errors.codes import ErrorCode
from intelligent_reimbursement_assitant.errors.contracts import ExecutionError
from intelligent_reimbursement_assitant.execution_state_service.domain.models import ExecutionStatus
from intelligent_reimbursement_assitant.langgraph_executor.ports.recovery import QARecoveryReader
from intelligent_reimbursement_assitant.policy_qa_service.domain.models import AnswerResult
from intelligent_reimbursement_assitant.policy_qa_service.ports.repository import QARepository


def reconcile_qa_completion(
    repository: QARepository, checkpoints: QARecoveryReader, job_id: UUID
) -> bool:
    with repository.session() as session:
        turn = repository.load_turn(session, job_id)
        status = repository.load_execution_status(session, job_id)
        if status == ExecutionStatus.SUCCEEDED:
            if turn.answer_ref is None:
                raise ValueError("Successful QA job has no answer reference")
            message = repository.load_messages(session, [UUID(turn.answer_ref)])[0]
            if not isinstance(message.payload, AnswerResult):
                raise ValueError("Answer reference does not identify an answer")
            return True
    recovery = checkpoints.read(job_id)
    if status == ExecutionStatus.CANCELLING and recovery.safe:
        raise ExecutionError(
            ErrorCode.EXECUTION_CANCEL_REQUESTED,
            "ABORT",
            "Native execution confirms a stopped boundary",
        )
    category = (
        "TASK_RETRYABLE"
        if status == ExecutionStatus.RUNNING and recovery.safe
        else "OUTCOME_UNKNOWN"
    )
    raise ExecutionError(ErrorCode.EXECUTION_LEASE_EXPIRED, category, "Execution lease expired")
