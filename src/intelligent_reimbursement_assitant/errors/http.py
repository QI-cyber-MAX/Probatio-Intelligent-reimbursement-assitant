"""Single HTTP mapping for the approved business errors only."""

from starlette.responses import JSONResponse

from intelligent_reimbursement_assitant.errors.codes import ErrorCode
from intelligent_reimbursement_assitant.errors.contracts import BusinessError

_STATUS = {
    ErrorCode.WIKI_NOT_FOUND: 404,
    ErrorCode.WIKI_PAGE_NOT_FOUND: 404,
    ErrorCode.WIKI_JOB_NOT_FOUND: 404,
    ErrorCode.WIKI_BUILD_IN_PROGRESS: 409,
    ErrorCode.WIKI_EDIT_IN_PROGRESS: 409,
    ErrorCode.WIKI_VERSION_CONFLICT: 409,
    ErrorCode.WIKI_SOURCE_REVISION_CONFLICT: 409,
    ErrorCode.WIKI_EDIT_ID_CONFLICT: 409,
    ErrorCode.WIKI_UNAVAILABLE: 503,
    ErrorCode.WIKI_EDIT_OUTCOME_UNKNOWN: 503,
    ErrorCode.DOCUMENT_SOURCE_NOT_FOUND: 404,
    ErrorCode.INGESTION_REBUILD_NOT_ALLOWED: 409,
    ErrorCode.INGESTION_SOURCE_BUSY: 409,
    ErrorCode.INGESTION_JOB_ID_CONFLICT: 409,
    ErrorCode.ARTIFACT_READ_ERROR: 500,
    ErrorCode.TASK_OPERATION_OUTCOME_UNKNOWN: 503,
    ErrorCode.CONVERSATION_MESSAGE_NOT_FOUND: 404,
    ErrorCode.SSE_CURSOR_INVALID: 409,
    ErrorCode.STREAM_UNAVAILABLE: 503,
    ErrorCode.QA_JOB_NOT_FOUND: 404,
    ErrorCode.QA_CLARIFICATION_NOT_FOUND: 404,
    ErrorCode.QA_STATE_CONFLICT: 409,
    ErrorCode.CLARIFICATION_MESSAGE_CONFLICT: 409,
    ErrorCode.CONVERSATION_NOT_FOUND: 404,
    ErrorCode.QUESTION_MESSAGE_CONFLICT: 409,
    ErrorCode.CONVERSATION_BUSY: 409,
    ErrorCode.QA_UNAVAILABLE: 503,
    ErrorCode.QA_ACCEPTANCE_OUTCOME_UNKNOWN: 503,
    ErrorCode.INVALID_UPLOAD: 400,
    ErrorCode.UPLOAD_TOO_LARGE: 413,
    ErrorCode.UNSUPPORTED_FILE_TYPE: 415,
    ErrorCode.FILE_TYPE_MISMATCH: 415,
    ErrorCode.INVALID_FILE_STRUCTURE: 422,
    ErrorCode.FILE_POLICY_REJECTED: 422,
    ErrorCode.UPLOAD_RESOURCE_LIMIT: 422,
    ErrorCode.UPLOAD_TIMEOUT: 503,
    ErrorCode.UPLOAD_BUSY: 503,
    ErrorCode.UPLOAD_IO_ERROR: 500,
    ErrorCode.FILE_IDENTITY_MISMATCH: 500,
    ErrorCode.SCAN_REJECTED: 422,
    ErrorCode.SCAN_UNAVAILABLE: 503,
    ErrorCode.SCAN_TIMEOUT: 503,
    ErrorCode.SCAN_INCOMPLETE: 503,
    ErrorCode.SCAN_RESOURCE_LIMIT: 503,
    ErrorCode.INVALID_REGISTRATION: 400,
    ErrorCode.KNOWLEDGE_BASE_NOT_FOUND: 404,
    ErrorCode.DOCUMENT_NOT_FOUND: 404,
    ErrorCode.INGESTION_JOB_NOT_FOUND: 404,
    ErrorCode.DOCUMENT_OWNERSHIP_MISMATCH: 400,
    ErrorCode.REGISTRATION_UNAVAILABLE: 503,
    ErrorCode.REGISTRATION_OUTCOME_UNKNOWN: 503,
}


def business_error_response(error: BusinessError[ErrorCode]) -> JSONResponse:
    return JSONResponse(error.model_dump(mode="json"), status_code=_STATUS[error.code])
