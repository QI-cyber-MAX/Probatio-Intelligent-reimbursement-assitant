"""Join all evidence assessment requests and accounting before one ordered state update.

The caller saves the returned batch before routing; persisted batches are reused
by execution records, never by treating a nonempty assessments array as a retry key.
"""

import asyncio
from collections.abc import Callable
from functools import partial
from types import MappingProxyType
from typing import Literal, cast

from pydantic import JsonValue

from intelligent_reimbursement_assitant.errors.contracts import ExecutionError
from intelligent_reimbursement_assitant.execution_state_service.application.service import (
    ExecutionStateService,
)
from intelligent_reimbursement_assitant.execution_state_service.domain.events import (
    ProgressFactory,
    ProgressStage,
)
from intelligent_reimbursement_assitant.execution_state_service.domain.models import (
    ExecutionContext,
)
from intelligent_reimbursement_assitant.model_gateway.domain.config import ModelSettings
from intelligent_reimbursement_assitant.model_gateway.domain.model_text import share_texts
from intelligent_reimbursement_assitant.model_gateway.ports.model_gateway import ModelGateway
from intelligent_reimbursement_assitant.policy_qa_graph.application.retrieval import (
    retrieval_round_allowed,
)
from intelligent_reimbursement_assitant.policy_qa_graph.domain.state import QAState
from intelligent_reimbursement_assitant.policy_qa_service.application.model_call import (
    call_structured,
)
from intelligent_reimbursement_assitant.policy_qa_service.application.service import PolicyQAService
from intelligent_reimbursement_assitant.policy_qa_service.domain.config import (
    QASettings,
)
from intelligent_reimbursement_assitant.policy_qa_service.domain.evidence import (
    assemble_evidence_clarification,
    build_evidence_request,
    clarification_options,
    merge_assessments,
    select_assessment_items,
)
from intelligent_reimbursement_assitant.policy_qa_service.domain.history import (
    project_exchanges,
)
from intelligent_reimbursement_assitant.policy_qa_service.domain.model_responses import (
    EvidenceClarificationResponse,
)
from intelligent_reimbursement_assitant.policy_qa_service.domain.models import (
    ItemAssessment,
    TaskPlan,
)
from intelligent_reimbursement_assitant.policy_qa_service.domain.prompts import (
    COMMON,
    EVIDENCE_CLARIFY,
    encode,
)
from intelligent_reimbursement_assitant.policy_qa_service.domain.understanding import (
    item_context,
)
from intelligent_reimbursement_assitant.policy_qa_service.ports.evidence_judge import (
    EvidenceJudgePort,
)


async def assess_evidence(
    state: QAState,
    ctx: ExecutionContext,
    *,
    judge: EvidenceJudgePort,
) -> QAState:
    plan = cast(TaskPlan, state["plan"])
    items = select_assessment_items(plan, state["assessments"])
    if not items:
        return {**state, "decision": None} if state["decision"] is not None else state
    requests = [
        build_evidence_request(
            item,
            plan,
            state["input"].snapshot,
            state["evidence_chunks"],
            state["evidence_ids_by_item"].get(item.item_id, []),
            state["retrieval_notes"].get(item.item_id),
        )
        for item in items
    ]
    needed = {
        fact.fact_id: fact
        for request in requests
        for fact in request.facts
        if fact.kind in ("number", "date")
    }
    tasks = [
        asyncio.create_task(
            judge.assess(
                request,
                ctx,
                fact_values=MappingProxyType(
                    {
                        key: state["fact_values"][key]
                        for key in request.item.fact_ids
                        if key in needed
                    }
                ),
            )
        )
        for request in requests
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
        "assessments": merge_assessments(
            plan, state["assessments"], [cast(ItemAssessment, result) for result in results]
        ),
        "decision": None,
    }


def route_assessments(
    state: QAState,
    settings: QASettings,
    *,
    allow_clarification: bool = True,
) -> Literal["retrieve", "clarification", "answer", "restricted"]:
    gaps = [gap for assessment in state["assessments"] for gap in assessment.gaps]
    if (
        allow_clarification
        and state["clarification_count"] < settings.clarification_limit
        and any(gap.kind == "USER_FACT" for gap in gaps)
    ):
        return "clarification"
    if (
        any(gap.kind == "POLICY_MISSING" for gap in gaps)
        and retrieval_round_allowed(state, settings)
        and state["tool_steps"] < settings.tool_step_limit
    ):
        return "retrieve"
    return (
        "answer" if any(assessment.claims for assessment in state["assessments"]) else "restricted"
    )


async def prepare_evidence_clarification(
    state: QAState,
    ctx: ExecutionContext,
    *,
    service: PolicyQAService,
    gateway: ModelGateway,
    state_service: ExecutionStateService,
    settings: QASettings,
    model_settings: ModelSettings,
    count_tokens: Callable[[str], int],
    progress: ProgressFactory | None = None,
) -> QAState:
    plan = cast(TaskPlan, state["plan"])
    options = clarification_options(plan, state["assessments"])
    history = await asyncio.to_thread(
        service.load_evidence_clarification_context,
        state["input"].context_message_ids,
        state["reply_message_ids"],
        state["input"].snapshot.kb_id,
    )
    messages: list[dict[str, JsonValue]] = [
        {"role": "system", "content": COMMON + EVIDENCE_CLARIFY},
        {
            "role": "user",
            "content": encode(
                share_texts(
                    {
                        "exchanges": project_exchanges(history),
                        "items": [
                            {
                                **item_context(item, plan.facts, state["fact_values"]),
                                "gaps": {
                                    label: description
                                    for label, (item_id, description) in options.items()
                                    if item_id == item.item_id
                                },
                            }
                            for item in plan.items
                            if any(item_id == item.item_id for item_id, _ in options.values())
                        ],
                    }
                )
            ),
        },
    ]
    clarification = await call_structured(
        messages=messages,
        response_type=EvidenceClarificationResponse,
        assemble=lambda response: assemble_evidence_clarification(response, plan, options),
        gateway=gateway,
        repository=service.repository,
        state_service=state_service,
        ctx=ctx,
        settings=model_settings,
        count_tokens=count_tokens,
        structure_repair_limit=settings.structure_repair_limit,
        before_call=partial(progress(ctx, None, None), ProgressStage.PREPARING_CLARIFICATION)
        if progress
        else None,
    )
    return {
        **state,
        "clarification": clarification,
        "plan": plan.model_copy(update={"clarification": clarification}),
    }
