"""Safe execution retry and output, independent of task state and HTTP."""

import asyncio
import traceback
from collections.abc import Awaitable, Callable
from typing import cast

from intelligent_reimbursement_assitant.errors.codes import ErrorCode
from intelligent_reimbursement_assitant.errors.contracts import ExecutionError

_FAILURE_SUMMARIES: dict[str, str] = {
    ErrorCode.WIKI_SOURCE_REVISION_CONFLICT: "来源已更新，本次内容未发布",
    ErrorCode.QA_STATE_CONFLICT: "旧任务使用了其他知识库历史，请在当前知识库重新提问",
    ErrorCode.WIKI_SOURCE_UNAVAILABLE: "Wiki来源产物不可用",
    ErrorCode.WIKI_MODEL_CONFIGURATION_ERROR: "Wiki模型配置或请求参数不可用",
    ErrorCode.WIKI_MODEL_UNAVAILABLE: "Wiki模型暂不可用",
    ErrorCode.WIKI_MODEL_INVALID_RESPONSE: "Wiki模型结构或引用无效",
    ErrorCode.WIKI_CONTENT_INVALID: "Wiki内容审核未通过",
    ErrorCode.WIKI_MODEL_OUTCOME_UNKNOWN: "Wiki模型调用或用量结果未知",
    ErrorCode.WIKI_CONTENT_TOO_LARGE: "Wiki模型输出达到长度上限",
    ErrorCode.WIKI_ARTIFACT_ERROR: "Wiki产物读写失败",
    ErrorCode.PARSER_UNAVAILABLE: "解析服务暂不可用",
    ErrorCode.CHUNKING_MODEL_CONFIGURATION_ERROR: "分块模型配置或请求参数不可用",
    ErrorCode.CHUNKING_MODEL_UNAVAILABLE: "分块模型暂不可用",
    ErrorCode.CHUNKING_MODEL_INVALID_RESPONSE: "分块模型未返回满足分组约束的有效响应",
    ErrorCode.CHUNKING_MODEL_OUTCOME_UNKNOWN: "分块模型调用或用量结果未知",
    ErrorCode.QA_TASK_UNPARSABLE: "当前问题不可解析，涉及个人隐私数据、违规行为请求或内容过滤",
    ErrorCode.QA_PARSE_UNSUPPORTED: "必要表达、单位或计算超出当前能力，无法可靠完成判断",
    ErrorCode.QA_MODEL_CONFIGURATION_ERROR: "问答模型配置或请求参数不可用",
    ErrorCode.QA_MODEL_UNAVAILABLE: "问答模型暂不可用",
    ErrorCode.QA_MODEL_INVALID_RESPONSE: "问答模型未返回有效结构化结果",
    ErrorCode.QA_EVIDENCE_CAPACITY_EXCEEDED: "检索证据交接超过正文缓存容量，修复后仍无法完成",
    ErrorCode.QA_MODEL_OUTPUT_LIMIT: "问答模型输出超过单次限额",
    ErrorCode.QA_MODEL_OUTCOME_UNKNOWN: "问答模型调用或用量结果未知",
    ErrorCode.EXECUTION_LEASE_LOST: "Execution lease is no longer valid",
    ErrorCode.EXECUTION_LEASE_EXPIRED: "Execution lease expired",
    ErrorCode.EXECUTION_CANCEL_REQUESTED: "Cancellation requested",
    ErrorCode.EXECUTION_INTERRUPTED: "Execution interrupted; recovery required",
    ErrorCode.EXECUTION_INTERNAL_ERROR: "Internal execution failure",
    ErrorCode.PARSER_CONFIGURATION_ERROR: "Parser configuration is unavailable",
    ErrorCode.DOCUMENT_PARSE_FAILED: "Document parsing failed",
    ErrorCode.DOCUMENT_PARSE_INCOMPLETE: "Document parsing is incomplete; manual review required",
    ErrorCode.DOCUMENT_PARSE_EMPTY: "Document has no ingestible content",
    ErrorCode.DOCUMENT_PARSE_TIMEOUT: "Document parsing timed out",
    ErrorCode.DOCUMENT_PARSE_RESOURCE_LIMIT: "Document parsing exceeded resource limits",
    ErrorCode.PARSE_RESULT_INVALID: "Document parsing result is invalid",
    ErrorCode.PARSING_ARTIFACT_ERROR: "Parsing artifact could not be accessed or restored",
    ErrorCode.CHUNKING_ARTIFACT_ERROR: "Chunking artifact could not be accessed or restored",
    ErrorCode.CHUNKING_CONTENT_TOO_LARGE: "Document content exceeds the chunking limit",
    ErrorCode.EMBEDDING_UNAVAILABLE: "Embedding service is unavailable",
    ErrorCode.EMBEDDING_TIMEOUT: "Embedding request timed out",
    ErrorCode.EMBEDDING_INVALID_RESPONSE: "Embedding response is invalid",
    ErrorCode.EMBEDDING_CONFIGURATION_ERROR: "Embedding configuration is unavailable",
    ErrorCode.RERANK_CONFIGURATION_ERROR: "Reranker configuration is unavailable",
    ErrorCode.RERANK_UNAVAILABLE: "Reranker is unavailable",
    ErrorCode.RERANK_INVALID_RESPONSE: "Reranker response is invalid",
    ErrorCode.RERANK_OUTCOME_UNKNOWN: "Reranker request or usage outcome is unknown",
    ErrorCode.RERANK_INPUT_TOO_LARGE: "Reranker input exceeds pair capacity",
    ErrorCode.INDEX_PROFILE_UNAVAILABLE: "Bound index profile is unavailable",
    ErrorCode.INDEX_READ_ERROR: "Content index could not be read",
    ErrorCode.INDEX_WRITE_ERROR: "Content index records could not be written",
    ErrorCode.INDEX_VISIBILITY_ERROR: "Content index queryability could not be confirmed",
    ErrorCode.INDEX_METADATA_SYNC_ERROR: "Content index metadata could not be synchronized",
}
_UNKNOWN_FAILURE = "Execution failed; investigation required"
_UNKNOWN_OUTCOME = "Outcome unknown; manual reconciliation required"
_SAFE_REASONS = frozenset(_FAILURE_SUMMARIES.values()) | {
    _UNKNOWN_FAILURE,
    _UNKNOWN_OUTCOME,
    "Automatic retry limit exhausted",
    "Execution budget exhausted or fully reserved",
}


def normalize_error(error: Exception) -> ExecutionError:
    if isinstance(error, ExecutionError):
        return error
    normalized = ExecutionError(
        ErrorCode.EXECUTION_INTERNAL_ERROR, "OUTCOME_UNKNOWN", "Unexpected execution failure"
    )
    normalized.__cause__ = error
    return normalized


def terminal_reason_for_failure(error: ExecutionError, exhausted_reason: str | None) -> str:
    """Format a disposition already decided by the state service."""
    if error.category == "OUTCOME_UNKNOWN":
        return _UNKNOWN_OUTCOME
    return exhausted_reason or _FAILURE_SUMMARIES.get(error.code, _UNKNOWN_FAILURE)


def public_terminal_reason(code: str | None, reason: str | None) -> str | None:
    """Project historical free text without changing persisted data or error codes."""
    if reason is None or reason in _SAFE_REASONS:
        return reason
    if reason.startswith("Outcome unknown; manual reconciliation required:"):
        return _UNKNOWN_OUTCOME
    return _FAILURE_SUMMARIES.get(code or "", _UNKNOWN_FAILURE)


def exception_diagnostic(error: BaseException) -> str:
    """Keep type and frame locations; never format exception text, inputs or locals."""
    frames = ";".join(
        f"{frame.f_code.co_filename}:{frame.f_code.co_name}:{line}"
        for frame, line in traceback.walk_tb(error.__traceback__)
    )
    return f"error_type={type(error).__name__} frames={frames}"


async def retry_safe_operation[T](
    operation: Callable[[], Awaitable[T]],
    check_execution: Callable[[], Awaitable[bool]],
    *,
    attempts: int = 3,
) -> T:
    """Only wrap a safe operation, never an entire workflow or an SDK retry loop."""
    if attempts < 1:
        raise ValueError("attempts must be positive")
    for attempt in range(attempts):
        if not await check_execution():
            # The guard committed budget exhaustion; do not invent another execution code.
            raise asyncio.CancelledError("Execution no longer permitted")
        try:
            return await operation()
        except ExecutionError as error:
            if error.category != "LOCAL_RETRYABLE":
                raise
            if attempt + 1 == attempts:
                raise ExecutionError(error.code, "TASK_RETRYABLE", error.message) from error
            await asyncio.sleep(0.1 * 2**attempt)
    raise AssertionError("unreachable")


async def gather_settled[T](*operations: Awaitable[T]) -> list[T]:
    """Join issued operations without cancelling siblings or hiding unknown outcomes."""
    joined = asyncio.gather(*operations, return_exceptions=True)
    cancelled = None
    try:
        results = await asyncio.shield(joined)
    except asyncio.CancelledError as error:
        cancelled = error
        results = await joined
    errors = [result for result in results if isinstance(result, BaseException)]
    if cancelled is not None:
        errors.append(cancelled)
    for error in errors:
        if isinstance(error, ExecutionError) and error.category == "OUTCOME_UNKNOWN":
            raise error
    if errors:
        raise errors[0]
    return cast(list[T], results)


async def run_io[T](
    operation: Callable[[], Awaitable[T]],
    pool: asyncio.Semaphore,
    check_execution: Callable[[], Awaitable[bool]],
) -> T:
    """Check mutable eligibility after queuing; settle the resource before releasing it."""
    async with pool:
        if not await check_execution():
            raise asyncio.CancelledError("Execution no longer permitted")
        pending = asyncio.ensure_future(operation())
        try:
            return await asyncio.shield(pending)
        except asyncio.CancelledError:
            await pending
            raise
