"""Settle every generated section and actual usage before saving the answer batch."""

import asyncio
from collections.abc import Callable
from typing import cast

from intelligent_reimbursement_assitant.errors.contracts import ExecutionError
from intelligent_reimbursement_assitant.execution_state_service.application.service import (
    ExecutionStateService,
)
from intelligent_reimbursement_assitant.execution_state_service.domain.events import ProgressFactory
from intelligent_reimbursement_assitant.execution_state_service.domain.models import (
    ExecutionContext,
)
from intelligent_reimbursement_assitant.model_gateway.domain.config import ModelSettings
from intelligent_reimbursement_assitant.model_gateway.ports.model_gateway import ModelGateway
from intelligent_reimbursement_assitant.policy_qa_graph.domain.state import QAState
from intelligent_reimbursement_assitant.policy_qa_service.application.answering import (
    generate_answer_section,
)
from intelligent_reimbursement_assitant.policy_qa_service.domain.answering import (
    build_unsupported_section,
    merge_answer_sections,
    select_answer_items,
)
from intelligent_reimbursement_assitant.policy_qa_service.domain.config import (
    QASettings,
)
from intelligent_reimbursement_assitant.policy_qa_service.domain.evidence import (
    build_evidence_request,
)
from intelligent_reimbursement_assitant.policy_qa_service.domain.models import (
    AnswerSection,
    TaskPlan,
)
from intelligent_reimbursement_assitant.policy_qa_service.ports.repository import QARepository


async def generate_answer(
    state: QAState,
    ctx: ExecutionContext,
    *,
    gateway: ModelGateway,
    repository: QARepository,
    state_service: ExecutionStateService,
    settings: QASettings,
    model_settings: ModelSettings,
    count_tokens: Callable[[str], int],
    progress: ProgressFactory | None = None,
) -> QAState:
    plan = cast(TaskPlan, state["plan"])
    items = select_answer_items(plan, state["answer_sections"])
    if not items:
        return state
    assessments = {assessment.item_id: assessment for assessment in state["assessments"]}
    requests = {
        item.item_id: build_evidence_request(
            item,
            plan,
            state["input"].snapshot,
            state["evidence_chunks"],
            list(
                dict.fromkeys(
                    key
                    for source in [
                        *assessments[item.item_id].claims,
                        *assessments[item.item_id].gaps,
                    ]
                    for key in source.evidence_chunk_ids
                )
            ),
        )
        for item in items
        if assessments[item.item_id].claims
    }
    sections = [
        build_unsupported_section(item, assessments[item.item_id])
        for item in items
        if not assessments[item.item_id].claims
    ]
    tasks = [
        asyncio.create_task(
            generate_answer_section(
                requests[item.item_id],
                assessments[item.item_id],
                ctx,
                fact_values=state["fact_values"],
                gateway=gateway,
                repository=repository,
                state_service=state_service,
                settings=settings,
                model_settings=model_settings,
                count_tokens=count_tokens,
                report_progress=progress(ctx, item.item_id, None) if progress else None,
            )
        )
        for item in items
        if assessments[item.item_id].claims
    ]
    joined = asyncio.gather(*tasks, return_exceptions=True)
    cancelled = False
    while True:
        try:
            results = await asyncio.shield(joined)
            break
        except asyncio.CancelledError:
            cancelled = True
    for result in results:
        if isinstance(result, ExecutionError) and result.category == "OUTCOME_UNKNOWN":
            raise result
    if cancelled:
        raise asyncio.CancelledError
    for result in results:
        if isinstance(result, asyncio.CancelledError) or (
            isinstance(result, ExecutionError) and result.category == "ABORT"
        ):
            raise result
    for result in results:
        if isinstance(result, BaseException):
            raise result
    return {
        **state,
        "answer_sections": merge_answer_sections(
            plan,
            state["answer_sections"],
            [*sections, *(cast(AnswerSection, result) for result in results)],
        ),
        "result": None,
    }
