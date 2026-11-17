"""Read only PG facts after Dispatcher has revoked an expired ingestion lease."""

from uuid import UUID

from intelligent_reimbursement_assitant.document_ingestion_service.ports.repository import (
    DocumentRepository,
)
from intelligent_reimbursement_assitant.errors.codes import ErrorCode
from intelligent_reimbursement_assitant.errors.contracts import ExecutionError
from intelligent_reimbursement_assitant.execution_state_service.domain.models import (
    ExecutionStatus,
    IngestionStage,
)


def reconcile_ingestion_completion(repository: DocumentRepository, job_id: UUID) -> bool:
    with repository.session() as session:
        facts = repository.load_completion_facts(session, job_id)
    stage, checkpoint = facts["stage"], facts["checkpoint"]
    stopped = stage == IngestionStage.REGISTERED or (
        stage == IngestionStage.PARSING and checkpoint.get("parsed_document_ref") is not None
    )
    if (
        facts["status"] in {ExecutionStatus.RUNNING, ExecutionStatus.CANCELLING}
        and stage == IngestionStage.CHUNKING
        and checkpoint.get("chunking_model_started")
        and checkpoint.get("chunk_batch_ref") is None
    ):
        raise ExecutionError(
            ErrorCode.CHUNKING_MODEL_OUTCOME_UNKNOWN,
            "OUTCOME_UNKNOWN",
            "Model call has no committed chunk artifact",
        )
    if facts["status"] == ExecutionStatus.CANCELLING:
        if stopped:
            raise ExecutionError(
                ErrorCode.EXECUTION_CANCEL_REQUESTED, "ABORT", "Cancellation requested"
            )
        raise ExecutionError(
            ErrorCode.EXECUTION_LEASE_EXPIRED, "OUTCOME_UNKNOWN", "Execution lease expired"
        )
    if facts["status"] != ExecutionStatus.RUNNING:
        raise ExecutionError(
            ErrorCode.EXECUTION_LEASE_LOST, "ABORT", "Execution lease is no longer valid"
        )
    if stage == IngestionStage.PUBLISHING:
        with repository.publication_scope(facts["document_id"]) as session:
            if session is None:
                return False
            raise ExecutionError(
                ErrorCode.EXECUTION_LEASE_EXPIRED, "TASK_RETRYABLE", "Execution lease expired"
            )
    safe = stopped or stage in {
        IngestionStage.NORMALIZING,
        IngestionStage.CHUNKING,
        IngestionStage.INDEXING,
    }
    safe = safe or (
        stage == IngestionStage.EMBEDDING and checkpoint.get("index_profile_id") is None
    )
    raise ExecutionError(
        ErrorCode.EXECUTION_LEASE_EXPIRED,
        "TASK_RETRYABLE" if safe else "OUTCOME_UNKNOWN",
        "Execution lease expired",
    )
