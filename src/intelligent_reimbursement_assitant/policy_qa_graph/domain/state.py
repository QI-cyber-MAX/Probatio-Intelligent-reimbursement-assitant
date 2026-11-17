"""Checkpoint state; node updates replace collections by value."""

import json
from datetime import datetime
from typing import TypedDict, cast
from uuid import UUID

from intelligent_reimbursement_assitant.document_ingestion_service.domain.models import Chunk
from intelligent_reimbursement_assitant.errors.codes import ErrorCode
from intelligent_reimbursement_assitant.errors.contracts import ExecutionError
from intelligent_reimbursement_assitant.policy_qa_service.domain.models import (
    AnswerResult,
    AnswerSections,
    AssessDecision,
    ClarificationRequest,
    EvidenceAssessment,
    FactValue,
    QAInput,
    ReadDecision,
    SearchBatch,
    SearchDecision,
    SearchQuery,
    TaskPlan,
    WikiReadDecision,
)
from intelligent_reimbursement_assitant.policy_qa_service.domain.tool_calling import (
    ChunkHistory,
    ToolSession,
)


class PendingToolExecution(TypedDict):
    item_id: str
    tool_call_id: str
    decision: SearchDecision | ReadDecision | WikiReadDecision


class QAState(TypedDict):
    message_id: UUID
    input: QAInput
    reference_time: datetime
    reply_message_ids: list[UUID]
    plan: TaskPlan | None
    fact_values: dict[str, FactValue]
    search_queries: list[SearchQuery]
    tool_sessions: dict[str, ToolSession]
    chunk_history_by_item: dict[str, dict[str, ChunkHistory]]
    decision: AssessDecision | None
    pending_tool_executions: list[PendingToolExecution]
    search_batches: list[SearchBatch]
    ranked_chunk_ids: dict[str, list[str]]
    evidence_chunks: dict[str, Chunk]
    retrieval_notes: dict[str, str]
    evidence_ids_by_item: dict[str, list[str]]
    assessments: EvidenceAssessment
    clarification: ClarificationRequest | None
    answer_sections: AnswerSections
    retrieval_round: int
    tool_steps: int
    clarification_count: int
    result: AnswerResult | None


def restore_tool_admissions(values: dict) -> QAState:
    """Translate the prior native protocol's single charged decision at read time."""
    state = {**values, "pending_tool_executions": values.get("pending_tool_executions", [])}
    decision = state.get("decision")
    if not isinstance(decision, (SearchDecision, ReadDecision, WikiReadDecision)):
        return cast(QAState, state)
    item_id = (
        next(q.item_id for q in state["search_queries"] if q.query_id == decision.query_id)
        if isinstance(decision, SearchDecision)
        else decision.item_id
    )
    session = state["tool_sessions"][item_id]
    completed = {m["tool_call_id"] for m in session["messages"] if m["role"] == "tool"}
    matches = []
    for message in session["messages"]:
        if message["role"] != "assistant":
            continue
        for call in message.get("tool_calls", []):
            if call["id"] in completed:
                continue
            function = call["function"]
            args = json.loads(function["arguments"])
            if isinstance(decision, SearchDecision):
                query = next(q for q in state["search_queries"] if q.query_id == decision.query_id)
                mode = "keyword_search" if query.mode == "keyword" else "semantic_search"
                match = function["name"] == mode
            elif isinstance(decision, ReadDecision):
                match = (
                    function["name"] == "chunk_read"
                    and [session["chunk_bindings"][k] for k in args["chunk_refs"]]
                    == decision.chunk_ids
                )
            else:
                match = (
                    function["name"] == "wiki_read"
                    and session["wiki_bindings"][args["page_ref"]] == decision.page_id
                )
            if match:
                matches.append(call["id"])
    if len(matches) != 1:
        raise ExecutionError(
            ErrorCode.QA_MODEL_OUTCOME_UNKNOWN,
            "OUTCOME_UNKNOWN",
            "Legacy tool admission is ambiguous",
        )
    state["pending_tool_executions"] = [
        {"item_id": item_id, "tool_call_id": matches[0], "decision": decision}
    ]
    state["decision"] = None
    return cast(QAState, state)
